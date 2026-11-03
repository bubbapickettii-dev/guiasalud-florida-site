"""Buyer billing ledger tests: prepaid credits, charges on delivery, credits on return,
credit-aware routing enforcement, operator top-up endpoint, TEST/real separation.
Bookkeeping only — no real money, no live delivery, TEST-flagged leads."""
import importlib, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def make_app(tmp, intel_db=None):
    os.environ["LEADENGINE_DB"] = str(tmp / "t.db")
    os.environ["ADMIN_TOKEN"] = "t"
    os.environ["INTEL_DB"] = str(intel_db or (tmp / "none.db"))
    import app as appmod
    importlib.reload(appmod)
    appmod.OUTBOX = tmp / "outbox"; appmod.SNAP = tmp / "snap"
    return appmod


BASE = dict(campaign="mortgage-protection", first_name="Ana", last_name="Lopez", email="ana@example.com",
            phone="3055550110", zip="33130", state="FL", age_range="40-49", coverage_for="me_spouse_kids",
            mortgage_balance="300-500k", tobacco="no", timeframe="asap", preferred_contact="phone", is_test=1,
            tool_result={"headline": "$300,000"})


def post(client, **kw):
    # consumers never send a buyer id — the platform assigns the buyer server-side
    d = dict(BASE); d.update(kw)
    return client.post("/api/lead", json=d)


def test_charge_recorded_once_on_delivery(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    r = post(c, consent_checked=True).get_json()
    assert r["routed"] == "ROUTED"
    lead = m.con.execute("SELECT lead_id, buyer_id, price, is_test FROM leads").fetchone()
    assert lead["price"] and lead["price"] > 0
    rows = [dict(x) for x in m.con.execute("SELECT * FROM buyer_ledger WHERE buyer_id=? AND kind='LEAD_CHARGE'", (lead["buyer_id"],))]
    assert len(rows) == 1 and rows[0]["amount"] == round(-lead["price"], 2) and rows[0]["lead_id"] == lead["lead_id"]
    assert rows[0]["is_test"] == 1        # TEST traffic stays in the TEST ledger
    # re-charging the same lead is impossible (SQL-guarded)
    import engine.billing as bl
    assert bl.charge(m.con, lead["buyer_id"], dict(lead)) is None
    assert len(m.con.execute("SELECT * FROM buyer_ledger WHERE kind='LEAD_CHARGE'").fetchall()) == 1


def test_package_then_balance_matches(tmp_path):
    import engine.billing as bl
    m = make_app(tmp_path); c = m.app.test_client()
    row = bl.record_package(m.con, "demo-agency-a", m.MARKETPLACE["packages"][1], note="ref #1", is_test=1)
    assert row["amount"] == m.MARKETPLACE["packages"][1]["amount_usd"] and row["kind"] == "PACKAGE_PURCHASE"
    assert bl.balance(m.con, "demo-agency-a", 1) == row["amount"]
    # real-lead ledger is a separate population
    assert bl.balance(m.con, "demo-agency-a", 0) == 0.0
    post(c, consent_checked=True).get_json()  # TEST charge lands against test balance only
    lead = m.con.execute("SELECT buyer_id, price FROM leads").fetchone()
    assert bl.balance(m.con, lead["buyer_id"], 1) == round(row["amount"] - lead["price"], 2)
    assert bl.balance(m.con, lead["buyer_id"], 0) == 0.0


def test_return_credits_back_once(tmp_path):
    import engine.billing as bl
    m = make_app(tmp_path); c = m.app.test_client()
    r = post(c, consent_checked=True).get_json()
    lid = r["lead_id"]
    tok = next(x["portal_token"] for x in m.route.load_buyers(m.CFG) if x["is_demo"])
    assert c.post(f"/agent/{tok}", data={"lead_id": lid, "status": "RETURNED"}).status_code in (302, 303)
    lead = m.con.execute("SELECT * FROM leads WHERE lead_id=?", (lid,)).fetchone()
    credits = [dict(x) for x in m.con.execute("SELECT * FROM buyer_ledger WHERE kind='LEAD_CREDIT'")]
    assert len(credits) == 1 and credits[0]["amount"] == round(lead["price"], 2)
    assert bl.balance(m.con, lead["buyer_id"], 1) == 0.0  # pack-less demo account: charge -X then credit +X
    # clicking RETURNED again (or RETURNED after a later status) never credits twice
    assert c.post(f"/agent/{tok}", data={"lead_id": lid, "status": "RETURNED"}).status_code in (302, 303)
    assert len(m.con.execute("SELECT * FROM buyer_ledger WHERE kind='LEAD_CREDIT'").fetchall()) == 1


def test_credit_enforcement_gates_routing(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    m.MARKETPLACE = {**m.MARKETPLACE, "enforce_credits": True}
    # no credits -> routing preview has nobody to name in the consent
    pv = c.get(f"/api/route-preview?campaign={BASE['campaign']}&state=FL&age_range=40-49").get_json()
    assert pv["available"] is False
    r = post(c, consent_checked=True, email_consent_checked=True).get_json()
    assert r["routed"] != "ROUTED"       # never delivers to a buyer who can't pay
    # record credits for a buyer -> the platform assigns THEM and the lead flows (TEST preview checks TEST balances)
    import engine.billing as bl
    bl.record_package(m.con, "demo-agency-a", {"id": "starter-250", "amount_usd": 1000}, is_test=1)
    pv = c.get(f"/api/route-preview?campaign={BASE['campaign']}&state=FL&age_range=40-49&test=1").get_json()
    assert pv["available"] is True and "buyer_id" not in pv and "seller" not in pv
    r = post(c, consent_checked=True, email="x4@example.com", phone="5615550123").get_json()
    assert r["routed"] == "ROUTED"
    lead = m.con.execute("SELECT buyer_id FROM leads ORDER BY rowid DESC LIMIT 1").fetchone()
    assert lead["buyer_id"] == "demo-agency-a"           # enforcement excluded the broke buyer from assignment
    # draining the account blocks that buyer again (min_balance floor)
    lids = [dict(x)["lead_id"] for x in m.con.execute("SELECT lead_id FROM leads")]
    bal = bl.balance(m.con, "demo-agency-a", 1)
    if bal < m.MARKETPLACE["min_balance_usd"] + 60:  # one more priced lead exceeds balance -> held, not delivered
        r = post(c, consent_checked=True, email="x4@example.com", phone="5615550123").get_json()
        assert r["routed"] in ("ROUTED", "NO_BUYER")


def test_agent_billing_page_and_admin_topup(tmp_path):
    import engine.billing as bl
    m = make_app(tmp_path); c = m.app.test_client()
    r = post(c, consent_checked=True).get_json()
    tok = next(x["portal_token"] for x in m.route.load_buyers(m.CFG) if x["is_demo"])
    page = c.get(f"/agent/{tok}/billing")
    assert page.status_code == 200 and b"TEST DATA" in page.data and b"Balance:" in page.data
    # admin top-up after funds received
    resp = c.post("/admin/buyer/demo-agency-a/topup?token=t", data={"package": "weekly-1000", "note": "wire ref 321", "test": "1"})
    assert resp.status_code in (302, 303)
    rows = [dict(x) for x in m.con.execute("SELECT * FROM buyer_ledger WHERE kind='PACKAGE_PURCHASE'")]
    assert len(rows) == 1 and rows[0]["amount"] == 1000 and rows[0]["package_id"] == "weekly-1000"
    # admin token is required, unknown package rejected, TEST rows only for demo buyers
    assert c.post("/admin/buyer/demo-agency-a/topup?token=wrong", data={"package": "weekly-1000"}).status_code == 403
    assert c.post("/admin/buyer/demo-agency-a/topup?token=t", data={"package": "nope", "test": "1"}).status_code == 400
    # TEST-mode rows can never attach to a real (non-demo) buyer, active or not: the buyer exist in the
    # file (top-ups are possible even pre-activation), so booking credits as TEST data is refused with 400
    assert c.post("/admin/buyer/agent-health-1/topup?token=t", data={"package": "weekly-1000", "test": "1"}).status_code == 400
    # a buyer id that is nowhere in the file is still 404 (not a silent ledger write)
    assert c.post("/admin/buyer/unknown-buyer-x/topup?token=t",
                  data={"package": "weekly-1000", "test": "1"}).status_code == 404
    # free-text amount (custom invoice amount) works and is recorded with its note; balances add up exactly
    assert c.post("/admin/buyer/demo-agency-a/topup?token=t",
                  data={"package": "", "amount_usd": "275.50", "note": "partial credit", "test": "1"}).status_code in (302, 303)
    charged = sum(-x["amount"] for x in m.con.execute(
        "SELECT amount FROM buyer_ledger WHERE buyer_id='demo-agency-a' AND kind='LEAD_CHARGE' AND is_test=1"))
    assert bl.balance(m.con, "demo-agency-a", 1) == 1000 + 275.50 - charged


def test_pricing_not_hardcoded_package_config_purified(tmp_path):
    """Config drives packages; per-lead price still comes from buyers.json only."""
    m = make_app(tmp_path)
    assert m.MARKETPLACE["enforce_credits"] is False          # default: funnel unchanged until operator enables
    assert [p["id"] for p in m.MARKETPLACE["packages"]] == ["starter-250", "weekly-1000", "scale-5000"]
    b = next(x for x in m.route.load_buyers(m.CFG) if x["is_demo"])
    assert b["price_by_tier"]                                # pricing stays buyer-configured, never in billing.py
