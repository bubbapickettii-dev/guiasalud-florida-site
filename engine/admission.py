"""IMPORTED CONTACTS — third-party records admitted WITHOUT claiming consent.

The correction this module makes: GS Allude could previously only hold contact records that a
person submitted themselves. That is the right rule for `leads` (and `engine/db.py` enforces it in
the database: a phone on a lead must be CONSUMER_SUBMITTED). It is the wrong rule for a contact
record acquired from a dataset, an export or a licensed source — those records are real, worth
keeping, and simply are NOT consent.

So there are now two separate populations in the same database, and they never blur:

  imported_contacts   a record we HOLD. Name/phone/email/source/collection date/provenance.
                      Stored as evidence of possession, nothing more.
  leads + consents    a person's OWN submission with their consent row. The only saleable
                      population; `engine/route.py` reads nothing else.

Eligibility ladder (per record, `marketing_eligibility`):

  NOT_ELIGIBLE        no permission basis recorded. The default and the only possible value an
                      import can produce. Not routed, not sellable, not contactable.
  USE_LIMITED         an operator recorded what a source's licence/terms actually permit for
                      THIS record (e.g. "reply to an inbound inquiry", "analytics only"). Still
                      not sellable, still never routed — a permitted non-marketing use is not
                      marketing permission.
  ELIGIBLE_CONSENTED  a real `consents` row exists for that same person's own submission and
                      the record is linked to the resulting lead (`lead_id`). This is the ONLY
                      value that corresponds to something a buyer may receive, and it is reached
                      exclusively through promote(), never through an import.

Why imported rows can never leak into a sale:
  * route() only ever sees `leads` rows, and only QUALIFIED + PHONE_LEAD + a phone.
  * a `leads` row cannot be created with a third-party phone (DB trigger, db.py).
  * this table's triggers refuse ELIGIBLE_CONSENTED and refuse a lead_id unless a `consents`
    row for that exact lead exists, so no amount of code — or a future refactor — can relabel a
    third-party record as consented.
  * imported evidence (phone, email, source, collection date) is immutable, so a re-import can
    never rewrite the facts an eligibility decision was made on.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ELIGIBILITY = ("NOT_ELIGIBLE", "USE_LIMITED", "ELIGIBLE_CONSENTED")
SALE_BLOCKED = ("NOT_ELIGIBLE", "USE_LIMITED")
# WHAT the record is. A healthcare provider is not a consumer and not a business lead; keeping
# them apart is what lets an operator see "we hold 4,102 real provider phones, 0 consumer contacts"
# instead of a single undifferentiated pile.
RECORD_TYPES = ("CONSUMER", "BUSINESS", "PROVIDER", "PROVIDER_ORG", "PROFESSIONAL", "UNKNOWN")
# HOW MUCH WE KNOW it is real. Separate from permission: a SYNTHETIC row is not a person at all,
# and a VERIFIED_REAL row is a real person we still have no permission to market to.
DATA_STATUSES = ("VERIFIED_REAL", "UNVERIFIED", "SYNTHETIC", "REJECTED")
DUP_STATES = ("UNIQUE", "DUPLICATE")

WHY = {
    "NOT_ELIGIBLE": "no permission basis recorded for this record. Possession is not permission.",
    "USE_LIMITED": "a non-marketing use was recorded for this source. Still not sellable or routable.",
    "ELIGIBLE_CONSENTED": "this person submitted the /check form themselves; their consent row is linked.",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS imported_contacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contact_key TEXT UNIQUE,            -- identity anchor: e:<email> | p:<phone10> | n:<name>|<state>
  source_id TEXT,                     -- dataset/source id (provenance, required)
  source_name TEXT DEFAULT '',
  source_url TEXT DEFAULT '',         -- where the record was published, or the dataset page
  source_record_id TEXT DEFAULT '',   -- the row id inside that source, when it has one
  collected_at TEXT DEFAULT '',       -- the SOURCE's own date (signup date, page date, export date)
  imported_at TEXT, import_batch TEXT DEFAULT '',
  license TEXT DEFAULT '',            -- the licence/terms label recorded for the source
  license_note TEXT DEFAULT '',
  record_type TEXT DEFAULT 'UNKNOWN', -- CONSUMER | BUSINESS | PROVIDER | PROVIDER_ORG | ...
  data_status TEXT DEFAULT 'UNVERIFIED', -- VERIFIED_REAL | UNVERIFIED | SYNTHETIC | REJECTED
  quality TEXT DEFAULT '{}',         -- JSON: which fields are present AND well-formed
  verified_at TEXT DEFAULT '', verified_by TEXT DEFAULT '',
  full_name TEXT DEFAULT '', first_name TEXT DEFAULT '', last_name TEXT DEFAULT '',
  email TEXT DEFAULT '', phone TEXT DEFAULT '', phone_e164 TEXT DEFAULT '',
  telegram TEXT DEFAULT '',
  city TEXT DEFAULT '', state TEXT DEFAULT '', zip TEXT DEFAULT '', country TEXT DEFAULT '',
  has_contact INTEGER DEFAULT 0,      -- did the source actually give a phone or an email?
  source_fields TEXT DEFAULT '{}',    -- every column the source supplied, verbatim
  evidence TEXT DEFAULT '{}',         -- provenance proof for this record (JSON)
  permitted_uses TEXT DEFAULT '',     -- operator-recorded permitted uses (comma list)
  marketing_eligibility TEXT DEFAULT 'NOT_ELIGIBLE',
  eligibility_reason TEXT DEFAULT '',
  consent_ref TEXT DEFAULT '',        -- leads.lead_id whose consent row authorises this record
  lead_id TEXT DEFAULT '',            -- set ONLY by promote()
  dup_status TEXT DEFAULT 'UNIQUE', dup_of INTEGER,
  is_test INTEGER DEFAULT 0,
  created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS import_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch TEXT UNIQUE, ts TEXT, source_id TEXT, source_name TEXT, source_url TEXT,
  license TEXT, record_type TEXT, received INTEGER DEFAULT 0, imported INTEGER DEFAULT 0,
  duplicates INTEGER DEFAULT 0, rejected INTEGER DEFAULT 0, missing_contact INTEGER DEFAULT 0,
  note TEXT DEFAULT ''
);
"""

# Hard guarantees, enforced by the database itself (same discipline as db.py's lead triggers):
#  1. a record cannot be labelled ELIGIBLE_CONSENTED without a real consent row behind it
#  2. a record cannot point at a lead that has no consent row
#  3. imported evidence can never be rewritten, so a re-import cannot launder eligibility
# SQLite accepts only one WHEN clause per trigger, so the insert guard is expressed as a single
# condition that is TRUE exactly when the row makes a consent claim and no consent row backs it.
TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS ic_insert_needs_consent BEFORE INSERT ON imported_contacts
WHEN (COALESCE(NEW.marketing_eligibility,'') = 'ELIGIBLE_CONSENTED'
      OR COALESCE(NEW.lead_id,'') != ''
      OR COALESCE(NEW.consent_ref,'') != '')
 AND (NOT EXISTS (SELECT 1 FROM consents WHERE lead_id = COALESCE(NEW.consent_ref,''))
      OR NOT EXISTS (SELECT 1 FROM consents WHERE lead_id = COALESCE(NEW.lead_id,''))
      OR COALESCE(NEW.consent_ref,'') != COALESCE(NEW.lead_id,''))
BEGIN SELECT RAISE(ABORT, 'imported contact cannot claim consent without a consents row'); END;
CREATE TRIGGER IF NOT EXISTS ic_eligibility_needs_consent
BEFORE UPDATE OF marketing_eligibility, consent_ref, lead_id ON imported_contacts
WHEN (COALESCE(NEW.marketing_eligibility,'') = 'ELIGIBLE_CONSENTED'
      OR COALESCE(NEW.lead_id,'') != '')
 AND (NOT EXISTS (SELECT 1 FROM consents WHERE lead_id = COALESCE(NEW.lead_id,''))
      OR COALESCE(NEW.consent_ref,'') != COALESCE(NEW.lead_id,''))
BEGIN SELECT RAISE(ABORT, 'imported contact cannot be made consented without a consents row'); END;
-- A generated row is not a person: it can never be labelled eligible, whatever else is true.
CREATE TRIGGER IF NOT EXISTS ic_synthetic_never_eligible
BEFORE UPDATE OF marketing_eligibility ON imported_contacts
WHEN COALESCE(NEW.data_status,'') = 'SYNTHETIC'
 AND COALESCE(NEW.marketing_eligibility,'') != 'NOT_ELIGIBLE'
BEGIN SELECT RAISE(ABORT, 'synthetic records can never be marketing-eligible'); END;
CREATE TRIGGER IF NOT EXISTS ic_evidence_immutable
BEFORE UPDATE OF phone, phone_e164, email, contact_key, source_id, source_url, collected_at
ON imported_contacts
WHEN COALESCE(OLD.phone,'') IS NOT COALESCE(NEW.phone,'')
  OR COALESCE(OLD.phone_e164,'') IS NOT COALESCE(NEW.phone_e164,'')
  OR COALESCE(OLD.email,'') IS NOT COALESCE(NEW.email,'')
  OR COALESCE(OLD.contact_key,'') IS NOT COALESCE(NEW.contact_key,'')
  OR COALESCE(OLD.source_id,'') IS NOT COALESCE(NEW.source_id,'')
  OR COALESCE(OLD.source_url,'') IS NOT COALESCE(NEW.source_url,'')
  OR COALESCE(OLD.collected_at,'') IS NOT COALESCE(NEW.collected_at,'')
BEGIN SELECT RAISE(ABORT, 'imported contact evidence is immutable'); END;
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure(con: sqlite3.Connection) -> sqlite3.Connection:
    """Create the admission tables + triggers on an EXISTING lead connection.

    Called from prospector.ensure(), so every path that builds a lead database (app startup,
    tools, tests) gets them — including the trigger guarantees.
    """
    con.executescript(SCHEMA)
    # additive migration for a table created by an earlier build (the live DB predates data status)
    have = {r[1] for r in con.execute("PRAGMA table_info(imported_contacts)")}
    for col, typ in (("data_status", "TEXT DEFAULT 'UNVERIFIED'"), ("quality", "TEXT DEFAULT '{}'"),
                     ("verified_at", "TEXT DEFAULT ''"), ("verified_by", "TEXT DEFAULT ''")):
        if col not in have:
            con.execute(f"ALTER TABLE imported_contacts ADD COLUMN {col} {typ}")
    # indexed AFTER the migration: a table created by an earlier build may lack some of these
    # columns, and an index on a missing column would break startup instead of just being unhelpful.
    have = {r[1] for r in con.execute("PRAGMA table_info(imported_contacts)")}
    for name, cols in (("ix_ic_elig", ("marketing_eligibility", "imported_at")),
                       ("ix_ic_source", ("source_id", "contact_key")),
                       ("ix_ic_phone", ("phone_e164",)), ("ix_ic_email", ("email",)),
                       ("ix_ic_status", ("data_status", "record_type"))):
        if set(cols) <= have:
            con.execute(f"CREATE INDEX IF NOT EXISTS {name} ON imported_contacts({','.join(cols)})")
    con.executescript(TRIGGERS)
    con.commit()
    return con


def connect(db_path: Path | str) -> sqlite3.Connection:
    from .db import connect as _lead_connect
    con = _lead_connect(db_path)
    return ensure(con)


# ------------------------------------------------------------------ normalization
_EMAIL_ALIASES = ("email", "e_mail", "email_address", "mail", "contact_email", "work_email")
_PHONE_ALIASES = ("phone", "phone_number", "telephone", "telephone_number", "tel", "mobile",
                  "cell", "cell_phone", "contact_phone", "whatsapp")
# A person's name and an organisation's name are NOT interchangeable: splitting "SOUTHEAST HEALTH
# MEDICAL CENTER" into a first and last name would invent two fields that do not exist.
_ORG_NAME_ALIASES = ("facility_name", "organization_name", "org_name", "business_name", "company",
                     "company_name", "provider_name")
_NAME_ALIASES = ("full_name", "name", "display_name", "person_name", "contact_name", "title",
                 "contact", "owner_name") + _ORG_NAME_ALIASES
_FIRST_ALIASES = ("first_name", "firstname", "given_name", "fname")
_LAST_ALIASES = ("last_name", "lastname", "surname", "family_name", "lname")
_SOURCE_URL_ALIASES = ("source_url", "url", "profile_url", "page_url", "website", "profile",
                       "link", "permalink", "post_url")
_RECORD_ID_ALIASES = ("id", "record_id", "row_id", "place_id", "cid", "uid", "npi",
                      "facility_id", "cms_certification_number_ccn", "public_identifier")
_COLLECTED_ALIASES = ("collected_at", "date", "scraped_at", "signup_date", "joined", "created",
                      "created_at", "updated", "fetched_at", "timestamp", "enumeration_date",
                      "registration_date", "last_updated")
_CITY_ALIASES = ("city", "town", "citytown", "locality", "municipality")
_STATE_ALIASES = ("state", "region", "st", "state_code", "province")
_ZIP_ALIASES = ("zip", "zip5", "zip_code", "postal_code", "postcode", "postcode_zip")
_COUNTRY_ALIASES = ("country", "country_code")
_TELEGRAM_ALIASES = ("telegram", "tg", "telegram_username", "telegram_handle", "username", "handle")

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\+?[0-9][0-9\s().\-]{6,}[0-9]")


def _slug(k) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(k or "").strip().lower()).strip("_")


def _key_slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def _norm_email(v: str) -> str:
    m = _EMAIL_RE.search(v or "")
    return m.group(0).lower() if m else ""


def _norm_phone(v: str) -> tuple[str, str]:
    """-> (human-readable digits, E.164-ish). US 11-digit leading 1 is folded to 10 digits."""
    m = _PHONE_RE.search(v or "")
    if not m:
        return "", ""
    digits = re.sub(r"\D", "", m.group(0))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) < 7 or len(digits) > 15:
        return "", ""
    e164 = "+1" + digits if len(digits) == 10 else "+" + digits
    return digits, e164


def norm_phone(v: str) -> str:
    """E.164 or ''. Public wrapper so a source evaluation measures with the SAME rule the
    importer applies — a coverage number that disagrees with what gets stored is worse than none."""
    return _norm_phone(v)[1]


def norm_email(v: str) -> str:
    return _norm_email(v)


def _split_name(full: str) -> tuple[str, str]:
    parts = [p for p in re.split(r"\s+", (full or "").strip()) if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


def normalize(raw: dict) -> dict:
    """Map a source row of ANY column naming onto the canonical contact fields.

    Nothing is invented: a field the source did not supply stays empty, and `source_fields`
    keeps the row exactly as it arrived so an operator can always see what was really there.
    `reason` explains why a row got no identity anchor (and is therefore rejected).
    """
    g = {_slug(k): v for k, v in (raw or {}).items()}

    def pick(*names):
        for n in names:
            v = g.get(_slug(n))
            if v is None:
                continue
            s = str(v).strip()
            if s and s.lower() not in ("none", "null", "nan", "-", "n/a"):
                return s
        return ""

    email = _norm_email(pick(*_EMAIL_ALIASES))
    phone, e164 = _norm_phone(pick(*_PHONE_ALIASES))
    first = pick(*_FIRST_ALIASES)
    last = pick(*_LAST_ALIASES)
    full = pick(*_NAME_ALIASES)
    org_val = pick(*_ORG_NAME_ALIASES)
    org_name = bool(org_val) and org_val == full
    if not full and (first or last):
        full = f"{first} {last}".strip()
    if not org_name and not (first and last) and full:
        f2, l2 = _split_name(full)
        first, last = first or f2, last or l2
    state = pick(*_STATE_ALIASES).upper()[:2]
    city = pick(*_CITY_ALIASES)

    key, reason = "", ""
    if email:
        key = "e:" + email
    elif phone:
        key = "p:" + phone
    elif full:
        key = "n:" + _key_slug(full) + "|" + state
    else:
        reason = "no_identity: the row carried no name, phone or email"

    # quality: what the source gave us AND whether it is well formed. Measured, not assumed -
    # a present-but-unusable phone is a different fact from a missing one.
    raw_phone = pick(*_PHONE_ALIASES)
    raw_email = pick(*_EMAIL_ALIASES)
    quality = {"has_name": 1 if full else 0, "org_name": 1 if org_name else 0,
               "has_phone": 1 if raw_phone else 0,
               "phone_valid": 1 if phone else 0, "has_email": 1 if raw_email else 0,
               "email_valid": 1 if email else 0, "has_city": 1 if city else 0,
               "has_state": 1 if state else 0, "has_zip": 1 if pick(*_ZIP_ALIASES) else 0,
               "phone_raw": raw_phone[:40] if raw_phone and not phone else ""}
    return {
        "contact_key": key, "reason": reason,
        "full_name": full[:160], "first_name": first[:80], "last_name": last[:80],
        "email": email, "phone": phone, "phone_e164": e164,
        "telegram": pick(*_TELEGRAM_ALIASES).lstrip("@")[:80],
        "city": city[:80], "state": state, "zip": pick(*_ZIP_ALIASES)[:12],
        "country": pick(*_COUNTRY_ALIASES)[:40].upper(),
        "source_url": pick(*_SOURCE_URL_ALIASES)[:500],
        "source_record_id": pick(*_RECORD_ID_ALIASES)[:120],
        "collected_at": pick(*_COLLECTED_ALIASES)[:40],
        "has_contact": 1 if (email or phone) else 0,
        "quality": quality,
        "source_fields": json.dumps({str(k): ("" if v is None else str(v)) for k, v in (raw or {}).items()},
                                    ensure_ascii=False)[:4000],
    }


def batch_id() -> str:
    return "b" + hashlib.sha1(now().encode() + str(id(object())).encode()).hexdigest()[:12]


def _different_person(a: str, b: str) -> bool:
    """Both rows name someone, and the names are not the same person's.

    Conservative on purpose: an empty name is never "different", so a nameless record still dedupes
    against the channel. Only two distinct, non-trivial names count as a collision.
    """
    ka, kb = _key_slug(a or ""), _key_slug(b or "")
    return bool(ka) and bool(kb) and ka != kb


def _shared_channel_key(con, base: str, full_name: str) -> str:
    """A key that keeps both people, while staying stable for re-imports of the same pair."""
    stem = f"{base}#{_key_slug(full_name)[:48]}" if full_name else base
    key, n = stem, 1
    while True:
        row = con.execute("SELECT full_name FROM imported_contacts WHERE contact_key=?",
                          (key,)).fetchone()
        # free, or already this same person's own row -> that is the key to reuse (a re-import of
        # the same pair then correctly lands as a DUPLICATE of that row, not a third one)
        if row is None or not _different_person(row["full_name"], full_name):
            return key
        n += 1
        key = f"{stem}~{n}"


# ------------------------------------------------------------------ import
def _lead_for_contact(con, rec: dict) -> sqlite3.Row | None:
    """Does a REAL consented lead already hold this same contact channel?

    Used only to report the overlap ("we already have this person's own submission") — an import
    never touches that lead, and never inherits its consent.
    """
    if rec.get("email"):
        r = con.execute("SELECT lead_id, qual_status FROM leads WHERE lower(email)=? LIMIT 1",
                        (rec["email"],)).fetchone()
        if r:
            return r
    for col, val in (("phone_e164", rec.get("phone_e164")), ("phone", rec.get("phone"))):
        if not val:
            continue
        try:
            r = con.execute(f"SELECT lead_id, qual_status FROM leads WHERE {col}=? LIMIT 1", (val,)).fetchone()
        except sqlite3.OperationalError:
            continue
        if r:
            return r
    return None


def import_rows(con, source: dict, rows, *, batch: str = "", record_type: str = "UNKNOWN",
                is_test: int = 0) -> dict:
    """Admit third-party records. Idempotent, provenance-preserving, and NEVER consent-bearing.

    source: {id, name?, url?, license?, license_note?, data_status?} — `id` is required: a record
    with no source cannot be audited later, so it is refused rather than stored anonymously.
    `data_status` is the source's authenticity rating (VERIFIED_REAL / UNVERIFIED / SYNTHETIC), kept
    deliberately separate from permission so "is it real" and "may we use it" never get merged.
    Returns counts plus the batch id, and writes an import_batches row.
    """
    sid = (source or {}).get("id") or ""
    if not sid:
        raise ValueError("import_rows needs source['id'] - a record with no provenance is not admissible")
    rtype = (record_type or "UNKNOWN").upper()
    if rtype not in RECORD_TYPES:
        raise ValueError(f"record_type must be one of {RECORD_TYPES}")
    # What we know about authenticity travels with the source, not with our hopes: an authoritative
    # registry ships VERIFIED_REAL, a vendor export ships UNVERIFIED until an operator attests,
    # generated data ships SYNTHETIC and can never be promoted.
    dstatus = (source or {}).get("data_status") or "UNVERIFIED"
    if dstatus not in DATA_STATUSES:
        raise ValueError(f"data_status must be one of {DATA_STATUSES}")
    batch = batch or batch_id()
    ts = now()
    stats = {"batch": batch, "received": 0, "imported": 0, "duplicates": 0, "rejected": 0,
             "missing_contact": 0, "already_a_lead": 0, "reasons": {}, "warnings": []}
    if not (source or {}).get("license"):
        stats["warnings"].append("no licence/terms basis recorded for this source - every record "
                                 "stays NOT_ELIGIBLE (possession only)")

    for raw in rows or []:
        stats["received"] += 1
        rec = normalize(raw if isinstance(raw, dict) else {})
        if not rec["contact_key"]:
            stats["rejected"] += 1
            stats["reasons"][rec["reason"]] = stats["reasons"].get(rec["reason"], 0) + 1
            continue
        prev = con.execute("SELECT id, full_name FROM imported_contacts WHERE contact_key=?",
                           (rec["contact_key"],)).fetchone()
        if prev and rec["contact_key"].startswith("p:") and _different_person(prev["full_name"],
                                                                           rec["full_name"]):
            # A shared PHONE is not the same person: a clinic switchboard, a household landline.
            # Two clearly different names on one number stay as two records — merging them would
            # silently delete a real human being. An email is not treated this way: the same inbox
            # with "Jane Doe" / "J. Doe" is one person, not two.
            rec["contact_key"] = _shared_channel_key(con, rec["contact_key"], rec["full_name"])
            rec["quality"]["shared_channel"] = 1
            prev = con.execute("SELECT id, full_name FROM imported_contacts WHERE contact_key=?",
                               (rec["contact_key"],)).fetchone()
        if prev:
            # A repeat of a record we already hold: keep the attempt auditable, change NOTHING on
            # the original (its consent evidence, eligibility and provenance are frozen).
            stats["duplicates"] += 1
            _insert(con, {
                "contact_key": None, "source_id": sid, "source_name": source.get("name", "")[:120],
                "source_url": rec["source_url"], "source_record_id": rec["source_record_id"],
                "collected_at": rec["collected_at"], "imported_at": ts, "import_batch": batch,
                "license": source.get("license", "")[:200],
                "license_note": source.get("license_note", "")[:500], "record_type": rtype,
                "data_status": dstatus, "quality": json.dumps(rec["quality"]),
                "full_name": rec["full_name"], "first_name": rec["first_name"],
                "last_name": rec["last_name"], "email": rec["email"], "phone": rec["phone"],
                "phone_e164": rec["phone_e164"], "telegram": rec["telegram"], "city": rec["city"],
                "state": rec["state"], "zip": rec["zip"], "country": rec["country"],
                "has_contact": rec["has_contact"], "source_fields": rec["source_fields"],
                "evidence": json.dumps({"duplicate_of": prev["id"], "import_batch": batch,
                                        "imported_at": ts}),
                "marketing_eligibility": "NOT_ELIGIBLE",
                "eligibility_reason": f"duplicate of imported contact #{prev['id']}; kept for "
                                      "audit, changes nothing",
                "dup_status": "DUPLICATE", "dup_of": prev["id"], "is_test": is_test,
                "created_at": ts, "updated_at": ts})
            continue
        lead = _lead_for_contact(con, rec)
        reason = WHY["NOT_ELIGIBLE"]
        if lead:
            stats["already_a_lead"] += 1
            reason = (f"this contact channel already has its own consented submission (lead "
                      f"{lead['lead_id']}, {lead['qual_status']}); the consent stays on that lead "
                      f"and was NOT inherited here")
        if not rec["has_contact"]:
            stats["missing_contact"] += 1
        _insert(con, {
            "contact_key": rec["contact_key"], "source_id": sid,
            "source_name": source.get("name", "")[:120], "source_url": rec["source_url"],
            "source_record_id": rec["source_record_id"], "collected_at": rec["collected_at"],
            "imported_at": ts, "import_batch": batch,
            "license": source.get("license", "")[:200],
            "license_note": source.get("license_note", "")[:500], "record_type": rtype,
            "data_status": dstatus, "quality": json.dumps(rec["quality"]),
            "full_name": rec["full_name"], "first_name": rec["first_name"],
            "last_name": rec["last_name"], "email": rec["email"], "phone": rec["phone"],
            "phone_e164": rec["phone_e164"], "telegram": rec["telegram"], "city": rec["city"],
            "state": rec["state"], "zip": rec["zip"], "country": rec["country"],
            "has_contact": rec["has_contact"], "source_fields": rec["source_fields"],
            "evidence": json.dumps({"source_id": sid, "source_url": rec["source_url"],
                                    "source_record_id": rec["source_record_id"],
                                    "collected_at": rec["collected_at"], "imported_at": ts,
                                    "import_batch": batch, "license": source.get("license", ""),
                                    "matched_lead": (lead["lead_id"] if lead else "")}),
            "marketing_eligibility": "NOT_ELIGIBLE", "eligibility_reason": reason,
            "dup_status": "UNIQUE", "is_test": is_test, "created_at": ts, "updated_at": ts})
        stats["imported"] += 1

    con.execute("INSERT OR REPLACE INTO import_batches (batch, ts, source_id, source_name,"
                " source_url, license, record_type, received, imported, duplicates, rejected,"
                " missing_contact, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (batch, ts, sid, source.get("name", "")[:120], source.get("url", "")[:500],
                 source.get("license", "")[:200], rtype, stats["received"], stats["imported"],
                 stats["duplicates"], stats["rejected"], stats["missing_contact"],
                 "; ".join(stats["warnings"])[:500]))
    con.commit()
    return stats


def _insert(con, vals: dict) -> None:
    """Column-mapped insert: names and values cannot drift out of order as the table grows."""
    cols = list(vals)
    con.execute(f"INSERT INTO imported_contacts ({','.join(cols)})"
                f" VALUES ({','.join('?' * len(cols))})", [vals[c] for c in cols])


def import_csv_text(con, source: dict, text: str, **kw) -> dict:
    import csv as _csv
    import io
    rows = [dict(r) for r in _csv.DictReader(io.StringIO(text or ""))]
    return import_rows(con, source, rows, **kw)


def import_file(con, source: dict, path: Path | str, **kw) -> dict:
    p = Path(path)
    if p.suffix.lower() in (".json", ".jsonl"):
        raw = p.read_text(encoding="utf-8-sig")
        data = json.loads(raw) if p.suffix.lower() == ".json" else [json.loads(x) for x in raw.splitlines() if x.strip()]
        if isinstance(data, dict):
            data = data.get("rows") or data.get("records") or data.get("data") or []
        return import_rows(con, source, data, **kw)
    return import_csv_text(con, source, p.read_text(encoding="utf-8-sig"), **kw)


# ------------------------------------------------------------------ eligibility
def _same_person(rec: dict, lead: dict) -> tuple[bool, str]:
    """Does this lead demonstrably belong to the person in this imported record?

    At least one positive match on a contact channel is required; a name alone is not proof.
    """
    rp, lp = _norm_phone(rec.get("phone_e164") or rec.get("phone") or ""), _norm_phone(lead.get("phone") or "")
    if rp[0] and lp[0] and rp[0] == lp[0]:
        return True, "phone matches the lead's own submitted number"
    re_, le = _norm_email(rec.get("email") or ""), _norm_email(lead.get("email") or "")
    if re_ and le and re_ == le:
        return True, "email matches the lead's own submitted address"
    return False, "the lead's own submitted contact does not match this record"


def promote(con, cid: int, lead_id: str, note: str = "") -> dict:
    """The ONLY way a third-party record becomes eligible: link it to the person's own submission.

    Refuses unless (a) the record is a unique original, (b) the lead exists, (c) that lead has a
    real consent row, and (d) the lead's own contact matches the record — i.e. the same person
    submitted the form themselves. Nothing here creates a lead, writes a consent, or edits a
    lead's fields; it only records the link on the imported row.
    """
    row = con.execute("SELECT * FROM imported_contacts WHERE id=?", (cid,)).fetchone()
    if not row:
        return {"ok": False, "reason": f"no imported contact #{cid}"}
    rec = dict(row)
    if rec.get("dup_status") == "DUPLICATE":
        return {"ok": False, "reason": f"#{cid} is a recorded duplicate - promote #{rec.get('dup_of')} instead"}
    if rec.get("data_status") == "SYNTHETIC":
        return {"ok": False, "reason": "this row is generated (SYNTHETIC) - it is not a person, so no "
                                        "submission can ever be theirs"}
    lead = con.execute("SELECT * FROM leads WHERE lead_id=?", (lead_id or "",)).fetchone()
    if not lead:
        return {"ok": False, "reason": f"no lead {lead_id!r} - only a person's own /check submission can be linked"}
    lead = dict(lead)
    if not con.execute("SELECT 1 FROM consents WHERE lead_id=?", (lead_id,)).fetchone():
        return {"ok": False, "reason": f"lead {lead_id} has no consent row - nothing may be labelled consented"}
    same, why = _same_person(rec, lead)
    if not same:
        return {"ok": False, "reason": why}
    if rec.get("lead_id") == lead_id and rec.get("marketing_eligibility") == "ELIGIBLE_CONSENTED":
        return {"ok": True, "reason": "already linked", "note": note[:300]}
    con.execute("UPDATE imported_contacts SET marketing_eligibility='ELIGIBLE_CONSENTED',"
                " consent_ref=?, lead_id=?, eligibility_reason=?, updated_at=? WHERE id=?",
                (lead_id, lead_id,
                 f"linked to the person's own consented submission {lead_id} ({why})"
                 + (f" - operator note: {note[:200]}" if note else ""),
                 now(), cid))
    con.commit()
    return {"ok": True, "reason": f"linked to consented lead {lead_id}", "note": note[:300]}


def record_use(con, cid: int, uses: str, note: str = "") -> dict:
    """Record what a source's licence/terms actually permit for THIS record.

    Deliberately cannot produce ELIGIBLE_CONSENTED: a permitted non-marketing use is not
    marketing permission, so the record stays in USE_LIMITED and stays out of routing.
    """
    uses = (uses or "").strip()[:300]
    if len(uses) < 3:
        return {"ok": False, "reason": "say which use the licence permits (3+ characters)"}
    cur = con.execute("UPDATE imported_contacts SET marketing_eligibility='USE_LIMITED',"
                      " permitted_uses=?, eligibility_reason=?, updated_at=?"
                      " WHERE id=? AND marketing_eligibility != 'ELIGIBLE_CONSENTED'",
                      (uses, "permitted use recorded, marketing NOT permitted: " + uses
                       + (f" - {note[:200]}" if note else ""), now(), cid))
    con.commit()
    if cur.rowcount:
        return {"ok": True, "reason": "USE_LIMITED recorded"}
    return {"ok": False, "reason": f"#{cid} not found, or already linked to a consented submission"}


def attest(con, cid: int, status: str, note: str = "") -> dict:
    """Operator judgement about whether the record is a real person — not about permission.

    Attesting VERIFIED_REAL says "I checked this record is real"; it does NOT make it marketable.
    An attestation needs a note naming what was checked, and it can never launder generated data.
    """
    status = (status or "").strip().upper()
    if status not in DATA_STATUSES:
        return {"ok": False, "reason": f"status must be one of {DATA_STATUSES}"}
    if len((note or "").strip()) < 10:
        return {"ok": False, "reason": "an attestation needs a 10+ character note about what you checked"}
    if status == "VERIFIED_REAL":
        r = con.execute("SELECT data_status FROM imported_contacts WHERE id=?", (cid,)).fetchone()
        if not r:
            return {"ok": False, "reason": f"no imported contact #{cid}"}
        if (r[0] or "") == "SYNTHETIC":
            return {"ok": False, "reason": "#" f"{cid} arrives from a source that generates records; "
                                            "a note cannot turn generated data into a person"}
    cur = con.execute("UPDATE imported_contacts SET data_status=?, verified_at=?, verified_by=?,"
                      " updated_at=? WHERE id=?",
                      (status, now(), (note or "")[:300], now(), cid))
    con.commit()
    return {"ok": bool(cur.rowcount), "reason": f"data status recorded as {status}"}


def ineligible_reason(rec: dict) -> str:
    """Why this row may not be routed or sold, in one line an operator can act on.

    Two different questions, answered separately: is it a real person (data_status), and are we
    allowed to market to them (marketing_eligibility)."""
    if rec.get("marketing_eligibility") == "ELIGIBLE_CONSENTED" and rec.get("lead_id"):
        return "eligible: linked to the person's own consented submission"
    if rec.get("dup_status") == "DUPLICATE":
        return f"duplicate of #{rec.get('dup_of')} - inert"
    if rec.get("data_status") == "SYNTHETIC":
        return "generated record (SYNTHETIC) - not a person; usable only as test data"
    if not rec.get("has_contact"):
        return "no phone or email in the source data - nothing to route even with consent"
    if rec.get("marketing_eligibility") == "USE_LIMITED":
        return f"use-limited ({rec.get('permitted_uses') or 'unspecified'}) - not marketing permission"
    if rec.get("data_status") == "UNVERIFIED":
        return ("unverified record with no permission basis: confirm it is a real person, then get "
                "their own submission or a documented permission basis")
    return "no consent, no licence basis: possession only (not routable, not sellable)"


# ------------------------------------------------------------------ reading
def list_rows(con, *, eligibility: str = "", source: str = "", record_type: str = "",
              data_status: str = "", dup: str = "UNIQUE", contact: str = "", q: str = "",
              limit: int = 300) -> list[dict]:
    where, args = ["1=1"], []
    if eligibility:
        where.append("marketing_eligibility=?"); args.append(eligibility)
    if data_status:
        where.append("data_status=?"); args.append(data_status)
    if source:
        where.append("source_id=?"); args.append(source)
    if record_type:
        where.append("record_type=?"); args.append(record_type)
    if dup:
        where.append("dup_status=?"); args.append(dup)
    if contact == "HAS_CONTACT":
        where.append("has_contact=1")
    elif contact == "NO_CONTACT":
        where.append("has_contact=0")
    if q:
        where.append("(full_name LIKE ? OR email LIKE ? OR phone LIKE ? OR phone_e164 LIKE ?)")
        args += [f"%{q}%"] * 4
    rows = con.execute("SELECT * FROM imported_contacts WHERE " + " AND ".join(where)
                       + " ORDER BY id DESC LIMIT ?", (*args, max(1, min(int(limit), 1000)))).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["why"] = ineligible_reason(d)
        for src, dst in (("evidence", "evidence_data"), ("quality", "quality_data")):
            try:
                d[dst] = json.loads(d.get(src) or "{}")
            except (TypeError, ValueError):
                d[dst] = {}
        out.append(d)
    return out


def counts(con) -> dict:
    def one(sql, *a):
        try:
            return con.execute(sql, a).fetchone()[0] or 0
        except sqlite3.OperationalError:
            return 0
    by_elig = {r[0] or "NOT_ELIGIBLE": r[1] for r in
               con.execute("SELECT marketing_eligibility, COUNT(*) FROM imported_contacts"
                           " WHERE dup_status='UNIQUE' GROUP BY marketing_eligibility")}
    by_type = {r[0] or "UNKNOWN": r[1] for r in
               con.execute("SELECT record_type, COUNT(*) FROM imported_contacts"
                           " WHERE dup_status='UNIQUE' GROUP BY record_type")}
    by_data = {r[0] or "UNVERIFIED": r[1] for r in
               con.execute("SELECT data_status, COUNT(*) FROM imported_contacts"
                           " WHERE dup_status='UNIQUE' GROUP BY data_status")}
    return {
        "total": one("SELECT COUNT(*) FROM imported_contacts"),
        "unique": one("SELECT COUNT(*) FROM imported_contacts WHERE dup_status='UNIQUE'"),
        "duplicates": one("SELECT COUNT(*) FROM imported_contacts WHERE dup_status='DUPLICATE'"),
        "has_contact": one("SELECT COUNT(*) FROM imported_contacts WHERE dup_status='UNIQUE' AND has_contact=1"),
        "with_phone": one("SELECT COUNT(*) FROM imported_contacts WHERE dup_status='UNIQUE' AND phone_e164 != ''"),
        "with_email": one("SELECT COUNT(*) FROM imported_contacts WHERE dup_status='UNIQUE' AND email != ''"),
        "eligible": one("SELECT COUNT(*) FROM imported_contacts WHERE marketing_eligibility='ELIGIBLE_CONSENTED'"),
        "by_eligibility": by_elig,
        "by_type": by_type,
        "by_data_status": by_data,
        "consumer_contacts": one("SELECT COUNT(*) FROM imported_contacts WHERE dup_status='UNIQUE'"
                                 " AND record_type='CONSUMER' AND has_contact=1"),
        # a phone the source DID publish but we cannot use ("phone_raw" non-empty). A valid number
        # leaves it empty, so an empty match must not be counted as invalid.
        "invalid_phones": one("SELECT COUNT(*) FROM imported_contacts WHERE dup_status='UNIQUE'"
                              " AND quality LIKE '%\"phone_raw\": \"%'"
                              " AND quality NOT LIKE '%\"phone_raw\": \"\"%'"),
        "sources": [dict(r) for r in con.execute(
            "SELECT source_id, source_name, license, COUNT(*) total, SUM(has_contact) with_contact,"
            " MIN(imported_at) first_seen, MAX(imported_at) last_seen"
            " FROM imported_contacts WHERE dup_status='UNIQUE' GROUP BY source_id ORDER BY total DESC")],
        "batches": [dict(r) for r in con.execute(
            "SELECT * FROM import_batches ORDER BY id DESC LIMIT 12")],
    }


def report(con) -> dict:
    """The audit an operator (or a buyer's compliance question) needs, from recorded state."""
    c = counts(con)
    c["sellable"] = con.execute(
        "SELECT COUNT(*) FROM imported_contacts WHERE marketing_eligibility='ELIGIBLE_CONSENTED'"
        " AND lead_id != '' AND dup_status='UNIQUE'").fetchone()[0]
    # A live proof that no lead row ever came from an import: a lead's phone must be
    # CONSUMER_SUBMITTED, and the db trigger refuses anything else.
    c["lead_rows_not_consumer_submitted"] = con.execute(
        "SELECT COUNT(*) FROM leads WHERE COALESCE(phone,'')!=''"
        " AND COALESCE(phone_source,'')!='CONSUMER_SUBMITTED'").fetchone()[0]
    c["routable_imported"] = con.execute(
        "SELECT COUNT(*) FROM imported_contacts ic JOIN leads l ON l.lead_id = ic.lead_id"
        " WHERE ic.marketing_eligibility != 'ELIGIBLE_CONSENTED'").fetchone()[0]
    return c
