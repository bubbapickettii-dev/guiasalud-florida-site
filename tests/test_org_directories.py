"""Focused tests for the official HRSA health-center bulk adapter (organization side).

Boundaries under test:
  1. Only ACTIVE sites in the requested state are accepted, and only with a usable published
     phone (a contactless row is never stored).
  2. Look-alike sites are excluded by default; the official identifiers are retained.
  3. Records are ORGANIZATION contacts: they land in org_contacts and never in the consumer
     prospect store.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engine.org_contacts as org          # noqa: E402
import engine.prospect_contacts as pcon    # noqa: E402

HEADER = ("Health Center Type,Health Center Number,BPHC Assigned Number,Site Name,Site Address,"
          "Site City,Site State Abbreviation,Site Postal Code,Site Telephone Number,"
          "Site Status Description,FQHC Site NPI Number,Health Center Name,"
          "Site Added to Scope this Date")

CSV = "\n".join([
    HEADER,
    # kept: active FL FQHC with a real phone
    'Federally Qualified Health Center (FQHC),H80CS1,BPS-H80-1,Duval Family Health Center,'
    '611 E Adams St,Jacksonville,FL,32202,904-394-8100,Active,101096,Duval,2021-01-04',
    # skipped: inactive
    'Federally Qualified Health Center (FQHC),H80CS2,BPS-H80-2,Closed Site,1 Main St,Miami,FL,'
    '33101,305-555-0111,Inactive,101097,Closed,2019-01-01',
    # skipped: other state
    'Federally Qualified Health Center (FQHC),H80CS3,BPS-H80-3,Georgia Site,9 Peachtree St,Atlanta,'
    'GA,30301,404-555-0122,Active,101098,Georgia,2020-01-01',
    # skipped: look-alike by default
    'FQHC Look-Alike,H80CS4,BPS-H80-4,Look Alike Site,2 Bay St,Tampa,FL,33602,813-555-0133,Active,'
    '101099,LookAlike,2022-01-01',
    # skipped: no phone at all
    'Federally Qualified Health Center (FQHC),H80CS5,BPS-H80-5,No Phone Site,3 Oak St,Orlando,FL,'
    '32801,,Active,101100,NoPhone,2023-01-01',
    # kept: a second distinct site
    'Federally Qualified Health Center (FQHC),H80CS6,BPS-H80-6,Beach Community Health,4 Surf Rd,'
    'Miami Beach,FL,33139,(305) 555-0144,Active,101101,Beach,2022-06-01',
])


def _fetcher(rows=CSV):
    def fetch_text(url):
        if url.endswith("robots.txt"):
            return "User-agent: *\nAllow: /\n"
        return rows
    return fetch_text


def test_hrsa_active_state_sites_with_phone(tmp_path):
    con = org.connect(tmp_path / "org.db")
    res = org.fetch_hrsa_sites(con, fetch_text=_fetcher(), state="FL")
    assert res["fetched"] == 6, res
    assert res["accepted"] == 2 and res["errors"] == 0, res
    names = {r["org_name"] for r in con.execute("SELECT org_name FROM org_contacts")}
    assert names == {"Duval Family Health Center", "Beach Community Health"}
    row = dict(con.execute(
        "SELECT * FROM org_contacts WHERE org_name='Duval Family Health Center'").fetchone())
    assert row["org_type"] == "clinic"
    assert row["record_kind"] == org.KIND_VERIFIED
    assert row["verify_status"] == "VERIFIED_PRIMARY"
    assert "+19043948100" in row["phones"]
    assert "BPS-H80-1" in row["record_ids"]
    assert "never a consumer record" in row["terms_note"]
    # idempotent: the same file re-run adds nothing new
    again = org.fetch_hrsa_sites(con, fetch_text=_fetcher(), state="FL")
    assert again["accepted"] == 0 and again["duplicates"] == 2, again


def test_hrsa_lookalikes_can_be_included_explicitly(tmp_path):
    con = org.connect(tmp_path / "org2.db")
    res = org.fetch_hrsa_sites(con, fetch_text=_fetcher(), state="FL", include_lookalikes=True)
    assert res["accepted"] == 3, res


def test_hrsa_robots_disallow_stops_the_download(tmp_path):
    con = org.connect(tmp_path / "org3.db")

    def fetch_text(url):
        if url.endswith("robots.txt"):
            return "User-agent: *\nDisallow: /\n"
        raise AssertionError("bulk file must not be fetched when robots disallows")

    res = org.fetch_hrsa_sites(con, fetch_text=fetch_text, state="FL")
    assert res.get("restricted") is True and res["accepted"] == 0, res


def test_org_records_never_enter_the_consumer_store(tmp_path):
    org_con = org.connect(tmp_path / "org4.db")
    ppl = pcon.connect(tmp_path / "people4.db")
    org.fetch_hrsa_sites(org_con, fetch_text=_fetcher(), state="FL")
    assert org_con.execute("SELECT COUNT(*) FROM org_contacts").fetchone()[0] == 2
    assert ppl.execute("SELECT COUNT(*) FROM prospect_people").fetchone()[0] == 0
