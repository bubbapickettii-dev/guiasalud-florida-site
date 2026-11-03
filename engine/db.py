"""Lead database. Three clearly separated populations:

  PROSPECT  - lives in the intelligence DB (../leadgen/data/leadgen.db). Never contacted by this app.
  RESPONDER - a `visitors` row that did more than view a page (tool_start or deeper).
  LEAD      - a `leads` row: voluntarily submitted + consent record + passed qualification.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS visitors (
  visitor_id TEXT PRIMARY KEY, first_seen TEXT, last_seen TEXT,
  first_campaign TEXT, first_utm_source TEXT, first_utm_medium TEXT, first_utm_campaign TEXT,
  first_utm_content TEXT, first_link_code TEXT, partner_id TEXT, referrer TEXT, landing_url TEXT,
  stage TEXT DEFAULT 'VISITOR',              -- VISITOR -> RESPONDER -> SUBMITTED
  is_test INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, visitor_id TEXT, session_id TEXT, campaign TEXT,
  event TEXT,                                 -- page_view, tool_start, tool_complete, step, form_start, submit, link_click
  step TEXT, utm_source TEXT, utm_medium TEXT, utm_campaign TEXT, utm_content TEXT, link_code TEXT,
  partner_id TEXT, page_url TEXT, data TEXT, is_test INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_ev_v ON events(visitor_id);
CREATE INDEX IF NOT EXISTS ix_ev_c ON events(campaign, event);

CREATE TABLE IF NOT EXISTS leads (
  lead_id TEXT PRIMARY KEY, created_at TEXT, visitor_id TEXT, is_test INTEGER DEFAULT 0,
  campaign TEXT, product TEXT, landing_url TEXT,
  utm_source TEXT, utm_medium TEXT, utm_campaign TEXT, utm_content TEXT, utm_term TEXT, link_code TEXT,
  partner_id TEXT, gclid TEXT, fbclid TEXT, referrer TEXT,
  first_name TEXT, last_name TEXT, email TEXT, phone TEXT, zip TEXT, state TEXT,
  age_range TEXT, coverage_for TEXT, coverage_amount TEXT, tobacco TEXT, health_rating TEXT,
  timeframe TEXT, preferred_contact TEXT, best_time TEXT, answers TEXT, tool_result TEXT,
  contact_scope TEXT,                         -- PHONE_AND_EMAIL (full consent) | EMAIL_ONLY
  qual_status TEXT,                           -- QUALIFIED | UNQUALIFIED | DUPLICATE | NURTURE
  qual_reasons TEXT, score REAL, tier TEXT, score_breakdown TEXT,
  intel_match TEXT,                           -- matched public-record signals (prospect DB), if any
  routing_status TEXT,                        -- PENDING | ROUTED | NO_BUYER | DELIVERED | ACCEPTED | RETURNED | NOT_ROUTABLE
  buyer_id TEXT, price REAL, routed_at TEXT, delivered_at TEXT,
  opted_out INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_lead_phone ON leads(phone);
CREATE INDEX IF NOT EXISTS ix_lead_email ON leads(email);
CREATE INDEX IF NOT EXISTS ix_lead_created ON leads(created_at);

CREATE TABLE IF NOT EXISTS consents (
  lead_id TEXT PRIMARY KEY, ts TEXT, consent_version TEXT, consent_text TEXT, seller_named TEXT,
  checkbox_checked INTEGER, scope TEXT, ip TEXT, user_agent TEXT, page_url TEXT,
  form_snapshot_sha256 TEXT, phone TEXT, email TEXT
); -- hourly per-IP lead-submission throttle keeps the /api/lead rate check on an index, not a table scan
CREATE INDEX IF NOT EXISTS ix_cons_ip ON consents(ip);  -- per-IP throttle lookup
-- Honeypot quarantine: a submission whose hidden trap field arrived filled gets 200'd to the
-- browser (bot stealth) but is NEVER silent for the operator — it is recorded here with the
-- fields an operator needs to manually recover a genuine consumer inquiry (Admin > Discards).
-- Genuinely-recovered records are entered through the existing Admin > New-lead form; nothing
-- here auto-qualifies.
CREATE TABLE IF NOT EXISTS discards (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT,
  reason TEXT,                -- honeypot | future reasons
  trap_value TEXT,            -- exactly what was in the honeypot field
  campaign TEXT, first_name TEXT, last_name TEXT, email TEXT, phone TEXT, zip TEXT,
  consent_checked INTEGER,
  ip TEXT, user_agent TEXT, page_url TEXT
);
CREATE INDEX IF NOT EXISTS ix_discard_ts ON discards(ts);
CREATE TABLE IF NOT EXISTS deliveries (
  id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id TEXT, buyer_id TEXT, ts TEXT, method TEXT,
  status TEXT, payload_path TEXT, response TEXT
);
-- EXCLUSIVITY, enforced by the database: a lead has at most ONE successful SALE delivery, ever
-- (outbox/webhook channels; FAILED ones may be retried — the lead never reached that buyer). The buyer's
-- email-notification rows are a different channel and never block or imply a sale delivery.
CREATE UNIQUE INDEX IF NOT EXISTS ux_deliveries_lead
  ON deliveries(lead_id) WHERE method IN ('outbox','webhook') AND status != 'FAILED';
CREATE TABLE IF NOT EXISTS links (            -- trackable links (QR codes, bios, partner flyers, ads)
  code TEXT PRIMARY KEY, campaign TEXT, channel TEXT, utm_source TEXT, utm_medium TEXT, utm_campaign TEXT,
  utm_content TEXT, partner_id TEXT, note TEXT, created_at TEXT, clicks INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS optouts (key TEXT PRIMARY KEY, kind TEXT, ts TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS lead_outcomes (       -- buyer feedback: the optimisation signal (pricing never depends on it)
  id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id TEXT, buyer_id TEXT, ts TEXT, outcome TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS experiments (         -- Distribution Lab: one row per acquisition test
  experiment_id TEXT PRIMARY KEY, name TEXT, campaign TEXT, audience TEXT, angle TEXT, channel TEXT, creative TEXT,
  landing_page TEXT, source TEXT, medium TEXT, language TEXT, partner_id TEXT, hypothesis TEXT,
  status TEXT DEFAULT 'PLANNED',                  -- PLANNED | LIVE | PAUSED | DONE | KILLED
  cost REAL DEFAULT 0, assumed_lead_value REAL, start_date TEXT, end_date TEXT, learning TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS partners (            -- distribution partners (never receive consumer PII)
  partner_id TEXT PRIMARY KEY, display_name TEXT, type TEXT, audience TEXT, default_angle TEXT, language TEXT,
  status TEXT, token TEXT UNIQUE, cobrand_approved INTEGER DEFAULT 0, evidence TEXT, contact_hint TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS partner_activity (    -- operator-recorded outreach pipeline, APPEND-ONLY audit trail
                                                 -- kind: STAGE | OUTREACH | FOLLOWUP | NOTE; dedupe_key blocks resubmit-retries
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, partner_id TEXT, kind TEXT, stage_from TEXT, stage_to TEXT,
  method TEXT, next_followup TEXT, note TEXT, dedupe_key TEXT UNIQUE, operator TEXT DEFAULT 'admin'
);
CREATE TABLE IF NOT EXISTS buyer_activity (     -- operator-recorded PROSPECT-BUYER follow-up, APPEND-ONLY audit trail.
                                                 -- Disjoint from partner_activity: partners distribute, buyers purchase leads.
                                                 -- kind: STAGE | OUTREACH | FOLLOWUP | NOTE; dedupe_key blocks resubmit-retries
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, buyer_id TEXT, kind TEXT, stage_from TEXT, stage_to TEXT,
  method TEXT, next_followup TEXT, note TEXT, dedupe_key TEXT UNIQUE, operator TEXT DEFAULT 'admin'
);
CREATE TABLE IF NOT EXISTS lead_views (lead_id TEXT, buyer_id TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS routing_decisions (  -- WHY a lead was assigned, withheld, or had no buyer
                                                -- decision: ASSIGNED | NO_BUYER | NOT_ROUTABLE
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, lead_id TEXT, decision TEXT, buyer_id TEXT,
  reason TEXT, candidates INTEGER, is_test INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_rdec_lead ON routing_decisions(lead_id);
CREATE TABLE IF NOT EXISTS buyer_ledger (       -- prepaid billing bookkeeping ONLY: no money moves in code
                                                -- PACKAGE_PURCHASE recorded by the operator after funds received offline;
                                                -- LEAD_CHARGE when a lead is delivered; LEAD_CREDIT when the buyer returns it
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, buyer_id TEXT, lead_id TEXT, package_id TEXT,
  kind TEXT, amount REAL, balance_after REAL, note TEXT, is_test INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_ledger_b ON buyer_ledger(buyer_id);
-- one charge / one return-credit per (lead, buyer): the partial unique index makes double-billing impossible
CREATE UNIQUE INDEX IF NOT EXISTS ux_ledger_lead ON buyer_ledger(lead_id, buyer_id, kind) WHERE lead_id != '';
CREATE TABLE IF NOT EXISTS buyer_payments (  -- payment rail: PENDING rows from server-created Checkout sessions;
                                             -- credits are granted ONLY by the signature-verified webhook (never the browser)
                                             -- status: PENDING -> PAID -> CREDITED  |  PENDING -> FAILED
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, buyer_id TEXT, package_id TEXT, amount_usd REAL, provider TEXT,
  session_id TEXT UNIQUE, session_url TEXT, status TEXT, webhook_event_id TEXT UNIQUE, is_test INTEGER DEFAULT 0, note TEXT
);
CREATE INDEX IF NOT EXISTS ix_pay_b ON buyer_payments(buyer_id);
CREATE TABLE IF NOT EXISTS webhook_events (   -- raw event-id dedupe: Stripe retries arrive as full re-deliveries
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, event_id TEXT UNIQUE, result TEXT
);
CREATE TABLE IF NOT EXISTS spend (               -- money spent per day per campaign (imported from ad-platform CSVs or typed in)
  id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, channel TEXT, engine TEXT, category TEXT, experiment_id TEXT,
  campaign_name TEXT, amount REAL, source TEXT, imported_at TEXT, UNIQUE(day, channel, campaign_name));
"""
MIGRATIONS = [("events", "variant", "TEXT"), ("leads", "variant", "TEXT"),
              ("events", "experiment_id", "TEXT"), ("events", "angle", "TEXT"), ("events", "lang", "TEXT"),
              ("visitors", "experiment_id", "TEXT"), ("visitors", "angle", "TEXT"), ("visitors", "lang", "TEXT"),
              ("links", "experiment_id", "TEXT"), ("links", "angle", "TEXT"), ("links", "lang", "TEXT"),
              ("leads", "experiment_id", "TEXT"), ("leads", "angle", "TEXT"), ("leads", "lang", "TEXT"),
              ("leads", "need_path", "TEXT"), ("leads", "household_size", "INTEGER"), ("leads", "income_band", "TEXT"),
              ("leads", "enrollment_situation", "TEXT"), ("leads", "urgency", "TEXT"), ("leads", "product_category", "TEXT"),
              ("leads", "flags", "TEXT"), ("leads", "agent_status", "TEXT"),
              # consumer-submitted phone is the product: first-class fields + provenance
              ("leads", "lead_type", "TEXT"),            # PHONE_LEAD (sellable) | EMAIL_ONLY_INQUIRY (not sold, not routed)
              ("leads", "phone_e164", "TEXT"), ("leads", "phone_valid", "INTEGER"), ("leads", "phone_check", "TEXT"),
              ("leads", "phone_area_region", "TEXT"), ("leads", "phone_line_type", "TEXT"), ("leads", "phone_line_type_source", "TEXT"),
              ("leads", "phone_source", "TEXT"),         # always CONSUMER_SUBMITTED (DB trigger)
              ("leads", "consent_ts", "TEXT"), ("leads", "consent_version", "TEXT"), ("leads", "tracking_id", "TEXT"),
              ("events", "detail", "TEXT"),
              # acquisition testing: landing page + phone-step wording on every touchpoint, richer experiment definitions
              ("events", "phone_variant", "TEXT"), ("events", "entry_page", "TEXT"),
              ("visitors", "phone_variant", "TEXT"), ("visitors", "entry_page", "TEXT"),
              ("leads", "phone_variant", "TEXT"), ("leads", "entry_page", "TEXT"),
              ("links", "landing_page", "TEXT"), ("links", "phone_variant", "TEXT"),
              # the consumer bridge: which situation the /i/<code> page should speak to (lost coverage,
              # COBRA, premium cost, 1099, enrollment window, family) - chosen from the person's own post
              ("links", "situation", "TEXT"),
              ("experiments", "demand_trigger", "TEXT"), ("experiments", "content_concept", "TEXT"), ("experiments", "offer_cta", "TEXT"),
              ("experiments", "tracking_id", "TEXT"), ("experiments", "phone_variant", "TEXT"),
              ("experiments", "pages", "TEXT"), ("experiments", "opportunity_id", "TEXT"),
              ("leads", "phone_verified", "INTEGER"), ("leads", "phone_verified_via", "TEXT"),
              ("leads", "sale_type", "TEXT"),      # EXCLUSIVE (the product) | '' (never sold)
              # buyer-desk workspace fields, written ONLY via the buyer's own desk (like agent_status):
              # pin on the buyer's lead list + the same per-lead record fields a working agent keeps up
              # to date (first contact date, estimated submitted AV, next follow-up). Never touched by
              # routing, pricing or delivery; visible only on the owning buyer's portal.
              ("leads", "pinned", "INTEGER DEFAULT 0"),
              ("leads", "contact_at", "TEXT"), ("leads", "est_av", "REAL"), ("leads", "followup_at", "TEXT"),
              # partner follow-up pipeline: current position + latest outreach fields (full history is partner_activity)
              ("partners", "stage", "TEXT"),
              ("partners", "last_outreach_at", "TEXT"), ("partners", "outreach_method", "TEXT"),
              ("partners", "last_outcome", "TEXT"), ("partners", "next_action", "TEXT"),
              ("partners", "next_followup", "TEXT"), ("partners", "notes", "TEXT")]

# Hard guarantees, enforced by the database itself (not just app code):
#  1. a phone on a lead must be consumer-submitted - nothing can insert a public-record/appended number
#  2. once stored, the consumer's phone and its provenance can't be edited or swapped
TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS leads_phone_must_be_consumer_submitted BEFORE INSERT ON leads
WHEN COALESCE(NEW.phone,'') != '' AND COALESCE(NEW.phone_source,'') != 'CONSUMER_SUBMITTED'
BEGIN SELECT RAISE(ABORT, 'lead phone must be CONSUMER_SUBMITTED'); END;
CREATE TRIGGER IF NOT EXISTS leads_phone_immutable BEFORE UPDATE OF phone, phone_e164, phone_source ON leads
WHEN COALESCE(OLD.phone,'') IS NOT COALESCE(NEW.phone,'') OR COALESCE(OLD.phone_e164,'') IS NOT COALESCE(NEW.phone_e164,'')
  OR COALESCE(OLD.phone_source,'') IS NOT COALESCE(NEW.phone_source,'')
BEGIN SELECT RAISE(ABORT, 'consumer-submitted phone is immutable'); END;
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    # WAL lets gunicorn threads coexist. Some network filesystems (hosted Python platforms) reject it: fall back to the default journal.
    mode = os.environ.get("LEADENGINE_JOURNAL", "WAL").upper()
    try:
        got = con.execute(f"PRAGMA journal_mode={mode}").fetchone()[0]
    except sqlite3.Error:
        got = None
    if mode == "WAL" and (got or "").lower() != "wal":
        con.execute("PRAGMA journal_mode=DELETE")
    con.executescript(SCHEMA)
    for table, col, typ in MIGRATIONS:
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    con.executescript(TRIGGERS)
    con.commit()
    return con


def insert(con, table: str, row: dict):
    cols = ",".join(row)
    con.execute(f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({','.join('?' * len(row))})",
                [json.dumps(v) if isinstance(v, (dict, list)) else v for v in row.values()])
