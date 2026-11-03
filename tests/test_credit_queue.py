"""Return-credit dispute queue (operator decision layer over the buyer's REFUSED return requests).

  - a buyer return that the configured policy REFUSES (e.g. outside the 72h return window) is
    recorded by app._set_status with the stable "no credit: " prefix and lands on /admin/credits
  - an in-policy return that WAS credited automatically NEVER appears in the queue
  - GRANT reuses billing.credit_return — the once-per-lead+buyer SQL-guarded credit — so a repeated
    grant, or a grant racing a desk heal, can never write a second LEAD_CREDIT
  - DENY records only the reasoning; the ledger is untouched
  - every decision carries a required note and is appended to the lead's history as CREDIT_DECISION
Bookkeeping only: TEST-funnel leads, no live payments, no real buyers.
"""
import importlib, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_billing import make_app, BASE, post


def demo_token(m):
    return next(x["portal_token"] for x in m.route.load_buyers(m.CFG) if x.get("is_demo"))


def refused_lead(tmp_path, m, c):
    """A delivered TEST lead the buyer tries to return OUTSIDE the 72h window -> refusal recorded."""
    r = post(c, consent_checked=True).get_json()
    assert r["routed"] == "ROUTED"
    lid = r["lead_id"]
    m.con.execute("UPDATE leads SET delivered_at=datetime('now','-4 days') WHERE lead_id=?", (lid,))
    m.con.commit()
    tok = demo_token(m)
    assert c.post(f"/agent/{tok}", data={"lead_id": lid, "status": "RETURNED"}).status_code in (302, 303)
    out = [dict(x) for x in m.con.execute(
        "SELECT * FROM lead_outcomes WHERE lead_id=? AND outcome='RETURNED'", (lid,))]
    assert len(out) == 1 and out[0]["note"].startswith("no credit: ")   # the stable queue-driving prefix
    return lid, tok


def test_refused_return_is_queued_for_operator(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    lid, tok = refused_lead(tmp_path, m, c)
    page = c.get("/admin/credits?token=t").get_data(as_text=True)
    assert lid[-8:] in page and "no credit: outside the configured return window" in page
    assert "Grant credit" in page and "Deny" in page          # both operator actions are offered
    assert "TEST" in page                                     # the operator can tell TEST rehearsals apart
    # a buyer whose return WAS in policy (fresh delivery, inside the window) never lands here
    r2 = post(c, consent_checked=True).get_json()
    assert c.post(f"/agent/{tok}", data={"lead_id": r2["lead_id"], "status": "RETURNED"}).status_code in (302, 303)
    page2 = c.get("/admin/credits?token=t").get_data(as_text=True)
    assert r2["lead_id"][-8:] not in page2                    # automatic credit -> not an operator item


def test_overview_promotes_pending_credit_requests(tmp_path):
    """The admin overview surfaces refused return-credit requests in the sale-integrity alert panel,
    so an unanswered request can't be missed behind the tab — and leaves when a decision records."""
    m = make_app(tmp_path); c = m.app.test_client()
    lid, tok = refused_lead(tmp_path, m, c)
    page = c.get("/admin?token=t").get_data(as_text=True)
    assert "Return credits awaiting decision" in page and lid[-8:] in page
    c.post(f"/admin/lead/{lid}/credit_decision?token=t", data={"decision": "deny", "note": "policy stands"})
    assert "Return credits awaiting decision" not in c.get("/admin?token=t").get_data(as_text=True)


def test_grant_credits_exactly_once_and_leaves_queue(tmp_path):
    import engine.billing as bl
    m = make_app(tmp_path); c = m.app.test_client()
    lid, tok = refused_lead(tmp_path, m, c)
    buyer = m.con.execute("SELECT buyer_id, price FROM leads WHERE lead_id=?", (lid,)).fetchone()
    price = round(buyer["price"], 2)
    assert bl.balance(m.con, buyer["buyer_id"], 1) == round(-price, 2)   # charged, never credited

    r = c.post(f"/admin/lead/{lid}/credit_decision?token=t",
               data={"decision": "grant", "note": "seller admits stale data — goodwill"}, follow_redirects=True)
    page = r.get_data(as_text=True)
    assert "credited" in page and "once" in page.lower()
    led = [dict(x) for x in m.con.execute(
        "SELECT * FROM buyer_ledger WHERE buyer_id=? AND lead_id=? AND kind='LEAD_CREDIT'", (buyer["buyer_id"], lid))]
    assert len(led) == 1 and led[0]["amount"] == price and led[0]["is_test"] == 1
    assert bl.balance(m.con, buyer["buyer_id"], 1) == 0.0                # back to zero, never below

    # granting AGAIN (operator double-click / desk heal raced in) writes nothing more
    r2 = c.post(f"/admin/lead/{lid}/credit_decision?token=t",
                data={"decision": "grant", "note": "retry after the first grant"}, follow_redirects=True)
    assert len(m.con.execute("SELECT * FROM buyer_ledger WHERE lead_id=? AND kind='LEAD_CREDIT'",
                             (lid,)).fetchall()) == 1
    assert "nothing more written" in r2.get_data(as_text=True)
    # the decision is on the lead's history (buyer-visible) and the queue saw it leave
    hist = [dict(x) for x in m.con.execute("SELECT * FROM lead_outcomes WHERE lead_id=? AND outcome='CREDIT_DECISION'", (lid,))]
    assert len(hist) == 2 and all("operator grant" in h["note"] for h in hist)
    page3 = c.get("/admin/credits?token=t").get_data(as_text=True)
    assert page3.count("/credit_decision") == 0   # the decision FORMS only render on queue rows: queue empty


def test_deny_records_reason_ledger_untouched(tmp_path):
    import engine.billing as bl
    m = make_app(tmp_path); c = m.app.test_client()
    lid, tok = refused_lead(tmp_path, m, c)
    buyer = m.con.execute("SELECT buyer_id, price FROM leads WHERE lead_id=?", (lid,)).fetchone()
    r = c.post(f"/admin/lead/{lid}/credit_decision?token=t",
               data={"decision": "deny", "note": "outside window by 24h, policy stands"}, follow_redirects=True)
    assert "untouched" in r.get_data(as_text=True)
    assert [dict(x)["kind"] for x in m.con.execute(
        "SELECT kind FROM buyer_ledger WHERE buyer_id=?", (buyer["buyer_id"],))] == ["LEAD_CHARGE"]
    assert bl.balance(m.con, buyer["buyer_id"], 1) == round(-buyer["price"], 2)
    hist = [dict(x) for x in m.con.execute("SELECT * FROM lead_outcomes WHERE lead_id=? AND outcome='CREDIT_DECISION'", (lid,))]
    assert len(hist) == 1 and "operator deny" in hist[0]["note"]
    trail_row = c.get("/admin/credits?token=t").get_data(as_text=True)
    assert "denied" in trail_row and buyer["buyer_id"] in trail_row
    assert trail_row.count("/credit_decision") == 0   # trail row only, queue empty


def test_decision_validation_and_auth(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    lid, tok = refused_lead(tmp_path, m, c)
    # a note is REQUIRED either way
    loc = c.post(f"/admin/lead/{lid}/credit_decision?token=t", data={"decision": "grant", "note": ""})
    err = loc.headers["Location"]; assert "decision+note+is+required" in err
    # an unknown decision never touches the database
    assert c.post(f"/admin/lead/{lid}/credit_decision?token=t", data={"decision": "maybe", "note": "x"}).status_code == 400
    assert not [dict(x) for x in m.con.execute("SELECT * FROM lead_outcomes WHERE outcome='CREDIT_DECISION'")]
    # the queue + decision route are admin-token guarded like every admin surface
    assert c.get("/admin/credits").status_code == 403
    assert c.get("/admin/credits?token=wrong").status_code == 403
    assert c.post(f"/admin/lead/{lid}/credit_decision", data={"decision": "grant", "note": "n"}).status_code == 403
    # granting on a non-delivered lead is refused before any ledger write
    m.con.execute("UPDATE leads SET routing_status='NO_BUYER', buyer_id='' WHERE lead_id=?", (lid,))
    m.con.commit()
    loc = c.post(f"/admin/lead/{lid}/credit_decision?token=t", data={"decision": "grant", "note": "n"})
    assert "is+not+a+delivered+lead" in loc.headers["Location"]
    assert not m.con.execute(
        "SELECT 1 FROM buyer_ledger WHERE lead_id=? AND kind='LEAD_CREDIT'", (lid,)).fetchone()
