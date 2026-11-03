"""Official public-data collectors: CMS open-data catalog + healthcare.gov content API.

HARD BOUNDARIES UNDER TEST (all offline — injected network never hits the real endpoints):
  1. Both collectors ingest ONLY into signals.db with kind='MARKET' — official datasets and
     help pages are market intelligence, never leads (acquisition-required distinction).
  2. Relevance filtering: irrelevant CMS datasets are dropped before storage.
  3. URL-dedup: a second identical run stores nothing new (same rule as every collector).
  4. A failing endpoint is logged in fetch_log and reported, never crashes the sweep.
  5. The saturation guard: default_signals kind assignment (demand_kind) routes community
     posts to INTEREST and everything else to MARKET.
"""
import importlib, os, sys, types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ROOT = Path(__file__).resolve().parents[1]

CMS_CATALOG = {
    "dataset": [
        {"identifier": "05979174-881c-4504-8d9b-a2edf61d5a47",
         "title": "Health Insurance Exchanges Annual Effectuated Enrollment",
         "description": "Counts of effectuated Marketplace enrollments by state and plan year.",
         "modified": "2026-02-14"},
        {"identifier": "1829fdb3-a6f9-4036-856a-3af1f7828c8c",
         "title": "Health Insurance Exchanges Monthly Effectuated Enrollment",
         "description": "Monthly effectuated enrollment counts for the individual Marketplaces.",
         "modified": "2026-03-01"},
        {"identifier": "00000000-not-relevant",
         "title": "National Correct Coding Initiative Edits inpatient files",
         "description": "Provider billing code edit tables."},   # irrelevant → filtered
    ]
}

HCGOV_FEED = [
    {"title": "What if I'm unemployed and need health coverage?", "es-title": "Si estoy desempleado",
     "url": "/unemployed/", "bite": "Options after losing job-based coverage: Marketplace, Medicaid, COBRA deadlines."},
    {"title": "Formula 1 merchandise store", "url": "/racing/", "bite": "not-real-content"},  # taxonomy-filtered out
]


def make_modules(tmp):
    os.environ["SIGNALS_DB"] = str(tmp / "signals_test.db")
    import engine.signals as sig
    importlib.reload(sig)
    import engine.official_data as od
    importlib.reload(od)
    return sig, od


def test_cms_official_data_ingests_market_kind(tmp_path):
    sig, od = make_modules(tmp_path)
    con = sig.connect(tmp_path / "s.db")
    calls = []
    res = od.fetch_cms_open_data(con, get_json=lambda url: calls.append(url) or CMS_CATALOG,
                                 sleeper=lambda s: None)
    assert res["fetched"] == 2            # the coding-edits dataset never matched the filter
    assert res["accepted"] == 2
    assert [c for c in calls] == [od.CMS_CATALOG_URL]     # exactly ONE per-run catalog request
    rows = [dict(r) for r in con.execute("SELECT * FROM signals")]
    assert all(r["source"] == "official_cms" for r in rows)
    assert all(r["kind"] == "MARKET" for r in rows), "official datasets must be MARKET, never INTEREST"
    assert all(r["url"].startswith("https://data.cms.gov/dataset/") for r in rows)
    assert any("Effectuated Enrollment" in r["title"] for r in rows)
    # dedupe: a second identical run stores nothing new
    res2 = od.fetch_cms_open_data(con, get_json=lambda url: CMS_CATALOG, sleeper=lambda s: None)
    assert res2["fetched"] == 2 and res2["accepted"] == 0
    assert con.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 2


def test_hcgov_content_api_taxonomy_filtered(tmp_path):
    sig, od = make_modules(tmp_path)
    con = sig.connect(tmp_path / "s.db")
    res = od.fetch_healthcare_gov_content(con, get_json=lambda url: HCGOV_FEED, sleeper=lambda s: None)
    assert res["fetched"] == 2            # both FED through the fetcher
    assert res["accepted"] == 1           # only the unemployment/coverage page cleared taxonomy
    rows = [dict(r) for r in con.execute("SELECT * FROM signals")]
    assert rows and rows[0]["kind"] == "MARKET" and rows[0]["source"] == "hcgov"
    assert rows[0]["url"].startswith("https://www.healthcare.gov/")
    assert "cobra" in (rows[0]["snippet"] + rows[0]["title"]).lower() or "coverage" in (rows[0]["snippet"] + rows[0]["title"]).lower()


def test_official_collectors_fail_soft(tmp_path):
    sig, od = make_modules(tmp_path)
    con = sig.connect(tmp_path / "s.db")

    class Boom(Exception):
        pass

    def broken(url):
        raise Boom("network down")

    for fn in (od.fetch_cms_open_data, od.fetch_healthcare_gov_content):
        res = fn(con, get_json=broken, sleeper=lambda s: None)
        assert res == {"fetched": 0, "accepted": 0}
    logs = [dict(r) for r in con.execute("SELECT * FROM fetch_log")]
    assert len(logs) == 2 and all("Boom" in l["note"] for l in logs)
    # nothing was stored from the failed run
    assert con.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0


def test_demand_kind_split(tmp_path):
    sig, _ = make_modules(tmp_path)
    # community post with a taxonomy topic → INTEREST
    assert sig.demand_kind("reddit", "cobra") == "INTEREST"
    assert sig.demand_kind("reddit_fallback", "job_loss") == "INTEREST"
    # same post without ANY topic hit → MARKET (unclassified chatter is not interest)
    assert sig.demand_kind("reddit", "none") == "MARKET"
    # news, official data and platform content is always intelligence
    for s in ("google_news", "official_cms", "hcgov", "youtube", "instagram"):
        assert sig.demand_kind(s, "cobra") == "MARKET"
