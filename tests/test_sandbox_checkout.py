"""Buyer self-serve SANDBOX top-up flow: with no real payment rail configured, the buyer can
rehearse a credit-pack purchase against a LEADENGINE-made checkout page.

Guarantees under test:
  - confirming records exactly ONE PACKAGE_PURCHASE row, on the is_test=1 (TEST) ledger lane,
    for ANY buyer (demo or real) — a sandbox purchase can never move real-lane balance;
  - the checkout is idempotent (confirm twice → credited once, cancel → nothing, re-confirm a
    cancelled row credits nothing);
  - sandbox rows are buyer-scoped (another buyer's token cannot view or confirm them, 404);
  - production refuses the sandbox entirely (rail-off redirect, zero sandbox rows).
Bookkeeping only: no money, no live delivery, no real buyer activation.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_billing import make_app
from test_buyer_portal import sandbox, save_buyer, buyer_row


def start_sandbox(m, c, tok, pkg="starter-250"):
    return c.post(f"/agent/{tok}/billing/package", data={"package": pkg})


def last_sandbox(m):
    return m.con.execute(
        "SELECT * FROM buyer_payments WHERE provider='sandbox' ORDER BY id DESC LIMIT 1").fetchone()


def pkg_rows(m, buyer_id):
    return [dict(r) for r in m.con.execute(
        "SELECT * FROM buyer_ledger WHERE buyer_id=? AND kind='PACKAGE_PURCHASE' ORDER BY id", (buyer_id,))]


def test_confirmation_credits_test_lane_once_exactly(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    r = start_sandbox(m, c, "demo-agency-a-token")
    assert r.status_code == 302 and "/billing/sandbox/" in r.headers["Location"]
    row = last_sandbox(m)
    assert row["buyer_id"] == "demo-agency-a" and row["is_test"] == 1 and row["status"] == "PENDING"
    assert row["amount_usd"] == 250.0 and "SANDBOX" in row["note"]

    page = c.get(f"/agent/demo-agency-a-token/billing/sandbox/{row['id']}")
    assert page.status_code == 200
    txt = page.get_data(as_text=True)
    assert "SANDBOX checkout" in txt and "TEST lane" in txt and "$250.00" in txt
    assert "no money moves" in txt or "no money is charged" in txt or "nothing is charged" in txt

    r2 = c.post(f"/agent/demo-agency-a-token/billing/sandbox/{row['id']}",
                data={"action": "confirm"})
    assert r2.status_code == 302 and "payment=sandbox_credited" in r2.headers["Location"]
    rows = pkg_rows(m, "demo-agency-a")
    assert len(rows) == 1 and rows[0]["amount"] == 250.0 and rows[0]["is_test"] == 1
    assert "SANDBOX" in rows[0]["note"] and rows[0]["lead_id"] == ""
    import engine.billing as bl
    assert bl.balance(m.con, "demo-agency-a", 1) == 250.0   # TEST lane grew
    assert bl.balance(m.con, "demo-agency-a", 0) == 0.0     # real lane untouched

    # idempotent: confirming again changes nothing
    r3 = c.post(f"/agent/demo-agency-a-token/billing/sandbox/{row['id']}", data={"action": "confirm"})
    assert "payment=sandbox_duplicate" in r3.headers["Location"]
    assert len(pkg_rows(m, "demo-agency-a")) == 1
    assert bl.balance(m.con, "demo-agency-a", 1) == 250.0


def test_cancel_records_nothing_and_cannot_be_reconfirmed_into_credit(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    start_sandbox(m, c, "demo-agency-a-token", pkg="weekly-1000")
    row = last_sandbox(m)
    r = c.post(f"/agent/demo-agency-a-token/billing/sandbox/{row['id']}", data={"action": "cancel"})
    assert r.status_code == 302 and "payment=sandbox_cancelled" in r.headers["Location"]
    assert last_sandbox(m)["status"] == "FAILED"
    assert pkg_rows(m, "demo-agency-a") == []
    import engine.billing as bl
    assert bl.balance(m.con, "demo-agency-a", 1) == 0.0
    # a cancelled row stays cancelled: confirming it credits nothing
    r2 = c.post(f"/agent/demo-agency-a-token/billing/sandbox/{row['id']}", data={"action": "confirm"})
    assert "payment=sandbox_duplicate" in r2.headers["Location"]
    assert pkg_rows(m, "demo-agency-a") == []


def _real_buyer_confirmed_page(tmp_path):
    """A real (non-demo) buyer rehearses one sandbox purchase. Verifies TEST-lane-only bookkeeping
    and returns (page_from_credited_redirect, client, buyer_token) for label/banner assertions."""
    m = make_app(tmp_path); sandbox(m, tmp_path); c = m.app.test_client()
    save_buyer(m, c, action="activate")
    tok = buyer_row(tmp_path / "cfg", "portal-test")["portal_token"]
    r = start_sandbox(m, c, tok)          # no "test" flag: the rail is off anyway
    assert "/billing/sandbox/" in r.headers["Location"]
    row = last_sandbox(m)
    assert row["is_test"] == 1            # hard-wired, regardless of buyer kind or form flags
    # exactly one credit, on the TEST lane, and the real-lane balance stays at zero
    cred_loc = c.post(f"/agent/{tok}/billing/sandbox/{row['id']}",
                      data={"action": "confirm"}).headers["Location"]
    assert "payment=sandbox_credited" in cred_loc
    rows = pkg_rows(m, "portal-test")
    assert len(rows) == 1 and rows[0]["is_test"] == 1 and rows[0]["amount"] == 250.0
    import engine.billing as bl
    assert bl.balance(m.con, "portal-test", 0) == 0.0
    assert bl.balance(m.con, "portal-test", 1) == 250.0
    # confirming again stays safe: nothing new is credited
    assert "payment=sandbox_duplicate" in c.post(
        f"/agent/{tok}/billing/sandbox/{row['id']}", data={"action": "confirm"}).headers["Location"]
    # the billing page after returning from the sandbox labels the purchase (TEST pill,
    # sandbox rail) and its banner tells the truth: TEST credits, no money moved
    page = c.get(cred_loc).get_data(as_text=True)
    return page, c, tok


def test_real_buyer_sandbox_never_touches_the_real_lane(tmp_path):
    """The critical one: a demoed REAL buyer rehearsing a purchase must see TEST-lane credits only."""
    page, _c, _tok = _real_buyer_confirmed_page(tmp_path)
    assert "Buy (TEST sandbox)" in page and ">TEST</span>" in page


def test_real_buyer_billing_page_banner_tells_the_truth(tmp_path):
    page, _c, _tok = _real_buyer_confirmed_page(tmp_path)
    assert "as TEST credits" in page and "no money moved" in page


def test_sandbox_rows_are_buyer_scoped(tmp_path):
    m = make_app(tmp_path); sandbox(m, tmp_path); c = m.app.test_client()
    save_buyer(m, c, action="activate")
    tok = buyer_row(tmp_path / "cfg", "portal-test")["portal_token"]
    start_sandbox(m, c, "demo-agency-a-token")
    row = last_sandbox(m)
    # another buyer's token: neither the page nor the confirm action is reachable (404, no credit)
    assert c.get(f"/agent/{tok}/billing/sandbox/{row['id']}").status_code == 404
    r = c.post(f"/agent/{tok}/billing/sandbox/{row['id']}", data={"action": "confirm"})
    assert r.status_code == 404
    assert pkg_rows(m, "demo-agency-a") == []
    # nonsense rowids and unknown packages are refused, not crashed
    assert c.get("/agent/demo-agency-a-token/billing/sandbox/999999").status_code == 404
    assert c.post("/agent/demo-agency-a-token/billing/sandbox/999999",
                  data={"action": "confirm"}).status_code == 404
    assert start_sandbox(m, c, "demo-agency-a-token", pkg="no-such-pack").status_code == 400
    # the owner can still confirm after the trespass attempts
    r2 = c.post(f"/agent/demo-agency-a-token/billing/sandbox/{row['id']}", data={"action": "confirm"})
    assert "payment=sandbox_credited" in r2.headers["Location"]
    assert [x["is_test"] for x in pkg_rows(m, "demo-agency-a")] == [1]


def test_production_refuses_the_sandbox_entirely(tmp_path, monkeypatch):
    m = make_app(tmp_path); c = m.app.test_client()
    monkeypatch.setenv("LEADENGINE_ENV", "production")
    r = start_sandbox(m, c, "demo-agency-a-token")
    assert r.status_code == 302 and "payment=rail_off" in r.headers["Location"]
    assert last_sandbox(m) is None                      # no sandbox row was ever created
    assert pkg_rows(m, "demo-agency-a") == []
    import engine.payments as pay
    assert pay.sandbox_allowed() is False
