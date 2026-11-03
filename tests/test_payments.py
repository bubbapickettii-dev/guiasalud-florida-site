"""Payment rail tests (Stripe Checkout, test mode only):

  - a checkout session is created SERVER-side from config prices (never from the browser)
  - the browser success page grants NOTHING
  - credits land ONLY via the signature-verified webhook (checkout.session.completed / async_payment_succeeded
    with payment_status == 'paid')
  - fulfillment is idempotent twice over: unique webhook_event_id + one-way PENDING -> PAID -> CREDITED
  - a session that was never created server-side is never credited
  - sk_live_ keys are refused unless explicitly approved; no live charges happen in tests

No network access: the stripe SDK calls are stubbed on the module created by make_app.
"""
import importlib, json, os, sys, types
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_billing import BASE, make_app, post  # shared fixture helpers


def tok(m, buyer_id="demo-agency-a"):
    """The buyer's portal token straight from config/buyers.json."""
    return next(b for b in __import__("engine.route", fromlist=["load_buyers"]).load_buyers(m.CFG)
                if b["id"] == buyer_id)["portal_token"]


def _stub_stripe(m, session_id="cs_test_123", url="https://checkout.stripe.com/c/pay/cs_test_123"):
    """Replace the two SDK entry points with fakes; returns (create_calls, construct_events)."""
    calls, events = [], []

    fake = types.ModuleType("stripe")
    fake.checkout = types.SimpleNamespace(Session=types.SimpleNamespace(
        create=lambda **kw: (calls.append(kw), {"id": session_id, "url": url})[1]))
    fake.Webhook = types.SimpleNamespace(construct_event=lambda payload, sig, secret: events.pop(0))
    sys.modules["stripe"] = fake
    return calls, events


def _evt(kind, sid, payment_status="paid", eid="evt_1"):
    return {"id": eid, "type": kind, "data": {"object": {"id": sid, "payment_status": payment_status}}}


def test_checkout_created_server_side_and_no_credits(tmp_path, monkeypatch):
    m = make_app(tmp_path)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_abc")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_abc")
    import engine.payments as pay
    calls, _ = _stub_stripe(m)
    # rebind module attrs in app.py's namespace
    monkeypatch.setattr(m, "payments", pay)

    c = m.app.test_client()
    r = c.post(f"/agent/{tok(m)}/billing/package", data={"package": "starter-250", "test": "1"})
    assert r.status_code == 302 and "checkout.stripe.com" in r.headers["Location"]
    assert calls and calls[0]["mode"] == "payment"
    assert calls[0]["client_reference_id"] == "demo-agency-a"
    assert calls[0]["line_items"][0]["price_data"]["unit_amount"] == 25000   # $250.00 — from config, server-side
    assert calls[0]["line_items"][0]["price_data"]["currency"] == "usd"
    row = m.con.execute("SELECT * FROM buyer_payments").fetchone()
    assert row["status"] == "PENDING" and row["amount_usd"] == 250.0 and row["is_test"] == 1
    # the browser success page did NOT credit anything
    balance = m.con.execute("SELECT COALESCE(SUM(amount),0) FROM buyer_ledger WHERE kind='PACKAGE_PURCHASE'").fetchone()[0]
    assert balance == 0.0
    # the billing page still renders after a purchase attempt
    pg = c.get(f"/agent/{tok(m)}/billing?payment=returning")
    assert pg.status_code == 200 and "pending" in pg.get_data(as_text=True).lower()


def test_webhook_grants_credits_only_on_verified_paid_event(tmp_path, monkeypatch):
    m = make_app(tmp_path)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_abc")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_abc")
    import engine.payments as pay
    _, events = _stub_stripe(m)
    monkeypatch.setattr(m, "payments", pay)
    chk = pay.create_checkout(m.con, {"id": "demo-agency-a", "portal_token": "tok"}, m.MARKETPLACE["packages"][0], "http://x", m.MARKETPLACE, is_test=1)
    assert chk and chk["session_id"] == "cs_test_123" and chk["amount_usd"] == 250.0
    m.con.commit()

    c = m.app.test_client()
    fake = sys.modules["stripe"]          # the stub installed by _stub_stripe (imported inside the engine)
    # forged/no signature never reaches the DB: construct_event raises
    def boom(payload, sig, secret): raise Exception("SignatureVerificationError")
    fake.Webhook.construct_event = boom
    r = c.post("/api/webhooks/stripe", data=b"{}", headers={"Stripe-Signature": "bad"})
    assert r.status_code == 400
    assert m.con.execute("SELECT COUNT(*) FROM buyer_payments WHERE status!='PENDING'").fetchone()[0] == 0

    # a signed completed event with payment_status != 'paid' grants nothing
    fake.Webhook.construct_event = lambda p, s, h: _evt("checkout.session.completed", "cs_test_123", payment_status="unpaid", eid="evt_0")
    r = c.post("/api/webhooks/stripe", data=b"{}", headers={"Stripe-Signature": "t=1,v1=x"})
    assert r.status_code == 200 and "no credits" in r.get_json()["reason"]
    assert m.con.execute("SELECT COALESCE(SUM(amount),0) FROM buyer_ledger WHERE kind='PACKAGE_PURCHASE'").fetchone()[0] == 0.0

    # signed paid event -> credits exactly the server-recorded amount, into the TEST ledger
    fake.Webhook.construct_event = lambda p, s, h: _evt("checkout.session.completed", "cs_test_123", eid="evt_1")
    r = c.post("/api/webhooks/stripe", data=b"{}", headers={"Stripe-Signature": "t=1,v1=x"})
    assert r.status_code == 200 and r.get_json()["state"] == "credited"
    row = m.con.execute("SELECT * FROM buyer_payments").fetchone()
    assert row["status"] == "CREDITED" and row["webhook_event_id"] == "evt_1"
    led = m.con.execute("SELECT * FROM buyer_ledger WHERE kind='PACKAGE_PURCHASE'").fetchone()
    assert led["amount"] == 250.0 and led["buyer_id"] == "demo-agency-a" and led["is_test"] == 1

    # Stripe retry (same event id) cannot double-credit
    r = c.post("/api/webhooks/stripe", data=b"{}", headers={"Stripe-Signature": "t=1,v1=x"})
    assert r.get_json()["state"] == "duplicate"
    # a different event id for the same already-CREDITED session also cannot double-credit
    fake.Webhook.construct_event = lambda p, s, h: _evt("checkout.session.async_payment_succeeded", "cs_test_123", eid="evt_2")
    r = c.post("/api/webhooks/stripe", data=b"{}", headers={"Stripe-Signature": "t=1,v1=x"})
    assert r.get_json()["state"] == "duplicate"
    assert m.con.execute("SELECT COUNT(*) FROM buyer_ledger WHERE kind='PACKAGE_PURCHASE'").fetchone()[0] == 1


def test_webhook_ignores_unknown_session_and_failure_events(tmp_path, monkeypatch):
    m = make_app(tmp_path)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_abc")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_abc")
    import engine.payments as pay
    _, events = _stub_stripe(m)
    fake = sys.modules["stripe"]
    monkeypatch.setattr(m, "payments", pay)
    c = m.app.test_client()

    # paid event for a session the server never created -> refused, nothing credited
    fake.Webhook.construct_event = lambda p, s, h: _evt("checkout.session.completed", "cs_unknown", eid="evt_unknown")
    r = c.post("/api/webhooks/stripe", data=b"{}", headers={"Stripe-Signature": "t=1,v1=x"})
    assert r.status_code == 200 and r.get_json()["state"] == "not_found"
    assert m.con.execute("SELECT COUNT(*) FROM buyer_ledger").fetchone()[0] == 0

    # failure events mark the PENDING row FAILED (row created by a server-side checkout)
    chk = pay.create_checkout(m.con, {"id": "demo-agency-a", "portal_token": "tok"}, m.MARKETPLACE["packages"][0], "http://x", m.MARKETPLACE, is_test=1)
    assert chk and m.con.execute("SELECT COUNT(*) FROM buyer_payments WHERE status='PENDING'").fetchone()[0] == 1
    m.con.commit()
    fake.Webhook.construct_event = lambda p, s, h: _evt("checkout.session.async_payment_failed", "cs_test_123", eid="evt_fail")
    r = c.post("/api/webhooks/stripe", data=b"{}", headers={"Stripe-Signature": "t=1,v1=x"})
    assert r.status_code == 200
    assert m.con.execute("SELECT status FROM buyer_payments").fetchone()[0] == "FAILED"


def test_live_keys_refused_without_approval_and_off_rail_by_default(tmp_path, monkeypatch):
    m = make_app(tmp_path)
    c = m.app.test_client()
    # no keys -> rail off. Outside production the buyer is offered the SANDBOX TEST checkout
    # (provider='sandbox', TEST lane, no Stripe session, no credits); production redirects rail_off.
    r = c.post(f"/agent/{tok(m)}/billing/package", data={"package": "starter-250", "test": "1"})
    assert r.status_code == 302
    row = m.con.execute("SELECT * FROM buyer_payments ORDER BY id DESC LIMIT 1").fetchone()
    assert row["provider"] == "sandbox" and row["is_test"] == 1 and row["session_id"] is None
    assert m.con.execute("SELECT COUNT(*) FROM buyer_payments WHERE provider='stripe'").fetchone()[0] == 0
    assert m.con.execute("SELECT COUNT(*) FROM buyer_ledger WHERE kind='PACKAGE_PURCHASE'").fetchone()[0] == 0

    # sk_live_ without live_payments config and without production env -> still off (the Stripe rail
    # refuses; the buyer again lands on the sandbox TEST checkout, never a live session)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_abc")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_abc")
    import engine.payments as pay
    monkeypatch.setattr(m, "payments", pay)
    n_sandbox = m.con.execute("SELECT COUNT(*) FROM buyer_payments WHERE provider='sandbox'").fetchone()[0]
    r = c.post(f"/agent/{tok(m)}/billing/package", data={"package": "starter-250", "test": "1"})
    assert "/billing/sandbox/" in r.headers["Location"]
    assert m.con.execute("SELECT COUNT(*) FROM buyer_payments WHERE provider='sandbox'").fetchone()[0] == n_sandbox + 1
    assert m.con.execute("SELECT COUNT(*) FROM buyer_payments WHERE provider='stripe'").fetchone()[0] == 0
    # and no webhook secret means the endpoint itself refuses (503)
    _, events = _stub_stripe(m)
    fake = sys.modules["stripe"]
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET")
    fake.Webhook.construct_event = lambda p, s, h: _evt("checkout.session.completed", "cs_test_123")
    r = c.post("/api/webhooks/stripe", data=b"{}", headers={"Stripe-Signature": "t=1,v1=x"})
    assert r.status_code == 503


def test_return_rules_enforced_and_recorded(tmp_path):
    m = make_app(tmp_path)
    m.MARKETPLACE.update({"returns": {"enabled": True, "window_hours": 24, "allow_after_contact": False,
                                      "reasons": ["unreachable", "no_agency_capacity"], "default_reason": "other"}})
    c = m.app.test_client()
    r = post(c, consent_checked=True).get_json()
    lead = dict(m.con.execute("SELECT * FROM leads").fetchone())
    portal = tok(m, lead["buyer_id"])

    def set_status(st, **kw):
        d = {"lead_id": lead["lead_id"], "status": st, **kw}
        return c.post(f"/agent/{portal}", data=d)

    # returned inside the window -> credited back once, reason recorded
    assert set_status("RETURNED", reason="unreachable").status_code == 302
    credits = m.con.execute("SELECT * FROM buyer_ledger WHERE kind='LEAD_CREDIT'").fetchall()
    assert len(credits) == 1 and credits[0]["lead_id"] == lead["lead_id"]
    note = m.con.execute("SELECT note FROM lead_outcomes WHERE outcome='RETURNED' ORDER BY id DESC").fetchone()[0]
    assert "unreachable" in note
    bal = m.con.execute("SELECT COALESCE(SUM(amount),0) FROM buyer_ledger WHERE buyer_id=? AND is_test=1", (lead["buyer_id"],)).fetchone()[0]
    assert bal == 0.0   # charged then credited back

    # clicking RETURN again does not credit twice (SQL-guarded)
    set_status("RETURNED", reason="unreachable")
    assert m.con.execute("SELECT COUNT(*) FROM buyer_ledger WHERE kind='LEAD_CREDIT'").fetchone()[0] == 1

    # outside the window -> recorded refusal, no second credit
    m.con.execute("UPDATE leads SET delivered_at='2020-01-01T00:00:00+00:00' WHERE lead_id=?", (lead["lead_id"],))
    m.con.execute("UPDATE leads SET agent_status='CONTACTED' WHERE lead_id=?", (lead["lead_id"],))
    m.con.commit()
    r = set_status("RETURNED", reason="no_agency_capacity")
    assert r.status_code == 302
    note = m.con.execute("SELECT note FROM lead_outcomes WHERE outcome='RETURNED' ORDER BY id DESC").fetchone()[0]
    assert "no credit" in note
    assert m.con.execute("SELECT COUNT(*) FROM buyer_ledger WHERE kind='LEAD_CREDIT'").fetchone()[0] == 1
