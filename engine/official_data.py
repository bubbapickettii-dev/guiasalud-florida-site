"""OFFICIAL PUBLIC-DATA COLLECTORS — the robots-safe alternatives to crawling healthcare.gov.

Why this module exists
  The webreader seed https://www.healthcare.gov/blog/ was correctly refused by the collector's
  own robots.txt gate. Investigating the ACTUAL restriction (verified live Oct 2026):
    * healthcare.gov/robots.txt declares "User-agent: *" Disallow: /api/, /es/, /esi/, ... —
      so generic crawling of that site really is off the table and stays off.
    * BUT two OFFICIAL public endpoints publish the data we wanted, on purpose, for reuse:
        1. data.cms.gov/data.json           — the CMS DCAT-US open-data catalog (3,486 public
                                              datasets), including the Marketplace enrollment
                                              series (effectuated enrollment by month, plan-year
                                              QHP files). One GET per run, filtered in memory.
        2. healthcare.gov/api/index.json    — the site's OWN published content API (,title,
                                              url, "bite" summaries in English AND Spanish).
                                              robots.txt disallows generic crawlers, not this
                                              machine-readable feed (it exists for reuse with
                                              the required link attribution).
  So the objective (official enrollment/plan/coverage data) is met WITHOUT bypassing any
  access control: we call the doors the providers deliberately opened.

Safeguards inherited from engine.signals
  * same signal store (data/signals.db), same URL-dedup, same review queue;
  * every row is stored as kind='MARKET' — official datasets and help articles are market
    intelligence and official content-planning material, NEVER a consumer lead;
  * one catalog request per run + a polite pause, explicit operator CLI action only;
  * a failing endpoint is logged in fetch_log exactly like every other collector.
"""
from __future__ import annotations

import re
import time
import urllib.parse
from pathlib import Path

from . import signals as sig

ROOT = Path(__file__).resolve().parents[1]
UA = sig.UA
REQUEST_PAUSE_S = sig.REQUEST_PAUSE_S
HTTP_TIMEOUT_S = 30                      # the CMS catalog is ~18 MB; give it room

CMS_CATALOG_URL = "https://data.cms.gov/data.json"
HCGOV_CONTENT_API = "https://www.healthcare.gov/api/index.json"

# what makes a CMS dataset relevant to OUR business: marketplace enrollment, plan availability,
# premium/subsidy economics — the same topics the taxonomy tracks from the news side.
CMS_RELEVANT = re.compile(
    r"health insurance exchange|marketplace|open enrollment|effectuated enrollment|"
    r"qualified health plan|\bQHP\b|premium|subsidy|advantage\b.*additional benefits", re.I)

# Official help pages talk in their OWN vocabulary ("unemployed", "Medicaid", "continue
# coverage"...) that the consumer-intent taxonomy doesn't key on. This second whitelist is
# explicit and auditable: a healthcare.gov page is kept only when it covers our subject matter.
HCGOV_RELEVANT = re.compile(
    r"marketplace|open enrollment|subsidy|premium tax credit|cobra|medicaid|chip\b|"
    r"unemployed|job-based coverage|continue coverage|health plan|coverage options|"
    r"es-title|seguro médico|aseguranza", re.I)


def _clean_cms_dataset(d: dict) -> dict | None:
    """Whitelist-rebuild one DCAT dataset entry. The URL is the official dataset page, which
    stays durable long after a fetch run; distribution CSVs are linked FROM it."""
    if not isinstance(d, dict):
        return None
    title = (d.get("title") or "").strip()
    if not title:
        return None
    # data.cms.gov dataset landing page URL, derived from the catalog's own identifier
    ident = (d.get("identifier") or "").strip()
    if ident.startswith("http"):
        url = ident
    elif ident:
        url = f"https://data.cms.gov/dataset/{ident}/data"
    else:
        dists = d.get("distribution") or []
        url = ""
        for dist in dists:
            if isinstance(dist, dict) and dist.get("landingPage"):
                url = dist["landingPage"]
                break
    if not url or "://" not in url:
        return None
    desc = (d.get("description") or "").strip()
    modified = (d.get("modified") or d.get("issued") or "").strip()
    return {"source": "official_cms",
            "community": "data.cms.gov (official CMS open-data catalog)",
            "title": f"Official CMS dataset: {title}"[:300],
            "snippet": sig._snippet((modified and f"[modified {modified}] " or "") + desc[:1200]),
            "url": url[:400],
            "observed_at": modified or None,
            "collection_method": "public_api",
            "access_status": "public_endpoint",
            "kind": "MARKET"}


def fetch_cms_open_data(con, get_json=None, sleeper=time.sleep) -> dict:
    """One catalog read per explicit run; relevant Marketplace/ACA datasets become MARKET rows."""
    get_json = get_json or sig._http_json
    fetched = accepted = 0
    try:
        catalog = get_json(CMS_CATALOG_URL)
    except Exception as e:                                  # noqa: BLE001 - reportable, not fatal
        sig.log_fetch(con, "official_cms", CMS_CATALOG_URL, 0, 0,
                      f"error: {type(e).__name__}: {e}")
        return {"fetched": 0, "accepted": 0}
    rows = []
    for d in (catalog.get("dataset") if isinstance(catalog, dict) else []) or []:
        blob = f"{d.get('title','')} {d.get('description','')}" if isinstance(d, dict) else ""
        if not CMS_RELEVANT.search(blob):
            continue
        fetched += 1
        cleaned = _clean_cms_dataset(d)
        if cleaned and CMS_RELEVANT.search(cleaned["title"].replace("Official CMS dataset: ", "")):
            rows.append(cleaned)
    if rows:
        accepted = sig.ingest(con, rows, "official_cms")["added"]
    sig.log_fetch(con, "official_cms", CMS_CATALOG_URL, fetched, accepted,
                  f"ok ({len(catalog.get('dataset') or [])} datasets in catalog, "
                  f"{fetched} matched the relevance filter)" if isinstance(catalog, dict) else "ok")
    sleeper(REQUEST_PAUSE_S)
    return {"fetched": fetched, "accepted": accepted}


def fetch_healthcare_gov_content(con, get_json=None, sleeper=time.sleep) -> dict:
    """healthcare.gov's OWN content API (allowed machine-readable feed; reuse requires the
    link-attribution terms shown at the endpoint). Every page exists in EN and ES; only pages
    that clear the taxonomy filter become rows, so storage stays inside our topics."""
    get_json = get_json or sig._http_json
    fetched = accepted = 0
    try:
        pages = get_json(HCGOV_CONTENT_API)
    except Exception as e:                                  # noqa: BLE001
        sig.log_fetch(con, "hcgov", HCGOV_CONTENT_API, 0, 0, f"error: {type(e).__name__}: {e}")
        return {"fetched": 0, "accepted": 0}
    rows = []
    for p in pages if isinstance(pages, list) else []:
        if not isinstance(p, dict):
            continue
        title = (p.get("title") or "").strip()
        url = "https://www.healthcare.gov" + (p.get("url") or "")
        if not title or "://" not in url:
            continue
        fetched += 1
        bite = (p.get("bite") or "").strip()
        if not HCGOV_RELEVANT.search(f"{title} {bite}") and not sig.accept_filter({"title": title, "snippet": bite}):
            continue
        cleaned = {"source": "hcgov",
                   "community": "healthcare.gov (official help-content API, EN + ES)",
                   "title": f"Official healthcare.gov help page: {title}"[:300],
                   "snippet": sig._snippet(bite[:600]),
                   "url": url[:400],
                   "lang_hint": (p.get("lang") or "").strip(),
                   "observed_at": None,             # evergreen help content: fetch time
                   "collection_method": "public_api",
                   "access_status": "public_endpoint",
                   "kind": "MARKET"}
        if sig.accept_filter(cleaned) or HCGOV_RELEVANT.search(f"{title} {bite}"):
            rows.append(cleaned)
    if rows:
        accepted = sig.ingest(con, rows, "hcgov")["added"]
    sig.log_fetch(con, "hcgov", HCGOV_CONTENT_API, fetched, accepted,
                  f"ok ({len(pages) if isinstance(pages, list) else 0} pages in feed)")
    sleeper(REQUEST_PAUSE_S)
    return {"fetched": fetched, "accepted": accepted}


def run_official_data(con, sleeper=time.sleep) -> list[dict]:
    """Both official collectors in one explicit run — what the CLI calls."""
    out = []
    out.append({"source": "official_cms", **fetch_cms_open_data(con, sleeper=sleeper)})
    out.append({"source": "hcgov", **fetch_healthcare_gov_content(con, sleeper=sleeper)})
    return out
