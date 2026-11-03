"""Buyer-visible return-policy guidance on the lead desk/detail pages.

  - the guidance is derived from _return_hint / _returns_allowed — the SAME function that decides
    whether a submitted return is credited — so what the buyer sees and what happens can't drift
  - conditions are shown BEFORE submission: allowed (with window hours), outside window, contacted
    already, returns disabled
  - it is policy text, never a promise: the page states the recorded rules decide at click time
  - a foreign buyer cannot read another buyer's lead page (server-side 404, not just hidden UI)
Bookkeeping only: TEST-funnel leads, no live payments, no real buyers.
"""
import importlib, os, shutil, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_billing import make_app, BASE, post


def demo_token(m):
    return next(x["portal_token"] for x in m.route.load_buyers(m.CFG) if x.get("is_demo"))


def delivered_lead(m, c, phone="3055550110", email="ana@example.com"):
    """A fresh delivered TEST lead; a unique phone + email keeps the platform's real deduplication
    (30-day phone OR email window) from collapsing repeated submissions into one lead."""
    r = post(c, consent_checked=True, phone=phone, email=email).get_json()
    assert r["routed"] == "ROUTED"
    return r["lead_id"]


def test_page_shows_allowed_policy_with_window_hours(tmp_path):
    import app as appmod
    m = make_app(tmp_path); c = m.app.test_client()
    lid = delivered_lead(m, c)
    tok = demo_token(m)
    b = next(x for x in m.route.load_buyers(m.CFG) if x.get("is_demo"))
    page = c.get(f"/agent/{tok}/lead/{lid}").get_data(as_text=True)
    # identical to the hint the submit click would evaluate: same function, current lead + buyer
    assert appmod._return_hint(dict(m.con.execute("SELECT * FROM leads WHERE lead_id=?", (lid,)).fetchone()), b) in page
    assert "72h" in page and "credit back into balance" in page      # the configured window is named
    assert "recorded rules and the ledger decide" in page                    # guidance ≠ decision
    # the desk list carries the same per-lead hint as its tooltip (buyer-specific window honoured)
    desk = c.get(f"/agent/{tok}").get_data(as_text=True)
    assert appmod._return_hint(dict(m.con.execute("SELECT * FROM leads WHERE lead_id=?", (lid,)).fetchone()), b) in desk


def test_page_shows_refusal_reasons_before_clicking(tmp_path):
    import app as appmod
    m = make_app(tmp_path); c = m.app.test_client()
    tok = demo_token(m)
    # 1) outside the return window -> the buyer sees THAT, not just a later refusal
    lid = delivered_lead(m, c)
    m.con.execute("UPDATE leads SET delivered_at=datetime('now','-4 days') WHERE lead_id=?", (lid,)); m.con.commit()
    page = c.get(f"/agent/{tok}/lead/{lid}").get_data(as_text=True)
    assert "no credit: outside the configured return window" in page
    # 2) contacted already -> returns close at first contact (allow_after_contact=false)
    lid2 = delivered_lead(m, c, phone="3055550111", email="ana2@example.com")
    assert c.post(f"/agent/{tok}", data={"lead_id": lid2, "status": "CONTACTED"}).status_code in (302, 303)
    page2 = c.get(f"/agent/{tok}/lead/{lid2}").get_data(as_text=True)
    assert "no credit: lead was already contacted" in page2
    # 3) returns disabled by the operator -> the page says so up front
    m.MARKETPLACE = {**m.MARKETPLACE, "returns": {**m.MARKETPLACE.get("returns", {}), "enabled": False}}
    lid3 = delivered_lead(m, c, phone="3055550112", email="ana3@example.com")
    page3 = c.get(f"/agent/{tok}/lead/{lid3}").get_data(as_text=True)
    assert "no credit: returns are disabled by the operator" in page3


def test_hint_follows_the_same_rules_as_the_real_decision(tmp_path):
    """Prove the displayed guidance and the actual decision can't drift: both run _returns_allowed."""
    import app as appmod
    m = make_app(tmp_path); c = m.app.test_client()
    b = next(x for x in m.route.load_buyers(m.CFG) if x.get("is_demo"))
    lid = delivered_lead(m, c)
    l = dict(m.con.execute("SELECT * FROM leads WHERE lead_id=?", (lid,)).fetchone())
    ok, why = appmod._returns_allowed(l, b)
    assert ok and "credit back into balance" in appmod._return_hint(l, b)   # ok describes the window
    # the per-deal window override and validation fallbacks behave as documented
    assert appmod._effective_window_hours({"return_window_hours": 12}) == 12.0
    base = float(m.MARKETPLACE["returns"]["window_hours"])
    assert appmod._effective_window_hours(None) == base
    for bad in ("nope", "0", "-5"):
        assert appmod._effective_window_hours({"return_window_hours": bad}) == base   # garbage never invents a window
    # after contact, ok flips and the hint carries the same 'why' the click will record
    c.post(f"/agent/{demo_token(m)}", data={"lead_id": lid, "status": "CONTACTED"})
    ok2, why2 = appmod._returns_allowed(dict(m.con.execute("SELECT * FROM leads WHERE lead_id=?", (lid,)).fetchone()), b)
    assert ok2 is False and appmod._return_hint(dict(m.con.execute("SELECT * FROM leads WHERE lead_id=?", (lid,)).fetchone()), b) == "no credit: " + why2


def test_guidance_is_buyer_scoped_no_cross_buyer_access(tmp_path):
    """Another buyer's token cannot open this buyer's lead detail page (authorization on server)."""
    from test_buyer_portal import sandbox, save_buyer
    m = make_app(tmp_path); c = m.app.test_client()
    sandbox(m, tmp_path)   # onboard a real (non-demo) buyer on a temp config copy
    save_buyer(m, c, action="activate")
    lid = delivered_lead(m, c)
    tok = demo_token(m)
    other = next(x["portal_token"] for x in m.route.load_buyers(m.CFG) if x["id"] == "portal-test")
    # deterministic ownership: the lead is re-assigned to the demo buyer in the DB (both buyers were
    # eligible when routing sold it, so the assignment could legitimately have landed on either)
    m.con.execute("UPDATE leads SET buyer_id='demo-agency-a' WHERE lead_id=?", (lid,)); m.con.commit()
    assert c.get(f"/agent/{other}/lead/{lid}").status_code == 404
    assert c.get(f"/agent/{tok}/lead/{lid}").status_code == 200
    desk = c.get(f"/agent/{other}").get_data(as_text=True)
    assert lid not in desk and "Bay Health" in desk   # the lead is not leaked into the foreign desk
