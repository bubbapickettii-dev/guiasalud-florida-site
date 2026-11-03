"""ORG CONTACT DISCOVERY — public ORGANIZATION contact records for partnership outreach.

Purpose boundary (the counterpart to engine/prospect_contacts.py):
  * prospect_contacts.py  = PERSON-shaped discovery (public posts). A public post is never
    consent; a row without invites-to-contact stays CONTACT_NOT_AVAILABLE and is never a lead.
  * THIS module  = ORGANIZATION-shaped discovery: businesses, clinics, nonprofits and other
    organizations that VOLUNTARILY publish contact details for inquiries (official public
    registries, public directories, org contact pages). These are business contacts for the
    operator's ONE-WAY outreach review pipeline — never insurance leads, never "demand
    signals", and nothing here ever stores a consumer's own data.

Classification ladder (distinct record kinds, per the separation requirement):
  PUBLIC_CONTACT_DISCOVERED      a contactable org record was extracted (with evidence)
  PUBLIC_CONTACT_VERIFIED        contact re-checked against its source (or source IS the
                                 official registry of record)
  PARTNER_CANDIDATE              operator reviewed it as a good outreach candidate
  (Consumer-side stages stay entirely in the existing funnel: tool_start -> ... -> consented
   phone lead CLASSIFIED as PHONE_LEAD with consent_version -- see app.py / engine.funnel.
   Nothing in this module can create, promote, or export a consumer lead.)

Sources (first implementset, all verified robots/terms-checked live Oct 2026):
  1. NPI Registry API (official CMS/NPPES, https://npiregistry.cms.hhs.gov/api/?version=2.1):
     no key, org entities (entity_type=2) with public telephone numbers; the API endpoint is
     the official machine-readable door — robots.txt of the UI host does not disallow /api
     (checked live; a robots-fetch failure never hard-stops the official API — recorded).
  2. Voluntary org directory/contact pages (config > org_contacts.seeds): pages robots.TXT
     ALLOWS for wildcards, orgs published there expressly for inquiries (e.g., diocese
     office listings, food-bank networks). Fetcher honors Crawl-delay, stores the source's
     own robots terms (ai-train=no is honored: content kept as literal reference rows only),
     and extracts only mailto:/tel: links (explicit publish-for-contact artifacts) plus
     labels adjacent to them. No phone-matching heuristics over free prose.

Per-source performance (source_metrics): records_fetched, records_accepted, contacts_extracted,
contacts_verified, duplicates_removed, errors, restrictions — the ranking signal is verified
records per fetched record, NOT pages scraped.

Nothing in this file contacts anyone, spends money, or bypasses any access control.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "org_contacts.db"

UA = "GSAlludePublicData/1.0 (+research operator; contact: admin only via site forms)"
HTTP_TIMEOUT_S = 30
REQUEST_PAUSE_S = 2.0
NPI_API = "https://npiregistry.cms.hhs.gov/api/"
NPI_LIMIT = 50

# record kinds — the classification ladder (see module docstring)
KIND_DISCOVERED = "PUBLIC_CONTACT_DISCOVERED"
KIND_VERIFIED = "PUBLIC_CONTACT_VERIFIED"
KIND_PARTNER = "PARTNER_CANDIDATE"
KIND_PARTNER_CANDIDATE = KIND_PARTNER

SCHEMA = """
CREATE TABLE IF NOT EXISTS org_contacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  org_key TEXT UNIQUE,             -- stable hash: normalized name + city + first contact
  org_name TEXT, org_type TEXT,    -- clinic | nonprofit | food_bank | staffing | education | other
  source TEXT,                     -- npi_registry | seed:<host> ...
  source_url TEXT,                 -- the page/API call the record came from (evidence anchor)
  record_ids TEXT,                 -- JSON: {"npi": "..."} etc. — official record identifiers
  emails TEXT,                     -- JSON: [{value, domain_kind, evidence}]
  phones TEXT,                     -- JSON: [{value, e164, evidence}]
  address TEXT, lang TEXT,
  purpose_note TEXT,               -- what the source says the contact is FOR (e.g. inquiries)
  published_at TEXT, collected_at TEXT,
  terms_note TEXT,                 -- the source's own robots/terms restrictions actually observed
  verify_status TEXT DEFAULT 'DISCOVERED_UNVERIFIED',  -- | VERIFIED_PRIMARY | VERIFIED_RECHECK
  verification_note TEXT,
  record_kind TEXT DEFAULT 'PUBLIC_CONTACT_DISCOVERED',
  dup_status TEXT DEFAULT 'UNIQUE', dup_of TEXT DEFAULT '',
  stage TEXT DEFAULT 'DISCOVERED', next_action TEXT,
  created_at TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_org_kind ON org_contacts(record_kind, source);
CREATE TABLE IF NOT EXISTS source_metrics (   -- one summary row per (source, run)
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, source TEXT,
  records_fetched INTEGER DEFAULT 0, records_accepted INTEGER DEFAULT 0,
  contacts_extracted INTEGER DEFAULT 0, contacts_verified INTEGER DEFAULT 0,
  duplicates_removed INTEGER DEFAULT 0, errors INTEGER DEFAULT 0,
  restrictions TEXT DEFAULT '', note TEXT DEFAULT ''
);
"""

NEXT_ACTION_BY_KIND = {
    KIND_DISCOVERED: "operator: open source_url and confirm the listing invites inquiries",
    KIND_VERIFIED: "operator: review as outreach candidate; nothing sends without approval",
    KIND_PARTNER_CANDIDATE: "operator: prepare outreach pack row; send only with explicit approval",
}


def connect(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.commit()
    return con


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm_name(n: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (n or "").lower()).split("&", 1)[0].strip()[:80]


def norm_phone(raw: str) -> str | None:
    """E.164-normalize a US public org phone; None when not a plausible 10-digit number."""
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) != 10 or d.startswith(("0", "1")):
        return None
    if len(set(d)) < 4:            # obviously fake blocks (1111111111, 9999999999)
        return None
    return "+1" + d


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
_SYSTEM_LOCALS = ("noreply", "no-reply", "donotreply", "do-not-reply", "postmaster",
                  "webmaster", "abuse", "notifications", "bounce", "autoresponder")
_DOMAIN_KIND = {"gov": "government_org", "edu": "education_org", "mil": "government_org"}


def _domain_kind(email: str) -> str:
    tld = email.rsplit(".", 1)[-1].lower()
    if tld in _DOMAIN_KIND:
        return _DOMAIN_KIND[tld]
    return "business_org"          # published-for-inquiries business/org mailboxes


# ----------------------------------------------------------------------- robots
def get_robots_terms(host_url: str, get_text=None) -> dict:
    """Fetch (and remember) the source's own robots.txt. Returns {allowed_multi, crawl_delay,
    extra:. Terms only — we don't reimplement a full parser; we honor the compound terms we
    can read and SKIP anything requiring deeper parsing."""
    parsed = urllib.parse.urlsplit(host_url)
    robots = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    terms = {"robots_url": robots, "allowed": True, "crawl_delay": 0.0, "flags": "",
             "note": "", "read_failed": False}
    try:
        text = (get_text or http_text)(robots)
        agent_star = False
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            key, _, val = line.partition(":")
            key, val = key.strip().lower(), val.strip()
            if key == "user-agent":
                agent_star = val in ("*",)
                continue
            if not agent_star:
                continue
            if key == "disallow":
                if val == "/":                       # full wildcard ban on this host
                    terms["allowed"] = False
            elif key == "crawl-delay":
                terms["crawl_delay"] = min(30.0, float(val or 0))
            elif key in ("ai-train", "use", "search"):
                terms["flags"] += f"{key}={val};"
    except Exception as e:                                    # noqa: BLE001
        terms["read_failed"] = True
        terms["note"] = f"robots read error: {type(e).__name__}"[:120]
    return terms


def http_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
        return resp.read().decode("utf-8", "replace")


def _http_json(url: str):
    return json.loads(http_text(url))


# --------------------------------------------------------------------- NPI adapter
# Taxonomy descriptions that produce ORGANIZATIONS useful as outreach candidates
# (community clinics, FQHCs, behavioral health, care coordination — partner-shaped orgs).
NPI_TAXONOMIES = (
    "Federally Qualified Health Center (FQHC)",
    "Community Health",
    "Freestanding Clinic",
    "Adult Day Care Program",
    "Public Health",
    "Behavioral Health & Social Service Organizations",
)


def fetch_npi_organizations(con, get_json=None, sleeper=time.sleep, state: str = "FL",
                            taxonomies=NPI_TAXONOMIES, fetch_all=False) -> dict:
    """Official CMS/NPPES registry: org entities for one state across partner-shaped taxonomies.
    One polite batch per taxonomy (NPI_LIMIT orgs), skip param NOT advanced across runs —
    dedupe makes repeat scans idempotent; the source's own normalization handles it."""
    get_json = get_json or _http_json
    fetched = accepted = extracted = verified = dupes = errors = 0
    restrictions = []
    robots = get_robots_terms(NPI_API)
    if robots["read_failed"]:
        restrictions.append(f"{robots['note']} (official API endpoint used; recorded, not hard-stopped)")
    for tax in taxonomies:
        q = urllib.parse.urlencode({"version": "2.1", "entity_type": "2", "state": state,
                                    "taxonomy_description": tax, "limit": NPI_LIMIT})
        url = f"{NPI_API}?{q}"
        try:
            payload = get_json(url)
        except Exception as e:                               # noqa: BLE001
            errors += 1
            _metric(con, "npi_registry", fetched, accepted, extracted, verified, dupes, errors,
                    "; ".join(restrictions), f"taxonomy '{tax}': {type(e).__name__}")
            continue
        rows = []
        for r in (payload.get("results") or []):
            fetched += 1
            basic = r.get("basic") or {}
            name = (basic.get("organization_name") or basic.get("org_name") or "").title()
            if not name:
                continue
            # phone selection: the LOCATION (practice) telephone wins over MAILING; NPPES
            # records list both — we take the org's public practice line when present.
            addrs = r.get("addresses") or []
            order = sorted(range(len(addrs)),
                           key=lambda i: 0 if (addrs[i].get("address_purpose") or "").upper() == "LOCATION" else 1)
            phone = None
            for i in order:
                tel = (addrs[i].get("telephone_number") or "").strip()
                if tel:
                    e164 = norm_phone(tel)
                    if e164:
                        phone = {"value": tel, "e164": e164,
                                 "evidence": f"NPPES {addrs[i].get('address_purpose','') or 'LOC'} address telephone_number"}
                        break
            city = ""
            addr_bits = []
            for a in (r.get("addresses") or []):
                city = city or (a.get("city") or "")
                addr_bits = [a.get("city") or "", a.get("state") or "", a.get("postal_code") or ""]
            taxo = (r.get("taxonomies") or [{}])[0].get("desc") or tax
            rec = {
                "org_name": name, "org_type": _org_type_from_taxonomy(taxo),
                "source": "npi_registry", "source_url": url,
                "record_ids": json.dumps({"npi": str(r.get("number") or "")}),
                "phones": json.dumps(phone and [phone] or []),
                "emails": "[]",
                "address": ", ".join(x for x in addr_bits if x),
                "record_kind": KIND_VERIFIED,           # source IS the official registry of record
                "terms_note": "Official CMS public registry API, no key; used for org outreach review",
                "purpose_note": "organization public telephone published in the registry",
            }
            rows.append(rec)
        tax_accepted = _upsert_orgs(con, rows, source="npi_registry", fetch_evidence=True)
        extracted += len(rows)
        verified += tax_accepted
        dupes += max(0, len(rows) - tax_accepted)
        _metric(con, "npi_registry", fetched, accepted + tax_accepted, len(rows), tax_accepted,
                max(0, len(rows) - tax_accepted), errors, "; ".join(restrictions), f"{tax}: ok")
        accepted += tax_accepted
        sleeper(REQUEST_PAUSE_S)
    con.commit()
    return {"fetched": fetched, "accepted": accepted, "extracted": extracted,
            "verified": verified, "duplicates": dupes, "errors": errors,
            "restrictions": "; ".join(restrictions)}


HRSA_SITES_URL = ("https://data.hrsa.gov/DataDownload/DD_Files/"
                  "Health_Center_Service_Delivery_and_LookAlike_Sites.csv")


def fetch_hrsa_sites(con, fetch_text=None, state: str = "FL", limit: int = 0,
                     include_lookalikes: bool = False) -> dict:
    """Official HRSA health-center site file (data.hrsa.gov bulk CSV, free, no key).

    WHY THIS SOURCE: it is a national registry of Federally Qualified Health Centers and
    look-alike sites — the organizations that uninsured consumers actually walk into for
    coverage help. Every row carries the SITE's own published telephone, address, status and
    official identifiers (BPHC number, NPI), so a record is verified against the registry of
    record at collection time. These are ORGANIZATION contacts for partnership outreach:
    they are never consumer leads and never mixed into the consumer prospect store.

    Filtering: Active sites only (a closed site is not a partner), one state per call, a row
    without a usable 10-digit published phone is skipped rather than stored contactless.
    """
    fetch_text = fetch_text or http_text
    robots = get_robots_terms(HRSA_SITES_URL, get_text=fetch_text)
    restrictions = ((robots.get("note") or "") + ";" + (robots.get("flags") or "")).strip(";")
    if robots.get("allowed") is False:
        _metric(con, "hrsa_health_centers", 0, 0, 0, 0, 0, 1,
                "robots Disallow recorded", "skipped: robots.txt disallows the bulk file")
        return {"fetched": 0, "accepted": 0, "restricted": True,
                "restrictions": restrictions or "robots disallow"}
    raw = fetch_text(HRSA_SITES_URL)
    reader = csv.DictReader(io.StringIO(raw))
    fetched = accepted = extracted = verified = errors = 0
    rows: list[dict] = []
    seen: set[str] = set()
    for r in reader:
        fetched += 1
        if (r.get("Site Status Description") or "").strip().lower() != "active":
            continue
        if state and (r.get("Site State Abbreviation") or "").strip().upper() != state.upper():
            continue
        hc_type = (r.get("Health Center Type") or "")
        if not include_lookalikes and "look-alike" in hc_type.lower():
            continue
        name = (r.get("Site Name") or r.get("Health Center Name") or "").strip()
        phone_raw = (r.get("Site Telephone Number") or "").strip()
        e164 = norm_phone(phone_raw) if phone_raw else None
        if not name or not e164:
            continue
        city = (r.get("Site City") or "").strip()
        address = ", ".join(x for x in (
            (r.get("Site Address") or "").strip(), city,
            (r.get("Site State Abbreviation") or "").strip(),
            (r.get("Site Postal Code") or "").strip()) if x)
        dkey = f"{_norm_name(name)}|{e164}"
        if dkey in seen:                      # same site listed twice in the file
            continue
        seen.add(dkey)
        rec = {
            "org_name": name, "org_type": "clinic",
            "source": "hrsa_health_centers", "source_url": HRSA_SITES_URL,
            "record_ids": json.dumps({
                "bphc_assigned_number": (r.get("BPHC Assigned Number") or "").strip(),
                "health_center_number": (r.get("Health Center Number") or "").strip(),
                "npi": (r.get("FQHC Site NPI Number") or "").strip(),
                "site_status": (r.get("Site Status Description") or "").strip(),
            }),
            "phones": json.dumps([{
                "value": phone_raw, "e164": e164,
                "evidence": "HRSA site file column 'Site Telephone Number' (public registry row)"}]),
            "emails": "[]",
            "address": address,
            "purpose_note": ("health-center site telephone published by HRSA in the official "
                             "public site file; used for partnership outreach review only"),
            "published_at": (r.get("Site Added to Scope this Date") or "").strip(),
            "terms_note": ("Official HRSA data.hrsa.gov bulk file, free public download, no key; "
                           "organization contact only, never a consumer record"),
            "record_kind": KIND_VERIFIED,     # source IS the registry of record
        }
        rows.append(rec)
        extracted += 1
        if limit and len(rows) >= limit:
            break
    try:
        accepted = _upsert_orgs(con, rows, source="hrsa_health_centers", fetch_evidence=True)
        verified = accepted
    except Exception as e:                               # noqa: BLE001 - store failure is reported
        errors += 1
        _metric(con, "hrsa_health_centers", fetched, 0, extracted, 0, 0, errors,
                restrictions, f"upsert failed: {type(e).__name__}: {e}"[:150])
        raise
    dupes = max(0, extracted - accepted)
    _metric(con, "hrsa_health_centers", fetched, accepted, extracted, verified, dupes, errors,
            restrictions, f"state={state} active_sites only")
    con.commit()
    return {"fetched": fetched, "accepted": accepted, "extracted": extracted,
            "verified": verified, "duplicates": dupes, "errors": errors,
            "restrictions": restrictions}


def _org_type_from_taxonomy(tax: str) -> str:
    t = (tax or "").lower()
    if "federally qualified" in t or "fqhc" in t or t == "community health":
        return "clinic"
    if "behavioral" in t or "social service" in t or "day care" in t:
        return "nonprofit"
    if "clinic" in t:
        return "clinic"
    if "public health" in t:
        return "publichealth"
    return "other"


# --------------------------------------------------- voluntary directory adapter
def fetch_org_seed(con, seed: dict, fetch_text=None, sleeper=time.sleep) -> dict:
    """One robots-checked page that VOLUNTARILY publishes org contact info. Extracts ONLY
    explicit publish-for-contact artifacts (mailto:/tel: links and labels immediately next
    to them). Keeps the source's own robots terms in terms_note; ai-train=no hosts are
    still fine for literal reference extraction (not AI training)."""
    url = seed["url"]
    host = urllib.parse.urlsplit(url).netloc
    robots = get_robots_terms(url, get_text=fetch_text)
    restrictions = (robots["note"] + ";" + robots["flags"]) if (robots["note"] or robots["flags"]) else ""
    if not robots["allowed"]:
        _metric(con, f"seed:{host}", 0, 0, 0, 0, 0, 1, f"robots Disallow recorded",
                f"skipped: robots.txt disallows")
        return {"fetched": 0, "accepted": 0, "restricted": True}
    got_text = (fetch_text or http_text)(url)
    rows = []
    for org in _extract_org_listing(got_text):
        rows.append({
            "org_name": org["name"][:160], "org_type": seed.get("org_type", "community_org"),
            "source": f"seed:{host}", "source_url": url,
            "record_ids": "[]",
            "emails": json.dumps(org["emails"]),
            "phones": json.dumps(org["phones"]),
            "address": org.get("address", ""), "lang": seed.get("lang", "en"),
            "record_kind": KIND_DISCOVERED,            # needs a recheck before VERIFIED
            "terms_note": (f"{robots['robots_url']} allowed; crawl-delay={robots['crawl_delay']}; "
                           f"{robots['flags']}" if robots["flags"] else "robots allowed"),
            "purpose_note": seed.get("purpose", "org contacts published for public inquiries"),
        })
    fetched, extracted = len(rows) or 0, len(rows)
    accepted = _upsert_orgs(con, rows, source=f"seed:{host}")
    _metric(con, f"seed:{host}", fetched, accepted, extracted, accepted,
            fetched - accepted, 0, restrictions, f"fetched ok; blocks extracted")
    sleeper(robots["crawl_delay"] or REQUEST_PAUSE_S)
    return {"fetched": fetched, "accepted": accepted, "restrictions": restrictions}


def _strip_html(raw: str) -> str:
    """HTML → readable text WITHOUT collapsing newlines (tags become line breaks)."""
    txt = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1\s*>", "\n", raw)
    txt = re.sub(r"(?is)<[^>]+>", "\n", txt)
    for ent, rep in (("&amp;", "&"), ("&nbsp;", " "), ("&#8217;", "'"), ("&rsquo;", "'"),
                     ("&#8221;", '"'), ("&quot;", '"'), ("&#8211;", "-"), ("&ndash;", "-"),
                     ("&mdash;", "-"), ("&gt;", ">"), ("&lt;", "<"), ("&#8216;", "'"),
                     ("&#8220;", '"'), ("&middot;", "-"), ("&#039;", "'")):
        txt = txt.replace(ent, rep)
    txt = re.sub(r"[ \t]+", " ", txt)
    return txt


def _valid_email(email: str) -> bool:
    """Plausibility gate: at least two labels and an ALPHABETIC TLD — drops version-string
    false matches like 'aos@2.3.1' loud from CDN script URLs."""
    addr = email.split("@", 1)
    if len(addr) != 2:
        return False
    domain = addr[1].rstrip(".")
    labels = domain.split(".")
    if len(labels) < 2:
        return False
    tld = labels[-1]
    return len(tld) >= 2 and tld.isalpha()


_MAILTO_RE = re.compile(r"mailto:([^\"'\?>\s]+)", re.I)


def _extract_org_listing(text: str) -> list[dict]:
    """Rows from a contact PAGE (raw HTML): org name nearest above or inline, contact artifacts
    on the same line or near it, PLUS explicit mailto:/tel: hrefs anywhere on the page (the
    strongest publish-for-contact evidence).
    Only org entries — pages listing named INDIVIDUALS with personal phones stay out by
    org_name heuristics (Inc/LLC/Church/Ministry/Center/County/School/Program markers or seeds' scope)."""
    href_emails, href_phones = [], []                 # explicit publish-for-contact links
    seen_values: set[str] = set()
    for m in _MAILTO_RE.finditer(text):
        e = urllib.parse.unquote(m.group(1)).strip().lower()
        if _valid_email(e) and not _is_system_mailbox(e):
            href_emails.append({"value": e, "domain_kind": _domain_kind(e),
                                "evidence": f"mailto: link: {text[max(0, m.start() - 50):m.end() + 30][:120]}"})
            seen_values.add(e)
    for m in re.finditer(r"tel:[+\d\-()\s]{7,20}", text, re.I):
        e164 = norm_phone(m.group(0)[4:].strip())
        if e164 and e164 not in [p["e164"] for p in href_phones]:
            href_phones.append({"value": m.group(0)[4:].strip(), "e164": e164,
                                "evidence": f"tel: link: {text[max(0, m.start() - 50):m.end() + 30][:120]}"})
    lines = [l.strip() for l in _strip_html(text).splitlines() if l.strip()]
    brand = _page_brand(text)
    # Directory pages that LABEL their fields ("Phone:", "Email:", "Address:") instead of
    # using org-shaped headings: each contact label opens a block and the org name is the
    # most recent short, label-free line above it (city / office / department names).
    blocks = _labeled_blocks(lines, brand)
    if blocks:
        return _attach_href(blocks, href_emails, href_phones, lines, seen_values, brand)
    groups = _groups_by_header(lines)
    if groups:
        return _attach_href(groups, href_emails, href_phones, lines, seen_values, brand)
    # flat pages: contact artifacts with NO heading structure at all. Each contact-carrying
    # line names itself — the text printed on that same line before the first artifact is
    # the only defensible owner (e.g. "Hope Center Inc (305) 555-0100").
    pool = _pool_by_line(lines)
    return _attach_href(pool, href_emails, href_phones, lines, seen_values, brand)


def _page_brand(text: str) -> str:
    """Site's own name token, taken from <title> / og:site_name (never invented)."""
    og = re.search(r"property=[\"']og:site_name[\"'][^>]*content=[\"']([^\"']{3,60})[\"']", text, re.I)
    if not og:
        og = re.search(r"content=[\"']([^\"']{3,60})[\"'][^>]*property=[\"']og:site_name[\"']", text, re.I)
    if og:
        return og.group(1).strip()
    t = re.search(r"<title[^>]*>([^<]{3,120})</title>", text, re.I)
    if t:
        return re.split(r" [|\u2013\u2014\u00bb] ", t.group(1))[0].strip()
    return ""


_BLOCK_LABEL = re.compile(
    r"(?i)^\s*(phone|telephone|t\u00e9l|tel\u00e9fono|tel|direcci\u00f3n|address|email|correo|contact)\s*[:]\s*(.*)$")
_GENERIC_NAMES = re.compile(
    r"(?i)^(contact us?|contact|send (us )?(a )?message|inquiries?|more information|details?|"
    r"office hours?|get directions|map|hours?|welcome|about us?)$")
_P_FURNITURE = re.compile(r"(?i)^(contact|about|our team|donate|home|menu|search|events|news|services|locations)(\b|$)")


def _is_page_furniture(text: str) -> bool:
    return bool(_P_FURNITURE.match(text))


def _label_kind(label: str) -> str | None:
    l = label.lower()
    if l.startswith(("phone", "tel", "t\u00e9l")) or "tel\u00e9fono" in l:
        return "phone"
    if l.startswith(("direcc", "address")):
        return "address"
    if l.startswith(("email", "correo")):
        return "email"
    return None


def _usable_name(line: str) -> bool:
    """A line that can serve as an office/organization name: short, no contact artifacts,
    no label shape, not a generic UI phrase."""
    return (2 <= len(line) <= 60
            and not _BLOCK_LABEL.match(line)
            and not _EMAIL_RE.search(line)
            and not re.search(r"\b\d{3}[\s\-\.)]\d{3}", line)
            and not _GENERIC_NAMES.match(line)
            and "," not in line                       # comma lists are nav/counties, never names
            and not line.endswith(":")
            and not _HOURS_RE.search(line)            # bureau hours text, never a name
            and not re.search(r"[A-Z]{2}\s+\d{5}", line)   # address tails, never a name
            and line[0].isupper()                     # sentences/junk start lowercase
            and line[0].isalpha())


_CITYLINE = re.compile(r"^([A-Za-z][A-Za-z .\'\-]{1,28})\s*,?\s*(?:[A-Z]{2}|Florida|Fl\b)\s+\d{5}")
_STREET_SUFFIX = re.compile(
    r"(?i)^(st|street|rd|road|ave|avenue|blvd|boulevard|dr|drive|ln|lane|way|ter|terrace|pkwy|"
    r"parkway|hwy|highway|cir|circle|ct|court|trail|pike|path|pl|place|gwy|gateway)$")


def _addr_city(address: str) -> str:
    """City from an office block's own LAST address line ("Sarasota, FL 34234" → "Sarasota").
    Walks the capitalized tokens from the end and STOPS at street-suffix words ("Trail",
    "Ave", …), so street words can never leak into the name."""
    m = _CITYLINE.match(address or "")
    if not m:
        return ""
    tokens = m.group(1).split()
    kept: list[str] = []
    for tok in reversed(tokens):
        if not kept:
            pass
        if tok.endswith(".") or _STREET_SUFFIX.match(tok) or any(ch.isdigit() for ch in tok):
            break
        if len(tok) < 2 or not tok[0].isupper():
            break
        kept.append(tok)
    return " ".join(reversed(kept)) if kept else ""


def _labeled_blocks(lines: list[str], brand: str) -> list[dict]:
    """Blocks on label-directory pages ("Address:" / "Phone:" / "Email:"). Office names come
    from the block's OWN content — the city inside its address, then a multi-word name line
    printed DIRECTLY above the block, then the email local part — never from scanning up
    past the block (that grabs nav links and neighboring blocks' leftovers)."""
    blocks, cur, pending = [], None, None          # pending: label waiting for its value line
    consumed: set[int] = set()
    above: dict = {}

    def flush():
        nonlocal cur
        if cur and (cur["emails"] or cur["phones"]):
            adj = above.get("line", "")
            city = _addr_city(cur.get("cityline", ""))
            cur["name"] = (cur["name"] or "").rstrip(":").strip()
            if adj and " " in adj and _usable_name(adj):
                cur["name"] = adj                 # a printed office/department title
            elif city:
                cur["name"] = city
            elif cur["emails"]:
                local = cur["emails"][0]["value"].split("@", 1)[0]
                if local and not any(ch.isdigit() for ch in local):
                    cur["name"] = local.replace("_", " ").title()
            if cur["name"]:
                blocks.append(cur)
        cur = None

    for i, line in enumerate(lines):
        m = _BLOCK_LABEL.match(line)
        if m:
            kind = _label_kind(m.group(1))
            rest = m.group(2).strip()
            if kind in ("phone", "email"):
                if cur is None or (kind == "phone" and cur["phones"]) or (kind == "email" and cur["emails"]):
                    flush()
                    cur = {"name": "", "emails": [], "phones": [], "address": "", "cityline": ""}
                    prev = lines[max(0, i - 1)] if i > 0 else ""
                    above = {"line": prev if (_usable_name(prev) and not _BLOCK_LABEL.match(prev)
                                              and (i - 1) not in consumed) else ""}
                if rest:
                    consumed.add(i)
                    if kind == "phone":
                        e164 = norm_phone(rest)
                        if e164 and _phone_ok(line) and e164 not in [p["e164"] for p in cur["phones"]]:
                            cur["phones"].append({"value": rest, "e164": e164, "evidence": line[:120]})
                    else:
                        e = rest.lower()
                        if _valid_email(e) and not _is_system_mailbox(e):
                            cur["emails"].append({"value": e, "domain_kind": _domain_kind(e), "evidence": line[:120]})
                    continue
                pending = kind
                consumed.add(i)
                continue
            if kind == "address":
                if cur is None:
                    cur = {"name": "", "emails": [], "phones": [], "address": "", "cityline": ""}
                    prev = lines[max(0, i - 1)] if i > 0 else ""
                    above = {"line": prev if (_usable_name(prev) and not _BLOCK_LABEL.match(prev)
                                              and (i - 1) not in consumed) else ""}
                elif cur["phones"] or cur["emails"]:
                    flush()                       # this Address: opens the NEXT office block
                    cur = {"name": "", "emails": [], "phones": [], "address": "", "cityline": ""}
                    prev = lines[max(0, i - 1)] if i > 0 else ""
                    above = {"line": prev if (_usable_name(prev) and not _BLOCK_LABEL.match(prev)
                                              and (i - 1) not in consumed) else ""}
                if rest:
                    cur["address"] = (cur["address"] + " " + rest).strip()
                    cur["cityline"] = rest
                    consumed.add(i)
                pending = "address"
                continue
        if pending and not m:
            # the value line(s) the previous label was waiting for; address keeps consuming
            # lines until the next label (street and city/zip print on separate lines)
            consumed.add(i)
            if pending == "phone" and cur is not None:
                e164 = norm_phone(line)
                if e164 and _phone_ok(line) and e164 not in [p["e164"] for p in cur["phones"]]:
                    cur["phones"].append({"value": line, "e164": e164, "evidence": line[:120]})
            elif pending == "email" and cur is not None:
                e = line.lower().strip()
                if _valid_email(e) and not _is_system_mailbox(e):
                    cur["emails"].append({"value": e, "domain_kind": _domain_kind(e), "evidence": line[:120]})
            elif pending == "address" and cur is not None:
                cur["address"] = (cur["address"] + " " + line).strip()
                cur["cityline"] = line
                continue                          # stay pending until the next label line
            pending = None
            continue
    flush()
    if brand:
        for b in blocks:
            if b["name"] and b["name"].lower() not in brand.lower():
                b["name"] = f"{brand} — {b['name']}"
            elif not b["name"]:
                b["name"] = brand
    return blocks


def _groups_by_header(lines: list[str]) -> list[dict]:
    groups, current = [], {"name": "", "emails": [], "phones": [], "address": ""}
    for line in lines:
        line_emails, line_phones = [], []
        for m in _EMAIL_RE.finditer(line):
            e = m.group(0).lower()
            if _valid_email(e) and not _is_system_mailbox(e):
                line_emails.append({"value": e, "domain_kind": _domain_kind(e), "evidence": line[:120]})
        for tel in re.findall(r"tel:[+\d\-()\s]{7,20}|(?:(?:\+?1)[\-\s.])?\(\d{3}\)[\-\s.]?\d{3}[\-\s.]?\d{4}\b", line):
            e164 = norm_phone(tel.replace("tel:", ""))
            if e164 and _phone_ok(line):
                line_phones.append({"value": tel.replace("tel:", "").strip(), "e164": e164,
                                    "evidence": line[:120]})
        if _looks_like_org_header(line):
            if current["name"] and (current["emails"] or current["phones"]):
                groups.append(current)
            # a header line never carries its own contacts (guarded in _looks_like_org_header),
            # so anything extracted on it belongs to the org arriving WITH it
            current = {"name": line, "emails": line_emails, "phones": line_phones, "address": line}
        else:
            for e in line_emails:
                if e["value"] not in [x["value"] for x in current["emails"]]:
                    current["emails"].append(e)
            for p in line_phones:
                if p["e164"] not in [x["e164"] for x in current["phones"]]:
                    current["phones"].append(p)
    if current["name"] and (current["emails"] or current["phones"]):
        groups.append(current)
    return groups


def _pool_by_line(lines: list[str]) -> list[dict]:
    """Flat contact pages: each contact-carrying line names itself (text before the first
    artifact on THAT line is the printed owner). Lines without self-names are skipped."""
    groups = []
    for line in lines:
        line_emails, line_phones = [], []
        for m in _EMAIL_RE.finditer(line):
            e = m.group(0).lower()
            if _valid_email(e) and not _is_system_mailbox(e):
                line_emails.append({"value": e, "domain_kind": _domain_kind(e), "evidence": line[:120]})
        for tel in re.findall(r"tel:[+\d\-()\s]{7,20}|(?:(?:\+?1)[\-\s.])?\(\d{3}\)[\-\s.]?\d{3}[\-\s.]?\d{4}\b", line):
            e164 = norm_phone(tel.replace("tel:", ""))
            if e164 and _phone_ok(line):
                line_phones.append({"value": tel.replace("tel:", "").strip(), "e164": e164,
                                    "evidence": line[:120]})
        if not (line_emails or line_phones):
            continue
        first = min((line.find(e["value"]) for e in line_emails if line.find(e["value"]) >= 0), default=10**9)
        first = min(first, min((line.find(p["value"]) for p in line_phones if line.find(p["value"]) >= 0), default=10**9))
        prefix = line[:first].strip(" \t,;–—-") if first < 10**9 else ""
        if _usable_name(prefix):
            groups.append({"name": prefix, "emails": line_emails, "phones": line_phones, "address": prefix})
    return groups


def _attach_href(groups: list[dict], href_emails: list[dict], href_phones: list[dict],
                 lines: list[str], seen_values: set[str] | None = None, brand: str = "") -> list[dict]:
    # CTA headlines ("Become a Partner Agency") are page furniture, not org names: the page's
    # own title/brand token is the truthful label for the whole page's contact details.
    if brand and groups:
        for g in groups:
            # page-title headers ("Contact Us - Harry Chapin Food Bank") keep just the part
            # that looks like the org itself
            parts = re.split(r" [\-\u2013\u2014|] ", g["name"])
            marked = [p.strip() for p in parts if _looks_like_org_header(p) and not _is_page_furniture(p)]
            if marked and len(marked) == 1 and not _P_FURNITURE.match(marked[0]):
                g["name"] = marked[0]
            low = g["name"].lower()
            if low.startswith(("become a", "become an", "join us", "get involved", "donate",
                               "volunteer", "connect", "reach out")):
                g["name"] = brand
    """Unroutable href contacts: attach when the page produced exactly ONE org group (the
    common case for a single-org contact page); otherwise surface a page-scoped row only
    when the page itself carries an org header marker — never guess an owner."""
    candidates = [g for g in groups if g.get("name")]
    if len(candidates) == 1 and (href_emails or href_phones):
        tgt = candidates[0]
        for e in href_emails:
            if e["value"] not in [x["value"] for x in tgt["emails"]]:
                tgt["emails"].append(e)
        for p in href_phones:
            if p["e164"] not in [x["e164"] for x in tgt["phones"]]:
                tgt["phones"].append(p)
    if not candidates and (href_emails or href_phones) and seen_values:
        # page carries off-site contact links but NO parseable org listing: keep them as a
        # page-scoped record only when the page title/heading itself is org-shaped
        page_hdr = next((l for l in lines if _looks_like_org_header(l)), "")
        if page_hdr:
            groups.append({"name": page_hdr, "emails": href_emails, "phones": href_phones,
                           "address": page_hdr})
    # when the listing lists several orgs, off-site hrefs stay UNATTACHED — attaching them to
    # a page junk-header (nav menus carry mailto links) would fabricate owners
    return groups


def _norm_text_pat(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").replace("\u2019", "'")).strip()


_HOURS_RE = re.compile(r"(?i)(a\.?m|p\.?m|hours|\bmon|\btue|\bwed|\bthu|\bfri|\bsat|\bsun)\b.*\d|\d.*(a\.?m|p\.?m)")

_LEGAL_PHONE = re.compile(
    r"(?i)registration (does not imply|by the state)| discrimination| endorsement| "
    r"1-800-HELP-FLA| disclaimer| registered with the")

def _phone_ok(line: str) -> bool:
    """Regulatory boilerplate numbers ("Registration does not imply endorsement…",
    discrimination-complaint hotlines) are never the org's own contact."""
    return not _LEGAL_PHONE.search(line)

def _looks_like_org_header(line: str) -> bool:
    return (6 <= len(line) <= 120 and not _EMAIL_RE.search(line)
            and not re.search(r"\b\d{3}[\s\-\.)]\d{3}", line)
            and not _HOURS_RE.search(line)               # bureau hours lines, not org names
            and not line.lower().startswith(("how to ", "about ", "contact ", "frequently", "before",
                                             "[", "@", "#", "©", "copyright", "become a", "become an"))
            and re.search(
                r"\b(inc|llc|corp|ministry|church|center|centre|clinic|services|agency|"
                r"organization|association|charit|program|network|outreach|mission|"
                r"coalition|diocese|parish|health|hospital|college|school|pantry|"
                r"food bank|office|council|society)\b", line, re.I))


def _is_system_mailbox(email: str) -> bool:
    local = email.split("@", 1)[0].lower()
    return any(s in local for s in _SYSTEM_LOCALS)


# ---------------------------------------------------------------- upsert / dedupe
def _org_key(org: dict) -> str:
    phone = ""
    try:
        ph = json.loads(org.get("phones") or "[]")
        phone = (ph[0]["e164"] if ph else "")
    except Exception:                                       # noqa: BLE001
        pass
    seed = "|".join([_norm_name(org.get("org_name") or ""), org.get("address", "")[:60], phone])
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:24]


def _upsert_orgs(con, rows: list[dict], source: str, fetch_evidence: bool = False) -> int:
    accepted = 0
    for org in rows:
        key = _org_key(org)
        if not org.get("org_name") or not (json.loads(org.get("phones") or "[]") or json.loads(org.get("emails") or "[]")):
            continue                                        # no contact surface → not usable, not stored
        existing = con.execute("SELECT id, verify_status, phones, emails, source_url FROM org_contacts WHERE org_key=?",
                               (key,)).fetchone()
        kind = org.get("record_kind", KIND_DISCOVERED)
        verify_status = ("VERIFIED_PRIMARY" if fetch_evidence
                         else "VERIFIED_RECHECK" if kind == KIND_VERIFIED
                         else "DISCOVERED_UNVERIFIED")
        if existing is None:
            con.execute(
                "INSERT INTO org_contacts (org_key, org_name, org_type, source, source_url, record_ids, emails,"
                " phones, address, lang, purpose_note, published_at, collected_at, terms_note, verify_status,"
                " verification_note, record_kind, dup_status, stage, next_action, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, org["org_name"], org.get("org_type", "other"), org.get("source", source),
                 org.get("source_url", ""), org.get("record_ids", "[]"), org.get("emails", "[]"),
                 org.get("phones", "[]"), org.get("address", ""), org.get("lang", "en"),
                 org.get("purpose_note", ""), org.get("published_at"), _now(), org.get("terms_note", ""),
                 verify_status, _verification_note(kind, fetch_evidence), kind, "UNIQUE", kind,
                 NEXT_ACTION_BY_KIND[kind], _now(), _now()))
            accepted += 1
        else:
            # merge new contact surfaces only; never upgrade verification without a recheck
            merged = _merge_contact_lists(existing["phones"], org.get("phones", "[]"))
            con.execute("UPDATE org_contacts SET phones=?, updated_at=?, source_url=? WHERE id=?",
                        (merged, _now(), org.get("source_url", existing["source_url"] or ""), existing["id"]))
    con.commit()
    return accepted


def _verification_note(kind: str, fetch_evidence: bool) -> str:
    if fetch_evidence:
        return "verified from primary source at collection: record came straight from the official registry"
    return "discovered; requires recheck of source_url before anything uses it"


def _merge_contact_lists(old_json: str, new_json: str) -> str:
    def parse(j):
        try:
            return json.loads(j or "[]")
        except Exception:                                    # noqa: BLE001
            return []
    old, new = parse(old_json), parse(new_json)
    seen = {p.get("e164") or p.get("value") for p in old}
    for c in new:
        if (c.get("e164") or c.get("value")) not in seen:
            old.append(c)
            seen.add(c.get("e164") or c.get("value"))
    return json.dumps(old)


def _metric(con, source: str, fetched, accepted, extracted, verified, dupes, errors,
            restrictions: str, note: str) -> None:
    con.execute("INSERT INTO source_metrics (ts, source, records_fetched, records_accepted,"
                " contacts_extracted, contacts_verified, duplicates_removed, errors,"
                " restrictions, note) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (_now(), source, fetched, accepted, extracted, verified, dupes, errors,
                 restrictions, note))
    con.commit()


# ------------------------------------------------------------------- verify & report
def verify_contacts(con, fetch_text=None, limit: int = 25) -> dict:
    """Recheck DISCOVERED rows against their own source page; matched exact contact →
    VERIFIED_RECHECK. Never marks verified anything whose contact didn't re-appear."""
    got = checked = 0
    for row in con.execute("SELECT * FROM org_contacts WHERE record_kind=? AND verify_status=? ORDER BY id LIMIT ?",
                           (KIND_DISCOVERED, "DISCOVERED_UNVERIFIED", limit)).fetchall():
        try:
            page = (fetch_text or http_text)(row["source_url"])
        except Exception:                                    # noqa: BLE001
            continue
        checked += 1
        phones = json.loads(row["phones"] or "[]")
        emails = json.loads(row["emails"] or "[]")
        if not (phones or emails):
            continue
        page_digits = re.sub(r"\D", "", page)
        page_lower = page.lower()
        # every recorded phone must re-appear (digit-normalized, without the +1 prefix)
        # and every recorded email must re-appear on the same page (emails are case-insensitive)
        ok = all((p.get("e164") or "")[2:] in page_digits for p in phones) \
            and all(e["value"].lower() in page_lower for e in emails)
        if ok:
            con.execute("UPDATE org_contacts SET verify_status='VERIFIED_RECHECK', record_kind=?, "
                        "verification_note=?, updated_at=? WHERE id=?",
                        (KIND_VERIFIED, "rechecked against source page: contacts re-appeared", _now(), row["id"]))
            got += 1
    con.commit()
    return {"checked": checked, "verified": got}


def perf_rank(con, limit: int = 20) -> list[dict]:
    """Sources ranked by verified-records-per-fetch-unit — the useful-work signal, not pages."""
    rows = con.execute(f"""
        SELECT source, SUM(records_fetched) fetched, SUM(records_accepted) accepted,
               SUM(contacts_extracted) extracted, SUM(contacts_verified) verified,
               SUM(duplicates_removed) dupes, SUM(errors) errors,
               MAX(restrictions) restrictions
        FROM source_metrics GROUP BY source ORDER BY
          (CAST(SUM(contacts_verified) AS FLOAT) / MAX(SUM(records_fetched), 1)) DESC
        LIMIT {int(limit)}""").fetchall()
    return [dict(r) for r in rows]
