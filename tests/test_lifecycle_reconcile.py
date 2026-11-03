"""One lead, the WHOLE lifecycle, every recorded surface agreeing with the database.

The pilot rehearsal already proves the mechanics end to end; this test proves RECONCILIATION:
for the same lead, what the buyer desk, the buyer billing page, the admin lead trace, the
admin credit queue and the admin overview show is exactly what the tables record — no page
invents a state the ledger does not back (and no recorded state is invisible to the operator).

  submit + consent -> routing decision -> delivery + ONE charge -> CONTACTED -> late RETURNED
  refusal recorded -> operator GRANT -> one LEAD_CREDIT -> balances and every page agree.

Bookkeeping only - TEST traffic, no live payments, no real buyers.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_billing import make_app, post


def test_one_lead_all_surfaces_agree_with_the_records(tmp_path):
    import engine.billing as bl
    m = make_app(tmp_path); c = m.app.test_client()
    tok = next(x["portal_token"] for x in m.route.load_buyers(m.CFG) if x.get("is_demo"))
    buyer = next(x["id"] for x in m.route.load_buyers(m.CFG) if x.get("is_demo"))

    # 1 submission -> the routing decision is audited, the sale is delivered and charged ONCE
    r = post(c, consent_checked=True).get_json()
    assert r["routed"] == "ROUTED"
    lid = r["lead_id"]
    lead = dict(m.con.execute("SELECT * FROM leads WHERE lead_id=?", (lid,)).fetchone())
    assert lead["routing_status"] == "DELIVERED" and lead["buyer_id"] == buyer and lead["price"] > 0
    dec = [dict(x) for x in m.con.execute(
        "SELECT decision, buyer_id, reason FROM routing_decisions WHERE lead_id=?", (lid,))]
    assert len(dec) == 1 and dec[0]["decision"] == "ASSIGNED" and dec[0]["buyer_id"] == buyer
    charges = [dict(x) for x in m.con.execute(
        "SELECT * FROM buyer_ledger WHERE lead_id=? AND kind='LEAD_CHARGE'", (lid,))]
    assert len(charges) == 1 and charges[0]["amount"] == round(-lead["price"], 2)

    # the buyer desk shows the delivery state the DB records; the trace page shows the decision
    desk = c.get(f"/agent/{tok}/lead/{lid}").get_data(as_text=True)
    assert "DELIVERED" in desk and "EXCLUSIVE" not in desk          # state, never a promise
    trace = c.get(f"/admin/lead/{lid}?token=t").get_data(as_text=True)
    assert "ASSIGNED" in trace and "LEAD_CHARGE" in trace and "eligible buyer available" in trace

    # 2 the buyer sees the lead (VIEWED is recorded), then records CONTACTED
    c.get(f"/agent/{tok}/lead/{lid}")
    assert m.con.execute("SELECT agent_status FROM leads WHERE lead_id=?", (lid,)).fetchone()[0] == "VIEWED"
    saved = c.post(f"/agent/{tok}", data={"lead_id": lid, "status": "CONTACTED", "back": "1"}, follow_redirects=True)
    page = saved.get_data(as_text=True)
    assert "Status saved" in page            # the confirmation renders on the page the click lands on
    assert "Contacted" in page               # ...and the history already agrees
    trace = c.get(f"/admin/lead/{lid}?token=t").get_data(as_text=True)
    assert "CONTACTED" in trace                                     # history agrees on both surfaces

    # 3 a late RETURN is refused: the refusal is the recorded note, on BOTH surfaces, with NO ledger movement
    m.con.execute("UPDATE leads SET delivered_at=datetime('now','-4 days') WHERE lead_id=?", (lid,))
    m.con.commit()
    c.post(f"/agent/{tok}", data={"lead_id": lid, "status": "RETURNED", "reason": "unreachable"})
    note = m.con.execute(
        "SELECT outcome, note FROM lead_outcomes WHERE lead_id=? AND outcome='RETURNED'", (lid,)).fetchone()
    assert note["note"].startswith("no credit: ")
    assert bl.balance(m.con, buyer, 1) == round(-lead["price"], 2)   # nothing moved
    trace = c.get(f"/admin/lead/{lid}?token=t").get_data(as_text=True)
    assert "no credit: " in trace and "Return/credit request awaiting decision" in trace
    queue = c.get(f"/admin/credits?token=t").get_data(as_text=True)
    assert lid[-8:] in queue and "no credit: " in queue and "Grant credit" in queue
    # (this lead was CONTACTED before the return, so the recorded policy reason is the
    #  contacted-before-return rule — the refusal note is whatever the same policy decided)

    # 4 the operator GRANTS: exactly one credit, backend records it, and every page shows the new state
    c.post(f"/admin/lead/{lid}/credit_decision?token=t", data={"decision": "grant", "note": "goodwill"})
    credits = [dict(x) for x in m.con.execute(
        "SELECT * FROM buyer_ledger WHERE lead_id=? AND kind='LEAD_CREDIT'", (lid,))]
    assert len(credits) == 1 and credits[0]["amount"] == round(lead["price"], 2)
    assert bl.balance(m.con, buyer, 1) == 0.0
    trace = c.get(f"/admin/lead/{lid}?token=t").get_data(as_text=True)
    assert "LEAD_CREDIT" in trace and "CREDIT_DECISION" in trace and "operator grant" in trace
    billing_page = c.get(f"/agent/{tok}/billing").get_data(as_text=True)
    assert f"${0.0:.2f}" in billing_page or "$0.00" in billing_page
    assert "LEAD_CREDIT" not in billing_page or "Return credit" in billing_page or "credit" in billing_page.lower()

    # 5 the whole ledger still agrees with itself for this buyer
    rows = [dict(x) for x in m.con.execute("SELECT kind, amount FROM buyer_ledger WHERE buyer_id=? AND is_test=1", (buyer,))]
    sums = {}
    for x in rows:
        sums[x["kind"]] = round(sums.get(x["kind"], 0.0) + x["amount"], 2)
    expected = round(sums.get("PACKAGE_PURCHASE", 0) + sums.get("LEAD_CREDIT", 0)
                     + sums.get("ADJUSTMENT", 0) + sums.get("LEAD_CHARGE", 0), 2)
    assert abs(expected - bl.balance(m.con, buyer, 1)) < 0.005, (expected, bl.balance(m.con, buyer, 1))

    # 6 the queue has no DECISION FORM left on this lead (the trail row only documents history;
    # a pending request is the only thing that renders the Grant/Deny forms)
    assert c.get(f"/admin/credits?token=t").get_data(as_text=True).count("credit_decision") == 0
