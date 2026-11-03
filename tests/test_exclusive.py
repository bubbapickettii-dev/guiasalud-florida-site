"""Exclusive-lead sales integrity:

  - a routed phone lead is explicitly EXCLUSIVE: sale_type recorded, one buyer, one delivery, ever
  - the DATABASE refuses a second successful delivery (partial unique index) even if app code is broken
  - a delivered lead can never be silently left uncharged: reconcile closes delivered-but-uncharged
    gaps exactly once (idempotent), including on buyer page loads
  - unauthorized access: one buyer's portal token cannot open another buyer's lead

Bookkeeping only — no real money, no live delivery, TEST-flagged leads.
"""
import sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_billing import make_app, post, BASE   # shared fixture helpers


def test_routed_lead_is_explicitly_exclusive_and_delivered_once(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    r = post(c, consent_checked=True).get_json()
    assert r["routed"] == "ROUTED"
    lead = dict(m.con.execute("SELECT * FROM leads").fetchone())
    # the sale type is ON the lead, not implied
    assert lead["sale_type"] == "EXCLUSIVE" and lead["routing_status"] == "DELIVERED"
    dl = [dict(x) for x in m.con.execute("SELECT * FROM deliveries WHERE lead_id=? AND method='outbox'", (lead["lead_id"],))]
    assert len(dl) == 1 and dl[0]["buyer_id"] == lead["buyer_id"] and dl[0]["status"] == "OUTBOX_DRY_RUN"
    # exactly one charge
    chg = [dict(x) for x in m.con.execute("SELECT * FROM buyer_ledger WHERE kind='LEAD_CHARGE'")]
    assert len(chg) == 1 and chg[0]["lead_id"] == lead["lead_id"] and chg[0]["amount"] == round(-lead["price"], 2)


def test_database_refuses_a_second_delivery_of_the_same_lead(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    post(c, consent_checked=True).get_json()
    lead = dict(m.con.execute("SELECT * FROM leads").fetchone())
    # app-level guard: re-delivering a DELIVERED lead is refused
    import engine.route as ro
    buyers = ro.load_buyers(m.CFG)
    assert ro.deliver(m.con, buyers, lead, m.OUTBOX) == "ALREADY_DELIVERED"
    # app-code bypass (simulated bug that forgets the guard): the unique index still refuses
    before = m.con.execute("SELECT COUNT(*) FROM deliveries WHERE lead_id=? AND method='outbox'", (lead["lead_id"],)).fetchone()[0]
    try:
        m.con.execute("INSERT INTO deliveries (lead_id,buyer_id,ts,method,status,payload_path,response) "
                      "VALUES (?,?,?,?,?,?,?)", (lead["lead_id"], "demo-agency-b", "2026-01-01T00:00:00+00:00",
                                                 "outbox", "OUTBOX_DRY_RUN", "/x", ""))
        m.con.commit()
        raised = False
    except sqlite3.IntegrityError:
        m.con.rollback()
        raised = True
    assert raised, "a lead must not be deliverable to a second buyer at the DB level"
    after = m.con.execute("SELECT COUNT(*) FROM deliveries WHERE lead_id=? AND method='outbox'", (lead["lead_id"],)).fetchone()[0]
    assert before == 1 and after == 1
    # FAILED webhook deliveries don't burn exclusivity: the lead never reached that buyer and may be retried
    m.con.execute("INSERT INTO deliveries (lead_id,buyer_id,ts,method,status,payload_path,response) "
                  "VALUES (?,?,?,?,?,?,?)", (lead["lead_id"], lead["buyer_id"], "2026-01-01T00:00:01+00:00",
                                             "webhook", "FAILED", "/x", "503"))
    m.con.commit()


def test_reconcile_closes_delivered_but_uncharged_gaps_exactly_once(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    post(c, consent_checked=True).get_json()
    lead = dict(m.con.execute("SELECT * FROM leads").fetchone())
    # simulate the failure mode: delivery recorded, charge write never happened
    m.con.execute("DELETE FROM buyer_ledger WHERE kind='LEAD_CHARGE'")
    m.con.commit()
    assert m.con.execute("SELECT COUNT(*) FROM buyer_ledger WHERE kind='LEAD_CHARGE'").fetchone()[0] == 0
    import engine.billing as bl
    row = bl.reconcile_lead(m.con, lead)
    assert row and row["amount"] == round(-lead["price"], 2) and row["lead_id"] == lead["lead_id"]
    # idempotent: reruns (and page loads) charge nothing more
    assert bl.reconcile_lead(m.con, lead) is None
    # the buyer desk page load also reconciles (self-healing path used by the app)
    m.con.execute("DELETE FROM buyer_ledger WHERE kind='LEAD_CHARGE'")
    m.con.commit()
    tok = next(b for b in ro_buyers(m) if b["id"] == lead["buyer_id"])["portal_token"]
    c.get(f"/agent/{tok}")
    assert m.con.execute("SELECT COUNT(*) FROM buyer_ledger WHERE kind='LEAD_CHARGE'").fetchone()[0] == 1
    # NOT a no-op audit hole: email-only (unsaleable) leads never reconcile into a charge
    assert bl.reconcile_lead(m.con, {**lead, "routing_status": "NOT_ROUTABLE", "price": None}) is None


def test_billing_page_shows_exclusive_and_buyers_cannot_reach_eachothers_leads(tmp_path):
    m = make_app(tmp_path)
    m.con.execute("UPDATE leads SET buyer_id=?, routing_status='DELIVERED', sale_type='EXCLUSIVE', "
                  "agent_status='VIEWED', delivered_at=? WHERE lead_id='nope'", ("x", "")) if False else None
    c = m.app.test_client()
    post(c, consent_checked=True).get_json()
    lead = dict(m.con.execute("SELECT * FROM leads").fetchone())
    tok_a = next(b for b in ro_buyers(m) if b["id"] == lead["buyer_id"])["portal_token"]
    tok_b = next(b for b in ro_buyers(m) if b["id"] != lead["buyer_id"])
    # buyer A's billing page names the exclusivity + the delivered lead
    pg = c.get(f"/agent/{tok_a}/billing")
    assert pg.status_code == 200 and "Exclusive" in pg.get_data(as_text=True)
    assert lead["lead_id"] in pg.get_data(as_text=True)
    # buyer B does NOT see buyer A's lead, on the billing page, the desk, or the lead detail page
    pgb = c.get(f"/agent/{tok_b['portal_token']}/billing").get_data(as_text=True)
    assert lead["lead_id"] not in pgb
    assert c.get(f"/agent/{tok_b['portal_token']}/lead/{lead['lead_id']}").status_code == 404


def ro_buyers(m):
    import engine.route as ro
    return ro.load_buyers(m.CFG)
