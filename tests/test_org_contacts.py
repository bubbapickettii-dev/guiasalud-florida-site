"""Focused tests for engine/org_contacts.py + tools/org_pipeline.py (offline, injected fetchers).

Covers: phone normalization, NPI adapter (official registry records -> VERIFIED org rows, dedupe),
robots-gated seed extraction (mailto/tel only; Disallow skipped and recorded; system mailboxes
excluded; individual personal posts never captured here), recheck verification, per-source
performance ranking, and the partner-sync classification boundary (partner rows created as
'DISCOVERED - not contacted'; nothing consumer-side is created, consumed, or exported).
Run: python -m pytest tests/test_org_contacts.py -q   (or python tests/test_org_contacts.py)
"""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import org_contacts as org          # noqa: E402


# ---------------------------------------------------------------- helpers
def tmp_con() -> sqlite3.Connection:
    fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False, dir=tempfile.gettempdir())
    fd.close()
    Path(fd.name).unlink()
    return org.connect(fd.name)


def tmp_con_path() -> Path:
    fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False, dir=tempfile.gettempdir())
    fd.close()
    Path(fd.name).unlink()
    return Path(fd.name)


def test_norm_phone():
    ok = org.norm_phone("(305) 555-0142")
    assert ok == "+13055550142", ok
    assert org.norm_phone("1-813-555-0133") == "+18135550133"
    assert org.norm_phone("0555 011") is None          # not 10 digits
    assert org.norm_phone("+3025550111 x2") is None    # wrong shape
    assert org.norm_phone("1111111111") is None        # fake block
    assert org.norm_phone("(305) 555-0142").startswith("+1")


def _npi_payload(results):
    return {"result_count": len(results), "results": results}


def _addr(purpose, tel, city="Tampa", state="FL", zip="33602"):
    return {"address_purpose": purpose, "telephone_number": tel,
            "city": city, "state": state, "postal_code": zip}


def test_npi_adapter_offline():
    con = tmp_con()
    calls = []

    def fake_json(url):
        calls.append(url)
        if "Federally+Qualified" in url or "Federally%20Qualified" in url:
            return _npi_payload([
                {"number": 1508000001, "enumeration_type": "NPI-2",
                 "basic": {"organization_name": "SUNSHINE COMMUNITY CLINIC INC"},
                 "addresses": [_addr("LOCATION", "(305) 555-0142"), _addr("MAILING", "(305) 555-9999")],
                 "taxonomies": [{"desc": "Federally Qualified Health Center (FQHC)"}]},
                {"number": 1508000002, "enumeration_type": "NPI-2",
                 "basic": {"organization_name": "NO PHONE ORG"},
                 "addresses": [_addr("LOCATION", "")], "taxonomies": [{"desc": "Freestanding Clinic"}]},
            ])
        return _npi_payload([])

    def fake_sleep(_):
        return None

    res = org.fetch_npi_organizations(con, get_json=fake_json, sleeper=fake_sleep, taxonomies=org.NPI_TAXONOMIES)
    row = con.execute("SELECT * FROM org_contacts").fetchall()
    assert len(row) == 1, "only the org with a usable phone is stored"
    org_row = dict(con.execute("SELECT * FROM org_contacts").fetchone())
    assert org_row["org_name"] == "Sunshine Community Clinic Inc"
    phones = json.loads(org_row["phones"])
    assert phones[0]["e164"] == "+13055550142", phones        # LOCATION phone wins over MAILING
    assert phones[0]["evidence"] and "NPPES" in phones[0]["evidence"]
    rec_ids = json.loads(org_row["record_ids"])
    assert rec_ids["npi"] == "1508000001"
    assert org_row["record_kind"] == "PUBLIC_CONTACT_VERIFIED"       # official registry of record
    assert org_row["source"] == "npi_registry" and "npiregistry.cms.hhs.gov/api" in org_row["source_url"]
    assert res["fetched"] == 2 and res["accepted"] == 1 and res["duplicates"] == 1
    # rerun is idempotent (dedupe by org_key)
    con.execute("DELETE FROM source_metrics")
    res2 = org.fetch_npi_organizations(con, get_json=fake_json, sleeper=fake_sleep, taxonomies=org.NPI_TAXONOMIES)
    assert res2["accepted"] == 0 and res2["duplicates"] == 2
    assert con.execute("SELECT COUNT(*) FROM org_contacts").fetchone()[0] == 1
    # metrics recorded per-source
    m = con.execute("SELECT SUM(errors) e, SUM(records_fetched) f FROM source_metrics WHERE source='npi_registry'").fetchone()
    assert m["f"] >= 4 and m["e"] == 0


def test_seed_extraction_rules():
    con = tmp_con()
    page = """<!doctype html><html><body>
      <h3>Charities of Hope Inc</h3>
      <p>Food pantry and case services. <a href="mailto:info@charitiesofhope.org">email</a>
         <a href="tel:+13055550155">(305) 555-0155</a></p>
      <h3>Webmaster Info</h3><p><a href="mailto:noreply@charitiesofhope.org">noreply</a></p>
      <h3>Unrelated Text</h3><p>call 555-0110 please</p>
      mostly free prose without headers mentioning help volunteers can phone us at (000) 000-0000
      </body></html>"""
    robots = "User-agent: *\nDisallow:\nCrawl-delay: 10\nai-train: no\n"

    def fake_text(url):
        return robots if url.endswith("robots.txt") else page

    def fake_sleep(s):
        assert s >= 10, "crawl-delay must be honored"

    seed = {"url": "https://example.org/contact", "org_type": "nonprofit", "lang": "en"}
    r = org.fetch_org_seed(con, seed, fetch_text=fake_text, sleeper=fake_sleep)
    row = dict(con.execute("SELECT * FROM org_contacts").fetchone())
    assert r["accepted"] == 1, r
    emails = json.loads(row["emails"])
    phones = json.loads(row["phones"])
    assert [e["value"] for e in emails] == ["info@charitiesofhope.org"]   # noreply excluded
    assert [p["e164"] for p in phones] == ["+13055550155"]               # prose 555-0110 not org-attached
    assert row["terms_note"].find("ai-train=no") >= 0                    # source terms recorded
    assert row["record_kind"] == "PUBLIC_CONTACT_DISCOVERED"
    # a robots full-ban skips the seed and records the restriction
    con2 = tmp_con()
    ban_page = "User-agent: *\nDisallow: /\n"

    def banned_text(url):
        return ban_page if url.endswith("robots.txt") else page

    r2 = org.fetch_org_seed(con2, seed, fetch_text=banned_text, sleeper=fake_sleep)
    assert r2.get("restricted") is True
    assert con2.execute("SELECT COUNT(*) FROM org_contacts").fetchone()[0] == 0
    assert con2.execute("SELECT COUNT(*) FROM source_metrics").fetchone()[0] == 1


def test_verify_recheck():
    con = tmp_con()
    page = "Hope Center Inc (305) 555-0155 info@hopecenter.org"
    page_robots = "User-agent: *\nDisallow:\n"

    def fake_text(url):
        return page_robots if url.endswith("robots.txt") else page

    org.fetch_org_seed(con, {"url": "https://example.org/contact"}, fetch_text=fake_text, sleeper=lambda s: None)
    row = con.execute("SELECT * FROM org_contacts").fetchone()
    assert row["record_kind"] == "PUBLIC_CONTACT_DISCOVERED"
    res = org.verify_contacts(con, fetch_text=lambda url: page)
    row = con.execute("SELECT * FROM org_contacts").fetchone()
    assert res["verified"] == 1 and row["record_kind"] == "PUBLIC_CONTACT_VERIFIED"
    assert "rechecked" in row["verification_note"].lower()
    # changed page must NOT verify
    con2 = tmp_con()
    org.fetch_org_seed(con2, {"url": "https://example.org/contact"}, fetch_text=fake_text, sleeper=lambda s: None)
    res2 = org.verify_contacts(con2, fetch_text=lambda url: "phone changed to (404) 000-1111")
    row2 = con2.execute("SELECT * FROM org_contacts").fetchone()
    assert res2["verified"] == 0 and row2["record_kind"] == "PUBLIC_CONTACT_DISCOVERED"


# ---------------------------------------------------------------- pipeline CLI
def test_partner_sync_b():
    import tools.org_pipeline as pipe
    org_path = tmp_con_path()
    org_db = org.connect(org_path)
    page = "Hope Center Inc (305) 555-0155 info@hopecenter.org"
    robots = "User-agent: *\nDisallow:\n"
    fake_text = lambda url: robots if url.endswith("robots.txt") else page   # noqa: E731
    org.fetch_org_seed(org_db, {"url": "https://example.org/contact"}, fetch_text=fake_text, sleeper=lambda s: None)
    org.verify_contacts(org_db, fetch_text=lambda url: page)
    # partner sync writes VERIFIED org rows as 'DISCOVERED - not contacted' into an ISOLATED leads db
    lead_path = tmp_con_path()
    import engine.db as lead_db_mod
    lead_con = lead_db_mod.connect(lead_path)          # creates the partners schema
    old_leads, old_org = pipe.LEAD_DB, pipe.ORG_DB
    pipe.LEAD_DB, pipe.ORG_DB = lead_path, org_path
    try:
        rc = pipe.cmd_partner_sync()
        after = lead_con.execute("SELECT COUNT(*) FROM partners").fetchone()[0]
        assert rc == 0 and after == 1
        p = dict(lead_con.execute("SELECT * FROM partners").fetchone())
        assert p["partner_id"].startswith("org-")
        assert p["status"] == "DISCOVERED - not contacted"
        assert p["cobrand_approved"] == 0
        assert "Outreach requires explicit operator approval" in p["contact_hint"]
        assert "+13055550155" in p["contact_hint"] and "info@hopecenter.org" in p["contact_hint"]
        assert "example.org/contact" in p["contact_hint"]
        # second sync is idempotent (same org_key partner_id)
        rc2 = pipe.cmd_partner_sync()
        assert rc2 == 0 and lead_con.execute("SELECT COUNT(*) FROM partners").fetchone()[0] == 1
    finally:
        pipe.LEAD_DB, pipe.ORG_DB = old_leads, old_org


def test_perf_rank_ordering():
    con = tmp_con()
    org._metric(con, "good_source", 10, 10, 10, 9, 1, 0, "", "")
    org._metric(con, "noisy_source", 1000, 2, 2, 1, 1, 3, "rate limited", "")
    rows = org.perf_rank(con)
    assert rows[0]["source"] == "good_source" and rows[1]["source"] == "noisy_source"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failed else 0)
