"""Consumer acquisition pipeline tests (engine/network.py, engine/leadgen.py, public directory).

The boundaries under test, in the order they protect the business:
  1. A verified ORGANIZATION becomes a referral partner - never a consumer lead, and never
     co-branded before the organization itself approves.
  2. Rebuilding the network is idempotent and reports every skipped row with a reason, so a thin
     run can never be mistaken for a complete one.
  3. The public directory only lists verified, unique, in-state rows that publish a phone.
  4. "Consented" requires all three facts (consumer-submitted phone, valid phone, stored consent
     record). A phone without the record is not a consented lead - that is the guard that keeps a
     future integration from quietly inventing consent.
  5. "Eligible for buyer routing" is 0 when no usable buyer exists, and becomes real the moment one
     does. The blocker list names that dependency instead of pretending the funnel is fine.
  6. TEST traffic never enters the real stages.
Bookkeeping only: synthetic rows, no network, no delivery, no payments.
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import leadgen, network                       # noqa: E402
from engine.db import connect                             # noqa: E402

TS = "2026-10-29T12:00:00"


# ------------------------------------------------------------------ fixtures
ORG_SCHEMA = """CREATE TABLE org_contacts (
  org_key TEXT UNIQUE, org_name TEXT, org_type TEXT, source TEXT, source_url TEXT,
  record_ids TEXT, phones TEXT, address TEXT, purpose_note TEXT, terms_note TEXT,
  record_kind TEXT, verify_status TEXT, dup_status TEXT)"""


def org_db(tmp, rows) -> Path:
    p = tmp / "org_contacts.db"
    con = sqlite3.connect(str(p))
    con.execute(ORG_SCHEMA)
    for i, r in enumerate(rows):
        base = {"org_key": f"key{i:03d}", "org_type": "clinic", "source": "hrsa_health_centers",
                "source_url": "https://data.hrsa.gov/x.csv", "record_ids": '{"id": "A1"}',
                "phones": json.dumps([{"value": "305-555-0100", "e164": "+13055550100",
                                       "evidence": "site telephone"}]) if r.get("phone", True) else "[]",
                "address": "100 Main St, Miami, FL, 33125", "purpose_note": "public phone",
                "terms_note": "public directory", "record_kind": "PUBLIC_CONTACT_VERIFIED",
                "verify_status": "VERIFIED_PRIMARY", "dup_status": "UNIQUE"}
        base.update({k: v for k, v in r.items() if k != "phone"})
        base["org_name"] = r.get("org_name") or f"Clinic {i}"
        con.execute(f"INSERT INTO org_contacts ({','.join(base)}) VALUES ({','.join('?' * len(base))})",
                    list(base.values()))
    con.commit()
    con.close()
    return p


def cfg_dir(tmp, buyers=None) -> Path:
    d = tmp / "cfg"
    d.mkdir(parents=True, exist_ok=True)
    (d / "buyers.json").write_text(json.dumps(buyers if buyers is not None else []), encoding="utf-8")
    (d / "site.json").write_text(json.dumps({"served_states": ["FL"], "consent_reviewed_by_counsel": False}),
                                 encoding="utf-8")
    return d


REAL_BUYER = {"id": "agency-real", "name": "Real Agency LLC", "is_demo": False, "active": True,
              "priority": 1, "licensed_states": ["FL"], "products": ["health_aca"],
              "age_min": 18, "age_max": 95, "tiers": ["A"], "daily_cap": 10,
              "price_by_tier": {"A": 55}, "delivery": {"method": "outbox"}}


def lead(con, lid, *, phone_valid=1, consent=True, qual="QUALIFIED", product="health_aca",
         routing="PENDING", test=0, delivered=False):
    con.execute("""INSERT INTO leads (lead_id, created_at, is_test, product, state, lead_type,
                   phone, phone_e164, phone_valid, phone_source, consent_ts, qual_status,
                   routing_status, buyer_id, price, delivered_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (lid, TS, test, product, "FL", "PHONE_LEAD" if phone_valid else "EMAIL_ONLY_INQUIRY",
                 "3055550110" if phone_valid else None, "+13055550110" if phone_valid else None,
                 phone_valid, "CONSUMER_SUBMITTED" if phone_valid else None,
                 TS if consent else None, qual, "DELIVERED" if delivered else routing,
                 "agency-real" if delivered else None, 55.0 if delivered else None,
                 TS if delivered else None))
    if consent and phone_valid:
        con.execute("""INSERT INTO consents (lead_id, ts, consent_version, consent_text, seller_named,
                       checkbox_checked, scope) VALUES (?,?,?,?,?,?,?)""",
                    (lid, TS, "v4", "I agree that GS Allude may share my information with a licensed agency.",
                     "Real Agency LLC", 1, "PHONE_AND_EMAIL"))


# ------------------------------------------------------------------ 1. org -> partner, never a lead
def test_verified_organization_becomes_gated_partner_never_a_consumer_lead(tmp_path):
    con = connect(tmp_path / "leads.db")
    orgs = org_db(tmp_path, [{"org_name": "Bayside Community Health"}])
    res = network.build(con, orgs, state="FL")
    assert res["stats"]["partners_created"] == 1
    p = con.execute("SELECT * FROM partners").fetchone()
    row = dict(p)
    assert row["partner_id"].startswith("net-") and row["cobrand_approved"] == 0
    assert "Bayside Community Health" in row["display_name"] and "(Miami)" in row["display_name"]
    assert "data.hrsa.gov" in row["evidence"]              # the operator can re-check the source
    assert network.partners_mod.stage_of(row) == "IDENTIFIED"
    # the org ledger never creates consumer rows
    assert con.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM consents").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM visitors").fetchone()[0] == 0


def test_build_is_idempotent_and_accounts_for_every_skip(tmp_path):
    con = connect(tmp_path / "leads.db")
    orgs = org_db(tmp_path, [
        {"org_name": "Good Clinic"},
        {"org_name": "No Phone Clinic", "phone": False},
        {"org_name": "Unverified Clinic", "record_kind": "PUBLIC_CONTACT_DISCOVERED", "verify_status": ""},
        {"org_name": "Duplicate Clinic", "dup_status": "DUPLICATE"},
        {"org_name": "Georgia Clinic", "address": "10 Peachtree St, Atlanta, GA, 30303"},
    ])
    first = network.build(con, orgs, state="FL")["stats"]
    assert (first["records_considered"], first["partners_created"]) == (5, 1)
    assert (first["skipped_no_phone"], first["skipped_unverified"], first["skipped_duplicate"],
            first["skipped_not_health"]) == (1, 1, 1, 1)
    second = network.build(con, orgs, state="FL")["stats"]
    assert (second["partners_created"], second["partners_existing"]) == (0, 1)
    assert con.execute("SELECT COUNT(*) FROM partners").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM network_runs").fetchone()[0] == 2   # both runs measured


def test_operator_edits_survive_a_rebuild(tmp_path):
    con = connect(tmp_path / "leads.db")
    orgs = org_db(tmp_path, [{"org_name": "Good Clinic"}])
    network.build(con, orgs, state="FL")
    pid = con.execute("SELECT partner_id FROM partners").fetchone()[0]
    con.execute("UPDATE partners SET status='ACTIVATED', stage='ACTIVATED', cobrand_approved=1 WHERE partner_id=?", (pid,))
    con.commit()
    network.build(con, orgs, state="FL")
    row = con.execute("SELECT status, stage, cobrand_approved FROM partners WHERE partner_id=?", (pid,)).fetchone()
    assert tuple(row) == ("ACTIVATED", "ACTIVATED", 1)


# ------------------------------------------------------------------ 2. kits
def test_kit_is_copy_ready_and_claims_no_affiliation(tmp_path):
    con = connect(tmp_path / "leads.db")
    network.build(con, org_db(tmp_path, [{"org_name": "Good Clinic"}]), state="FL")
    p = dict(con.execute("SELECT * FROM partners").fetchone())
    k = network.kit(p, "https://example.org")
    assert k["link"] == f"https://example.org/p/{p['partner_id']}"
    assert "/check" not in k["card_en"] and "example.org/p/" in k["card_en"]
    assert "Nobody calls you unless you ask" in k["card_en"]
    assert "not a government agency" in k["card_en"]
    assert "nadie te llama a menos que" in k["card_es"].lower()
    assert k["cobrand_approved"] is False
    assert "nothing" in k["note"].lower()


def test_kit_export_marks_every_row_as_needing_approval(tmp_path):
    con = connect(tmp_path / "leads.db")
    network.build(con, org_db(tmp_path, [{"org_name": "A Clinic"}, {"org_name": "B Clinic"}]), state="FL")
    out = tmp_path / "kits.csv"
    res = network.export_kits(con, "https://example.org", "Guía Salud", out)
    assert res["partners"] == 2
    rows = list(__import__("csv").DictReader(out.open(encoding="utf-8-sig")))
    assert len(rows) == 2 and all("APPROVAL REQUIRED" in r["approval"] for r in rows)
    assert all(r["phone"] == "305-555-0100" for r in rows)
    assert all(r["cobrand_approved"] == "0" for r in rows)


# ------------------------------------------------------------------ 3. public directory
def test_directory_lists_only_verified_in_state_rows_with_a_phone(tmp_path):
    orgs = org_db(tmp_path, [
        {"org_name": "Miami Clinic"}, {"org_name": "Miami Clinic 2", "address": "9 Ocean Dr, Miami, FL, 33139"},
        {"org_name": "No Phone", "phone": False},
        {"org_name": "Unverified", "record_kind": "PUBLIC_CONTACT_DISCOVERED", "verify_status": ""},
        {"org_name": "Duplicate", "dup_status": "DUPLICATE"},
        {"org_name": "Tampa Clinic", "address": "1 Bay St, Tampa, FL, 33602"},
        {"org_name": "Not A Consumer Org", "org_type": "staffing"},
    ])
    d = network.directory(orgs, state="FL")
    names = {r["name"] for r in d["rows"]}
    assert names == {"Miami Clinic", "Miami Clinic 2", "Tampa Clinic"}
    assert d["total"] == 3 and d["grand_total"] == 3 and d["cities"] == ["Miami", "Tampa"]
    assert all(r["phone"] for r in d["rows"])
    m = network.directory(orgs, state="FL", city="miami")
    assert m["total"] == 2 and {r["city"] for r in m["rows"]} == {"Miami"}
    assert m["grand_total"] == 3                       # filter total never hides the real universe
    p = network.directory(orgs, state="FL", per_page=2, page=2)
    assert len(p["rows"]) == 1 and p["pages"] == 2


# ------------------------------------------------------------------ 4 + 5. stages and gate
def test_consent_record_is_required_for_the_consented_stage(tmp_path):
    con = connect(tmp_path / "leads.db")
    lead(con, "L1", consent=True)          # full evidence
    lead(con, "L2", consent=False)         # phone, valid, no consent record
    con.commit()
    st = leadgen.stage_counts(con)
    assert st["submitted"] == 2
    assert st["consented"] == 1            # the guard, not the phone count
    assert st["contact_verified"] == 1
    assert st["qualified"] == 1


def test_routing_eligibility_depends_on_a_usable_buyer_existing(tmp_path):
    con = connect(tmp_path / "leads.db")
    lead(con, "L1", consent=True)
    con.commit()
    none = leadgen.report(con, cfg_dir(tmp_path, []))
    assert none["stages"]["routing_eligible"] == 0
    assert any(b["key"] == "named_buyer" and b["blocking"] for b in none["blockers"])
    assert none["gate"]["routable_products"] == []
    demo_only = leadgen.report(con, cfg_dir(tmp_path / "d", [dict(REAL_BUYER, is_demo=True)]))
    assert demo_only["gate"]["buyers_real"] == 0 and demo_only["stages"]["routing_eligible"] == 0
    real = leadgen.report(con, cfg_dir(tmp_path / "r", [REAL_BUYER]))
    assert real["gate"]["routable_products"] == ["health_aca"]
    assert real["stages"]["routing_eligible"] == 1
    assert not any(b["key"] == "named_buyer" and b["blocking"] for b in real["blockers"])


def test_test_traffic_never_enters_the_real_stages(tmp_path):
    con = connect(tmp_path / "leads.db")
    lead(con, "REAL1", consent=True)
    lead(con, "TEST1", consent=True, test=1)
    con.commit()
    real = leadgen.stage_counts(con, 0)
    test = leadgen.stage_counts(con, 1)
    assert (real["submitted"], real["consented"], real["qualified"]) == (1, 1, 1)
    assert (test["submitted"], test["consented"], test["qualified"]) == (1, 1, 1)


def test_demo_buyer_delivery_never_counts_as_purchased(tmp_path):
    """A demo placeholder never pays, so its delivery is reported as suspect REAL data instead of
    inflating the only number a buyer cares about."""
    con = connect(tmp_path / "leads.db")
    lead(con, "L1", consent=True)
    con.execute("UPDATE leads SET routing_status='DELIVERED', buyer_id='demo-health-agent', price=35 WHERE lead_id='L1'")
    con.commit()
    st = leadgen.stage_counts(con)
    assert st["delivered"] == 0 and st["demo_delivered"] == 1


def test_delivered_stage_comes_from_routing_not_from_consent(tmp_path):
    con = connect(tmp_path / "leads.db")
    lead(con, "L1", consent=True)
    lead(con, "L2", consent=True, delivered=True)
    con.commit()
    st = leadgen.stage_counts(con)
    assert st["consented"] == 2 and st["delivered"] == 1


# ------------------------------------------------------------------ 6. admin surface
def test_admin_leadgen_page_requires_token_and_renders(tmp_path):
    from test_billing import make_app
    m = make_app(tmp_path)
    lead(m.con, "L1", consent=True)
    m.con.commit()
    c = m.app.test_client()
    assert c.get("/admin/leadgen").status_code == 403
    page = c.get("/admin/leadgen?token=t")
    body = page.get_data(as_text=True)
    assert page.status_code == 200
    for label in ("Visitors", "Started inquiries", "Consented leads", "Verified contact details",
                  "Qualified leads", "Eligible for buyer routing", "Purchased &amp; delivered"):
        assert label in body, label
    assert "Buyer gate" in body and "Referral network" in body
    csv_body = c.get("/admin/leadgen.csv?token=t").get_data(as_text=True)
    assert csv_body.splitlines()[0] == "stage,label,count,pct_of_visitors,definition"
    assert "routing_eligible" in csv_body


def test_public_directory_pages_render_and_are_in_the_sitemap(tmp_path):
    from test_billing import make_app
    m = make_app(tmp_path)
    m.ORG_DB = org_db(tmp_path, [{"org_name": "Bayside Community Health"}])
    c = m.app.test_client()
    for url in ("/free-help", "/ayuda-gratis", "/free-help?city=Miami"):
        r = c.get(url)
        assert r.status_code == 200, url
        assert "Bayside Community Health" in r.get_data(as_text=True), url
    assert "MedicalClinic" in c.get("/free-help").get_data(as_text=True)
    xml = c.get("/sitemap.xml").get_data(as_text=True)
    assert "/free-help" in xml and "/ayuda-gratis" in xml


def test_directory_page_never_shows_unverified_rows(tmp_path):
    from test_billing import make_app
    m = make_app(tmp_path)
    m.ORG_DB = org_db(tmp_path, [
        {"org_name": "Listed Clinic"},
        {"org_name": "Hidden Unverified", "record_kind": "PUBLIC_CONTACT_DISCOVERED", "verify_status": ""},
    ])
    body = m.app.test_client().get("/free-help").get_data(as_text=True)
    assert "Listed Clinic" in body and "Hidden Unverified" not in body


def test_report_is_read_only(tmp_path):
    """A reporting call must never create, route, sell or touch a lead."""
    con = connect(tmp_path / "leads.db")
    lead(con, "L1", consent=True)
    con.commit()
    before = [tuple(r) for r in con.execute("SELECT * FROM leads")]
    con2 = connect(tmp_path / "leads.db")
    leadgen.report(con2, cfg_dir(tmp_path, [REAL_BUYER]))
    after = [tuple(r) for r in con2.execute("SELECT * FROM leads")]
    assert before == after
    assert con2.execute("SELECT COUNT(*) FROM consents").fetchone()[0] == 1
