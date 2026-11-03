"""Imported-contact admission: third-party records are HELD, never assumed consented.

The boundary under test: `imported_contacts` may describe a real person with a phone and an
email, and still must never be routable, sellable, or relabelled as a consumer submission. The
only door to eligibility is the person's own /check submission (a lead + its consent row) being
linked back to the record.

Offline tests only — no network, no scrapers, no sends.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import admission as adm                # noqa: E402
from engine import route as rt                     # noqa: E402


def _db():
    return os.path.join(tempfile.mkdtemp(), "leads.db")


def con():
    return adm.connect(_db())


SRC = {"id": "panel-2026-04", "name": "Licensed panel export", "url": "https://example.test/panel",
       "license": "vendor agreement §3 (inbound follow-up only)"}


def _lead(con, lead_id="L-0001", phone="+13055550142", email="jane@example.com", with_consent=True,
          qual="QUALIFIED", lead_type="PHONE_LEAD"):
    """A REAL consumer submission: the only way one exists in this system."""
    con.execute(
        "INSERT INTO leads (lead_id, created_at, first_name, last_name, email, phone, phone_e164,"
        " phone_source, state, zip, product, age_range, tier, qual_status, lead_type, contact_scope,"
        " routing_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (lead_id, adm.now(), "Jane", "Doe", email, phone, phone, "CONSUMER_SUBMITTED", "FL", "33101",
         "health_aca", "30-39", "A", qual, lead_type, "PHONE_AND_EMAIL", "PENDING"))
    if with_consent:
        con.execute(
            "INSERT INTO consents (lead_id, ts, consent_version, consent_text, seller_named,"
            " checkbox_checked, scope, ip, page_url, phone, email) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (lead_id, adm.now(), "v3", "I agree GS Allude may share my details with one licensed "
             "agency.", "GS Allude", 1, "PHONE_AND_EMAIL", "127.0.0.1", "/check", phone, email))
    con.commit()


def _row(**kw):
    base = {"name": "Jane Doe", "email": "jane@example.com", "phone": "(305) 555-0142",
            "state": "FL", "city": "Miami", "collected_at": "2026-03-11", "id": "rec-1"}
    base.update(kw)
    return base


# ------------------------------------------------------------------ possession != permission
def test_import_creates_a_record_but_no_lead_and_no_consent():
    c = con()
    r = adm.import_rows(c, SRC, [_row()])
    assert r["imported"] == 1 and r["received"] == 1
    row = dict(c.execute("SELECT * FROM imported_contacts").fetchone())
    assert row["phone_e164"] == "+13055550142" and row["email"] == "jane@example.com"
    assert row["source_id"] == "panel-2026-04" and row["collected_at"] == "2026-03-11"
    assert row["marketing_eligibility"] == "NOT_ELIGIBLE"
    assert row["lead_id"] == "" and row["consent_ref"] == ""
    # the two populations stay separate: importing created nothing saleable
    assert c.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM consents").fetchone()[0] == 0
    assert "no permission basis" in adm.ineligible_reason(row)


def test_source_id_is_required():
    c = con()
    with pytest.raises(ValueError):
        adm.import_rows(c, {"name": "anonymous"}, [_row()])


def test_row_without_identity_is_rejected_and_row_without_contact_is_kept():
    c = con()
    r = adm.import_rows(c, SRC, [{"notes": "no idea who this is"},
                                 {"name": "Marcus Webb", "state": "FL"}])
    assert r["rejected"] == 1 and r["imported"] == 1
    assert r["missing_contact"] == 1
    row = dict(c.execute("SELECT * FROM imported_contacts").fetchone())
    assert row["full_name"] == "Marcus Webb" and row["has_contact"] == 0
    assert row["contact_key"].startswith("n:")          # dedupe anchor: name + state
    assert "no phone or email" in adm.ineligible_reason(row)
    assert r["warnings"] == []                          # SRC does declare a licence basis


def test_licence_is_recorded_and_a_missing_one_warns():
    c = con()
    r = adm.import_rows(c, {"id": "scrape-x"}, [_row(phone="3055559999", email="")])
    assert r["imported"] == 1 and r["warnings"]
    row = dict(c.execute("SELECT * FROM imported_contacts").fetchone())
    assert row["license"] == ""
    assert row["marketing_eligibility"] == "NOT_ELIGIBLE"


# ------------------------------------------------------------------ dedupe preserves evidence
def test_reimport_marks_a_duplicate_and_changes_nothing_on_the_original():
    c = con()
    adm.import_rows(c, SRC, [_row()])
    orig = dict(c.execute("SELECT * FROM imported_contacts").fetchone())
    # promote first, so we can prove a re-import cannot disturb consent evidence
    _lead(c)
    assert adm.promote(c, orig["id"], "L-0001")["ok"]
    before = dict(c.execute("SELECT * FROM imported_contacts WHERE id=?", (orig["id"],)).fetchone())
    assert before["marketing_eligibility"] == "ELIGIBLE_CONSENTED"

    r2 = adm.import_rows(c, {"id": "other-panel", "license": "x"}, [_row(phone="305-555-0142")])
    assert r2["duplicates"] == 1 and r2["imported"] == 0
    after_orig = dict(c.execute("SELECT * FROM imported_contacts WHERE id=?", (orig["id"],)).fetchone())
    for k in ("marketing_eligibility", "lead_id", "consent_ref", "phone", "email", "source_id",
              "collected_at", "eligibility_reason"):
        assert after_orig[k] == before[k], k
    dup = dict(c.execute("SELECT * FROM imported_contacts WHERE dup_status='DUPLICATE'").fetchone())
    assert dup["dup_of"] == orig["id"] and dup["contact_key"] is None
    assert dup["marketing_eligibility"] == "NOT_ELIGIBLE" and dup["lead_id"] == ""
    assert adm.counts(c)["unique"] == 1


def test_imported_evidence_is_immutable():
    c = con()
    adm.import_rows(c, SRC, [_row()])
    cid = c.execute("SELECT id FROM imported_contacts").fetchone()[0]
    with pytest.raises(Exception) as e:
        c.execute("UPDATE imported_contacts SET phone='3055550000' WHERE id=?", (cid,))
    assert "immutable" in str(e.value)


def test_dedupe_prefers_email_then_phone():
    c = con()
    adm.import_rows(c, SRC, [_row(name="Jane Doe", email="Jane@Example.com", phone="3055550142"),
                             _row(name="J. Doe", email="jane@example.com", phone="3055550142"),
                             _row(name="Other Person", email="", phone="305-555-7788"),
                             _row(name="Other Person", email="", phone="+1 305 555 7788")])
    c_unique = {r["contact_key"] for r in c.execute(
        "SELECT contact_key FROM imported_contacts WHERE contact_key IS NOT NULL")}
    assert c_unique == {"e:jane@example.com", "p:3055557788"}


# ------------------------------------------------------------------ never routable, never sold
def test_imported_record_can_never_become_a_routable_lead():
    c = con()
    adm.import_rows(c, SRC, [_row()])
    row = dict(c.execute("SELECT * FROM imported_contacts").fetchone())
    # (a) the imported number cannot be written as a lead at all (db.py trigger)
    with pytest.raises(Exception) as e:
        c.execute("INSERT INTO leads (lead_id, created_at, phone, phone_e164, phone_source,"
                  " qual_status, lead_type) VALUES (?,?,?,?,?,?,?)",
                  ("IMPORTED-1", adm.now(), row["phone"], row["phone_e164"], "IMPORTED",
                   "QUALIFIED", "PHONE_LEAD"))
    assert "CONSUMER_SUBMITTED" in str(e.value)
    # (b) even a hand-built lead dict that declares where its number came from is refused
    out = rt.route(c, [], {"lead_id": "IMPORTED-1", "qual_status": "QUALIFIED",
                           "lead_type": "PHONE_LEAD", "phone": row["phone"],
                           "phone_source": "IMPORTED", "product": "health_aca",
                           "state": "FL", "age_range": "30-39", "tier": "A"})
    assert out["routing_status"] == "NOT_ROUTABLE" and out["buyer_id"] is None
    assert adm.report(c)["lead_rows_not_consumer_submitted"] == 0


def test_no_lead_row_can_carry_an_imported_number():
    c = con()
    adm.import_rows(c, SRC, [_row()])
    with pytest.raises(Exception) as e:
        c.execute("INSERT INTO leads (lead_id, created_at, phone, phone_source) VALUES (?,?,?,?)",
                  ("X", adm.now(), "3055550142", "IMPORTED"))
    assert "CONSUMER_SUBMITTED" in str(e.value)
    assert adm.report(c)["lead_rows_not_consumer_submitted"] == 0


# ------------------------------------------------------------------ the one legitimate door
def test_promotion_requires_the_persons_own_consented_submission():
    c = con()
    adm.import_rows(c, SRC, [_row()])
    cid = c.execute("SELECT id FROM imported_contacts").fetchone()[0]
    # (a) no such lead
    assert not adm.promote(c, cid, "NOPE")["ok"]
    # (b) a lead with no consent row, even with the matching phone
    _lead(c, lead_id="L-NOCONSENT", with_consent=False)
    r = adm.promote(c, cid, "L-NOCONSENT")
    assert not r["ok"] and "no consent row" in r["reason"]
    # (c) a consented lead that belongs to somebody else
    _lead(c, lead_id="L-OTHER", phone="+13055550199", email="someone.else@example.com")
    r = adm.promote(c, cid, "L-OTHER")
    assert not r["ok"] and "does not match" in r["reason"]
    # (d) the same person's own submission: allowed, and it records why
    _lead(c, lead_id="L-0001")
    r = adm.promote(c, cid, "L-0001", "matched on the phone she submitted herself")
    assert r["ok"]
    row = dict(c.execute("SELECT * FROM imported_contacts").fetchone())
    assert row["marketing_eligibility"] == "ELIGIBLE_CONSENTED"
    assert row["consent_ref"] == row["lead_id"] == "L-0001"
    assert "phone matches" in row["eligibility_reason"]
    # promoting created no lead and wrote no consent: it only recorded the link
    assert c.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 3
    assert c.execute("SELECT COUNT(*) FROM consents").fetchone()[0] == 2
    assert adm.report(c)["sellable"] == 1


def test_duplicate_rows_cannot_be_promoted():
    c = con()
    adm.import_rows(c, SRC, [_row(), _row(name="Jane Doe")])
    dup_id = c.execute("SELECT id FROM imported_contacts WHERE dup_status='DUPLICATE'").fetchone()[0]
    _lead(c)
    r = adm.promote(c, dup_id, "L-0001")
    assert not r["ok"] and "duplicate" in r["reason"]


def test_trigger_refuses_a_consent_label_with_no_consent_row():
    c = con()
    adm.import_rows(c, SRC, [_row()])
    cid = c.execute("SELECT id FROM imported_contacts").fetchone()[0]
    with pytest.raises(Exception) as e:
        c.execute("UPDATE imported_contacts SET marketing_eligibility='ELIGIBLE_CONSENTED',"
                  " consent_ref='FAKE', lead_id='FAKE' WHERE id=?", (cid,))
    assert "consent" in str(e.value).lower()
    assert dict(c.execute("SELECT * FROM imported_contacts").fetchone())["marketing_eligibility"] \
        == "NOT_ELIGIBLE"


def test_a_permitted_use_never_becomes_marketing_permission():
    c = con()
    adm.import_rows(c, SRC, [_row()])
    cid = c.execute("SELECT id FROM imported_contacts").fetchone()[0]
    assert adm.record_use(c, cid, "reply to an inbound inquiry only")["ok"]
    row = dict(c.execute("SELECT * FROM imported_contacts").fetchone())
    assert row["marketing_eligibility"] == "USE_LIMITED" and row["lead_id"] == ""
    assert "not marketing permission" in adm.ineligible_reason(row)
    assert adm.report(c)["sellable"] == 0
    # a consented row cannot be downgraded into a use-limited one
    _lead(c)
    adm.promote(c, cid, "L-0001")
    assert not adm.record_use(c, cid, "analytics only")["ok"]


# ------------------------------------------------------------------ overlap with real leads
def test_an_existing_consented_lead_is_reported_but_never_inherited():
    c = con()
    _lead(c)
    r = adm.import_rows(c, SRC, [_row()])
    assert r["imported"] == 1 and r["already_a_lead"] == 1
    row = dict(c.execute("SELECT * FROM imported_contacts").fetchone())
    assert row["marketing_eligibility"] == "NOT_ELIGIBLE" and row["lead_id"] == ""
    assert json.loads(row["evidence"])["matched_lead"] == "L-0001"
    assert "NOT inherited" in row["eligibility_reason"]
    # the lead itself is untouched
    lead = dict(c.execute("SELECT * FROM leads WHERE lead_id='L-0001'").fetchone())
    assert lead["routing_status"] == "PENDING" and lead["phone_source"] == "CONSUMER_SUBMITTED"


# ------------------------------------------------------------------ real export shapes
def test_maps_scraper_export_maps_to_a_business_record_with_a_phone():
    """gosom/google-maps-scraper CSV columns (title/phone/website) — business contacts only."""
    c = con()
    r = adm.import_rows(c, {"id": "maps-dump", "license": "not granted: Maps ToS forbids scraping"},
                        [{"title": "Gulf Coast Insurance", "phone": "(305) 555-0100",
                          "website": "https://gulfcoast.example", "category": "insurance_agency",
                          "place_id": "ChIJ123"}], record_type="BUSINESS")
    assert r["imported"] == 1
    row = dict(c.execute("SELECT * FROM imported_contacts").fetchone())
    assert row["full_name"] == "Gulf Coast Insurance" and row["phone_e164"] == "+13055550100"
    assert row["record_type"] == "BUSINESS" and row["source_record_id"] == "ChIJ123"
    assert row["source_url"] == "https://gulfcoast.example"
    # a listing name is not a person: still NOT_ELIGIBLE, still nothing to route
    assert row["marketing_eligibility"] == "NOT_ELIGIBLE"


def test_linkedin_style_profile_gives_a_name_and_no_contact_channel():
    """joeyism/linkedin_scraper's Person model: name/headline/location, no email or phone field."""
    c = con()
    r = adm.import_rows(c, {"id": "li-profiles", "license": "LinkedIn User Agreement forbids scraping"},
                        [{"name": "Dana Ruiz", "headline": "Operations manager",
                          "location": "Miami, Florida", "profile_url": "https://linkedin.test/in/dana"}],
                        record_type="PROFESSIONAL")
    assert r["imported"] == 1 and r["missing_contact"] == 1
    row = dict(c.execute("SELECT * FROM imported_contacts").fetchone())
    assert row["has_contact"] == 0 and row["email"] == "" and row["phone"] == ""
    assert json.loads(row["source_fields"])["headline"] == "Operations manager"


# ------------------------------------------------------------------ what the record IS vs what we may DO
SRC_REAL = {"id": "nppes-test", "name": "Registry slice", "url": "https://example.test/npi",
            "license": "public registry", "data_status": "VERIFIED_REAL"}
SRC_SYN = {"id": "gen-test", "name": "Generated users", "url": "https://example.test/gen",
           "license": "CC-BY-NC-4.0", "data_status": "SYNTHETIC"}


def test_authenticity_travels_with_the_source_not_with_hope():
    c = con()
    adm.import_rows(c, SRC_REAL, [_row()], record_type="PROVIDER")
    adm.import_rows(c, SRC_SYN, [_row(name="Synthetic Person", email="s@example.com",
                                      phone="3055550123")])
    rows = {r["full_name"]: dict(r) for r in c.execute("SELECT * FROM imported_contacts")}
    assert rows["Jane Doe"]["data_status"] == "VERIFIED_REAL"
    assert rows["Jane Doe"]["record_type"] == "PROVIDER"
    assert rows["Synthetic Person"]["data_status"] == "SYNTHETIC"
    # both are still unmarketed: real-ness never implies permission
    assert all(r["marketing_eligibility"] == "NOT_ELIGIBLE" for r in rows.values())
    assert "generated record" in adm.ineligible_reason(rows["Synthetic Person"])
    rep = adm.report(c)
    assert rep["by_data_status"]["VERIFIED_REAL"] == 1 and rep["by_data_status"]["SYNTHETIC"] == 1
    assert rep["by_type"]["PROVIDER"] == 1 and rep["sellable"] == 0


def test_a_generated_row_can_never_be_promoted_or_made_eligible():
    c = con()
    adm.import_rows(c, SRC_SYN, [_row(name="Synthetic Person", email="s@example.com")])
    cid = c.execute("SELECT id FROM imported_contacts").fetchone()[0]
    _lead(c)                                     # even a real consented submission cannot launder it
    r = adm.promote(c, cid, "L-0001")
    assert not r["ok"] and "SYNTHETIC" in r["reason"]
    # a note cannot turn generated data into a person either
    bad = adm.attest(c, cid, "VERIFIED_REAL", "I checked and it looks real to me on 2026-11-01")
    assert not bad["ok"] and "generates records" in bad["reason"]
    with pytest.raises(Exception) as e:
        c.execute("UPDATE imported_contacts SET marketing_eligibility='ELIGIBLE_CONSENTED',"
                  " consent_ref='L-0001', lead_id='L-0001' WHERE id=?", (cid,))
    assert "synthetic" in str(e.value).lower()


def test_attestation_needs_evidence_and_never_confers_permission():
    c = con()
    adm.import_rows(c, {"id": "vendor-x", "license": "vendor agreement"}, [_row()])
    cid = c.execute("SELECT id FROM imported_contacts").fetchone()[0]
    assert not adm.attest(c, cid, "VERIFIED_REAL", "too short")["ok"]
    assert adm.attest(c, cid, "VERIFIED_REAL", "matched the record against the vendor's sample page")["ok"]
    row = dict(c.execute("SELECT * FROM imported_contacts").fetchone())
    assert row["data_status"] == "VERIFIED_REAL"
    assert "vendor's sample page" in row["verified_by"] and row["verified_at"]
    assert row["marketing_eligibility"] == "NOT_ELIGIBLE" and row["lead_id"] == ""


def test_two_people_sharing_one_number_stay_two_records():
    """A clinic switchboard is not one person: merging on the phone alone would delete a human."""
    c = con()
    r = adm.import_rows(c, SRC_REAL, [
        {"name": "Linda Ablecop", "phone": "305-555-0100", "state": "FL"},
        {"name": "Maria Dambeck", "phone": "305-555-0100", "state": "FL"},
        {"name": "Linda Ablecop", "phone": "305-555-0100", "state": "FL"}], record_type="PROVIDER")
    assert r["imported"] == 2 and r["duplicates"] == 1
    names = sorted(x[0] for x in c.execute("SELECT full_name FROM imported_contacts"))
    assert names == ["Linda Ablecop", "Linda Ablecop", "Maria Dambeck"]   # 2 live + 1 duplicate row
    kept = [dict(x) for x in c.execute("SELECT * FROM imported_contacts WHERE dup_status='UNIQUE'")
            if x["dup_status"] == "UNIQUE"]
    keys = sorted(x["contact_key"] for x in kept)
    assert keys == ["p:3055550100", "p:3055550100#maria-dambeck"]      # the switchboard keeps both
    assert json.loads(kept[1]["quality"])["shared_channel"] == 1


def test_an_organisation_name_is_not_split_into_a_persons_name():
    c = con()
    adm.import_rows(c, SRC_REAL, [{"facility_name": "SOUTHEAST HEALTH MEDICAL CENTER",
                                   "telephone_number": "(334) 793-8701", "citytown": "DOTHAN",
                                   "state": "AL", "zip_code": "36301", "facility_id": "010001"}],
                    record_type="PROVIDER_ORG")
    row = dict(c.execute("SELECT * FROM imported_contacts").fetchone())
    assert row["full_name"] == "SOUTHEAST HEALTH MEDICAL CENTER"
    assert row["first_name"] == "" and row["last_name"] == ""
    assert row["phone_e164"] == "+13347938701" and row["source_record_id"] == "010001"
    assert json.loads(row["quality"])["org_name"] == 1


def test_a_present_but_unusable_phone_is_recorded_as_such():
    c = con()
    adm.import_rows(c, SRC_REAL, [{"name": "Bad Phone", "phone": "12345", "state": "FL"}])
    row = dict(c.execute("SELECT * FROM imported_contacts").fetchone())
    q = json.loads(row["quality"])
    assert q["has_phone"] == 1 and q["phone_valid"] == 0 and q["phone_raw"] == "12345"
    assert row["has_contact"] == 0
    assert adm.report(c)["invalid_phones"] == 1


# ------------------------------------------------------------------ measured source evaluation
def test_source_evaluation_measures_coverage_without_network():
    from engine import source_eval
    rows = [{"full_name": "Ann Reyes", "phone": "305-555-0111", "email": "ann@example.com",
             "city": "Miami", "state": "FL", "zip": "33101", "npi": "1",
             "enumeration_date": "2020-01-01"},
            {"full_name": "Bo Tran", "phone": "305-555-0111", "city": "Miami", "state": "FL",
             "zip": "33102", "npi": "2", "enumeration_date": "2021-01-01"},
            {"full_name": "Bo Tran", "phone": "305-555-0111", "city": "Miami", "state": "FL",
             "zip": "33102", "npi": "3", "enumeration_date": "2021-01-01"}]
    rep = source_eval.evaluate(rows, "nppes")
    assert rep["records"] == 3
    assert rep["coverage"]["full_name"] == {"n": 3, "pct": 100.0}
    assert rep["coverage"]["email"] == {"n": 1, "pct": 33.3}
    assert rep["valid_phones"] == 3 and rep["valid_emails"] == 1
    assert rep["identity_shapes"] == {"name+phone": 2, "name+phone+email": 1}
    # Ann is keyed by her email, so the two identical Bo rows are the exact duplicate here
    assert rep["duplicate_rows"] == 1 and rep["shared_contact_channels"] == 0
    # different people on one switchboard: shared channel, NOT duplicates of each other
    shared = source_eval.evaluate([{"full_name": "Ann Reyes", "phone": "305-555-0111", "state": "FL"},
                                   {"full_name": "Bo Tran", "phone": "305-555-0111", "state": "FL"}], "nppes")
    assert shared["duplicate_rows"] == 0 and shared["shared_contact_channels"] == 1
    assert shared["shared_channel_rows"] == 2


def test_source_evaluation_never_invents_a_fetcher_and_reports_budget_blocks():
    from engine import source_eval
    rep = source_eval.run("census-cbp")
    assert rep["measured"] is False and rep["evaluation"] is None
    assert "0 contact fields" in rep["finding"]
    md = source_eval.to_markdown([rep])
    assert "Not measured in this run" in md and "census-cbp" in md


# ------------------------------------------------------------------ the operator's actual interface
# These go through the Flask app the operator uses (/admin/contacts), not the engine functions, so
# the page, the paste-import form and the promote action are exercised as delivered.
from tests.test_engine import BASE as CONSUMER_BASE, make_app, post  # noqa: E402
from urllib.parse import unquote_plus                      # noqa: E402


def _flash(r) -> str:
    """The operator-facing message Flask put in the redirect (query string, decoded)."""
    return unquote_plus(r.headers.get("Location", ""))


def test_admin_contacts_renders_and_import_end_to_end(tmp_path):
    m = make_app(tmp_path)
    c = m.app.test_client()
    page = c.get("/admin/contacts?token=t")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "Imported contacts" in body and "Import an export" in body
    assert "Nothing imported yet" in body

    r = c.post("/admin/contacts/import?token=t", data={
        "source_id": "panel-2026-04", "source_name": "Panel export",
        "source_url": "https://example.test/panel", "license": "vendor agreement §3",
        "record_type": "CONSUMER",
        "csv": "name,email,phone,state,collected_at\nJane Doe,jane@example.com,305-555-0142,FL,2026-03-11\n"})
    assert r.status_code == 302 and "1 imported" in _flash(r)
    page = c.get("/admin/contacts?token=t").get_data(as_text=True)
    assert "Jane Doe" in page and "+13055550142" in page and "panel-2026-04" in page
    assert "not eligible" in page and "no permission basis" in page
    assert "vendor agreement" in page
    # the import created no lead and no consent
    assert m.con.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0
    assert m.con.execute("SELECT COUNT(*) FROM consents").fetchone()[0] == 0


def test_admin_contacts_import_without_a_source_is_refused(tmp_path):
    m = make_app(tmp_path)
    r = m.app.test_client().post("/admin/contacts/import?token=t",
                                 data={"source_id": "", "csv": "name\nJane Doe\n"})
    assert r.status_code == 302
    assert "source id is required" in _flash(r)
    assert m.con.execute("SELECT COUNT(*) FROM imported_contacts").fetchone()[0] == 0


def test_promotion_through_the_admin_page_after_a_real_submission(tmp_path):
    """The whole legitimate path, as the operator performs it: a real consumer submits /check (a
    lead + consent row exist), their record is imported, and the operator links the two. Nothing
    is created by the link, and the lead's own record is untouched."""
    m = make_app(tmp_path)
    c = m.app.test_client()
    assert post(c, consent_checked=True).status_code in (200, 302)
    lead = dict(m.con.execute("SELECT lead_id, phone, routing_status FROM leads").fetchone())
    consents_before = m.con.execute("SELECT COUNT(*) FROM consents").fetchone()[0]

    c.post("/admin/contacts/import?token=t", data={
        "source_id": "panel-2026-04", "license": "vendor agreement §3", "record_type": "CONSUMER",
        "csv": f"name,phone,email,state\nAna Lopez,{lead['phone']},ana@example.com,FL\n"})
    row = dict(m.con.execute("SELECT * FROM imported_contacts").fetchone())
    assert row["marketing_eligibility"] == "NOT_ELIGIBLE" and row["lead_id"] == ""
    page = c.get("/admin/contacts?token=t").get_data(as_text=True)
    assert "consent NOT inherited" in page

    r = c.post(f"/admin/contacts/{row['id']}/promote?token=t",
               data={"lead_id": lead["lead_id"], "note": "same phone she submitted"})
    assert r.status_code == 302 and "linked to consented lead" in _flash(r)
    row = dict(m.con.execute("SELECT * FROM imported_contacts").fetchone())
    assert row["marketing_eligibility"] == "ELIGIBLE_CONSENTED"
    assert row["lead_id"] == row["consent_ref"] == lead["lead_id"]
    assert m.con.execute("SELECT COUNT(*) FROM consents").fetchone()[0] == consents_before
    after = dict(m.con.execute("SELECT lead_id, phone, routing_status FROM leads").fetchone())
    assert after == lead                                   # the lead was not rewritten
    assert "eligible" in c.get("/admin/contacts?token=t").get_data(as_text=True)


def test_admin_contacts_promote_to_a_non_matching_lead_is_refused(tmp_path):
    m = make_app(tmp_path)
    c = m.app.test_client()
    post(c, consent_checked=True)
    lead_id = m.con.execute("SELECT lead_id FROM leads").fetchone()[0]
    c.post("/admin/contacts/import?token=t", data={
        "source_id": "panel-2026-04", "license": "x",
        "csv": "name,phone\nSomebody Else,305-555-0900\n"})
    cid = m.con.execute("SELECT id FROM imported_contacts").fetchone()[0]
    r = c.post(f"/admin/contacts/{cid}/promote?token=t", data={"lead_id": lead_id})
    assert "does not match" in _flash(r)
    assert dict(m.con.execute("SELECT * FROM imported_contacts").fetchone())["lead_id"] == ""


def test_csv_text_import_reports_counts():
    c = con()
    csv_text = ("name,email,phone,state\n"
                "Jane Doe,jane@example.com,305-555-0142,FL\n"
                "Jane Doe,jane@example.com,305-555-0142,FL\n"
                ",,,FL\n")
    r = adm.import_csv_text(c, SRC, csv_text)
    assert (r["received"], r["imported"], r["duplicates"], r["rejected"]) == (3, 1, 1, 1)
    assert adm.counts(c)["batches"][0]["batch"] == r["batch"]
