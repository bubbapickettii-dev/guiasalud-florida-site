"""Webhook delivery reliability for the sold lead:

  - a webhook 4xx/5xx during delivery still completes the SALE (consent named this buyer, lead delivered
    to the portal) but records a FAILED delivery row — the buyer's CRM never got it
  - the buyer's desk re-posts the EXACT recorded payload automatically, appends RESENT/FAILED rows,
    and stops retrying after the budget is exhausted (operator conversation, not infinite retries)
  - a webhook that RAISES during the first delivery leaves the sale stranded as ROUTED — recovery
    delivers + charges it exactly once once the webhook is healthy again
No live network calls: requests.post is stubbed. No live payments. TEST-flagged leads.
Onboarding runs on a TEMP COPY of config/buyers.json — tests never touch the real config.
"""
import json, sys, urllib.parse
from pathlib import Path

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_billing import make_app, post


class _StubResp:
    def __init__(self, ok):
        self.ok, self.status_code, self.text = ok, (200 if ok else 502), "stub"


def _patch_requests(monkey_fn):
    import requests as req
    orig = req.post
    req.post = monkey_fn
    return orig


def save_buyer(m, method="webhook", url="https://crm.example/hook", tmp=None):
    if tmp is not None:                      # sandbox: onboarding writes buyers.json — stay on a temp copy
        import shutil
        cfg = tmp / "cfg"
        shutil.copytree(m.CFG, cfg)
        m.CFG = cfg
    c = m.app.test_client()
    form = {"buyer_id": "hookbuyer", "action": "activate", "name": "Hooked CRM LLC", "license_number": "A444",
            "npn": "666666", "licensed_states": "FL", "products": "mortgage_protection", "tiers": "A,B",
            "price_A": "60", "price_B": "35", "daily_cap": "5", "age_min": "30", "age_max": "64",
            "delivery_method": method, "delivery_webhook": url}
    if method != "webhook":
        form["products"] = "mortgage_protection"
        form2 = dict(form); form2["delivery_method"] = "outbox"       # keep the funnel routable
        c.post("/admin/buyers?token=t", data={**form, "delivery_method": "outbox"})
        return c, form
    c.post("/admin/buyers?token=t", data=form)
    return c, form


def _advance_webhook(results):
    """Queue of results for successive webhook posts (True ok / exception raises). The platform now
    posts the RAW payload bytes with an x-ticket-signature header — the stub accepts that call shape
    and records the last post's headers so tests can assert the HMAC signature is present."""
    import requests as req
    state = {"orig": req.post, "i": 0, "last": {}}

    def fake(url, json=None, timeout=None, data=None, headers=None, **kw):
        r = results[min(state["i"], len(results) - 1)]
        state["i"] += 1
        state["last"] = {"url": url, "data": data, "headers": headers or {}}
        if r == "raise":
            raise ConnectionError("crm unreachable")
        return _StubResp(r) if isinstance(r, bool) else r
    req.post = fake
    return state


def test_failed_webhook_is_resent_from_recorded_payload_with_budget(tmp_path):
    m = make_app(tmp_path); c, form = save_buyer(m, tmp=tmp_path)
    import os
    os.environ["LEADENGINE_LIVE_DELIVERY"] = "1"
    state = _advance_webhook([False, True])          # first attempt 502, retry OK
    try:
        r =    post(c, consent_checked=True).get_json()   # real buyer outranks demo placeholders
        assert r["routed"] == "ROUTED"
        lead = m.con.execute("SELECT * FROM leads").fetchone()
        assert lead["routing_status"] == "DELIVERED" and lead["buyer_id"] == "hookbuyer"
        rows = [dict(x) for x in m.con.execute(
            "SELECT method, status FROM deliveries WHERE lead_id=? AND method='webhook'", (lead["lead_id"],))]
        assert [x["status"] for x in rows] == ["FAILED"]         # sale stands, CRM missed it
        # a webhook POST is signed with the onboarding-issued secret: sha256=HMAC-SHA256 over the raw body,
        # verifiable buyer-side from the x-ticket-signature header
        sig = state["last"].get("headers", {}).get("x-ticket-signature", "")
        assert sig.startswith("sha256="), sig
        secret = next(x["delivery"]["webhook_secret"] for x in json.loads((m.CFG / "buyers.json").read_text(encoding="utf-8"))
                      if x["id"] == "hookbuyer")
        import hashlib, hmac as _hmac
        expect = "sha256=" + _hmac.new(secret.encode("utf-8"), state["last"]["data"], hashlib.sha256).hexdigest()
        assert sig == expect, "signature must be HMAC-SHA256 over the raw posted body with the buyer's secret"
        charge = m.con.execute("SELECT COUNT(*) FROM buyer_ledger WHERE lead_id=? AND kind='LEAD_CHARGE'",
                               (lead["lead_id"],)).fetchone()[0]
        assert charge == 1                                       # charged for a delivered lead
        # the buyer's desk redelivers from the ORIGINAL payload file
        tok = next(x["portal_token"] for x in json.loads((m.CFG / "buyers.json").read_text(encoding="utf-8"))
                   if x["id"] == "hookbuyer")
        c.get(f"/agent/{tok}")
        rows = [dict(x)["status"] for x in m.con.execute(
            "SELECT status FROM deliveries WHERE lead_id=? AND method='webhook' ORDER BY id", (lead["lead_id"],))]
        assert rows == ["FAILED", "RESENT"]
        # idempotence: another desk load adds nothing new
        c.get(f"/agent/{tok}")
        rows = [dict(x)["status"] for x in m.con.execute(
            "SELECT status FROM deliveries WHERE lead_id=? AND method='webhook' ORDER BY id", (lead["lead_id"],))]
        assert rows == ["FAILED", "RESENT"]
    finally:
        os.environ.pop("LEADENGINE_LIVE_DELIVERY", None)
        import requests as req
        req.post = state["orig"]


def test_webhook_budget_exhaustion_stops_retrying(tmp_path):
    m = make_app(tmp_path); c, form = save_buyer(m, tmp=tmp_path)
    import os
    os.environ["LEADENGINE_LIVE_DELIVERY"] = "1"
    state = _advance_webhook([False])                # every attempt fails
    try:
        r =    post(c, consent_checked=True).get_json()   # real buyer outranks demo placeholders
        lead = m.con.execute("SELECT * FROM leads").fetchone()
        tok = next(x["portal_token"] for x in json.loads((m.CFG / "buyers.json").read_text(encoding="utf-8"))
                   if x["id"] == "hookbuyer")
        for _ in range(7):                            # 5 retry budget + slack
            c.get(f"/agent/{tok}")
        rows = [dict(x)["status"] for x in m.con.execute(
            "SELECT status FROM deliveries WHERE lead_id=? AND method='webhook' ORDER BY id", (lead["lead_id"],))]
        assert rows.count("FAILED") == 1 + 5          # the original attempt + the full budget, never more
    finally:
        os.environ.pop("LEADENGINE_LIVE_DELIVERY", None)
        import requests as req
        req.post = state["orig"]


def test_webhook_crash_strands_then_recovery_sells_once(tmp_path):
    m = make_app(tmp_path); c, form = save_buyer(m, tmp=tmp_path)
    import os, engine.route as ro
    os.environ["LEADENGINE_LIVE_DELIVERY"] = "1"
    state = _advance_webhook(["raise", True])         # crash at delivery, success at recovery
    try:
        r =    post(c, consent_checked=True).get_json()   # real buyer outranks demo placeholders
        assert r["routed"] == "ROUTED"
        lead = m.con.execute("SELECT * FROM leads").fetchone()
        assert lead["routing_status"] == "ROUTED"     # stranded: nothing delivered, nothing charged
        assert m.con.execute("SELECT COUNT(*) FROM buyer_ledger WHERE kind='LEAD_CHARGE'").fetchone()[0] == 0
        tok = next(x["portal_token"] for x in json.loads((m.CFG / "buyers.json").read_text(encoding="utf-8"))
                   if x["id"] == "hookbuyer")
        c.get(f"/agent/{tok}")                        # the desk heals the sale
        healed = m.con.execute("SELECT * FROM leads WHERE lead_id=?", (lead["lead_id"],)).fetchone()
        assert healed["routing_status"] == "DELIVERED"
        charges = [dict(x) for x in m.con.execute(
            "SELECT amount FROM buyer_ledger WHERE lead_id=? AND kind='LEAD_CHARGE'", (lead["lead_id"],))]
        assert len(charges) == 1 and charges[0]["amount"] == round(-float(lead["price"]), 2)
        rows = [dict(x)["status"] for x in m.con.execute(
            "SELECT status FROM deliveries WHERE lead_id=? AND method='webhook' ORDER BY id", (lead["lead_id"],))]
        assert rows == ["SENT"]                       # one successful delivery, exactly one
    finally:
        os.environ.pop("LEADENGINE_LIVE_DELIVERY", None)
        import requests as req
        req.post = state["orig"]


def test_operator_resend_after_budget_exhaustion(tmp_path):
    """The admin escalation proves: one unrudgeted re-post of the ORIGINAL payload to the buyer's CURRENT
    webhook URL, a RESENT deliveries row, and no re-sale / no extra charge / routing state untouched."""
    m = make_app(tmp_path); c, form = save_buyer(m, tmp=tmp_path)
    import os
    os.environ["LEADENGINE_LIVE_DELIVERY"] = "1"
    state = _advance_webhook([False])                 # every attempt fails — budget exhausts
    try:
        post(c, consent_checked=True).get_json()
        lead = m.con.execute("SELECT * FROM leads").fetchone()
        tok = next(x["portal_token"] for x in json.loads((m.CFG / "buyers.json").read_text(encoding="utf-8"))
                   if x["id"] == "hookbuyer")
        for _ in range(5):                            # exhaust the automatic budget
            c.get(f"/agent/{tok}")
        n_before = m.con.execute("SELECT COUNT(*) FROM deliveries WHERE lead_id=? AND method='webhook'",
                                 (lead["lead_id"],)).fetchone()[0]
        ledger_before = m.con.execute("SELECT COUNT(*) FROM buyer_ledger WHERE lead_id=?", (lead["lead_id"],)).fetchone()[0]
        # operator escalation re-posts and stops failing
        _advance_webhook([True])
        r = c.post(f"/admin/lead/{lead['lead_id']}/resend?token=t")
        assert r.status_code == 302
        rows = [dict(x)["status"] for x in m.con.execute(
            "SELECT status FROM deliveries WHERE lead_id=? AND method='webhook' ORDER BY id", (lead["lead_id"],))]
        assert rows[-1] == "RESENT" and len(rows) == n_before + 1
        assert m.con.execute("SELECT COUNT(*) FROM buyer_ledger WHERE lead_id=?", (lead["lead_id"],)).fetchone()[0] == ledger_before

        healed = m.con.execute("SELECT routing_status FROM leads WHERE lead_id=?", (lead["lead_id"],)).fetchone()[0]
        assert healed == "DELIVERED"
        # refusing an unroutable lead and a buyer who is not on webhook
        assert c.post(f"/admin/lead/{lead['lead_id']}/resend?token=t").status_code == 302   # still delivered: allowed again
    finally:
        os.environ.pop("LEADENGINE_LIVE_DELIVERY", None)
        import requests as req
        req.post = state["orig"]


def test_operator_resend_off_in_staging(tmp_path):
    """No LEADENGINE_LIVE_DELIVERY: nothing leaves the machine even on an explicit operator resend."""
    m = make_app(tmp_path); c, form = save_buyer(m, tmp=tmp_path)
    r = post(c, consent_checked=True).get_json()
    assert r["routed"] == "ROUTED"
    lead = m.con.execute("SELECT * FROM leads").fetchone()
    assert lead["routing_status"] == "DELIVERED"
    n_before = m.con.execute("SELECT COUNT(*) FROM deliveries WHERE lead_id=?", (lead["lead_id"],)).fetchone()[0]
    resp = c.post(f"/admin/lead/{lead['lead_id']}/resend?token=t", follow_redirects=True)
    assert "LEADENGINE_LIVE_DELIVERY is off" in resp.get_data(as_text=True)
    n_after = m.con.execute("SELECT COUNT(*) FROM deliveries WHERE lead_id=?", (lead["lead_id"],)).fetchone()[0]
    assert n_after == n_before           # staging: a resend appends nothing and posts nothing
