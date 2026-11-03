"""BUSINESS-BUYER PROSPECT REGISTRY — buyer-acquisition intelligence.

Purpose: GS-allude needs business CUSTOMERS (agencies, brokerages, licensed agents,
distribution partners) before exclusive leads can be sold. This module is the dedicated
prospect registry for that pipeline — SEPARATE from consumer data:

  - its own database file (data/prospects.db), never the leads DB;
  - rows are PUBLIC-RECORD businesses, never consumer inquiries;
  - nothing here routes, delivers, charges, or ACTIVATES a buyer: a prospect becomes a
    buyer only through the existing operator onboarding/verification flow (app.py);
  - no contact action exists: no send, no email, no call. Operator outreach is manual.

Sources (consistent with engine.sources.PROSPECT_SOURCES, honest):
  manual / operator CSV import  — COLLECTS (this module's ingest paths)
  Google Places API (official)  — NEEDS_KEY: adapter present, GOOGLE_PLACES_API_KEY unset;
                                  no call happens until the key exists AND the operator
                                  records budget approval (billing is per request).
  google-maps-scraper-kit       — TOS_BLOCKED: its method drives a headless browser over
                                  google.com/maps against Google's ToS (its own README
                                  states this). MIT license is irrelevant to the ACCESS
                                  method, so that capability stays blocked + documented.
  GeoLeadScraper CSV export     — OPERATOR_FILE: a browser-extension Maps export an
                                  operator produced under their own account. Taken as a
                                  business-IDENTITY list only (name/address/phone/website)
                                  and landed NEW/UNVERIFIED like any other operator CSV.
                                  Same Maps access caveat as above (docs/GEOLEADSCRAPER_REVIEW.md).

Data fields kept to public business info: name, category, public phone/website,
address parts, source + evidence URL, verification state, review decision, notes.
Minimized by design: no emails unless the operator files them deliberately, no social
scraping fields, no personal names.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "prospects.db"

DECISIONS = ("NEW", "UNDER_REVIEW", "PRIORITIZED", "QUALIFIED", "NOT_A_FIT", "ARCHIVED",
             "CONTACTS_AGREEMENT_DRAFTED")
VERIFY_STATES = ("UNVERIFIED", "PUBLIC_DETAILS_CHECKED", "OPERATOR_VERIFIED", "DENIED")

# The operator-driven prospect-to-buyer lifecycle. Every transition is an EXPLICIT operator
# action recorded in prospect_log; nothing here advances on its own and ACTIVATED only mirrors
# a live buyer's state (read from buyers.json at render time) — it is never set from here.
STAGES = ("NEW", "RESEARCHED", "QUALIFIED", "DISQUALIFIED", "READY_FOR_ONBOARDING",
          "ONBOARDING_STARTED", "ONBOARDED", "ACTIVATED")
STAGE_LABEL = {
    "NEW": "Recorded — needs research", "RESEARCHED": "Researched — needs qualification",
    "QUALIFIED": "Qualified — verification + commercial review next",
    "DISQUALIFIED": "Disqualified", "READY_FOR_ONBOARDING": "Ready for onboarding handoff",
    "ONBOARDING_STARTED": "Onboarding started (buyer draft exists, INACTIVE)",
    "ONBOARDED": "Onboarded — buyer draft complete, awaiting activation",
    "ACTIVATED": "Activated buyer (live in the marketplace)",
}
# Stages an operator may move a prospect TO in the UI. ONBOARDED/ACTIVATED are recorded by
# the handoff/activation flows, not hand-edited, so they are not offered as manual targets.
MANUAL_STAGES = ("NEW", "RESEARCHED", "QUALIFIED", "DISQUALIFIED", "READY_FOR_ONBOARDING")

# Commercial-qualification + follow-up fields. ALL optional: blank means genuinely unknown
# and every blank is rendered as an explicit 'unknown' — never inferred, never defaulted.
# stated_* names mark buyer-claimed numbers (volume/budget): they are recorded as CLAIMS,
# not verified demand, and the ~$1,000/week benchmark from early conversations stays a
# hypothesis, never a default commit.
BIZ_FIELDS = {
    "contact_person": "contact_person", "biz_email": "biz_email",
    "biz_phone": "public_phone",
    "service_territory": "service_territory",
    "interested": "interested",    "lead_types": "lead_types",
    "stated_weekly_volume": "stated_weekly_volume",
    "stated_frequency": "stated_frequency",
    "stated_budget_usd": "stated_budget_usd", "delivery_pref": "delivery_pref",
    "proposed_price": "proposed_price", "agreed_price": "agreed_price",
    "agreement_status": "agreement_status", "open_questions": "open_questions",
    "followup_note": "followup_note", "next_action": "next_action",
    "next_action_date": "next_action_date", "notes": "notes",
}
# What an operator texts into next_action_date must stay a real date if present.
DATE_FIELDS = ("next_action_date",)

SCHEMA = """
CREATE TABLE IF NOT EXISTS prospects (
  id INTEGER PRIMARY KEY,
  prospect_key TEXT UNIQUE,
  name TEXT NOT NULL,
  category TEXT DEFAULT '',
  public_phone TEXT DEFAULT '',
  website TEXT DEFAULT '',
  address TEXT DEFAULT '',
  city TEXT DEFAULT '',
  state TEXT DEFAULT '',
  zip5 TEXT DEFAULT '',
  source TEXT DEFAULT 'manual',
  evidence_url TEXT DEFAULT '',
  decision TEXT DEFAULT 'NEW',
  verify_state TEXT DEFAULT 'UNVERIFIED',
  verify_note TEXT DEFAULT '',
  notes TEXT DEFAULT '',
  created_at TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS prospect_log (
  id INTEGER PRIMARY KEY,
  prospect_id INTEGER,
  ts TEXT, action TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS collect_log (
  id INTEGER PRIMARY KEY,
  ts TEXT, source TEXT, query TEXT,
  fetched INTEGER, added INTEGER, note TEXT
);
"""


MIGRATED_COLUMNS = (("stage", "TEXT DEFAULT 'NEW'"), ("contact_person", "TEXT DEFAULT ''"),
                    ("biz_email", "TEXT DEFAULT ''"),
                    ("service_territory", "TEXT DEFAULT ''"),
                    ("interested", "TEXT DEFAULT ''"),
                    ("lead_types", "TEXT DEFAULT ''"),
                    ("stated_weekly_volume", "TEXT DEFAULT ''"),
                    ("stated_frequency", "TEXT DEFAULT ''"),
                    ("stated_budget_usd", "TEXT DEFAULT ''"),
                    ("delivery_pref", "TEXT DEFAULT ''"),
                    ("proposed_price", "TEXT DEFAULT ''"),
                    ("agreed_price", "TEXT DEFAULT ''"),
                    ("agreement_status", "TEXT DEFAULT ''"),
                    ("open_questions", "TEXT DEFAULT ''"),
                    ("followup_note", "TEXT DEFAULT ''"),
                    ("next_action", "TEXT DEFAULT ''"),
                    ("next_action_date", "TEXT DEFAULT ''"),
                    ("buyer_id", "TEXT DEFAULT ''"),
                    # worklist classification, persisted so the admin can filter and sort on it
                    # instead of recomputing 25k rows per request. Populated by import_worklist.
                    ("segment", "TEXT DEFAULT ''"),
                    ("audience", "TEXT DEFAULT ''"),
                    ("priority", "INTEGER DEFAULT 0"),
                    ("phone_evidence", "TEXT DEFAULT ''"),
                    ("phone_shared_by", "INTEGER DEFAULT 0"),
                    ("outreach_status", "TEXT DEFAULT 'NOT_CONTACTED'"),
                    ("worklist_synced_at", "TEXT DEFAULT ''"))


def _migrate(con: sqlite3.Connection) -> None:
    """Backward-compatible: add any missing columns to an existing prospects.db in place.
    Existing rows keep every value; new columns default to blank/'NEW'. One try/except per
    column (SQLite has no ADD COLUMN IF NOT EXISTS before 3.35)."""
    have = {r[1] for r in con.execute("PRAGMA table_info(prospects)")}
    for col, ddl in MIGRATED_COLUMNS:
        if col not in have:
            try:
                con.execute(f"ALTER TABLE prospects ADD COLUMN {col} {ddl}")
            except sqlite3.OperationalError:      # raced concurrently — column exists now
                pass
    con.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def _norm_site(w: str) -> str:
    """'https://www.SunshineHealthExample/x' -> 'sunshinehealthexample' style host key."""
    w = (w or "").strip().lower()
    w = re.sub(r"^https?://", "", w)
    w = re.sub(r"^www\.", "", w)
    return w.split("/")[0]


def prospect_key(name: str, zip5: str = "", website: str = "") -> str:
    """Dedupe: website wins when given (scheme/www/path agnostic); otherwise same
    business name (case-insensitive) within one ZIP."""
    base = _norm_site(website)
    if not base:
        base = f"{(name or '').strip().lower()}|{(zip5 or '').strip()}"
    return hashlib.sha1(base.encode()).hexdigest()[:24]


def ingest(con: sqlite3.Connection, rows: list[dict], source: str = "manual") -> dict:
    """Insert new / refresh existing prospect rows. Explicit whitelist rebuild (same
    discipline as signals.ingest): only public business fields move in. Existing rows
    keep their decision/verify state; new non-empty details fill blank fields only."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    added = updated = skipped = 0
    for r in rows or []:
        name = (r.get("name") or "").strip()
        if not name:
            skipped += 1
            continue
        website = (r.get("website") or "").strip()[:300]
        zip5 = (r.get("zip5") or r.get("zip") or "").strip()[:10]
        key = prospect_key(name, zip5, website)
        vals = {
            "category": (r.get("category") or "")[:80],
            "public_phone": (r.get("phone") or r.get("public_phone") or "")[:40],
            "website": website,
            "address": (r.get("address") or "")[:300],
            "city": (r.get("city") or "")[:80],
            "state": (r.get("state") or "")[:20],
            "zip5": zip5,
            "evidence_url": (r.get("evidence_url") or r.get("url") or "")[:400],
            # a publicly published owner/decision-maker (registry officer, authorized official,
            # tax-receipt owner) and a published business mailbox. Never inferred.
            "contact_person": (r.get("contact_person") or r.get("owner") or "")[:120],
            "biz_email": (r.get("biz_email") or "")[:200],
        }
        row = con.execute("SELECT id, contact_person, biz_email FROM prospects "
                          "WHERE prospect_key=?", (key,)).fetchone()
        if row:
            # fill-blank semantics for the two fields a second source must never overwrite:
            # an existing named owner / mailbox keeps its provenance.
            sets = [(k, v) for k, v in vals.items() if v
                    and not (k in ("contact_person", "biz_email") and (row[k] or "").strip())]
            if sets:
                clause = ", ".join(f"{k}=?" for k, _ in sets)
                con.execute(f"UPDATE prospects SET {clause}, updated_at=? WHERE id=?",
                            [v for _, v in sets] + [now, row["id"]])
            updated += 1
            continue
        con.execute("INSERT INTO prospects (prospect_key, name, category, public_phone,"
                    " website, address, city, state, zip5, source, evidence_url, decision,"
                    " verify_state, notes, contact_person, biz_email, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (key, name[:200], vals["category"], vals["public_phone"], vals["website"],
                     vals["address"], vals["city"], vals["state"], vals["zip5"],
                     (r.get("source") or source)[:40], vals["evidence_url"],
                     "NEW", "UNVERIFIED", (r.get("notes") or "")[:500],
                     vals["contact_person"], vals["biz_email"], now, now))
        added += 1
    con.commit()
    return {"added": added, "updated": updated, "skipped": skipped}


def import_csv(con, path: Path, source: str) -> dict:
    """Operator-supplied CSV (e.g., an official-API export). Required column: name;
    optional: category, phone, website, address, city, state, zip5, url/evidence_url."""
    import csv as _csv
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = [dict(r) for r in _csv.DictReader(fh)]
    return ingest(con, rows, source)


# ------------------------------------------------------- GeoLeadScraper CSV bridge
# A GeoLeadScraper export uses its own column names (title/categories/postal_code/...).
# Translating them here means an operator export lands through the SAME ingest(), with
# the same dedupe, the same NEW/UNVERIFIED state and the same public-business field set
# — no second application, no second database, no new contact action.
def _addr_state_zip(address: str) -> tuple[str, str]:
    """'4040 Galt Ocean Dr L15, Fort Lauderdale, FL 33308' -> ('FL', '33308')."""
    m = re.search(r",\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?\s*$", (address or "").strip())
    return (m.group(1), m.group(2)) if m else ("", "")


def _site_root(url: str) -> str:
    """Maps listings often point at a menu/branch deep link with tracking params
    ('/menu/', '?utm_source=google'). Keep the URL able to dedupe by host."""
    u = (url or "").strip()
    if not u or u.lower().startswith(("mailto:", "tel:")):
        return ""
    if "://" not in u:
        u = "https://" + u
    p = urlsplit(u)
    if not p.netloc:
        return ""
    return f"{p.scheme}://{p.netloc}{p.path}".rstrip("/")


def geoleads_rows(rows: list[dict]) -> list[dict]:
    """Map GeoLeadScraper export rows onto this registry's public business fields.
    Deliberately drops its email/socials/review/hotel columns: this registry holds
    business identity only (module docstring), and those columns are exactly what a
    second operator review has to re-derive from the business's own website."""
    out: list[dict] = []
    for r in rows or []:
        name = (r.get("title") or r.get("name") or "").strip()
        if not name:
            continue
        address = (r.get("address") or "").strip()
        state = (r.get("state") or "").strip()
        zip5 = (r.get("postal_code") or r.get("zip5") or r.get("zip") or "").strip()
        a_state, a_zip = _addr_state_zip(address)
        if not zip5:
            zip5 = a_zip
        elif not re.fullmatch(r"\d{5}(-\d{4})?", zip5) and a_zip:
            zip5 = a_zip
        if a_state and len(state) != 2:    # '', 'Florida' -> 'FL', matching existing rows
            state = a_state
        out.append({
            "name": name,
            "category": (r.get("main_category") or r.get("categories") or "").strip(),
            "phone": (r.get("phone") or r.get("phones") or "").strip(),
            "website": _site_root(r.get("website") or ""),
            "address": address,
            "city": (r.get("city") or "").strip(),
            "state": state,
            "zip5": zip5.split("-")[0][:5],
            "evidence_url": (r.get("maps_url") or r.get("url") or "").strip(),
        })
    return out


def import_geoleads_csv(con, path: Path, source: str = "geoleadscraper") -> dict:
    """Import a GeoLeadScraper CSV/JSON export through geoleads_rows() + ingest().
    Rows without a business name are skipped, never guessed."""
    import csv as _csv
    p = Path(path)
    if p.suffix.lower() in (".json", ".jsonl"):
        raw = p.read_text(encoding="utf-8-sig")
        data = json.loads(raw) if p.suffix.lower() == ".json" else [
            json.loads(x) for x in raw.splitlines() if x.strip()]
        if isinstance(data, dict):
            data = data.get("data") or data.get("rows") or data.get("records") or []
    else:
        with open(p, newline="", encoding="utf-8-sig") as fh:
            data = [dict(r) for r in _csv.DictReader(fh)]
    res = ingest(con, geoleads_rows([d for d in data if isinstance(d, dict)]), source)
    res["mapped"] = len(data or [])
    return res


# ------------------------------------------- Miami-Dade Local Business Tax (owner names)
# Official county open-data feature service (no key, public). Every row is a 2026 business
# tax receipt: business name, OWNERNAME (the filer's officer/owner string), street address,
# city, ZIP, and the county's own activity class. No phone or email exists in this file —
# which is why NPPES below exists as the phone source and enrichment fills mailboxes.
MDC_LBT_SERVICE = ("https://services.arcgis.com/8Pc9XBTAsYuxx9Ny/arcgis/rest/services/"
                   "Local_Business_Tax_Feature_Layer_View/FeatureServer/0")
LBT_PUBLIC_TERMS = ("Miami-Dade County public open-data feature service (Local Business Tax "
                    "receipts, current tax year); county-published owner officer string")

# owner-operator classes: trades, licensed professions, services, retail, food, small shops.
# Excluded on purpose: banks, utilities, pari-mutuel, cemeteries, nonprofits, junk dealers.
LBT_CLASSES = (
    "Contracting",
    "Professions, professional associations",
    "Service business/multiple service business or Passenger transportation services",
    "Retail sales",
    "Eating establishments or Catering businesses",
    "Administrative office, operation center or Other unclassified businesses",
    "Dealer in tangible personal property",
    "Educational, training institutions",
    "Manufacturing, recycling, processing, etc.",
)

_BUS_SUFFIX = r"(?:LLC|L\.L\.C|INC|INCORPORATED|CORP|CORPORATION|PA|P\.A|PLLC|LP|LLP|PLC|CO)"
_TITLES = ("MANAGING MEMBER", "REGISTERED AGENT", "AUTHORIZED REPRESENTATIVE",
           "AUTHORIZED REP", "SOLE PROPRIETOR", "VICE PRESIDENT", "VICE PRES",
           "PRESIDENT", "SECRETARY", "TREASURER", "DIRECTOR", "PRINCIPAL", "QUALIFIER",
           "PROPRIETOR", "PARTNER", "MANAGER", "MEMBER", "OFFICER", "TRUSTEE", "OWNER",
           "CEO", "CFO", "COO", "PRES", "SEC", "TREAS", "MGR", "VP", "AGENT")
_NAME_RE = re.compile(r"^[A-Z][A-Za-z.'\-]*(?: [A-Z][A-Za-z.'\-]*){1,3}$")


def lbt_owner_person(owner_raw: str) -> str:
    """Pull the human decision-maker out of a county owner string, or '' when there is none.

    'HICKEY LAW FIRM PA C/O JOHN H HICKEY PRES'      -> 'John H Hickey'
    'TORR CONSTRUCTION LLC JULIO CESAR TORRES, QUALIFIER' -> 'Julio Cesar Torres'
    'CALLE 23 KENDALL CORP C/O IOANNIS SOTIROPOULOS PRES' -> 'Ioannis Sotiropoulos'
    'TREEHOUSE' / 'SMILES DENTAL PA'                -> ''  (a business, not a person)

    The county publishes this string itself; nothing is inferred beyond removing the
    corporate wrapper and the office title.
    """
    s = re.sub(r"\s+", " ", (owner_raw or "").upper()).strip()
    if not s:
        return ""
    if "C/O" in s:
        s = s.split("C/O", 1)[1].strip()
    s = re.sub(r"[,;].*$", "", s).strip()

    def _strip_titles(v: str) -> str:
        changed = True
        while changed:
            changed = False
            for t in _TITLES:
                if v.endswith(" " + t):
                    v, changed = v[: -len(t) - 1].strip(), True
        return v

    s = _strip_titles(s)
    m = list(re.finditer(_BUS_SUFFIX + r"\b", s))
    if m:                                   # 'TORR CONSTRUCTION LLC JULIO ...' -> take the tail
        s = s[m[-1].end():].strip()
    s = _strip_titles(s).strip(" .,-")
    if not s or not _NAME_RE.match(s) or re.search(r"\d", s):
        return ""
    return " ".join(w.capitalize() if w.isupper() else w for w in s.split())


def _http_json(url: str, timeout: int = 40):
    req = urllib.request.Request(url, headers={
        "User-Agent": "GSAlludePublicData/1.0 (+operator research; public open data)",
        "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:   # noqa: S310 - fixed public hosts
        return json.loads(resp.read().decode("utf-8", "replace"))


def lbt_rows(features: list[dict]) -> list[dict]:
    """Map ArcGIS LBT features onto registry rows (business identity + published owner)."""
    out: list[dict] = []
    for f in features or []:
        a = f.get("attributes") or f
        name = re.sub(r"\s+", " ", (a.get("BUSNAME") or "").strip())
        if not name:
            continue
        addr = " ".join(x for x in (a.get("BUSADDR") or "", a.get("BUSADDR2") or "") if x).strip()
        zip5 = re.sub(r"\D", "", (a.get("ZIPCODE") or ""))[:5]
        owner = lbt_owner_person(a.get("OWNERNAME") or "")
        oid = a.get("OBJECTID")
        out.append({
            "name": name.title() if name.isupper() else name,
            "category": (a.get("CATGRYNAME") or a.get("CLASSDESC") or "").strip()[:80],
            "contact_person": owner,
            "address": re.sub(r"\s+", " ", addr)[:300],
            "city": (a.get("BUSCITY") or "").strip().title(),
            "state": "FL",
            "zip5": zip5,
            "evidence_url": f"{MDC_LBT_SERVICE}/{oid}?f=json" if oid else MDC_LBT_SERVICE,
            "source": "mdc_lbt",
            "notes": (f"MDC business tax receipt {a.get('RECEIPTNO') or ''} ({a.get('YEAR') or ''}); "
                      f"county class: {a.get('CLASSDESC') or ''}; owner string as filed: "
                      f"{(a.get('OWNERNAME') or '')[:110]}"),
        })
    return out


def fetch_mdc_lbt(con, classes=LBT_CLASSES, limit: int = 2000, page: int = 1000,
                  get_json=None, sleeper=time.sleep, pause: float = 0.5) -> dict:
    """Page the county business-tax service for owner-operator classes and ingest the rows.
    One collect_log row records exactly what was asked and what landed."""
    get_json = get_json or _http_json
    quoted = ",".join("'" + str(c).replace("'", "''") + "'" for c in classes)
    where = f"CLASSDESC IN ({quoted})"
    fields = ("OBJECTID,YEAR,RECEIPTNO,OWNERNAME,BUSNAME,BUSADDR,BUSADDR2,BUSCITY,ZIPCODE,"
              "CLASSDESC,CATGRYNAME,BUSNAICSCD")
    fetched = mapped = added = updated = skipped = 0
    offset = 0
    errors = 0
    while mapped < limit:
        q = urllib.parse.urlencode({
            "where": where, "outFields": fields, "returnGeometry": "false",
            "orderByFields": "OBJECTID", "resultOffset": offset,
            "resultRecordCount": min(page, limit - mapped), "f": "json"})
        try:
            payload = get_json(f"{MDC_LBT_SERVICE}/query?{q}")
        except Exception:                                     # noqa: BLE001 - logged, not fatal
            errors += 1
            break
        feats = payload.get("features") or []
        if not feats:
            break
        fetched += len(feats)
        rows = lbt_rows(feats)
        mapped += len(rows)
        res = ingest(con, rows, "mdc_lbt")
        added += res["added"]
        updated += res["updated"]
        skipped += res["skipped"]
        offset += len(feats)
        if len(feats) < min(page, limit):
            break
        sleeper(pause)
    collect_log(con, "mdc_lbt", where, fetched, added,
                f"mapped={mapped} updated={updated} skipped={skipped} errors={errors}; "
                f"{LBT_PUBLIC_TERMS}")
    return {"fetched": fetched, "mapped": mapped, "added": added, "updated": updated,
            "skipped": skipped, "errors": errors}


# ------------------------------------------------------- NPPES practices (official phone)
# CMS/NPPES public API, no key. Organization entities (NPI-2) carry BOTH the practice name and
# the registry's authorized-official name + the practice's published telephone — the one
# combination that yields a named decision-maker AND a callable professional number.
NPI_API = "https://npiregistry.cms.hhs.gov/api/"
NPI_CITIES = ("Miami", "Hialeah", "Coral Gables", "Doral", "Miami Beach", "Homestead",
              "Miami Lakes", "Kendall", "Fort Lauderdale", "Hollywood", "Pompano Beach",
              "Plantation", "Davie", "Sunrise", "Boca Raton", "West Palm Beach",
              "Delray Beach", "Boynton Beach", "Wellington", "Jupiter",
              # second ring: Broward + Palm Beach suburbs where owner-run practices sit
              "Coral Springs", "Pembroke Pines", "Miramar", "Weston", "Margate",
              "Tamarac", "Deerfield Beach", "Lake Worth", "Palm Beach Gardens",
              "Greenacres", "Royal Palm Beach", "Aventura", "Miami Gardens",
              "Cutler Bay", "Hallandale Beach")
NPI_TAXONOMIES = ("Dentist", "Chiropractor", "Optometrist", "Physical Therapist",
                  "Podiatrist", "Mental Health Counselor", "Clinical Social Worker",
                  "Marriage & Family Therapist", "Nurse Practitioner", "Family Medicine",
                  "Internal Medicine", "Occupational Therapist", "Speech-Language Pathologist")
# Same licensed professions, queried as INDIVIDUALS: the solo practitioner / associate who
# owns their own book and buys coverage personally (NPI-1), not the clinic entity (NPI-2).
NPI_INDIVIDUAL_TAXONOMIES = NPI_TAXONOMIES + ("Psychologist", "Physician Assistant",
                                              "Registered Nurse", "Acupuncturist")


def npi_rows(results: list[dict], query_url: str) -> list[dict]:
    out: list[dict] = []
    for r in results or []:
        basic = r.get("basic") or {}
        name = (basic.get("organization_name") or "").strip()
        if not name or (basic.get("status") or "A").upper() != "A":
            continue                                    # deactivated NPI is not a live practice
        loc = next((a for a in (r.get("addresses") or [])
                    if (a.get("address_purpose") or "").upper() == "LOCATION"), None)
        loc = loc or next(iter(r.get("addresses") or [{}]), {})
        person_bits = [basic.get("authorized_official_name_prefix") or "",
                       basic.get("authorized_official_first_name") or "",
                       basic.get("authorized_official_middle_name") or "",
                       basic.get("authorized_official_last_name") or "",
                       basic.get("authorized_official_name_suffix") or ""]
        person = " ".join(x.strip() for x in person_bits if x and x.strip() not in ("--",))
        person = re.sub(r"\s+", " ", person).strip()
        cred = (basic.get("authorized_official_credential") or "").strip()
        if cred and cred not in ("--", ""):
            person = f"{person}, {cred}" if person else ""
        taxo = next((t.get("desc") for t in (r.get("taxonomies") or []) if t.get("desc")), "")
        addr = " ".join(x for x in ((loc.get("address_1") or "").strip(),
                                    (loc.get("address_2") or "").strip()) if x)
        out.append({
            "name": name.title() if name.isupper() else name,
            "category": (taxo or "Health practice")[:80],
            "contact_person": person[:120],
            "phone": (loc.get("telephone_number") or "").strip(),
            "address": addr[:300],
            "city": (loc.get("city") or "").strip().title(),
            "state": (loc.get("state") or "FL").strip().upper(),
            "zip5": re.sub(r"\D", "", (loc.get("postal_code") or ""))[:5],
            "evidence_url": query_url,
            "source": "npi_practices",
            "notes": (f"NPPES NPI {r.get('number')}; authorized official as registered: "
                      f"{person or '(none published)'}; practice telephone from the LOCATION address"),
        })
    return out


def npi_individual_rows(results: list[dict], query_url: str) -> list[dict]:
    """Solo/self-employed licensed professionals from NPPES (NPI-1): the provider IS the
    business, so their published name is the decision-maker and the LOCATION address carries
    the practice telephone. All fields are the ones CMS publishes for the NPI."""
    out: list[dict] = []
    for r in results or []:
        basic = r.get("basic") or {}
        if (basic.get("status") or "A").upper() != "A":
            continue
        bits = [basic.get("name_prefix") or "", basic.get("first_name") or "",
                basic.get("middle_name") or "", basic.get("last_name") or "",
                basic.get("name_suffix") or ""]
        person = re.sub(r"\s+", " ", " ".join(x.strip() for x in bits
                                                 if x and x.strip() not in ("--",))).strip()
        if not person:
            continue
        cred = (basic.get("credential") or "").strip()
        display = f"{person}, {cred}" if cred and cred not in ("--", "") else person
        loc = next((a for a in (r.get("addresses") or [])
                    if (a.get("address_purpose") or "").upper() == "LOCATION"), None)
        loc = loc or next(iter(r.get("addresses") or [{}]), {})
        phone = (loc.get("telephone_number") or "").strip()
        if not phone:
            continue                              # a record we cannot call is not worth a row
        taxo = next((t.get("desc") for t in (r.get("taxonomies") or []) if t.get("desc")), "")
        addr = " ".join(x for x in ((loc.get("address_1") or "").strip(),
                                    (loc.get("address_2") or "").strip()) if x)
        out.append({
            "name": display[:200],
            "category": (taxo or "Health professional")[:80],
            "contact_person": person[:120],       # the licensee is the decision-maker
            "phone": phone,
            "address": addr[:300],
            "city": (loc.get("city") or "").strip().title(),
            "state": (loc.get("state") or "FL").strip().upper(),
            "zip5": re.sub(r"\D", "", (loc.get("postal_code") or ""))[:5],
            "evidence_url": query_url,
            "source": "npi_individuals",
            "notes": (f"NPPES NPI {r.get('number')}; individual provider (self-employed "
                      f"professional); practice telephone from the LOCATION address"),
        })
    return out


def fetch_npi_individuals(con, cities=NPI_CITIES, taxonomies=NPI_INDIVIDUAL_TAXONOMIES,
                          per_query: int = 200, max_rows: int = 3000, get_json=None,
                          sleeper=time.sleep, pause: float = 1.2) -> dict:
    """One NPPES call per (city, taxonomy) for INDIVIDUAL providers — solo practitioners,
    associates and self-employed professionals who buy their own coverage."""
    get_json = get_json or _http_json
    fetched = mapped = added = updated = errors = 0
    for city in cities:
        for tax in taxonomies:
            if mapped >= max_rows:
                break
            q = urllib.parse.urlencode({"version": "2.1", "enumeration_type": "NPI-1",
                                        "state": "FL", "city": city,
                                        "taxonomy_description": tax,
                                        "limit": min(per_query, max_rows - mapped)})
            url = f"{NPI_API}?{q}"
            try:
                payload = get_json(url)
            except Exception:                               # noqa: BLE001 - one call never kills a run
                errors += 1
                continue
            results = payload.get("results") or []
            fetched += len(results)
            rows = npi_individual_rows(results, url)
            mapped += len(rows)
            res = ingest(con, rows, "npi_individuals")
            added += res["added"]
            updated += res["updated"]
            sleeper(pause)
    collect_log(con, "npi_individuals", f"{len(cities)} cities x {len(taxonomies)} taxonomies (NPI-1)",
                fetched, added, f"mapped={mapped} updated={updated} errors={errors}; "
                                f"official CMS NPPES API, individual providers only")
    return {"fetched": fetched, "mapped": mapped, "added": added, "updated": updated,
            "errors": errors}


def fetch_npi_practices(con, cities=NPI_CITIES, taxonomies=NPI_TAXONOMIES, per_query: int = 200,
                        max_rows: int = 3000, get_json=None, sleeper=time.sleep,
                        pause: float = 1.2) -> dict:
    """One polite NPPES call per (city, taxonomy), deduped by the registry itself.
    Repeat runs are idempotent: same NPI -> same prospect_key -> update, never a new row."""
    get_json = get_json or _http_json
    fetched = mapped = added = updated = errors = 0
    for city in cities:
        for tax in taxonomies:
            if mapped >= max_rows:
                break
            q = urllib.parse.urlencode({"version": "2.1", "enumeration_type": "NPI-2",
                                        "state": "FL", "city": city,
                                        "taxonomy_description": tax,
                                        "limit": min(per_query, max_rows - mapped)})
            url = f"{NPI_API}?{q}"
            try:
                payload = get_json(url)
            except Exception:                               # noqa: BLE001 - one call never kills a run
                errors += 1
                continue
            results = payload.get("results") or []
            fetched += len(results)
            rows = npi_rows(results, url)
            mapped += len(rows)
            res = ingest(con, rows, "npi_practices")
            added += res["added"]
            updated += res["updated"]
            sleeper(pause)
    collect_log(con, "npi_practices", f"{len(cities)} cities x {len(taxonomies)} taxonomies (FL)",
                fetched, added, f"mapped={mapped} updated={updated} errors={errors}; "
                                f"official CMS NPPES API, org entities only")
    return {"fetched": fetched, "mapped": mapped, "added": added, "updated": updated,
            "errors": errors}


# ------------------------------------------------- OpenStreetMap published contacts
# Overpass API (open data, ODbL, no key). Mappers publish phone/website/email on business POIs,
# so one query per county returns thousands of businesses that have ALREADY published a
# professional contact channel — no directory scraping, no map-site extraction.
OVERPASS = "https://overpass-api.de/api/interpreter"
OSM_BBOXES = {                      # (south, west, north, east)
    "miami-dade": (25.13, -80.90, 25.98, -80.11),
    "broward": (25.95, -80.48, 26.34, -80.06),
    "palm-beach": (26.32, -80.55, 26.98, -80.03),
}
# Owner-operator POI classes only. A phone-bearing shop/office/craft is a small business; a
# museum or a bank branch is not. Querying by class (not by tag presence) keeps each county
# response small enough for the public Overpass endpoint to answer in one go.
OSM_SELECTORS = {
    "amenity": ("dentist|doctors|clinic|veterinary|car_repair|restaurant|cafe|fast_food|"
                "driving_school|pharmacy"),
    "shop": ("car_repair|beauty|hairdresser|florist|furniture|bakery|butcher|hardware|"
             "electronics_repair|laundry|dry_cleaning|tattoo|optician|pet|medical_supply|"
             "clothes|jewelry|mobile_phone|computer|photo|travel_agency|funeral_directors|car"),
    "office": ("lawyer|accountant|insurance|real_estate|financial|tax_advisor|"
               "employment_agency|educational_institution|it|architect|engineer|consulting"),
    "craft": ("electrician|plumber|carpenter|hvac|painter|roofer|gardener|photographer|"
              "metal_construction"),
    "healthcare": ("dentist|doctor|clinic|physiotherapist|chiropractor|optometrist|podiatrist|"
                   "psychotherapist"),
}
# Second pass: the long tail of owner-run storefronts, trades and professional offices that
# the first (targeted) pass leaves out. Same rule — a named, non-chain POI that publishes a
# phone or email — so every added row is reachable without any scraping.
OSM_SELECTORS_EXT = {
    "craft": ("tiler|plasterer|glaziery|insulation|scaffolder|stonemason|joiner|bricklayer|"
              "blacksmith|goldsmith|shoemaker|tailor|upholsterer|window_construction|"
              "electronics_repair|sign_maker|printer|brewery|distillery|caterer|event_services"),
    "shop": ("doityourself|garden_centre|motorcycle|bicycle|sports|toys|books|shoes|fabric|"
             "paint|flooring|kitchen|appliance|bed|antiques|art|music|gift|stationery|"
             "confectionery|deli|greengrocer|seafood|wine|alcohol|pet_grooming|massage|nails|"
             "cosmetics|hearing_aids|medical|nutrition_supplements|vacant|trade|wholesale|"
             "lottery|pawnbroker|money_lender|insurance|car_parts|tyres|bathroom_furnishing|"
             "electrical|lighting|interior_decoration|frame|copyshop|photo|travel_agency"),
    "office": ("estate_agent|company|advertising_agency|notary|surveyor|physician|therapist|"
               "research|coworking|newspaper|telecommunication|diplomatic|forestry|"
               "charity|association|ngo|religion|guide|logistics|property_management"),
    "amenity": ("veterinary|bar|pub|nightclub|ice_cream|juice_bar|food_court|childcare|"
                "kindergarten|social_facility|community_centre|events_venue|studio|"
                "funeral_hall|internet_cafe|marketplace"),
    "leisure": ("fitness_centre|dance|sports_centre|bowling_alley|hackerspace|escape_game"),
}
_OSM_CAT_KEYS = ("amenity", "shop", "office", "craft", "healthcare", "tourism", "leisure")


def _osm_category(tags: dict) -> str:
    for k in _OSM_CAT_KEYS:
        if tags.get(k):
            return f"{k}={tags[k]}"[:80]
    return (tags.get("operator") or "business")[:80]


def osm_rows(elements: list[dict]) -> list[dict]:
    """Map Overpass elements to registry rows. Chains are skipped (a franchise is not an
    owner-operated prospect) and nothing is inferred beyond the tags a mapper published."""
    out: list[dict] = []
    for e in elements or []:
        t = e.get("tags") or {}
        name = (t.get("name") or "").strip()
        if not name or t.get("brand") or t.get("brand:wikidata"):
            continue
        phone = (t.get("phone") or t.get("contact:phone") or "").strip()
        email = (t.get("email") or t.get("contact:email") or "").strip()
        site = (t.get("website") or t.get("contact:website") or "").strip()
        if not (phone or email):        # a row with neither is not an acquisition record
            continue
        street = " ".join(x for x in (t.get("addr:housenumber") or "",
                                      t.get("addr:street") or "") if x).strip()
        out.append({
            "name": name[:200],
            "category": _osm_category(t),
            "phone": phone[:40],
            "biz_email": email[:200],
            "website": site[:300],
            "address": street[:300],
            "city": (t.get("addr:city") or "").strip()[:80],
            "state": (t.get("addr:state") or "FL").strip()[:20],
            "zip5": re.sub(r"\D", "", t.get("addr:postcode") or "")[:5],
            "evidence_url": f"https://www.openstreetmap.org/{e.get('type')}/{e.get('id')}",
            "source": "osm_business",
            "notes": ("OpenStreetMap business POI (ODbL), contact tags as published by mappers; "
                      f"operator tag: {(t.get('operator') or '(none)')[:60]}"),
        })
    return out


def fetch_osm_businesses(con, areas=None, limit: int = 8000, post=None,
                         sleeper=time.sleep, pause: float = 3.0,
                         selectors=None, tag: str = "") -> dict:
    """One Overpass query per county for named POIs that publish a phone or an email."""
    areas = areas or OSM_BBOXES
    post = post or _http_post_json
    selectors = selectors or OSM_SELECTORS
    label = f"overpass {tag}".replace("  ", " ").strip()
    fetched = mapped = added = updated = errors = 0
    for area_name, (s, w, n, e) in areas.items():
        box = f"({s},{w},{n},{e})"
        parts = "".join(f'nwr["{k}"~"^({rx})$"]{box};' for k, rx in selectors.items())
        q = f"[out:json][timeout:180];({parts});out tags center;"
        payload = None
        for attempt in range(3):        # the public endpoint rate-limits; back off, don't give up
            try:
                payload = post(OVERPASS, {"data": q})
                break
            except Exception:                         # noqa: BLE001 - one area never kills the run
                if attempt == 2:
                    errors += 1
                sleeper(20 * (attempt + 1))
        if payload is None:
            continue
        els = payload.get("elements") or []
        fetched += len(els)
        rows = osm_rows(els)
        if limit:
            rows = rows[:max(0, limit - mapped)]
        mapped += len(rows)
        res = ingest(con, rows, "osm_business")
        added += res["added"]
        updated += res["updated"]
        collect_log(con, "osm_business", f"{label} {area_name}".strip(), len(els), res["added"],
                    f"mapped={len(rows)} updated={res['updated']}; OSM ODbL public contact tags")
        sleeper(pause)
    return {"fetched": fetched, "mapped": mapped, "added": added, "updated": updated,
            "errors": errors}


def _http_post_json(url: str, fields: dict, timeout: int = 240):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": "GSAlludePublicData/1.0 (+operator research; OSM ODbL)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:   # noqa: S310 - fixed public host
        return json.loads(resp.read().decode("utf-8", "replace"))


# --------------------------------------------- cross-source phone match (no guessing)
def _name_key(name: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    s = re.sub(r"\b(llc|inc|corp|corporation|incorporated|pa|pllc|lp|llp|co|company)\b", " ", s)
    s = " ".join(t for t in s.split() if len(t) > 1)      # single letters left by 'P.A.' / 'L.L.C.'
    return re.sub(r"\s+", " ", s).strip()


def _names_agree(name: str, names: set[str]) -> bool:
    """Token-overlap test between a record's name and the names seen at that address.
    '1000 Degrees Pizzeria North Miami' vs '1000 Degrees Neapolitan Pizzeria' agrees;
    'Yazuka Llc' vs 'Jorge Chervony P.A. Attorney At Law' does not."""
    mine = set(_name_key(name).split())
    if not mine:
        return False
    for other in names:
        theirs = set((other or "").split())
        if not theirs:
            continue
        shared = len(mine & theirs)
        if shared >= 2 and shared / len(mine | theirs) >= 0.5:
            return True
        if mine <= theirs or theirs <= mine:
            return True
    return False


def _addr_key(address: str, zip5: str) -> str:
    """'7950 NW 53RD ST STE 337 PMB 1028' -> '7950 nw 53rd st' + zip — units never matter
    for deciding whether two records are the same premises."""
    s = (address or "").upper()
    s = re.sub(r"\b(STE|SUITE|UNIT|APT|FL|FLOOR|BLDG|BUILDING|PMB|#)\b.*$", "", s)
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return f"{s}|{(zip5 or '').strip()}" if s and zip5 else ""


def match_by_address(con) -> dict:
    """Same premises = same business: copy a contact channel from a record at the same
    normalized street address + ZIP, only when exactly one value is on offer.
    This is how a county owner record becomes a callable prospect."""
    donors: dict[str, dict] = {}
    for r in con.execute("SELECT name, address, zip5, public_phone, biz_email, website, "
                         "contact_person, source FROM prospects "
                         "WHERE COALESCE(address,'')<>'' AND COALESCE(zip5,'')<>''"):
        k = _addr_key(r["address"], r["zip5"])
        if not k:
            continue
        d = donors.setdefault(k, {"phone": set(), "email": set(), "website": set(),
                                  "owner": set(), "names": set(), "source": r["source"]})
        d["names"].add(_name_key(r["name"]))
        for f, col in (("phone", "public_phone"), ("email", "biz_email"),
                       ("website", "website"), ("owner", "contact_person")):
            if (r[col] or "").strip():
                d[f].add((r[col] or "").strip())
    filled = phones = emails = sites = owners = 0
    for r in [dict(x) for x in con.execute(
            "SELECT id, name, address, zip5, public_phone, biz_email, website, contact_person "
            "FROM prospects WHERE COALESCE(address,'')<>'' AND COALESCE(zip5,'')<>''")]:
        k = _addr_key(r["address"], r["zip5"])
        d = donors.get(k)
        # A street address alone is NOT identity: offices and plazas host many tenants (a
        # lawyer and an LLC at 407 Lincoln Rd). Require the names to agree as well, so an
        # address match can never attach one business's phone to another's record.
        if not d or len(d["names"]) > 1 or not _names_agree(r["name"], d["names"]):
            continue
        sets, notes = [], []
        if not (r["public_phone"] or "").strip() and len(d["phone"]) == 1:
            p = next(iter(d["phone"]))
            sets.append(("public_phone", p)); notes.append("phone"); phones += 1
        if not (r["biz_email"] or "").strip() and len(d["email"]) == 1:
            e = next(iter(d["email"]))
            sets.append(("biz_email", e)); notes.append("email"); emails += 1
        if not (r["website"] or "").strip() and len(d["website"]) == 1:
            w = next(iter(d["website"]))
            sets.append(("website", w)); notes.append("website"); sites += 1
        if not (r["contact_person"] or "").strip() and len(d["owner"]) == 1:
            o = next(iter(d["owner"]))
            sets.append(("contact_person", o)); notes.append("owner"); owners += 1
        if not sets:
            continue
        clause = ", ".join(f"{c}=?" for c, _ in sets)
        con.execute(f"UPDATE prospects SET {clause}, "
                    "notes=COALESCE(notes,'')||?, updated_at=? WHERE id=?",
                    [v for _, v in sets] +
                    [f" | {'/'.join(notes)} copied from {d['source']} record at the same "
                     f"street address+ZIP", _now(), r["id"]])
        filled += 1
    con.commit()
    collect_log(con, "address_match", "same normalized street address + ZIP", filled, filled,
                f"rows_enriched={filled} phones={phones} emails={emails} "
                f"websites={sites} owners={owners}")
    return {"rows_enriched": filled, "phones": phones, "emails": emails,
            "websites": sites, "owners": owners}


def match_phones(con) -> dict:
    """Give phone-less rows a number ONLY when another row proves it: same normalized business
    name AND same 5-digit ZIP, with a real published phone. Provenance is written into notes.
    This is a join on published records, not a guess — and it is skipped when ambiguous."""
    donors: dict[tuple, list] = {}
    for r in con.execute("SELECT name, zip5, public_phone, source, evidence_url FROM prospects "
                         "WHERE COALESCE(public_phone,'')<>'' AND COALESCE(zip5,'')<>''"):
        donors.setdefault((_name_key(r["name"]), (r["zip5"] or "").strip()), []).append(dict(r))
    filled = ambiguous = 0
    for r in [dict(x) for x in con.execute(
            "SELECT id, name, zip5 FROM prospects WHERE COALESCE(public_phone,'')='' "
            "AND COALESCE(zip5,'')<>''")]:
        cand = donors.get((_name_key(r["name"]), (r["zip5"] or "").strip())) or []
        phones = {c["public_phone"] for c in cand if c["public_phone"]}
        if len(phones) == 1:                     # exactly one number for this name+ZIP
            p = phones.pop()
            src = cand[0]["source"]
            con.execute("UPDATE prospects SET public_phone=?, notes=COALESCE(notes,'')||?, "
                        "updated_at=? WHERE id=?",
                        (p, f" | phone matched from {src} record (same name+ZIP)", _now(), r["id"]))
            filled += 1
        elif len(phones) > 1:
            ambiguous += 1
    con.commit()
    collect_log(con, "phone_match", "same normalized name + same ZIP", filled + ambiguous,
                filled, f"filled={filled} ambiguous_skipped={ambiguous}")
    return {"filled": filled, "ambiguous": ambiguous}


# ------------------------------------------------------- published-mailbox enrichment
_MAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
_BAD_MAIL = ("example.", "sentry", "wixpress", "domain.com", "email.com", "@2x", ".png",
             ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")
_CONTACT_HINT = re.compile(r"contact|about|team|staff|our-?people|meet", re.I)
# WAFs reject a non-browser User-Agent before robots.txt is even read, which made a
# published mailbox invisible. Identify ourselves honestly, but in a form sites serve.
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 GSAlludePublicData/1.0")
_SKIP_HOSTS = ("facebook.com", "instagram.com", "linkedin.com", "x.com", "twitter.com",
               "tiktok.com", "yelp.com", "yellowpages.com")   # social/platform pages: never crawl


def _http_html(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA,
                                               "Accept": "text/html,application/xhtml+xml"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(1_500_000).decode("utf-8", "replace")


def _emails_in(html: str) -> list[str]:
    mailtos = [urllib.parse.unquote(m).split("?")[0] for m in
               re.findall(r"mailto:([^\"'>\s]+)", html, re.I)]
    found = [m.lower().strip().strip(".,;") for m in mailtos + _MAIL_RE.findall(html)]
    return [m for m in dict.fromkeys(found)
            if "@" in m and not any(b in m for b in _BAD_MAIL) and 6 < len(m) < 90
            and not re.search(r"@[0-9.]+", m)]                 # 'x@11.7.10' style junk


def site_emails(url: str, fetch=None, timeout: int = 20, pages: int = 3) -> dict:
    """Mailboxes a business publishes on its OWN site: mailto/text on the homepage, then up to
    `pages` same-host contact/about/team pages. robots.txt is honoured (Disallow: / skips)."""
    from engine import org_contacts as _org          # reuse the repo's robots reader
    base = url if "://" in url else "https://" + url
    if any(h in urlsplit(base).netloc for h in _SKIP_HOSTS):
        return {"emails": [], "page": base, "note": "platform page, not the business's own site"}
    terms = _org.get_robots_terms(url, get_text=fetch)
    if terms.get("allowed") is False:
        return {"emails": [], "page": base, "note": "robots.txt disallows crawling"}
    fetch = fetch or _http_html
    try:
        html = fetch(base)
    except Exception as e:                                    # noqa: BLE001 - reported, not raised
        return {"emails": [], "page": base, "note": f"fetch failed: {type(e).__name__}"}
    found = _emails_in(html)
    page = base
    if not found:
        host = urlsplit(base).netloc
        seen, tried = set(), 0
        for href in re.findall(r'href=["\']([^"\']+)["\']', html):
            if tried >= max(1, int(pages)):
                break
            if not _CONTACT_HINT.search(href):
                continue
            cand = urllib.parse.urljoin(base, href).split("#")[0]
            if urlsplit(cand).netloc != host or cand in seen:
                continue
            seen.add(cand); tried += 1
            try:
                sub = fetch(cand)
            except Exception:                             # noqa: BLE001
                continue
            found = _emails_in(sub)
            if found:
                page = cand
                break
    return {"emails": found[:3], "page": page,
            "note": "" if found else "no published mailbox found"}


def enrich_emails(con, limit: int = 50, source: str = "", pause: float = 1.0,
                  fetch=None, sleeper=time.sleep, workers: int = 1,
                  newest: bool = False) -> dict:
    """Fill biz_email from the business's own website for rows that have a site and no mailbox.
    Never guesses, never pattern-builds, never buys a list. Each write records the page it came from.

    workers>1 fetches sites concurrently and still writes sequentially on this connection
    (sqlite3 connections are per-thread; the crawl is the slow part, not the write).
    One polite request per host, robots.txt honoured exactly as in the serial path."""
    sql = ("SELECT id, name, website FROM prospects WHERE COALESCE(biz_email,'')='' "
           "AND COALESCE(website,'')<>''")
    args: list = []
    if source:
        sql += " AND source=?"
        args.append(source)
    sql += " ORDER BY id DESC LIMIT ?" if newest else " ORDER BY id LIMIT ?"
    args.append(int(limit))
    rows = [dict(r) for r in con.execute(sql, args)]
    hit = miss = 0
    if workers and workers > 1 and fetch is None and rows:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=int(workers)) as ex:
            results = list(ex.map(lambda r: site_emails(r["website"]), rows))
    else:
        results = []
        for r in rows:
            results.append(site_emails(r["website"], fetch=fetch))
            sleeper(pause)
    for r, res in zip(rows, results):
        if not res["emails"]:
            miss += 1
        else:
            hit += 1
            note_add = f" | mailbox from own site {res['page']}"
            con.execute("UPDATE prospects SET biz_email=?, notes=COALESCE(notes,'')||?, "
                        "updated_at=? WHERE id=?",
                        (res["emails"][0], note_add, _now(), r["id"]))
            con.commit()
    collect_log(con, "own_site_email", f"enrich limit={limit} source={source or 'any'}",
                len(rows), hit, f"found={hit} none={miss}")
    return {"checked": len(rows), "found": hit, "none": miss}


EXPORT_COLUMNS = (("id", "id"), ("name", "business_name"), ("contact_person", "decision_maker"),
                  ("public_phone", "professional_phone"), ("biz_email", "professional_email"),
                  ("website", "website"), ("address", "address"), ("city", "city"),
                  ("state", "state"), ("zip5", "zip"), ("category", "industry"),
                  ("source", "source"), ("evidence_url", "source_url"),
                  ("verify_state", "verification"), ("decision", "decision"),
                  ("notes", "provenance_notes"), ("updated_at", "updated_at"))


def export_usable(con, path, limit: int = 0, require_phone: bool = False) -> dict:
    """Write the operator's working list as CSV: only rows with a real contact channel, best
    first (named decision-maker + phone, then phone, then mailbox). Nothing is invented; the
    source_url and provenance note travel with every row."""
    import csv
    where = "COALESCE(public_phone,'')<>'' OR COALESCE(biz_email,'')<>''"
    if require_phone:
        where = "COALESCE(public_phone,'')<>''"
    sql = (f"SELECT * FROM prospects WHERE {where} AND decision<>'NOT_A_FIT' "
           "ORDER BY (COALESCE(contact_person,'')<>'') DESC, "
           "(COALESCE(public_phone,'')<>'') DESC, (COALESCE(biz_email,'')<>'') DESC, id")
    rows = [dict(r) for r in con.execute(sql)]
    if limit:
        rows = rows[:int(limit)]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([h for _, h in EXPORT_COLUMNS])
        for r in rows:
            w.writerow([(r.get(k) or "") for k, _ in EXPORT_COLUMNS])
    return {"rows": len(rows), "path": str(path),
            "with_phone": sum(1 for r in rows if (r.get("public_phone") or "").strip()),
            "with_email": sum(1 for r in rows if (r.get("biz_email") or "").strip()),
            "with_owner": sum(1 for r in rows if (r.get("contact_person") or "").strip())}


# ------------------------------------------------------------- segmented working list
# The operator's day-to-day list. Every record lands in EXACTLY ONE bucket, so the counts
# reconcile against the registry: 3 workable segments + 3 exclusion reasons = all rows.
SEG_OWNER = "owner_named_phone"          # named decision-maker AND a phone
SEG_PHONE = "business_phone_no_owner"    # phone, no published owner
SEG_EMAIL = "email_only"                 # mailbox, no phone
SEGMENTS = (SEG_OWNER, SEG_PHONE, SEG_EMAIL)
EX_DUPLICATE = "duplicate"
EX_IRRELEVANT = "irrelevant"
EX_INCOMPLETE = "incomplete"
EXCLUSIONS = (EX_DUPLICATE, EX_IRRELEVANT, EX_INCOMPLETE)
# POI classes that are not a business anyone buys coverage for: consulates, social/community
# facilities, places of worship, NGOs. Kept in the registry, excluded from the working list.
IRRELEVANT_CATEGORIES = {"office=diplomatic", "amenity=social_facility", "amenity=community_centre",
                         "office=ngo", "office=charity", "office=religion",
                         "amenity=place_of_worship", "amenity=public_building"}
# Same audience is NOT the same list: a solo licensee buying his own coverage and a clinic
# buying group coverage are different conversations.
AUDIENCE = {"npi_individuals": "professional", "npi_practices": "health_practice"}

WORKLIST_COLUMNS = (("id", "record_id"), ("priority", "priority"), ("segment", "segment"),
                    ("audience", "audience"), ("name", "business_name"),
                    ("contact_person", "decision_maker"), ("owner_type", "owner_type"),
                    ("public_phone", "professional_phone"),
                    ("phone_evidence", "phone_evidence"), ("biz_email", "professional_email"),
                    ("website", "website"), ("address", "street_address"), ("city", "city"),
                    ("state", "state"), ("zip5", "zip"), ("category", "industry"),
                    ("source", "source"), ("evidence_url", "source_url"),
                    ("created_at", "collected_at"), ("updated_at", "last_updated"),
                    ("verify_state", "verification"), ("decision", "decision"),
                    ("stage", "pipeline_stage"), ("outreach_status", "outreach_status"),
                    ("rows_sharing_phone", "phone_shared_by"),
                    ("notes", "provenance_notes"))


# Owner strings that are a company, not a person: the county files the corporation or trustee
# in the officer field, and a caller needs to know which one they are holding.
_ENTITY_RE = re.compile(r"\b(LLC|L\.L\.C|INC|CORP|CORPORATION|COMPANY|PLLC|P\.A|PA|LP|LLP|TRUST|"
                        r"ESTATE|HOLDINGS|GROUP|ENTERPRISES|SERVICES|MANAGEMENT|ASSOCIATION|"
                        r"FOUNDATION|CHURCH|MINISTRIES)\b", re.I)


def _priority(r: dict) -> int:
    """Sort key an operator can act on. 1 is the best evidence we have: a named person, a line
    that is not shared with other records, and a Miami-Dade address. Nothing is implied about
    the person's coverage."""
    seg = r["segment"]
    if seg == SEG_OWNER:
        if r["phone_evidence"] != "direct":
            return 4
        if r["owner_type"] != "person":
            return 3
        return 1 if r["county"] == "miami-dade" else 2
    if seg == SEG_PHONE:
        return 5
    if seg == SEG_EMAIL:
        return 6
    return 9


def _phone_key(raw: str) -> str:
    """Last 10 digits: '+1 (305) 555-0141' and '3055550141' are the same line. A phone shared
    by many records is an institution's switchboard, not a professional's direct line."""
    d = re.sub(r"\D", "", raw or "")
    return d[-10:] if len(d) >= 10 else ""


def worklist(con) -> dict:
    """Classify every registry row into one workable segment or one exclusion reason, plus the
    evidence needed to work the list (is that phone a direct line or a shared switchboard?).
    Read-only: this reports, it never edits the registry."""
    from collections import Counter
    rows = [dict(r) for r in con.execute("SELECT * FROM prospects")]
    phone_counts = Counter(_phone_key(r.get("public_phone") or "") for r in rows)
    phone_counts.pop("", None)

    def quality(r: dict) -> tuple:
        return (1 if (r.get("contact_person") or "").strip() else 0,
                1 if (r.get("public_phone") or "").strip() else 0,
                1 if (r.get("biz_email") or "").strip() else 0,
                1 if (r.get("website") or "").strip() else 0,
                -(r.get("id") or 0))                    # most complete wins; ties -> oldest id

    best: dict[tuple, int] = {}
    by_id: dict[int, dict] = {}
    for r in rows:
        k = (_name_key(r.get("name") or ""), (r.get("zip5") or "").strip())
        if not k[0] or not k[1]:
            continue                                    # can't prove identity without both
        by_id[r["id"]] = r
        cur = best.get(k)
        if cur is None:
            best[k] = r["id"]
        elif quality(r) > quality(by_id[cur]):
            best[k] = r["id"]

    out: list[dict] = []
    for r in rows:
        phone = (r.get("public_phone") or "").strip()
        email = (r.get("biz_email") or "").strip()
        owner = (r.get("contact_person") or "").strip()
        pk = _phone_key(phone)
        shared = phone_counts.get(pk, 0) if pk else 0
        k = (_name_key(r.get("name") or ""), (r.get("zip5") or "").strip())
        if (r.get("category") or "").strip() in IRRELEVANT_CATEGORIES:
            seg = EX_IRRELEVANT
        elif k[0] and k[1] and best.get(k) != r["id"]:
            seg = EX_DUPLICATE
        elif not phone and not email:
            seg = EX_INCOMPLETE
        elif owner and phone:
            seg = SEG_OWNER
        elif phone:
            seg = SEG_PHONE
        else:
            seg = SEG_EMAIL
        r = dict(r)
        r["segment"] = seg
        r["audience"] = AUDIENCE.get(r.get("source") or "", "business")
        r["phone_evidence"] = ("none" if not pk else
                               "direct" if shared <= 2 else "shared")
        r["rows_sharing_phone"] = shared
        # a stored status wins: if an operator ever records an attempt, re-classifying must not
        # silently reset the row to NOT_CONTACTED (nothing in this code contacts anyone)
        r["outreach_status"] = ((r.get("outreach_status") or "").strip() or "NOT_CONTACTED")
        r["county"] = _county(r.get("city") or "", r.get("zip5") or "")
        r["owner_type"] = ("none" if not owner else
                           "entity" if _ENTITY_RE.search(owner) else "person")
        r["priority"] = _priority(r)
        out.append(r)

    def n(seg):
        return sum(1 for r in out if r["segment"] == seg)

    work = [r for r in out if r["segment"] in SEGMENTS]
    stats = {
        "examined": len(out),
        "segments": {s: n(s) for s in SEGMENTS},
        "exclusions": {s: n(s) for s in EXCLUSIONS},
        "working_list": len(work),
        "owner_named_phone": n(SEG_OWNER),
        "owner_named_phone_direct": sum(1 for r in out if r["segment"] == SEG_OWNER
                                        and r["phone_evidence"] == "direct"),
        "owner_named_phone_shared": sum(1 for r in out if r["segment"] == SEG_OWNER
                                        and r["phone_evidence"] == "shared"),
        "phone_no_owner": n(SEG_PHONE),
        "phone_no_owner_direct": sum(1 for r in out if r["segment"] == SEG_PHONE
                                     and r["phone_evidence"] == "direct"),
        "email_on_worklist": sum(1 for r in work if (r.get("biz_email") or "").strip()),
        "email_only_records": n(SEG_EMAIL),
        # Evidence for the one sentence an operator needs: are the NAMES and the NUMBERS in
        # the same rows? Registry filings carry an officer but no phone; storefront pulls
        # carry a phone but no officer. Counted, never guessed.
        "owner_no_channel": sum(1 for r in out if (r.get("contact_person") or "").strip()
                                and not (r.get("public_phone") or "").strip()
                                and not (r.get("biz_email") or "").strip()),
        "owner_no_channel_by_source": dict(Counter(
            r.get("source") or "(none)" for r in out
            if (r.get("contact_person") or "").strip()
            and not (r.get("public_phone") or "").strip()
            and not (r.get("biz_email") or "").strip()).most_common(5)),
        "phone_no_owner_by_source": dict(Counter(
            r.get("source") or "(none)" for r in out if r["segment"] == SEG_PHONE
        ).most_common(5)),
    }
    stats["by_audience"] = {a: sum(1 for r in work if r["audience"] == a)
                            for a in ("business", "professional", "health_practice")}
    # A business is identified by name + ZIP. Without a ZIP, two rows cannot be proven to be
    # the same business (two branches of one chain, for instance), so each counts once.
    biz = [r for r in work if r["audience"] != "professional"]
    stats["unique_businesses"] = (len({(_name_key(r.get("name") or ""), r["zip5"])
                                       for r in biz if (r.get("zip5") or "").strip()})
                                  + sum(1 for r in biz if not (r.get("zip5") or "").strip()))
    stats["businesses_without_zip"] = sum(1 for r in biz if not (r.get("zip5") or "").strip())
    stats["unique_professionals"] = sum(1 for r in work if r["audience"] == "professional")
    stats["by_county"] = dict(Counter(r["county"] for r in work).most_common())
    stats["by_industry"] = dict(Counter(((r.get("category") or "").strip().rstrip(",").strip()
                                         or "(unknown)") for r in work).most_common(15))
    stats["by_priority"] = {p: sum(1 for r in work if r["priority"] == p)
                            for p in (1, 2, 3, 4, 5, 6)}
    stats["segments_by_audience"] = {
        s: {a: sum(1 for r in work if r["segment"] == s and r["audience"] == a)
            for a in ("business", "professional", "health_practice")}
        for s in SEGMENTS}
    stats["reconciles"] = (sum(stats["segments"].values()) + sum(stats["exclusions"].values())
                           == stats["examined"] == len(out))
    return {"rows": out, "stats": stats}


_WL_CACHE: dict[tuple, dict] = {}


def worklist_stats_cached(con) -> dict:
    """`worklist()`'s stats, memoised on the DB identity and the registry's own freshness
    (row count + newest updated_at). Ingest and field updates always bump updated_at, so a
    stale answer cannot outlive a write; the admin page renders in milliseconds instead of
    re-classifying every row on each load."""
    try:
        path = con.execute("PRAGMA database_list").fetchone()[2]
        stamp = tuple(con.execute(
            "SELECT COUNT(*), COALESCE(MAX(updated_at),''), COALESCE(MAX(worklist_synced_at),'')"
            " FROM prospects").fetchone())
    except sqlite3.Error:
        return worklist(con)["stats"]
    key = (path, *stamp)
    if key not in _WL_CACHE:
        _WL_CACHE.clear()                       # one DB state at a time: bounded memory
        _WL_CACHE[key] = worklist(con)["stats"]
    return _WL_CACHE[key]


_COUNTY_CITIES = {
    "miami-dade": ("miami", "hialeah", "coral gables", "doral", "miami beach", "homestead",
                   "miami lakes", "kendall", "aventura", "north miami", "north miami beach",
                   "south miami", "miami gardens", "cutler bay", "palmetto bay", "pinecrest",
                   "hialeah gardens", "miami springs", "opa-locka", "sunny isles beach",
                   "key biscayne", "bal harbour", "surfside", "bay harbor islands",
                   "sweetwater", "medley", "west miami", "golden beach"),
    "broward": ("fort lauderdale", "hollywood", "pompano beach", "plantation", "davie",
                "sunrise", "coral springs", "margate", "tamarac", "lauderhill", "weston",
                "miramar", "pembroke pines", "hallandale beach", "hallandale",
                "deerfield beach", "oakland park", "wilton manors", "coconut creek",
                "cooper city", "parkland", "lighthouse point", "dania beach"),
    "palm-beach": ("boca raton", "west palm beach", "delray beach", "boynton beach",
                   "wellington", "jupiter", "lake worth", "palm beach gardens",
                   "royal palm beach", "greenacres", "lantana", "palm springs",
                   "riviera beach", "north palm beach", "palm beach", "tequesta",
                   "juno beach", "lake park", "atlantis", "highland beach", "westlake"),
}


# The 330xx block is mostly Miami-Dade but contains the Upper/Middle Keys, which are Monroe
# County. Left in, they would overstate Miami-Dade coverage by a handful of rows.
_MONROE_ZIPS = {"33001", "33036", "33037", "33050", "33051", "33052", "33070"}


def _county(city: str, zip5: str) -> str:
    """Geography straight from the record: city when published, ZIP prefix otherwise."""
    c = (city or "").strip().lower()
    for county, cities in _COUNTY_CITIES.items():
        if c in cities:
            return county
    if (zip5 or "")[:5] in _MONROE_ZIPS:
        return "monroe"
    if c in ("key largo", "tavernier", "islamorada", "marathon", "key colony beach"):
        return "monroe"
    z = (zip5 or "")[:3]
    if z.isdigit():
        z = int(z)
        if 330 <= z <= 332:
            return "miami-dade"
        if z == 333:
            return "broward"
        if z == 334:
            return "palm-beach"
    return "unassigned"


def export_worklist(con, outdir) -> dict:
    """Write the segmented list to `outdir`: one CSV per segment, one per exclusion reason,
    plus SUMMARY.md/json. Rows keep their registry id, source URL and provenance note."""
    import csv
    res = worklist(con)
    rows, stats = res["rows"], res["stats"]
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    files = {}
    for aud, label in (("business", "businesses"), ("professional", "professionals"),
                       ("health_practice", "health_practices")):
        files[(SEG_OWNER, aud)] = f"01_owner_named_phone_{label}.csv"
    files.update({(SEG_PHONE, None): "02_business_phone_no_owner.csv",
                  (SEG_EMAIL, None): "03_email_only.csv",
                  (EX_INCOMPLETE, None): "04_excluded_incomplete.csv",
                  (EX_DUPLICATE, None): "05_excluded_duplicate.csv",
                  (EX_IRRELEVANT, None): "06_excluded_irrelevant.csv"})
    written = {}
    for (seg, aud), fname in files.items():
        subset = [r for r in rows if r["segment"] == seg
                  and (aud is None or r["audience"] == aud)]
        subset.sort(key=lambda r: (r["priority"], r.get("county") != "miami-dade",
                                   r.get("county") or "", r.get("category") or "", r["id"]))
        p = outdir / fname
        with p.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([h for _, h in WORKLIST_COLUMNS])
            for r in subset:
                w.writerow([(r.get(k) if r.get(k) is not None else "")
                            for k, _ in WORKLIST_COLUMNS])
        written[fname] = len(subset)
    # First batch an operator can start on today: the best-evidenced rows (named person, a line
    # they do not share, Miami-Dade), ordered by ZIP so calls batch geographically and the file
    # spans the county instead of one city, capped so it opens fast. These rows are phone-only
    # by nature — NPPES publishes no mailbox.
    pool = [r for r in rows if r["segment"] == SEG_OWNER and r["priority"] == 1]
    by_zip: dict[str, list] = {}
    for r in sorted(pool, key=lambda r: r["id"]):
        by_zip.setdefault(r.get("zip5") or "", []).append(r)
    picked: list = []                                  # round-robin over ZIPs, then re-sort by
    while len(picked) < 300 and any(by_zip.values()):  # ZIP: the batch spans the county and
        for z in sorted(by_zip):                       # still reads as one drive-able route
            if by_zip[z] and len(picked) < 300:
                picked.append(by_zip[z].pop(0))
    starter = sorted(picked, key=lambda r: ((r.get("zip5") or ""), (r.get("city") or "").lower(), r["id"]))
    sp = outdir / "00_start_here_first_300.csv"
    with sp.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([h for _, h in WORKLIST_COLUMNS])
        for r in starter:
            w.writerow([(r.get(k) if r.get(k) is not None else "")
                        for k, _ in WORKLIST_COLUMNS])
    stats["start_here"] = len(starter)
    stats["start_here_with_email"] = sum(1 for r in starter if (r.get("biz_email") or "").strip())
    stats["start_here_with_website"] = sum(1 for r in starter if (r.get("website") or "").strip())
    (outdir / "summary.json").write_text(json.dumps(stats, indent=1), encoding="utf-8")
    (outdir / "SUMMARY.md").write_text(_summary_md(stats), encoding="utf-8")
    return {"outdir": str(outdir), "written": written, "stats": stats}


# Fields the import is allowed to write. Everything else on the row — name, contacts,
# source_url, created_at, notes, decision, verify_state, stage — is provenance and is never
# touched by an import of a derived list.
WORKLIST_SYNC_FIELDS = ("segment", "audience", "priority", "phone_evidence",
                        "phone_shared_by", "outreach_status")

WORKLIST_SEGMENT_FILES = ("01_owner_named_phone_businesses.csv",
                          "01_owner_named_phone_professionals.csv",
                          "01_owner_named_phone_health_practices.csv",
                          "02_business_phone_no_owner.csv",
                          "03_email_only.csv")


def _digits(raw: str) -> str:
    return re.sub(r"\D", "", raw or "")[-10:]


def import_worklist(con: sqlite3.Connection, rows: list[dict], *, dry_run: bool = False) -> dict:
    """Persist the verified working list INTO the registry, keyed by the stable record id.

    The export is a projection of this same table, so this is not a re-upload of public data:
    it stamps each registry row with the segmentation an operator works from (`segment`,
    `audience`, `priority`, `phone_evidence`, `phone_shared_by`, `outreach_status`) so the
    admin can filter and sort on it in SQL. Three rules keep it safe:

    * the classification written is recomputed from the registry row, never copied from the
      CSV, so a stale file cannot relabel a record;
    * identity is checked (record id must exist and its name must match) — a mismatch is
      rejected, not guessed at;
    * only `WORKLIST_SYNC_FIELDS` are written. Contacts, source_url, dates, notes, decision and
      verification state keep their original provenance, and `updated_at` is untouched so the
      operator's own ordering is not disturbed (`worklist_synced_at` records the import).
    """
    current = {r["id"]: r for r in worklist(con)["rows"]}
    # what the row HOLDS today (worklist() overwrites `segment`/`priority` with the computed
    # answer, so the stored values must be read separately or the diff is always empty)
    stored = {r["id"]: dict(r) for r in con.execute(
        "SELECT id, segment, audience, priority, phone_evidence, phone_shared_by,"
        " outreach_status, worklist_synced_at FROM prospects")}
    now = _now()
    rep = {"attempted": 0, "matched": 0, "imported": 0, "updated": 0, "unchanged": 0,
           "excluded_in_export": 0, "already_on_list": 0,
           "rejected": {"unknown_record_id": 0, "name_mismatch": 0, "invalid_row": 0}}
    updates: list[tuple] = []
    for src in rows or []:
        rep["attempted"] += 1
        rid = (src.get("record_id") or "").strip()
        if not rid.isdigit():
            rep["rejected"]["invalid_row"] += 1
            continue
        r = current.get(int(rid))
        if r is None:
            rep["rejected"]["unknown_record_id"] += 1
            continue
        rep["matched"] += 1
        # identity guard: the id must still be the business the export named
        csv_key = _name_key(src.get("business_name") or "")
        if csv_key and csv_key != _name_key(r.get("name") or ""):
            rep["rejected"]["name_mismatch"] += 1
            continue
        if r["segment"] not in SEGMENTS:           # duplicate / incomplete / irrelevant today
            rep["excluded_in_export"] += 1
            continue
        was = stored.get(r["id"], {})
        if (was.get("worklist_synced_at") or "").strip():
            rep["already_on_list"] += 1
        want = {"segment": r["segment"], "audience": r["audience"],
                "priority": r["priority"], "phone_evidence": r["phone_evidence"],
                "phone_shared_by": r["rows_sharing_phone"]}
        # never reset a status an operator recorded later
        status = (r.get("outreach_status") or "").strip() or "NOT_CONTACTED"
        want["outreach_status"] = status
        if all(con_norm(was.get(k)) == con_norm(v) for k, v in want.items()):
            rep["unchanged"] += 1
            continue
        updates.append((want["segment"], want["audience"], int(want["priority"]),
                        want["phone_evidence"], int(want["phone_shared_by"]),
                        want["outreach_status"], now, int(rid)))
        rep["updated"] += 1
    if updates and not dry_run:
        con.executemany("UPDATE prospects SET segment=?, audience=?, priority=?, phone_evidence=?,"
                        " phone_shared_by=?, outreach_status=?, worklist_synced_at=? WHERE id=?",
                        updates)
        con.commit()
    rep["imported"] = rep["updated"] + rep["unchanged"]
    rep["dry_run"] = dry_run
    return rep


def con_norm(v):
    """Compare what SQLite holds with what the classifier produced ('' / None / 0 all mean
    'not set'), so a re-import of an unchanged list is a no-op."""
    if v is None:
        return ""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return 0 if v == 0 else v
    return str(v).strip()


def import_worklist_dir(con, outdir, *, dry_run: bool = False) -> dict:
    """Read a verified export directory back in and import every working-list row.

    The starter file (`00_*`) is a VIEW of segment 1, so it is deliberately not read (reading it
    would double-count those rows). Exclusion files are read only to report why they are not on
    the working list."""
    import csv
    outdir = Path(outdir)
    if not outdir.is_dir():
        raise FileNotFoundError(f"no export directory at {outdir}")
    rows: list[dict] = []
    per_file = {}
    for fname in WORKLIST_SEGMENT_FILES:
        p = outdir / fname
        if not p.exists():
            continue
        with p.open(encoding="utf-8") as f:
            got = list(csv.DictReader(f))
        per_file[fname] = len(got)
        rows.extend(got)
    rep = import_worklist(con, rows, dry_run=dry_run)
    not_imported = {}
    for fname, label in (("04_excluded_incomplete.csv", "incomplete: no phone and no email"),
                         ("05_excluded_duplicate.csv", "duplicate: thinner copy of a name+ZIP"),
                         ("06_excluded_irrelevant.csv", "irrelevant: non-business POI class")):
        p = outdir / fname
        if p.exists():
            with p.open(encoding="utf-8") as f:
                not_imported[label] = max(0, sum(1 for _ in csv.DictReader(f)))
    rep["files"] = per_file
    rep["not_imported"] = not_imported
    rep["outdir"] = str(outdir)
    rep["registry_total"] = con.execute("SELECT COUNT(*) FROM prospects").fetchone()[0]
    rep["on_working_list"] = con.execute(
        "SELECT COUNT(*) FROM prospects WHERE segment IN (?,?,?)", SEGMENTS).fetchone()[0]
    rep["by_segment"] = {r[0] or "(none)": r[1] for r in con.execute(
        "SELECT segment, COUNT(*) FROM prospects GROUP BY 1 ORDER BY 2 DESC")}
    rep["by_audience_on_list"] = {r[0] or "(none)": r[1] for r in con.execute(
        "SELECT audience, COUNT(*) FROM prospects WHERE segment IN (?,?,?)"
        " GROUP BY 1 ORDER BY 2 DESC", SEGMENTS)}
    return rep


def _summary_md(s: dict) -> str:
    seg = s["segments"]
    exc = s["exclusions"]
    lines = [
        "# GS Allude business prospect worklist",
        "",
        "Derived from `data/prospects.db` by `python tools/prospects.py segments`. Every registry",
        "row lands in exactly one bucket, so the numbers below reconcile to the database.",
        "Nothing here has been contacted: `outreach_status` is `NOT_CONTACTED` on every row.",
        "",
        "## Counts",
        "",
        "| Bucket | Records |",
        "|---|---|",
        f"| Registry rows examined | {s['examined']:,} |",
        f"| **Working list (segments 1-3)** | **{s['working_list']:,}** |",
        f"| 1. Named owner + usable phone | {seg[SEG_OWNER]:,} "
        f"({s['owner_named_phone_direct']:,} direct lines, "
        f"{s['owner_named_phone_shared']:,} shared switchboards) |",
        f"| 2. Business phone, no named owner | {seg[SEG_PHONE]:,} "
        f"({s['phone_no_owner_direct']:,} direct lines) |",
        f"| 3. Email only (no phone) | {seg[SEG_EMAIL]:,} |",
        f"| Excluded: incomplete (no phone, no email) | {exc[EX_INCOMPLETE]:,} |",
        f"| Excluded: duplicate (same name + ZIP, thinner copy) | {exc[EX_DUPLICATE]:,} |",
        f"| Excluded: irrelevant POI class | {exc[EX_IRRELEVANT]:,} |",
        "",
        f"Rows with an email on the working list: **{s['email_on_worklist']:,}** "
        f"(of which {s['email_only_records']:,} have no phone at all).",
        "",
        "## Audience (kept separate on purpose)",
        "",
        "| Audience | Records |",
        "|---|---|",
        f"| Businesses (storefronts, trades, offices, county registrations) | "
        f"{s['by_audience']['business']:,} |",
        f"| Individual professionals (self-employed licensees) | "
        f"{s['by_audience']['professional']:,} |",
        f"| Health practices (group coverage buyer) | {s['by_audience']['health_practice']:,} |",
        "",
        f"Unique businesses (name + ZIP; the {s['businesses_without_zip']:,} rows with no ZIP "
        f"counted individually because identity cannot be proven): **{s['unique_businesses']:,}** · "
        f"unique professionals: **{s['unique_professionals']:,}**.",
        "",
        "## Geography (working list)",
        "",
        "| County | Records |",
        "|---|---|",
    ]
    for k, v in s["by_county"].items():
        lines.append(f"| {k} | {v:,} |")
    sba = s["segments_by_audience"]
    lines += ["", "## Segment 1 by audience", "",
              "| Audience | Named owner + phone |", "|---|---|"]
    for a, label in (("business", "Businesses"), ("professional", "Individual professionals"),
                     ("health_practice", "Health practices")):
        lines.append(f"| {label} | {sba[SEG_OWNER][a]:,} |")
    lines += [
        "",
        "## Priority within the working list",
        "",
        "| Priority | Meaning | Records |",
        "|---|---|---|",
        f"| 1 | named person, direct line, Miami-Dade | {s['by_priority'][1]:,} |",
        f"| 2 | named person, direct line, outside Miami-Dade | {s['by_priority'][2]:,} |",
        f"| 3 | named decision-maker is a company/trust, direct line | {s['by_priority'][3]:,} |",
        f"| 4 | named person, but the line is shared by 3+ records | {s['by_priority'][4]:,} |",
        f"| 5 | business phone, no published owner | {s['by_priority'][5]:,} |",
        f"| 6 | email only, no phone | {s['by_priority'][6]:,} |",
        "",
        "## Top industries (working list)", "", "| Industry | Records |", "|---|---|"]
    for k, v in s["by_industry"].items():
        lines.append(f"| {k} | {v:,} |")
    def _src(d: dict) -> str:
        return ", ".join(f"`{k}` {v:,}" for k, v in d.items()) or "n/a"

    pct = (100.0 * s["email_on_worklist"] / s["working_list"]) if s["working_list"] else 0.0
    lines += [
        "",
        "## What limits this list (measured, not estimated)",
        "",
        f"The gap is not phone coverage — {s['owner_named_phone']:,} rows pair a named person with a "
        f"number, {s['owner_named_phone_direct']:,} of them a line the person is not sharing with "
        "other records. The gap is that names and numbers live in different files and no public "
        "field joins them:",
        "",
        f"* **{s['owner_no_channel']:,} rows name an owner and publish no phone and no email** "
        f"(largest sources: {_src(s['owner_no_channel_by_source'])}). These are county "
        "registrations — the officer is real, the contact channel was never in the file. They are "
        "in `04_excluded_incomplete.csv`, names intact, excluded from the callable list rather "
        "than guessed at.",
        f"* **{s['phone_no_owner']:,} rows publish a phone and no owner** (largest sources: "
        f"{_src(s['phone_no_owner_by_source'])}). A caller reaches the front desk and has to ask "
        "for the owner; the segment is labelled that way.",
        f"* **Email coverage is {pct:.1f}%** ({s['email_on_worklist']:,} of "
        f"{s['working_list']:,}). NPPES and the county tax file publish no mailbox at all; only "
        "addresses a business printed on its own site are here. No address was pattern-generated.",
        "* **Nothing on this list is verified.** Every row is `UNVERIFIED` because no operator has "
        "recorded a named check. A published number is evidence that the number was published, "
        "nothing more.",
        "",
        "**The single biggest obstacle to using this commercially:** matching an owner name to a "
        f"phone number for ordinary businesses. {s['owner_no_channel']:,} named owners have no "
        "channel and "
        f"{s['phone_no_owner']:,} phone numbers have no name; closing that needs a source that "
        "carries both (a licensed business-filing-to-phone join or operator lookups), not more "
        "scraping of either file. Self-employed professionals do not have this problem — their "
        "licence record is the join — which is why that segment is the cleanest place to start.",
        "",
        "## Files",
        "",
        "| File | Contents |",
        "|---|---|",
        f"| `00_start_here_first_300.csv` | first batch: {s.get('start_here', 0)} priority-1 rows "
        "(named person, unshared line, Miami-Dade), ordered by ZIP so calls batch "
        "geographically |",
        "| `01_owner_named_phone_businesses.csv` | named owner + phone: storefronts, trades, offices |",
        "| `01_owner_named_phone_professionals.csv` | named owner + phone: self-employed professionals |",
        "| `01_owner_named_phone_health_practices.csv` | named authorized official + practice phone |",
        "| `02_business_phone_no_owner.csv` | phone, owner not published |",
        "| `03_email_only.csv` | mailbox, no phone |",
        "| `04_excluded_incomplete.csv` | no phone and no email |",
        "| `05_excluded_duplicate.csv` | thinner copy of a name+ZIP already in the list |",
        "| `06_excluded_irrelevant.csv` | non-business POI classes |",
        "",
        "Rows are sorted by `priority`, then Miami-Dade first, then industry. Columns:",
        "`record_id` (registry id), `priority`, `segment`, `audience`, `business_name`,",
        "`decision_maker`, `owner_type` (person / entity / none), `professional_phone`,",
        "`phone_evidence` (direct / shared / none), `professional_email`, `website`,",
        "`street_address`, `city`, `state`, `zip`, `industry`, `source`, `source_url`,",
        "`collected_at`, `last_updated`, `verification`, `decision`, `pipeline_stage`,",
        "`outreach_status`, `phone_shared_by` (how many records publish that same number — 3+",
        "means a switchboard, not a direct line), `provenance_notes`.",
        "",
        "`verification` is the registry's own state: every row is `UNVERIFIED` until an operator",
        "records a named check. `phone_evidence=shared` does not mean wrong, it means the number",
        "reaches the institution, not necessarily the named person.",
    ]
    return "\n".join(lines) + "\n"


def acquisition_stats(con) -> dict:
    """Measured acquisition state — what an operator can act on, by source."""
    def _n(where: str) -> int:
        return con.execute(f"SELECT COUNT(*) FROM prospects WHERE {where}").fetchone()[0]
    total = _n("1=1")
    by_src = [dict(r) for r in con.execute(
        "SELECT source, COUNT(*) n, SUM(COALESCE(public_phone,'')<>'') phone, "
        "SUM(COALESCE(biz_email,'')<>'') email, SUM(COALESCE(contact_person,'')<>'') owner "
        "FROM prospects GROUP BY source ORDER BY n DESC")]
    return {
        "total": total,
        "with_phone": _n("COALESCE(public_phone,'')<>''"),
        "with_email": _n("COALESCE(biz_email,'')<>''"),
        "with_owner": _n("COALESCE(contact_person,'')<>''"),
        "with_website": _n("COALESCE(website,'')<>''"),
        "missing_any_contact": _n("COALESCE(public_phone,'')='' AND COALESCE(biz_email,'')=''"),
        "usable_phone_or_email": _n("COALESCE(public_phone,'')<>'' OR COALESCE(biz_email,'')<>''"),
        "named_and_reachable": _n("COALESCE(contact_person,'')<>'' AND (COALESCE(public_phone,'')<>'' "
                                    "OR COALESCE(biz_email,'')<>'')"),
        "complete_core": _n("COALESCE(public_phone,'')<>'' AND COALESCE(biz_email,'')<>'' "
                            "AND COALESCE(contact_person,'')<>''"),
        "by_source": by_src,
    }


def decide(con: sqlite3.Connection, pid: int, decision: str, note: str = "") -> bool:
    assert decision in DECISIONS, decision
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = con.execute("UPDATE prospects SET decision=?, updated_at=? WHERE id=?",
                      (decision, now, pid))
    con.execute("INSERT INTO prospect_log (prospect_id, ts, action, note) VALUES (?,?,?,?)",
                (pid, now, f"decision:{decision}", note[:500]))
    con.commit()
    return cur.rowcount > 0


def verify(con: sqlite3.Connection, pid: int, verify_state: str, note: str = "") -> bool:
    """A verification is an explicit operator record with a named source — same
    discipline as buyer verification in app.py. NEVER auto-derived; never activates."""
    assert verify_state in VERIFY_STATES, verify_state
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = con.execute("UPDATE prospects SET verify_state=?, verify_note=?, updated_at=? WHERE id=?",
                      (verify_state, note[:300], now, pid))
    con.execute("INSERT INTO prospect_log (prospect_id, ts, action, note) VALUES (?,?,?,?)",
                (pid, now, f"verify:{verify_state}", note[:500]))
    con.commit()
    return cur.rowcount > 0


def queue(con: sqlite3.Connection, *, decision: str = "", source: str = "",
          category: str = "", q: str = "", state: str = "", verify_state: str = "",
          segment: str = "", priority: int = 0, order: str = "updated",
          limit: int = 100) -> list[dict]:
    """Filtered prospect queue. `segment`/`priority` filter on the classification the worklist
    import persisted, so the working list is queryable without recomputing it."""
    sql = "SELECT * FROM prospects WHERE 1=1"
    args: list = []
    if decision:
        sql += " AND decision=?"; args.append(decision)
    if segment:
        sql += " AND segment=?"; args.append(segment)
    if priority:
        sql += " AND priority=?"; args.append(int(priority))
    if source:
        sql += " AND source=?"; args.append(source)
    if category:
        sql += " AND category LIKE ?"; args.append(f"%{category}%")
    if state:
        sql += " AND state=?"; args.append(state)
    if verify_state:
        sql += " AND verify_state=?"; args.append(verify_state)
    if q:
        sql += " AND (name LIKE ? OR website LIKE ? OR city LIKE ?)"
        args += [f"%{q}%"] * 3
    if order == "priority":   # working list first (priority 0 = never classified → last)
        sql += " ORDER BY CASE WHEN priority>0 THEN priority ELSE 99 END, name COLLATE NOCASE LIMIT ?"
    else:
        sql += " ORDER BY updated_at DESC LIMIT ?"
    args.append(max(1, min(limit, 500)))
    return [dict(r) for r in con.execute(sql, args)]


def facets(con: sqlite3.Connection) -> dict:
    f = {"decision": [], "source": [], "verify": []}
    for key, col in (("decision", "decision"), ("source", "source"),
                     ("verify", "verify_state")):
        f[key] = [r[0] for r in con.execute(
            f"SELECT DISTINCT {col} FROM prospects WHERE {col}<>'' ORDER BY 1")]
    return f


def counts(con: sqlite3.Connection) -> dict:
    out = {r[0] or "NEW": r[1] for r in
           con.execute("SELECT decision, COUNT(*) FROM prospects GROUP BY 1")}
    out["TOTAL"] = sum(out.values())
    return out


def log(con: sqlite3.Connection, pid: int, limit: int = 20) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT ts, action, note FROM prospect_log WHERE prospect_id=?"
        " ORDER BY id DESC LIMIT ?", (pid, limit))]


# ------------------------------------------------------- lifecycle: stages + field updates
def set_stage(con: sqlite3.Connection, pid: int, stage: str, note: str = "",
              allow_terminal: bool = False) -> bool:
    """Move a prospect along the LIFECYCLE with an evidence note. QUALIFIED / DISQUALIFIED /
    READY_FOR_ONBOARDING require a note (what makes it so); ONBOARDED / ACTIVATED are only
    recorded by the handoff / activation flows (allow_terminal), never hand-edited."""
    assert stage in STAGES, stage
    need_note = stage in ("QUALIFIED", "DISQUALIFIED", "READY_FOR_ONBOARDING")
    if need_note and not note.strip():
        raise ValueError(f"a note is required to set stage '{stage}' — what makes it so")
    if stage in ("ONBOARDED", "ACTIVATED") and not allow_terminal:
        raise ValueError(stage + " is recorded by the handoff/activation flow, not set by hand")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = con.execute("UPDATE prospects SET stage=?, updated_at=? WHERE id=?",
                      (stage, now, pid))
    con.execute("INSERT INTO prospect_log (prospect_id, ts, action, note) VALUES (?,?,?,?)",
                (pid, now, f"stage:{stage}", note[:500]))
    con.commit()
    return cur.rowcount > 0


def set_stage_checked(con, pid: int, stage: str, note: str = "", allow_terminal: bool = False):
    """set_stage() that returns an error string instead of raising (for route handlers)."""
    try:
        set_stage(con, pid, stage, note, allow_terminal)
        return None
    except (ValueError, AssertionError) as exc:
        return str(exc)


def update_fields(con: sqlite3.Connection, pid: int, fields: dict) -> list[str]:
    """Update ONLY the explicitly supplied fields from BIZ_FIELDS (whitelist — a form can
    never touch decision/verification/keys by accident). Blank-string values are deliberately
    ALLOWED: clearing a half-remembered detail is an operator act. Never invents defaults.
    Re-sending an unchanged value is a no-op (no log noise). Returns the names of columns
    actually changed."""
    if not fields:
        return []
    cur = con.execute("SELECT * FROM prospects WHERE id=?", (pid,)).fetchone()
    if not cur:
        raise ValueError(f"prospect #{pid} not found")
    cur = dict(cur)
    fields = {k: v for k, v in fields.items()
              if k in BIZ_FIELDS and (str(v or "").strip() or "") != (cur.get(BIZ_FIELDS[k]) or "").strip()}
    if not fields:
        return []
    sets, changed, args = [], [], []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for key, val in fields.items():
        col = BIZ_FIELDS.get(key)
        if not col:
            continue                      # unknown key: never write it, never widen the whitelist
        v = str(val or "").strip()[:400]
        if key in DATE_FIELDS and v:
            try:
                v = __import__("datetime").date.fromisoformat(v).isoformat()
            except ValueError:
                raise ValueError(f"'{key}' must be a YYYY-MM-DD date, got {v!r}") from None
        sets.append(f"{col}=?"); args.append(v); changed.append(col)
    if not sets:
        return []
    args += [now, pid]
    con.execute(f"UPDATE prospects SET {', '.join(sets)}, updated_at=? WHERE id=?", args)
    for col, val in zip(changed, args[:-2]):
        con.execute("INSERT INTO prospect_log (prospect_id, ts, action, note) VALUES (?,?,?,?)",
                    (pid, now, f"field:{col}", (val or "(cleared)")[:300]))
    con.commit()
    return changed


def detail(con: sqlite3.Connection, pid: int) -> dict | None:
    r = con.execute("SELECT * FROM prospects WHERE id=?", (pid,)).fetchone()
    return dict(r) if r else None


def needs_attention(r: dict) -> list[str]:
    """What a reviewer should look at before trusting this row - from RECORDED
    state only: basics missing, no qualification decision recorded, stage ahead of its
    evidence, or a stale next action."""
    gaps = []
    if not (r.get("website") or "").strip() and not ((r.get("public_phone") or "").strip()):
        gaps.append("no website and no phone recorded — basic identity incomplete")
    if not (r.get("address") or "").strip():
        gaps.append("no address recorded")
    if r.get("decision") in ("", "NEW"):
        gaps.append("no qualification decision recorded yet")
    if r.get("stage") in ("QUALIFIED", "READY_FOR_ONBOARDING") and \
            r.get("verify_state") not in ("PUBLIC_DETAILS_CHECKED", "OPERATOR_VERIFIED"):
        gaps.append("stage is ahead of verification — verify public details before handing off")
    if (r.get("stage") in ("RESEARCHED", "QUALIFIED", "READY_FOR_ONBOARDING")
            and not (r.get("next_action") or "").strip()):
        gaps.append("active stage but no next action recorded")
    if (r.get("next_action") or "").strip() and not (r.get("followup_note") or "").strip() \
            and r.get("updated_at") and r["updated_at"][:10] < r.get("created_at", "")[:10]:
        gaps.append("follow-up fields untouched since creation")
    return gaps


def stage_counts(con: sqlite3.Connection) -> dict:
    out = {s: 0 for s in STAGES}
    for stage, n in con.execute("SELECT stage, COUNT(*) FROM prospects GROUP BY 1"):
        if stage in out:
            out[stage] = n
    out["TOTAL"] = sum(out.values())
    return out


# ------------------------------------------------------- prospect → buyer onboarding handoff
def duplicate_buyer(row: dict, buyers: list[dict]) -> dict | None:
    """Find an existing buyer this prospect would duplicate: same website in any recorded
    delivery/webhook/contact field, or the same business name (case-insensitive). The
    handoff refuses while this matches — no accidental second buyer account."""
    site = _norm_site(row.get("website") or "")
    name = (row.get("name") or "").strip().lower()
    if not site and not name:
        return None
    for b in buyers:
        if not isinstance(b, dict) or b.get("is_demo"):
            continue                          # demo placeholders are fixtures, never duplicates
        s = json.dumps(b, ensure_ascii=False).lower()
        if site and site in s:
            return b
        if name and (b.get("name") or "").strip().lower() == name:
            return b
    return None


def handoff_gate(row: dict | None) -> list[str]:
    """Everything still missing before a prospect may be handed to buyer onboarding.
    Nothing here activates anything: the gate only guards the DRAFT creation."""
    if not row:
        return ["prospect not found"]
    missing = []
    if row.get("decision") != "QUALIFIED":
        missing.append(f"qualification decision is '{row.get('decision') or 'NEW'}' — record QUALIFIED with the why")
    if row.get("verify_state") != "OPERATOR_VERIFIED":
        missing.append(f"business identity/license verification is '{row.get('verify_state') or 'UNVERIFIED'}' — record OPERATOR_VERIFIED with where you checked")
    if row.get("stage") not in ("QUALIFIED", "READY_FOR_ONBOARDING"):
        missing.append(f"lifecycle stage is '{row.get('stage') or 'NEW'}' — need QUALIFIED or READY_FOR_ONBOARDING")
    if not (row.get("state") or row.get("service_territory") or "").strip():
        missing.append("no service territory / licensed state recorded — the buyer draft needs at least one")
    return missing


def handoff_buyer_draft(row: dict, buyers: list[dict]) -> dict:
    """Build the INACTIVE buyer draft the existing onboarding flow expects. Carries forward
    ONLY facts the operator recorded; unknowns stay blank (license/NPN/dates are filled at
    onboarding, never guessed). The draft is never active and has no portal token.
    Raises ValueError with the exact blockers if the gate is not satisfied."""
    missing = handoff_gate(row)
    dup = duplicate_buyer(row, buyers)
    if dup:
        missing.append(f"already a buyer record: '{dup.get('name') or dup.get('id')}' (id {dup.get('id')}) — resolve the duplicate instead of creating a second account")
    if missing:
        raise ValueError("handoff refused — " + " | ".join(missing))
    base = re.sub(r"[^a-z0-9]+", "-", (row.get("name") or "prospect").lower()).strip("-")[:24] or "prospect"
    bid, n = base, 1
    taken = {b.get("id", "").lower() for b in buyers}
    while bid.lower() in taken:
        n += 1; bid = f"{base}-{n}"
    territory = (row.get("service_territory") or row.get("state") or "").strip()
    states = sorted({s.strip().upper()[:2] for s in re.split(r"[,; /]+", territory) if len(s.strip()) == 2})
    lead_types = sorted({t.strip() for t in (row.get("lead_types") or "").split(",") if t.strip()})
    return {
        "id": bid, "name": row.get("name") or bid, "active": False, "is_demo": False,
        "priority": 2, "exclusive": True, "tiers": ["A", "B"],
        "licensed_states": states or ["FL"],
        "products": [t for t in lead_types if t in _ALLOWED_BUYER_PRODUCTS] or ["health_aca"],
        "price_by_tier": {}, "daily_cap": 0, "age_min": 18, "age_max": 100,
        "delivery": {"method": "outbox" if not (row.get("biz_email") or "").strip() else "email",
                     "email": (row.get("biz_email") or "").strip()[:200]},
        "converted_from_prospect": {"prospect_id": row.get("id"),
                                    "prospect_source": row.get("source") or ""},
    }


def set_buyer_link(con: sqlite3.Connection, pid: int, buyer_id: str) -> None:
    """Record which buyer account a prospect was converted into (traceability in both
    directions). Re-linking overwrites the pointer and logs the change."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    prev = (con.execute("SELECT buyer_id FROM prospects WHERE id=?", (pid,)).fetchone() or [None])[0]
    con.execute("UPDATE prospects SET buyer_id=?, updated_at=? WHERE id=?", (buyer_id[:80], now, pid))
    con.execute("INSERT INTO prospect_log (prospect_id, ts, action, note) VALUES (?,?,?,?)",
                (pid, now, "handoff",
                 (f"buyer draft created: {buyer_id}" if not prev
                  else f"buyer link changed {prev} -> {buyer_id}")[:500]))
    con.commit()


# ------------------------------------------------------- onboarding-readiness checklist
CHECK_GATE = ("PROSPECT",)
CHECK_MISSING, CHECK_PENDING, CHECK_REJECTED, CHECK_OK = "missing", "pending review", "rejected", "complete"


def gate_item(row: dict) -> dict:
    """The prospect-side half of the checklist. `source` tells the operator WHERE the field
    lives, `kind` whether it is operator-PROVIDED info or an INDEPENDENT verification.
    pending review = decision recorded, independent verification not completed yet."""
    dec = row.get("decision") or "NEW"
    ver = row.get("verify_state") or "UNVERIFIED"
    territory = (row.get("state") or row.get("service_territory") or "").strip()
    return {
        "items": [
            {"label": "Qualification decision recorded", "status": CHECK_OK if dec == "QUALIFIED"
             else (CHECK_REJECTED if dec in ("NOT_A_FIT", "ARCHIVED") else CHECK_MISSING),
             "source": "operator decision (prospect record)", "kind": "provided",
             "detail": f"decision is {dec or 'NEW'}"},
            {"label": "Business identity / licensing verified",
             "status": CHECK_OK if ver == "OPERATOR_VERIFIED"
             else (CHECK_REJECTED if ver == "DENIED" else
                   (CHECK_PENDING if ver == "PUBLIC_DETAILS_CHECKED" else CHECK_MISSING)),
             "source": "independent check — operator record naming what was checked + where/when",
             "kind": "verified",
             "detail": (row.get("verify_note") or "")[:200] or "no verification note recorded"},
            {"label": "Service territory / licensed region recorded",
             "status": CHECK_OK if territory else CHECK_MISSING,
             "source": "as stated by the business or found publicly (prospect record)",
             "kind": "provided", "detail": territory or "unknown"},
            {"label": "Lifecycle stage at QUALIFIED or READY_FOR_ONBOARDING",
             "status": CHECK_OK if row.get("stage") in ("QUALIFIED", "READY_FOR_ONBOARDING",
                                                        "ONBOARDING_STARTED", "ONBOARDED", "ACTIVATED")
             else CHECK_MISSING,
             "source": "operator stage ladder (prospect record)", "kind": "provided",
             "detail": row.get("stage") or "NEW"},
        ],
    }


def decision_reasons(con: sqlite3.Connection, limit: int = 500) -> dict:
    """prospects.id -> the note of the operator's LAST decision-record (for showing the
    recorded why next to DISQUALIFIED / any decision status)."""
    out: dict = {}
    for r in con.execute(
            f"SELECT prospect_id, note FROM prospect_log WHERE action LIKE 'decision:%'"
            f" ORDER BY id DESC LIMIT {int(limit)}"):
        if r["prospect_id"] not in out:            # first hit = most recent decision
            out[r["prospect_id"]] = (r["note"] or "")[:200]
    return out


# --------------------------------------------------------------------------- adapters
def fetch_places(con, queries: list[dict], search_text=None) -> dict:
    """Google Places API (official, paid per request) adapter.

    Boundaries enforced IN CODE: refuses unless GOOGLE_PLACES_API_KEY is set AND
    config/sources.json says \"places\": {\"approved\": true} (an explicit operator
    record — spending is never implicit). `search_text` injectable for offline tests.
    Normalizes into THIS registry, not the signal loop (a business is not a signal).

    Per-query error handling: one failed Text Search request is logged to collect_log
    and the sweep continues to the next query — an API hiccup never aborts the whole
    run or double-charges completed queries. Returns a result dict with fetched / added
    / updated / skipped / errors so callers can report the sweep honestly.
    """
    key = (os.environ.get("GOOGLE_PLACES_API_KEY") or "").strip()
    approved = _places_approved()
    if not key or not approved:
        raise NotImplementedError(
            "Google Places adapter is gated: GOOGLE_PLACES_API_KEY "
            + ("is unset" if not key else "is set but budget approval"
               " is missing (config > places > approved: true)")
            + " — no API call was made (per-request billing)")
    if search_text is None:
        search_text = _places_search_text(key)
    fetched = added = updated = errors = 0
    skipped_total = 0
    for q in queries:
        qtext = (q.get("text_query") or "").strip()
        if not qtext:
            continue
        fetched_in_q = added_in_q = updated_in_q = skipped_in_q = 0
        try:
            out = search_text(qtext)
        except Exception as e:              # noqa: BLE001 - network trouble is a reportable condition
            errors += 1
            collect_log(con, "google_places", qtext, fetched_in_q, added_in_q,
                        f"error: {type(e).__name__}: {e}")
            continue
        rows = []
        for c in out.get("places", []):
            fetched_in_q += 1
            rows.append({
                "name": (c.get("displayName", {}).get("text") or "")[0:200] if c.get("displayName") else "",
                "category": (c.get("primaryTypeDisplayName", {}).get("text") or "")[:80],
                "phone": (c.get("nationalPhoneNumber") or "")[:40],
                "website": (c.get("websiteUri") or "")[:300],
                "address": (c.get("formattedAddress") or "")[:300],
                "city": (c.get("addressComponents") and _place_city(c) or ""),
                "state": (c.get("addressComponents") and _place_state(c) or ""),
                "zip5": (c.get("addressComponents") and _place_zip(c) or ""),
                "evidence_url": (c.get("googleMapsUri") or "")[:400],
                "source": "google_places",
            })
        if rows:
            ing = ingest(con, rows, "google_places")
            added_in_q += ing.get("added", 0)
            updated_in_q += ing.get("updated", 0)
            skipped_in_q += ing.get("skipped", 0)
        fetched += fetched_in_q
        added += added_in_q
        updated += updated_in_q
        skipped_total += skipped_in_q
        collect_log(con, "google_places", qtext, fetched_in_q, added_in_q,
                    "ok" if not errors else f"ok (with {errors} failed query(ies) before this one)")
    return {"fetched": fetched, "added": added, "updated": updated,
            "skipped": skipped_total, "errors": errors}


def collect_log(con, source: str, query: str, fetched: int, added: int, note: str = "") -> None:
    """One collect_log row per fetch attempt against a prospect source (like the signal
    loop's fetch_log): makes last-success / last-error status visible in the admin
    console instead of buried in a CLI scrollback."""
    con.execute("INSERT INTO collect_log (ts, source, query, fetched, added, note) "
                "VALUES (datetime('now'),?,?,?,?,?)",
                (source, (query or "")[:200], int(fetched), int(added), (note or "")[:300]))
    con.commit()


def collect_log_recent(con, limit: int = 8) -> list[dict]:
    return [dict(r) for r in con.execute(
        f"SELECT ts, source, query, fetched, added, note FROM collect_log "
        f"ORDER BY id DESC LIMIT {int(limit)}")]



PLACES_COMPONENT = {"administrative_area_level_2": "city", "locality": "city",
                    "administrative_area_level_1": "state", "postal_code": "zip5"}


def _place_field(c: dict, kind: str) -> str:
    for comp in c.get("addressComponents", []):
        for t in comp.get("types", []):
            if t == kind:
                return comp.get("longText", "")
    return ""


def _place_city(c): return _place_field(c, "locality")


def _place_state(c): return _place_field(c, "administrative_area_level_1")


def _place_zip(c): return _place_field(c, "postal_code")


def _places_approved() -> bool:
    try:
        cfg = json.loads((ROOT / "config" / "sources.json").read_text(encoding="utf-8"))
        return bool(cfg.get("places", {}).get("approved"))
    except (OSError, ValueError):
        return False


def _places_search_text(key: str):
    import urllib.request
    ua = "GSAlludeProspectDiscovery/1.0"

    def _call(text_query: str) -> dict:
        body = json.dumps({"textQuery": text_query}).encode()
        req = urllib.request.Request(
            "https://places.googleapis.com/v1/places:searchText", data=body,
            headers={"Content-Type": "application/json", "User-Agent": ua,
                     "X-Goog-Api-Key": key,
                     "X-Goog-FieldMask": "places.displayName,places.primaryTypeDisplayName,"
                                         "places.nationalPhoneNumber,places.websiteUri,"
                                         "places.formattedAddress,places.addressComponents,"
                                         "places.googleMapsUri"})
        with urllib.request.urlopen(req, timeout=20) as resp:   # noqa: S310 - official API, gated above
            return json.loads(resp.read().decode("utf-8", "replace"))
    return _call


# --------------------------------------------------------------------------- registry row group
def prospect_sources() -> list[dict]:
    """One row per acquisition path with its honest status (mirrors docs/SOURCES.md)."""
    key = bool((os.environ.get("GOOGLE_PLACES_API_KEY") or "").strip())
    return [
        {"id": "manual_csv", "label": "Operator research + CSV import (public directories, "
         "license boards, chambers, referral partners)", "status": "COLLECTS",
         "cost": "$0",
         "how": "Admin > Prospects (add/import) or tools/prospects.py import csv --file X --source manual",
         "notes": "Working today: rows land in data/prospects.db as decision NEW, "
                  "verify_state UNVERIFIED; no contact action exists anywhere."},
        {"id": "google_places", "label": "Google Places API (official, billed per request)",
         "status": "NEEDS_KEY",
         "cost": "Google Places billing",
         "how": "tools/prospects.py places --query \"insurance agencies in Miami, FL\" → "
                "engine/prospects.py fetch_places() — runs only with GOOGLE_PLACES_API_KEY "
                "AND sources.json > places > approved: true (an explicit operator record)",
         "notes": "The ToS-clean path once a paid budget is approved. Adapter refuses and "
                  "names the missing dependency without key + approval; per-query errors are "
                  "logged to prospects.collect_log and visible on /admin/integrations."},
        {"id": "maps_scraper_kit", "label": "google-maps-scraper-kit (MIT) — headless browser "
         "over google.com/maps", "status": "TOS_BLOCKED", "cost": "$0 but access risk",
         "how": "NOT integrated: the tool's own README states scraping Google Maps is "
                "against Google's ToS. License doesn't change the access method.",
         "notes": "Blocked by our access boundary; documented, not implemented. The "
                  "permitted alternative is the official Places API above."},
        {"id": "geoleadscraper_csv", "label": "GeoLeadScraper browser-extension export "
         "(Google Maps / Yandex Maps / 2GIS) — operator-run", "status": "OPERATOR_FILE",
         "cost": "$0 (no API bill)",
         "how": "The operator runs the extension in their own browser and imports its CSV: "
                "tools/prospects.py import geoleads --file export.csv → "
                "engine/prospects.py import_geoleads_csv() (title→name, address → city/state/zip)",
         "notes": "Identity list only: name, category, public phone, website, address, "
                  "maps_url as evidence. Emails/socials from the export are DROPPED here; "
                  "they must be re-derived from the business's own site. Same Maps access "
                  "caveat as maps_scraper_kit — see docs/GEOLEADSCRAPER_REVIEW.md. Rows land "
                  "NEW/UNVERIFIED; nothing in this path contacts anyone."},
        {"id": "mdc_lbt", "label": "Miami-Dade Local Business Tax receipts (official county "
         "open data, ArcGIS)", "status": "COLLECTS", "cost": "$0",
         "how": "tools/prospects.py lbt --limit N → engine/prospects.py fetch_mdc_lbt()",
         "notes": "Business name, published officer string, address, ZIP, county activity "
                  "class. No phone or email in the county file: those are filled only from a "
                  "business's own site or another published record at the same premises."},
        {"id": "npi_nppes", "label": "CMS NPPES NPI Registry API (official, free): health "
         "practices (NPI-2) and individual providers (NPI-1)", "status": "COLLECTS",
         "cost": "$0",
         "how": "tools/prospects.py npi [--max N] / npi-individuals [--max N] [--cities \"A,B\"]",
         "notes": "Org rows carry the registered authorized official as decision-maker; "
                  "individual rows ARE the self-employed professional. Practice telephone and "
                  "address come from the published LOCATION address. No emails in NPPES."},
        {"id": "osm_overpass", "label": "OpenStreetMap business POIs with published contact "
         "tags (Overpass API, ODbL, free)", "status": "COLLECTS", "cost": "$0",
         "how": "tools/prospects.py osm [--area miami-dade|broward|palm-beach] [--extended]",
         "notes": "Only named, non-chain POIs that already publish a phone or email. "
                  "Owner names are not in OSM; mailboxes are then read from the business's "
                  "own site (robots.txt honoured)."},
    ]
