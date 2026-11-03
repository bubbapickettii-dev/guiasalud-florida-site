"""SOURCE EVALUATION — what a data source actually contains, measured before we admit it.

The question this answers is not "is this source allowed?" but **"what is in it?"**: how many
records, which fields really carry data, how many phones/emails are well formed, how much of it is
duplicated, and whether the rows are real people, organisations, or generated text.

Design rules:
  * Every number is measured from fetched rows or read from the source's own API/schema. A field
    that is absent is reported as 0 % coverage, never assumed present.
  * Authenticity is a separate axis from permission (`data_status` vs `marketing_eligibility`).
  * Fetchers are injectable (`fetcher=`) so the evaluation is testable offline, the same pattern
    engine/discovery.py uses.
  * Nothing here contacts anyone, and nothing here marks a record marketable.

Registry entries without a fetcher are documented findings (measured earlier or blocked by an
access requirement), and they say so rather than pretending to be measured now.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections import Counter

from . import admission as adm

UA = {"User-Agent": "gs-allude-source-eval/1.0 (contact: ops@gs-allude.example)"}
CANON = ("full_name", "first_name", "last_name", "phone", "email", "city", "state", "zip",
         "source_record_id", "collected_at", "telegram")


class AccessBlocked(RuntimeError):
    """The source cannot be read with what we have (key, login, payment, contract)."""


def _http(url: str, timeout: int = 40) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as f:
        return f.read()


# ------------------------------------------------------------------ fetchers (public, no key)
# NPPES rejects a query that only filters by state/enumeration type ("requires additional search
# criteria"), so a taxonomy is always supplied. These are the health-adjacent individual provider
# types this business already cares about; the operator can override with --taxonomy.
NPPES_TAXONOMIES = ("Registered Nurse", "Physician Assistant", "Dietitian, Registered",
                    "Social Worker", "Physical Therapist", "Occupational Therapist")


def fetch_nppes(limit: int = 200, state: str = "FL", taxonomy: str = "", **_):
    """CMS NPPES NPI Registry — INDIVIDUAL providers (NPI-1): real name + practice phone."""
    rows, skip, seen = [], 0, set()
    taxonomies = [taxonomy] if taxonomy else list(NPPES_TAXONOMIES)
    for tax in taxonomies:
        if len(rows) >= limit:
            break
        skip = 0
        while len(rows) < limit:
            q = {"version": "2.1", "state": state, "enumeration_type": "NPI-1",
                 "taxonomy_description": tax, "limit": min(200, limit - len(rows)), "skip": skip}
            data = json.loads(_http("https://npiregistry.cms.hhs.gov/api/?" + urllib.parse.urlencode(q)))
            batch = data.get("results") or []
            if not batch:
                break
            for r in batch:
                npi = r.get("number") or ""
                if npi and npi in seen:
                    continue
                seen.add(npi)
                b = r.get("basic") or {}
                addr = next((a for a in (r.get("addresses") or []) if a.get("address_purpose") == "LOCATION"),
                            (r.get("addresses") or [{}])[0])
                rows.append({
                    "npi": npi,
                    "full_name": f"{b.get('first_name') or ''} {b.get('last_name') or ''}".strip(),
                    "first_name": b.get("first_name") or "",
                    "last_name": b.get("last_name") or "",
                    "phone": addr.get("telephone_number") or "",
                    "fax": addr.get("fax_number") or "",
                    "city": (addr.get("city") or ""),
                    "state": addr.get("state") or "",
                    "zip": (addr.get("postal_code") or "")[:5],
                    "taxonomy": tax,
                    # per-record provenance: the NPI resolves this exact provider at the source
                    "source_url": f"https://npiregistry.cms.hhs.gov/provider-view/{npi}" if npi else "",
                    "enumeration_date": b.get("enumeration_date") or "",
                    "last_updated": b.get("last_updated") or "",
                    "status": b.get("status") or "",
                })
            skip += len(batch)
            if len(batch) < 200 or skip >= 1000:      # NPPES caps deep paging; stay within it
                break
    return rows[:limit]


def fetch_cms_facility(limit: int = 200, dataset: str = "xubh-q36u", **_):
    """CMS provider-data facility listing (default: Hospital General Information):
    organisation name + published telephone. No email, no individual person."""
    out, offset = [], 0
    while len(out) < limit:
        u = (f"https://data.cms.gov/provider-data/api/1/datastore/query/{dataset}/0"
             f"?limit={min(500, limit - len(out))}&offset={offset}")
        data = json.loads(_http(u))
        batch = data.get("results") or []
        if not batch:
            break
        for r in batch:
            fid = r.get("facility_id") or r.get("cms_certification_number_ccn") or ""
            out.append({
                "facility_id": fid,
                "source_url": f"https://data.cms.gov/provider-data/dataset/{dataset}",
                "facility_name": r.get("facility_name") or "",
                "address": r.get("address") or r.get("address_line_1") or "",
                "city": r.get("citytown") or "",
                "state": r.get("state") or "",
                "zip": (r.get("zip_code") or "")[:5],
                "phone": r.get("telephone_number") or "",
                "type": r.get("hospital_type") or r.get("profit_or_nonprofit") or "",
            })
        offset += len(batch)
        if len(batch) < 500:
            break
    return out[:limit]


def fetch_hf_synthetic(limit: int = 50, **_):
    """The shipped sample of ziadatalabs/FreeCustomerData50M.

    Included precisely because it is the trap: 50M rows of full_name/email/phone/address that the
    publisher's own file name calls `synthetic_users_50M.parquet`.
    """
    import csv
    import io
    raw = _http("https://huggingface.co/datasets/ziadatalabs/FreeCustomerData50M"
                "/resolve/main/sample_50rows.csv").decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(raw)))
    return rows[:limit]


# ------------------------------------------------------------------ registry
SOURCES = {
    "nppes": {
        "title": "CMS NPPES NPI Registry — individual providers",
        "url": "https://npiregistry.cms.hhs.gov/api/",
        "record_type": "PROVIDER", "data_status": "VERIFIED_REAL",
        "license": "CMS public data; NPPES terms of use prohibit use for marketing or solicitation",
        "license_note": "real names + published practice phones; NO email field exists in the registry",
        "fields": ["full_name", "first_name", "last_name", "phone", "city", "state", "zip",
                   "source_record_id", "collected_at"],
        "access": "public API, no key, no login",
        "fetcher": fetch_nppes, "synthetic": False,
    },
    "cms-facility": {
        "title": "CMS provider-data — facility listings (hospitals, dialysis, home health)",
        "url": "https://data.cms.gov/provider-data/",
        "record_type": "PROVIDER_ORG", "data_status": "VERIFIED_REAL",
        "license": "CMS provider data, public domain (see each dataset's terms)",
        "license_note": "organisation name + published telephone; not an individual consumer",
        "fields": ["full_name", "phone", "city", "state", "zip", "source_record_id"],
        "access": "public datastore API, no key",
        "fetcher": fetch_cms_facility, "synthetic": False,
    },
    "hf-freecustomerdata50m": {
        "title": "HuggingFace ziadatalabs/FreeCustomerData50M",
        "url": "https://huggingface.co/datasets/ziadatalabs/FreeCustomerData50M",
        "record_type": "CONSUMER", "data_status": "SYNTHETIC",
        "license": "CC-BY-NC-4.0 — but the records are GENERATED, not licensed real data",
        "license_note": "publisher's own file is named synthetic_users_50M.parquet",
        "fields": ["full_name", "phone", "email", "street_address", "city", "state", "zip",
                   "collected_at"],
        "access": "public download sample; parquet is 50M generated rows",
        "fetcher": fetch_hf_synthetic, "synthetic": True,
    },
    "census-cbp": {
        "title": "Census County Business Patterns / ZBP",
        "url": "https://www.census.gov/data/developers/data-sets/cbp-zbp.html",
        "record_type": "BUSINESS", "data_status": "VERIFIED_REAL",
        "license": "US Census public data",
        "license_note": "aggregate establishment counts per NAICS/county — no establishment records, no contacts",
        "fields": [],
        "access": "API key now required (api.census.gov returns 'Missing Key')",
        "fetcher": None, "synthetic": False,
        "finding": "0 contact fields by construction: the API returns NAME/NAICS/ESTAB/EMP aggregates. "
                   "Useful for market sizing, not for contacting anyone.",
    },
    "sec-edgar": {
        "title": "SEC EDGAR",
        "url": "https://www.sec.gov/about/developer-resources",
        "record_type": "BUSINESS", "data_status": "VERIFIED_REAL",
        "license": "public filings; fair-access rate limits + declared User-Agent required",
        "license_note": "company names, addresses, filings; individual officers appear in Forms 3/4/8-K",
        "fields": ["full_name", "city", "state", "zip"],
        "access": "public JSON, no key",
        "fetcher": None, "synthetic": False,
        "finding": "company-level: name + business/mailing address (phone field is present but usually "
                   "empty); no email; officers are named but not contactable as individuals.",
    },
    "dataverse-yglydy": {
        "title": "Harvard Dataverse — Amazon purchase histories (doi:10.7910/DVN/YGLYDY)",
        "url": "https://doi.org/10.7910/DVN/YGLYDY",
        "record_type": "CONSUMER", "data_status": "VERIFIED_REAL",
        "license": "CC0 1.0",
        "license_note": "survey purchases by anonymised workers; no identity fields",
        "fields": [],
        "access": "public (313 MB csv)",
        "fetcher": None, "synthetic": False,
        "finding": "behavioural survey microdata keyed by anonymous worker id — no names, phones or emails.",
    },
    "mendeley-customer-purchase": {
        "title": "Mendeley Data — Customer Purchase Behavior (10.17632/xv99b2mtfk.1)",
        "url": "https://data.mendeley.com/datasets/xv99b2mtfk/1",
        "record_type": "CONSUMER", "data_status": "UNVERIFIED",
        "license": "see Mendeley record (research licence)",
        "license_note": "single dataset file, academic purpose",
        "fields": [],
        "access": "public download",
        "fetcher": None, "synthetic": False,
        "finding": "purchase-behaviour survey; no contact fields.",
    },
    "linkedin_scraper": {
        "title": "joeyism/linkedin_scraper (GitHub)",
        "url": "https://github.com/joeyism/linkedin_scraper",
        "record_type": "PROFESSIONAL", "data_status": "VERIFIED_REAL",
        "license": "Apache-2.0 code; collection violates LinkedIn User Agreement §8.2",
        "license_note": "requires an authenticated member session",
        "fields": ["full_name", "city", "state"],
        "access": "authenticated LinkedIn session required (session.json)",
        "fetcher": None, "synthetic": False,
        "finding": "Person model: name/headline/location/about/experience + contacts[] from the "
                   "contact-info modal. NO email field, NO phone field. No downloadable dataset.",
    },
    "google_maps_scraper": {
        "title": "gosom/google-maps-scraper (GitHub)",
        "url": "https://github.com/gosom/google-maps-scraper",
        "record_type": "BUSINESS", "data_status": "VERIFIED_REAL",
        "license": "MIT code; Google Maps terms forbid automated access",
        "license_note": "-email crawls the business's own website for addresses",
        "fields": ["full_name", "phone", "city", "state", "zip", "source_record_id"],
        "access": "live Maps scraping (proxy spend, ToS risk)",
        "fetcher": None, "synthetic": False,
        "finding": "place listings: business title, address, business phone, website, place_id/cid. "
                   "No individual consumer names, phones or emails.",
    },
    "csv": {
        "title": "Operator-supplied export (CSV/JSON file)",
        "url": "",
        "record_type": "UNKNOWN", "data_status": "UNVERIFIED",
        "license": "whatever the operator records for that vendor/licence",
        "license_note": "the path a licensed consumer-contact vendor's export takes",
        "fields": ["full_name", "phone", "email", "city", "state", "zip", "collected_at"],
        "access": "local file",
        "fetcher": None, "synthetic": False,
    },
}


def describe(source_id: str) -> dict:
    if source_id not in SOURCES:
        raise KeyError(f"unknown source {source_id!r}; known: {sorted(SOURCES)}")
    s = dict(SOURCES[source_id])
    s["source_id"] = source_id
    s.pop("fetcher", None)
    return s


# ------------------------------------------------------------------ evaluation
def evaluate(rows: list[dict], source_id: str = "") -> dict:
    """Field coverage, validity and duplication for real fetched rows.

    Measured with `admission.normalize` — the same mapping the import uses — so the numbers here
    are exactly what the store will hold, not a second opinion.
    """
    recs = [adm.normalize(r) for r in (rows or [])]
    n = len(recs)
    cov = {f: sum(1 for r in recs if (r.get(f) or "")) for f in CANON}
    phones = [r for r in recs if r.get("phone")]
    emails = [r for r in recs if r.get("email")]
    raw_phone = sum(1 for r in recs if r["quality"].get("has_phone"))
    raw_email = sum(1 for r in recs if r["quality"].get("has_email"))
    keys = Counter(r["contact_key"] for r in recs if r["contact_key"])
    # Two different things share the word "duplicate" and must be reported separately:
    #   exact duplicate  - the same contact channel AND the same name (one person, twice)
    #   shared channel   - the same number/email under DIFFERENT names (clinic switchboard,
    #                      household line). Merging those would delete real people.
    by_key: dict[str, set] = {}
    for r in recs:
        if r["contact_key"]:
            by_key.setdefault(r["contact_key"], set()).add(adm._key_slug(r["full_name"]))
    shared = {k: v for k, v in by_key.items() if len(v) > 1}
    dups = {k: c for k, c in keys.items() if c > 1 and len(by_key.get(k, ())) <= 1}
    bad_phone = [r["quality"]["phone_raw"] for r in recs if r["quality"].get("phone_raw")]
    shapes = Counter()
    for r in recs:
        has_n, has_p, has_e = bool(r["full_name"]), bool(r["phone"]), bool(r["email"])
        shapes["name+phone+email" if has_n and has_p and has_e else
               "name+phone" if has_n and has_p else
               "name+email" if has_n and has_e else
               "name only" if has_n else
               "contact, no name" if (has_p or has_e) else "nothing usable"] += 1
    pct = lambda k: round(100.0 * k / n, 1) if n else 0.0          # noqa: E731
    return {
        "source_id": source_id, "records": n,
        "coverage": {f: {"n": cov[f], "pct": pct(cov[f])} for f in CANON},
        "has_name": cov["full_name"], "has_phone_raw": raw_phone, "has_email_raw": raw_email,
        "valid_phones": len(phones), "valid_emails": len(emails),
        "phone_valid_pct": pct(len(phones)), "email_valid_pct": pct(len(emails)),
        "invalid_or_unusable_phones": len(bad_phone),
        "invalid_phone_examples": bad_phone[:5],
        "identity_shapes": dict(shapes),
        "duplicate_keys": len(dups), "duplicate_rows": sum(c - 1 for c in dups.values()),
        "duplicate_examples": list(dups)[:3],
        "shared_contact_channels": len(shared),
        "shared_channel_rows": sum(len(v) for v in shared.values()),
        "shared_channel_names": [sorted(v)[:3] for v in list(shared.values())[:3]],
        "no_identity_rows": sum(1 for r in recs if not r["contact_key"]),
    }


def run(source_id: str, limit: int = 200, fetcher=None, **opts) -> dict:
    """Fetch + evaluate one source. `fetcher(limit=..., **opts)` overrides the network call."""
    if source_id not in SOURCES:
        raise KeyError(f"unknown source {source_id!r}; known: {sorted(SOURCES)}")
    s = SOURCES[source_id]
    f = fetcher or s.get("fetcher")
    meta = describe(source_id)
    if f is None:
        meta["measured"] = False
        meta["evaluation"] = None
        return meta                                   # documented finding only
    rows = f(limit=limit, **opts) or []
    rep = evaluate(rows, source_id)
    meta.update({"measured": True, "evaluation": rep, "rows": rows})
    return meta


def to_markdown(reports: list[dict]) -> str:
    """The report an operator (or counsel) reads: one block per source, measured numbers first."""
    out = ["# Source evaluation — measured contents, not marketing claims", ""]
    for r in reports:
        e = r.get("evaluation")
        out.append(f"## {r['title']}")
        out.append(f"`{r['source_id']}` · {r['record_type']} · data status **{r['data_status']}** · "
                   f"access: {r['access']}")
        out.append(f"Licence/terms: {r['license']}")
        if not r.get("measured"):
            out.append(f"**Not measured in this run.** {r.get('finding', '')}")
            out.append("")
            continue
        out.append(f"**Records evaluated: {e['records']}** · valid phones {e['valid_phones']} "
                   f"({e['phone_valid_pct']} %) · valid emails {e['valid_emails']} "
                   f"({e['email_valid_pct']} %) · exact duplicates {e['duplicate_rows']} · "
                   f"shared contact channels {e['shared_contact_channels']} · "
                   f"rows with no identity {e['no_identity_rows']}")
        out.append("| field | rows | coverage |")
        out.append("|---|---|---|")
        for f_, c in e["coverage"].items():
            out.append(f"| {f_} | {c['n']} | {c['pct']} % |")
        out.append(f"Identity shapes: {json.dumps(e['identity_shapes'])}")
        if e["invalid_phone_examples"]:
            out.append(f"Unusable phone values: {e['invalid_phone_examples']}")
        out.append("")
    return "\n".join(out)
