"""Admin /admin/economics: recorded money must equal the ledger + leads tables, and the
SCENARIO calculator must stay arithmetic over explicit assumptions — never a result or
forecast. Reuses test_billing.make_app so ledger moves come from the real billing engine.

Run: python -m pytest tests/test_admin_economics.py -q
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_billing import make_app, BASE, post


def test_recorded_money_reconciles_after_charge_and_return(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    r = post(c, consent_checked=True).get_json()
    lid = r["lead_id"]
    tok = next(x["portal_token"] for x in m.route.load_buyers(m.CFG) if x["is_demo"])
    c.post(f"/agent/{tok}", data={"lead_id": lid, "status": "RETURNED"})
    assert c.get("/admin/economics").status_code == 403                 # admin-token guarded
    page = c.get("/admin/economics?token=t").get_data(as_text=True)
    assert page
    assert page.count("LEAD_CHARGE") >= 1 and page.count("LEAD_CREDIT") >= 1   # the move is rendered
    assert page.count("matches") == 4                                  # every check reconciles: 1 charge=1 delivered, 1 credit=1 returned outcome


def test_reconciliation_flags_drift(tmp_path):
    """If the ledger and the leads table disagree (simulated: remove the credit row), the page
    names the mismatch instead of claiming everything matches."""
    m = make_app(tmp_path); c = m.app.test_client()
    r = post(c, consent_checked=True).get_json()
    lid = r["lead_id"]
    tok = next(x["portal_token"] for x in m.route.load_buyers(m.CFG) if x["is_demo"])
    c.post(f"/agent/{tok}", data={"lead_id": lid, "status": "RETURNED"})
    import engine.billing as bl
    m.con.execute("DELETE FROM buyer_ledger WHERE kind='LEAD_CREDIT'"); m.con.commit()
    page = c.get("/admin/economics?token=t").get_data(as_text=True)
    assert "1 returned leads vs 0 ledger credits" in page


def test_scenario_calculator_is_arithmetic_and_labeled(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    page = c.get("/admin/economics?token=t&leads=4&price=50&return_pct=10").get_data(as_text=True)
    assert "SCENARIO" in page and "EDITABLE" in page.upper()            # never a silent projection
    assert "$1,260.00" in page          # 4/day x $50 x 7 - 10% refund allowance = 1400 - 140
    assert "$140.00" in page            # the refund line is shown separately, not hidden
    # empty inputs fall back to recorded-config defaults without 500ing
    r = c.get("/admin/economics?token=t&leads=abc"); assert r.status_code == 200
