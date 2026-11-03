"""Buyer-facing return & credit history on the billing page (evidence from existing records only).

  - no return history -> a clear empty state (not a fake zero)
  - a refused return request shows with its actual refusal reason and NO ledger credit
  - an operator GRANT is only "credited" when the buyer_ledger confirms the LEAD_CREDIT
  - an operator DENY shows without any credit amount
  - a ledger credit that exists without a recorded request (desk heal / direct path) still reads as credited
  - isolation: another buyer's billing page never lists this buyer's return rows
Bookkeeping only: TEST-funnel leads, no live payments, no real buyers.
"""
import importlib, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_billing import make_app
from test_buyer_portal import sandbox, save_buyer
from test_credit_queue import refused_lead, demo_token
from test_return_guidance import delivered_lead


def billing_page(m, tok):
    return m.app.test_client().get(f"/agent/{tok}/billing").get_data(as_text=True)


def test_returned_status_cell_agrees_with_ledger(tmp_path):
    """The Delivered-leads table must never claim money the ledger does not show: a RETURNED status
    with no LEAD_CREDIT row reads as 'returned — credit refused', only a real ledger credit may say
    'returned & credited'. A refused return used to render as credited on this very table."""
    m = make_app(tmp_path); c = m.app.test_client()
    lid, tok = refused_lead(tmp_path, m, c)
    cell = c.get(f"/agent/{tok}/billing").get_data(as_text=True)
    assert "returned — credit refused (operator review)" in cell   # no credit claimed
    assert ">returned &amp; credited<" not in cell                 # no delivered-leads row claims credited
    c.post(f"/admin/lead/{lid}/credit_decision?token=t", data={"decision": "grant", "note": "operator grant"})
    cell2 = c.get(f"/agent/{tok}/billing").get_data(as_text=True)
    assert ">returned &amp; credited<" in cell2                     # backed by the LEAD_CREDIT row
    assert m.con.execute("SELECT COUNT(*) FROM buyer_ledger WHERE lead_id=? AND kind='LEAD_CREDIT'",
                         (lid,)).fetchone()[0] == 1                 # ...and exactly once, unchanged rules


def test_empty_history_state(tmp_path):
    m = make_app(tmp_path)
    delivered_lead(m, m.app.test_client())      # delivered, but no return ever submitted
    page = billing_page(m, demo_token(m))
    assert "Return &amp; credit history" in page
    assert "No returns submitted yet" in page


def test_refused_request_shows_reason_and_no_credit(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    lid, tok = refused_lead(tmp_path, m, c)
    lead = m.con.execute("SELECT price FROM leads WHERE lead_id=?", (lid,)).fetchone()
    assert not m.con.execute("SELECT 1 FROM buyer_ledger WHERE lead_id=? AND kind='LEAD_CREDIT'", (lid,)).fetchone()
    page = billing_page(m, tok)
    assert lid in page                                          # listed, scoped to THIS buyer
    assert "Awaiting operator review" in page                   # decision not made yet
    assert "no credit: outside the configured return window" in page   # the recorded refusal reason
    assert "(ledger)" not in page                               # no credit amount claimed from the ledger


def test_grant_only_counts_as_credit_via_ledger(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    lid, tok = refused_lead(tmp_path, m, c)
    price = m.con.execute("SELECT price FROM leads WHERE lead_id=?", (lid,)).fetchone()["price"]
    r = c.post(f"/admin/lead/{lid}/credit_decision?token=t",
               data={"decision": "grant", "note": "operator verified the seller's stale data"}, follow_redirects=True)
    assert "credited" in r.get_data(as_text=True)
    page = billing_page(m, tok)
    assert "Credited back to your balance" in page
    assert "+$" + "{:.2f}".format(round(price, 2)) in page and "(ledger)" in page
    assert "operator grant" in page and "operator verified" in page    # the written decision is visible


def test_deny_decision_shows_no_credit(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    lid, tok = refused_lead(tmp_path, m, c)
    c.post(f"/admin/lead/{lid}/credit_decision?token=t",
           data={"decision": "deny", "note": "outside window; policy stands"})
    page = billing_page(m, tok)
    assert "Operator denied" in page and "policy stands" in page
    assert "(ledger)" not in page


def test_ledger_credit_reads_credited_even_without_request_record(tmp_path):
    """A LEAD_CREDIT row IS the money-back proof: it renders as credited even if the request outcome
    row is missing (desk heal / direct path) — and nothing is invented beyond it. The fixture lead
    also carries a NULL price on purpose: the Delivered-leads table renders every DELIVERED lead, so
    a price-less row must never 500 the page."""
    m = make_app(tmp_path)
    tok = demo_token(m)
    demo_id = next(x["id"] for x in m.route.load_buyers(m.CFG) if x.get("is_demo"))
    m.con.execute("""INSERT INTO leads (lead_id, created_at, is_test, campaign, product, state, zip, phone, phone_source,
        qual_status, lead_type, tier, score, routing_status, buyer_id, contact_scope, consent_ts, delivered_at, price)
        VALUES ('LH1', datetime('now'), 1, 'camp', 'mortgage_protection', 'FL', '33101', '3055550120', 'CONSUMER_SUBMITTED',
        'QUALIFIED', 'PHONE_LEAD', 'A', 60, 'DELIVERED', ?, 'PHONE_AND_EMAIL', datetime('now'), NULL, NULL)""", (demo_id,))
    m.con.execute("INSERT INTO buyer_ledger (ts,buyer_id,lead_id,package_id,kind,amount,balance_after,note,is_test) "
                  "VALUES (?,?,?,'','LEAD_CREDIT',60.0,0,'returned tier A',1)", ("2026-10-20T10:00:00", demo_id, "LH1"))
    m.con.commit()
    page = billing_page(m, tok)   # must not 500 despite the NULL-priced delivered lead
    assert "Credited back to your balance" in page and "+$60.00" in page and "(ledger)" in page
    assert "LH1" in page                          # the Delivered-leads row rendered too (price-less -> $0.00)


def test_history_is_buyer_scoped(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    sandbox(m, tmp_path); save_buyer(m, c, action="activate")
    # a delivered lead the demo buyer owns but returned outside the window -> real refusal record
    lid = delivered_lead(m, c)
    m.con.execute("UPDATE leads SET buyer_id='demo-agency-a', delivered_at=datetime('now','-4 days') WHERE lead_id=?", (lid,))
    m.con.commit()
    tok = demo_token(m)
    assert c.post(f"/agent/{tok}", data={"lead_id": lid, "status": "RETURNED"}).status_code in (302, 303)
    assert [dict(x) for x in m.con.execute("SELECT 1 FROM lead_outcomes WHERE lead_id=? AND outcome='RETURNED'", (lid,))]
    other = next(x["portal_token"] for x in m.route.load_buyers(m.CFG) if x["id"] == "portal-test")
    opage = billing_page(m, other)
    assert lid not in opage                     # the foreign buyer never sees this row
    assert "No returns submitted yet" in opage  # their own (empty) history
    assert "Bay Health" in opage                # it is still THEIR billing page
    ppage = billing_page(m, tok)
    assert lid in ppage and "Return &amp; credit history" in ppage
