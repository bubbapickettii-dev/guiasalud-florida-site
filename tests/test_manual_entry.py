"""Operator manual lead entry (/admin/leads/new): the safe-path guarantees.

Under test:
  - no pass-through: full consent evidence is recorded, weaker evidence records an
    EMAIL_ONLY / NURTURE lead that is NEVER routed, sold, or charged;
  - a QUALIFIED consented phone lead is routed through the SAME platform assignment
    (routing_decisions audit row; never a pre-chosen buyer) and charged once via the
    same atomic deliver+charge as a consumer submission;
  - duplicates are recorded as DUPLICATE (never sold, never re-charged);
  - the phone DB trigger still refuses any non-CONSUMER_SUBMITTED phone (manual entry
    cannot smuggle in an appended/public-record number via phone provenance omission);
  - provenance + TEST lane: TEST leads stay on the TEST ledger lane.
"""
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def make_app(tmp):
    import os, importlib
    os.environ["LEADENGINE_DB"] = str(tmp / "t.db")
    os.environ["ADMIN_TOKEN"] = "t"
    os.environ["INTEL_DB"] = str(tmp / "none.db")
    import app as appmod
    importlib.reload(appmod)
    # staging mode: route/gate behavior identical, live delivery stays off (dry-run outbox)
    os.environ.pop("LEADENGINE_LIVE_DELIVERY", None)
    appmod.OUTBOX = tmp / "outbox"; appmod.SNAP = tmp / "snap"
    appmod.PRODUCTION = False
    return appmod


def save(m, c, **over):
    d = dict(campaign="oep-2027-health", first_name="Maria", last_name="Torres",
             phone="3055550140", email="maria@example.com", zip="33130", state="FL",
             age_range="40-49", source="inbound_call", note="called after the OEP page",
             consent_attested="on", consent_ts="2026-10-27T10:30",
             consent_text="I agree to be contacted by GS Allude and the licensed buyers it may assign.",
             household_size="3", income_band="100-150", urgency="HIGH",
             enrollment_situation="SEP_LIKELY", preferred_contact="phone", is_test=1)
    d.update(over)
    d.pop("is_test", None)
    if d.get("is_test_flag"):
        d["test"] = "1"; d.pop("is_test_flag", None)
    else:
        d["test"] = "1"  # default TEST data in tests
    return c.post("/admin/leads/new?token=t", data=d)


def rows(m, table="leads", where=""):
    return [dict(r) for r in m.con.execute(f"SELECT * FROM {table} {where}")]


def test_full_consent_phone_lead_routes_through_platform_and_charges_once(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    r = save(m, c)
    assert r.status_code == 302, r.get_data(as_text=True)[:400]
    lead = rows(m)[0]
    # consented phone lead -> QUALIFIED and phone is consumer-submitted (trigger would abort otherwise)
    assert lead["qual_status"] == "QUALIFIED" and lead["contact_scope"] == "PHONE_AND_EMAIL"
    assert lead["phone_source"] == "CONSUMER_SUBMITTED" and lead["phone_valid"] == 1
    # routed via the SAME platform assignment, audited — and the atomic deliver+charge ran
    assert lead["routing_status"] == "DELIVERED" and lead["buyer_id"]
    dec = rows(m, "routing_decisions")[0]
    assert dec["decision"] == "ASSIGNED" and dec["buyer_id"] == lead["buyer_id"]
    assert "manual entry" in dec["reason"]
    # charged exactly once on the TEST lane (delivery is outbox/staging)
    ch = rows(m, "buyer_ledger", "WHERE kind='LEAD_CHARGE'")
    assert len(ch) == 1 and ch[0]["lead_id"] == lead["lead_id"] and ch[0]["is_test"] == 1
    assert ch[0]["amount"] == round(-lead["price"], 2)
    # consent row holds the operator evidence, honestly marked
    cons = rows(m, "consents")[0]
    assert cons["checkbox_checked"] == 1 and cons["scope"] == "PHONE_AND_EMAIL"
    assert "operator-recorded consent" in cons["consent_text"] and "inbound_call" in cons["consent_text"]
    # provenance recorded on the lead
    import json
    me = json.loads(lead["answers"] or "{}").get("manual_entry", {})
    assert me["source"] == "inbound_call" and me["by"] == "admin"


def test_missing_consent_evidence_cannot_sell_a_phone(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    r = save(m, c, consent_attested="", consent_ts="", consent_text="")
    assert r.status_code == 302
    lead = rows(m)[0]
    assert lead["contact_scope"] == "EMAIL_ONLY" and lead["qual_status"] != "UNQUALIFIED"
    assert lead["routing_status"] == "PENDING" and lead["buyer_id"] is None and not lead["price"]
    dec = rows(m, "routing_decisions")
    assert dec[0]["decision"] == "NOT_ROUTABLE"   # consent missing -> never offered anywhere
    assert rows(m, "buyer_ledger") == []          # nothing charged


def test_duplicate_recorded_never_sold(tmp_path):
    import engine.billing as bl
    m = make_app(tmp_path); c = m.app.test_client()
    save(m, c)
    lead1 = rows(m)[0]
    n_charge = len(rows(m, "buyer_ledger", "WHERE kind='LEAD_CHARGE'"))
    bal = bl.balance(m.con, lead1["buyer_id"], 1)
    r = save(m, c, email="other@example.com")     # same phone, different email
    assert r.status_code == 302
    lead2 = rows(m, "leads", "ORDER BY created_at DESC LIMIT 1")[0]
    assert lead2["qual_status"] == "DUPLICATE"
    assert lead2["routing_status"] == "PENDING" and lead2["buyer_id"] is None
    assert len(rows(m, "buyer_ledger", "WHERE kind='LEAD_CHARGE'")) == n_charge
    assert bl.balance(m.con, lead1["buyer_id"], 1) == bal


def test_junk_or_bad_data_fails_qualification_not_routed(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    r = save(m, c, zip="33x")     # invalid ZIP -> hard rule
    assert r.status_code == 302
    lead = rows(m)[0]
    assert lead["qual_status"] == "UNQUALIFIED" and lead["routing_status"] == "PENDING"
    assert not lead["price"] and rows(m, "buyer_ledger") == []


def test_form_rejects_missing_provenance_and_name_with_preserved_input(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    r = c.post("/admin/leads/new?token=t", data={"campaign": "oep-2027-health", "first_name": "Ana",
                                         "phone": "3055550140", "email": "a@example.com"})
    assert r.status_code == 422
    txt = r.get_data(as_text=True)
    assert "name are required" in txt and "provenance" in txt.lower()
    assert 'value="Ana"' in txt   # what was typed is preserved
    assert rows(m) == []          # nothing half-saved


def test_manual_entry_cannot_insert_a_non_consumer_phone(tmp_path):
    """Even a 'successful' manual save carries phone_source=CONSUMER_SUBMITTED; there is no
    flag that lets the operator sneak in an appended number — the DB trigger is the wall."""
    import sqlite3
    m = make_app(tmp_path); c = m.app.test_client()
    with pytest.raises(sqlite3.IntegrityError):
        m.con.execute("INSERT INTO leads (lead_id, created_at, campaign, product, first_name, last_name, "
                      "phone, phone_source, qual_status, routing_status) VALUES ('Lx','now','oep-2027-health',"
                      "'health_aca','A','B','3055550140','APPENDED','QUALIFIED','PENDING')")
        m.con.commit()
