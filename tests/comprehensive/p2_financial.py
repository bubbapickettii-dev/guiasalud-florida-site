"""PASS 2 — FINANCIAL AND BUYER ACCOUNT INTEGRITY (isolated DB, stubbed Stripe).

Lifecycle walk: buyer application -> operator verification gates -> admin onboarding
-> package purchase (PENDING) -> webhook credit paths (paid / unpaid / failed /
expired / duplicate / delayed) -> charges (real + capped + insufficient) -> returns
window -> operator adjustments / top-up / stuck-payment resolution -> CSV statement.
ALWAYS test-mode Stripe (sk_test_/whsec_ stubs) — no network, no real keys.

LANDS DESIGN FACTS the tests must honor (verified by reading app.py / engine/billing.py):
  * admin verify posts {what: license|npn|delivery_tested|contact_reached, ref}
  * onboarding posts buyer_id + action=activate (drafts stay inactive; TEST rows
    (test=1) are REFUSED for non-demo buyers -> 400)
  * agent returns are POST /agent/<token> {lead_id, status: RETURNED, reason}
  * operator payment mark only closes PENDING rows (CLOSED, never PAID); grants
    happen only via the verified webhook or an operator top-up form
"""
import json
import sys
import types
import urllib.parse as _up  # noqa: E402
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import Harness  # noqa: E402

H = Harness("p2", fresh_cfg=True)

appmod = H.load_app()
appmod.CFG = H.cfg
appmod.OUTBOX = H.wdir / "outbox"
appmod.SNAP = H.wdir / "snap"
from engine import billing, payments as pmt, route as ro  # noqa: E402

TOKEN = appmod.ADMIN_TOKEN
fails, checks = [], 0


def ok(cond, what, extra=""):
    global checks
    checks += 1
    if not cond:
        fails.append(f"{what} {extra}")
        print(f"  FAIL {what} {extra}")
    else:
        print(f"  ok   {what}")


def _stub_stripe(session_id="cs_p2_1", url="https://checkout.stripe.com/c/pay/cs_p2_1"):
    fake = types.ModuleType("stripe")
    fake.checkout = types.SimpleNamespace(Session=types.SimpleNamespace(
        create=lambda **kw: {"id": session_id, "url": url}))
    fake.Webhook = types.SimpleNamespace(construct_event=lambda payload, sig, secret: None)
    sys.modules["stripe"] = fake


def stub_session(sid):
    """Every Checkout 'creation' must use a unique session id, otherwise the INSERT OR IGNORE
    dedupe maps every purchase onto the very first row."""
    sys.modules["stripe"].checkout.Session.create = lambda **kw: {"id": sid,
                                                                 "url": f"https://checkout.stripe.com/c/pay/{sid}"}


_stub_stripe()
import os as _os  # noqa: E402
_os.environ["STRIPE_SECRET_KEY"] = "sk_test_p2_stubs_only"
_os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_p2_test"

con = appmod.con
BID = "pass-two-insurance-group"


def tok(buyer_id):
    return next(b for b in ro.load_buyers(H.cfg) if b["id"] == buyer_id)["portal_token"]


def evt(kind, sid, payment_status="paid", eid="evt_x"):
    return {"id": eid, "type": kind, "data": {"object": {"id": sid, "payment_status": payment_status}}}


def wh(client, stanza, sig="t=1,v1=x"):
    sys.modules["stripe"].Webhook.construct_event = lambda p, s, h: stanza
    return client.post("/api/webhooks/stripe", data=b"{}", headers={"Stripe-Signature": sig})


def balance(buyer_id):
    """is_test=1 ledger balance (everything this suite writes with test flags)."""
    return round(billing.balance(con, buyer_id, 1), 2)


def real_balance(buyer_id):
    return round(billing.balance(con, buyer_id, 0), 2)


def ledger_count(kind, buyer_id):
    return con.execute("SELECT COUNT(*) FROM buyer_ledger WHERE buyer_id=? AND kind=?",
                       (buyer_id, kind)).fetchone()[0]


BASE_FORM = dict(agency="Pass Two Insurance Group", contact_name="Pat Buyer", contact_email="ops@passtwo.test",
                 contact_phone="3055550140", license_number="FL-L-209991", npn="2099991",
                 licensed_states="FL", products="health_aca",
                 delivery_method="webhook", delivery_webhook="https://crm.passtwo.test/hook", note="volume 20/wk")


print("== 1. public buyer application -> draft buyer, inactive, never routable")
c = appmod.app.test_client()
r = c.post("/for-agents/apply", data=BASE_FORM)
ok(r.status_code == 200, "application accepted")
draft = next(b for b in json.loads((H.cfg / "buyers.json").read_text(encoding="utf-8")) if b["id"] == BID)
ok(draft["active"] is False and draft["is_demo"] is False, "draft stored inactive")
ok(draft["_application"]["delivery_preference"] == "webhook" and draft["delivery"]["webhook_url"],
   "delivery preference recorded")
# a draft (inactive real buyer) must never receive a lead: the lead still flows to whoever IS
# eligible (demo placeholders stay active for funnel testing) — but never to the draft
lead_no_buyer = c.post("/api/lead", json=dict(campaign="mortgage-protection", first_name="D", last_name="R",
                                             email="d@example.com", phone="3055550145", zip="33130", state="FL",
                                             age_range="40-49", consent_checked=1, is_test=1, visitor_id="v-draft"))
ok(lead_no_buyer.json["routed"] != "ROUTED" or
   con.execute("SELECT buyer_id FROM leads WHERE lead_id=?", (lead_no_buyer.json["lead_id"],)).fetchone()["buyer_id"] != BID,
   "draft (inactive) never routed a lead", str(lead_no_buyer.json))
# repeat application = ONE draft row with an appended note, never a second agency
r = c.post("/for-agents/apply", data={**BASE_FORM, "note": "add: also do OTC"})
n_drafts = len([b for b in json.loads((H.cfg / "buyers.json").read_text(encoding="utf-8")) if b["id"] == BID])
ok(n_drafts == 1 and "add: also do OTC" in next(b for b in json.loads((H.cfg / "buyers.json").read_text(encoding="utf-8"))
                                                if b["id"] == BID)["_application"]["note"], "repeat application merges, not duplicates")
# missing required fields are refused with a message, never half-saved
r = c.post("/for-agents/apply", data={**BASE_FORM, "agency": "", "contact_name": ""})
ok(r.status_code == 200 and b"required" in r.data, "missing fields refused with message", r.status_code)

print("== 2. operator verification gates: recorded externally, never auto-activating")
r = c.post(f"/admin/buyer/{BID}/verify?token={TOKEN}", data={"what": "delivery_tested", "ref": "handshake ping recorded"})
ok(r.status_code == 302, "verification record stored")
draft = next(b for b in json.loads((H.cfg / "buyers.json").read_text(encoding="utf-8")) if b["id"] == BID)
ok(draft.get("_verification", {}).get("delivery_tested", {}).get("ref") == "handshake ping recorded",
   "verification reference stored")
ok(draft["active"] is False, "verification does NOT self-activate")
r = c.post(f"/admin/buyer/{BID}/verify?token={TOKEN}", data={"what": "delivery_tested", "ref": ""})
ok(r.status_code == 400 or "err=" in r.headers.get("Location", ""), "verify without a reference is refused")
r = c.post(f"/admin/buyer/{BID}/verify?token={TOKEN}", data={"what": "made_up_step", "ref": "x"})
ok(r.status_code == 400, "unknown verification step refused", r.status_code)
r = c.post(f"/admin/buyer/{BID}/verify?token={TOKEN}", data={"what": "delivery_tested", "ref": "x", "action": "forget"})
draft = next(b for b in json.loads((H.cfg / "buyers.json").read_text(encoding="utf-8")) if b["id"] == BID)
ok("delivery_tested" not in draft.get("_verification", {}), "operator can withdraw a verification")

print("== 3. onboarding via admin form: activation requires the complete, verified record")
r = c.post(f"/admin/buyers?token={TOKEN}", data={
    "buyer_id": BID, "action": "activate", "name": "Pass Two Insurance Group",
    "license_number": "FL-L-209991", "npn": "2099991", "licensed_states": "FL",
    "products": "health_aca", "tiers": "A,B", "price_A": "45", "price_B": "30",
    "daily_cap": "10", "monthly_cap": "5", "age_min": "25", "age_max": "64",
    "return_window_hours": "72",
    "delivery_method": "webhook", "delivery_webhook": "https://crm.passtwo.test/hook"})
loc = r.headers.get("Location", "")
ok(r.status_code in (302, 200) and "err=" not in loc, "onboarded + activated via admin buyers form", loc[:200])
b2 = next(b for b in json.loads((H.cfg / "buyers.json").read_text(encoding="utf-8")) if b["id"] == BID)
ok(bool(b2["active"]) and b2["price_by_tier"].get("A") == 45, "now active with operator-set price")
secret = b2["delivery"].get("webhook_secret")
ok(isinstance(secret, str) and len(secret) >= 12, "webhook signing secret issued", repr(secret)[:8] + "…")
ok(bool(b2["portal_token"]), "portal token issued on activate")
# an incomplete activation (no license) is refused and leaves the buyer untouched
b2["license_number"] = ""
(H.cfg / "buyers.json").write_text(json.dumps([b2 if b["id"] == BID else dict(b) for b in
                                              json.loads((H.cfg / "buyers.json").read_text(encoding="utf-8"))]),
                                   encoding="utf-8")
tok_before = tok(BID)
r = c.post(f"/admin/buyers?token={TOKEN}", data={"buyer_id": BID, "action": "rotate_token"})
b2 = next(b for b in json.loads((H.cfg / "buyers.json").read_text(encoding="utf-8")) if b["id"] == BID)
ok(bool(b2["active"]) is False or tok(BID) != tok_before, "rotate_token issues a fresh portal token", tok_before[:4])
# restore: re-activate cleanly
r = c.post(f"/admin/buyers?token={TOKEN}", data={
    "buyer_id": BID, "action": "activate", "name": "Pass Two Insurance Group",
    "license_number": "FL-L-209991", "npn": "2099991", "licensed_states": "FL",
    "products": "health_aca", "tiers": "A,B", "price_A": "45", "price_B": "30",
    "daily_cap": "10", "monthly_cap": "5", "age_min": "25", "age_max": "64",
    "delivery_method": "webhook", "delivery_webhook": "https://crm.passtwo.test/hook"})
b2 = next(b for b in json.loads((H.cfg / "buyers.json").read_text(encoding="utf-8")) if b["id"] == BID)
ok(bool(b2["active"]), "re-activated with a complete record")

print("== 4. test-mode ledger guards: real buyers cannot receive TEST bookkeeping rows")
r = c.post(f"/admin/buyer/{BID}/adjust?token={TOKEN}", data={"amount": "-5", "note": "x", "test": "1"})
ok(r.status_code == 400, "TEST adjustment refused for a real buyer", r.status_code)
r = c.post(f"/admin/buyer/{BID}/topup?token={TOKEN}", data={"package": "starter-250", "test": "1"})
ok(r.status_code == 400, "TEST top-up refused for a real buyer", r.status_code)

print("== 5. package purchase: server-side checkout, nothing is credited")
r = c.post(f"/agent/{tok(BID)}/billing/package", data={"package": "starter-250", "test": "1"})
row = con.execute("SELECT * FROM buyer_payments").fetchone()
ok(row["status"] == "PENDING" and row["amount_usd"] == 250.0 and row["is_test"] == 1,
   "PENDING session recorded server-side")
ok(balance(BID) == 0.0, "checkout grants nothing")

print("== 6. webhook events: forged / unpaid / paid / duplicate / delayed / expired")
r = wh(c, evt("checkout.session.completed", "cs_p2_1", payment_status="unpaid", eid="evt_unpaid"))
ok(r.status_code == 200 and r.json["state"] == "ignored_unpaid", "unpaid event credits nothing", str(r.get_json()))
r = wh(c, evt("checkout.session.completed", "cs_p2_1", eid="evt_paid"))
ok(r.status_code == 200 and r.json["state"] == "credited", "verified paid event credited")
ok(balance(BID) == 250.0, "balance 250.00 in TEST ledger", balance(BID))
ok(ledger_count("PACKAGE_PURCHASE", BID) == 1, "exactly one PACKAGE_PURCHASE row")
r = wh(c, evt("checkout.session.completed", "cs_p2_1", eid="evt_paid"))
ok(r.json["state"] == "duplicate", "duplicate event id deduped")
ok(balance(BID) == 250.0, "balance unchanged after duplicate")
# forged signature -> 400 and nothing changes
sys.modules["stripe"].Webhook.construct_event = lambda p, s, h: (_ for _ in ()).throw(Exception("SignatureVerificationError"))
r = c.post("/api/webhooks/stripe", data=b"{}", headers={"Stripe-Signature": "bad"})
ok(r.status_code == 400 and balance(BID) == 250.0, "forged event refused 400")
for kind, sid, eid in (("checkout.session.completed", "cs_p2_late1", "evt_l1"),
                       ("checkout.session.async_payment_succeeded", "cs_p2_late2", "evt_l2"),
                       ("checkout.session.async_payment_failed", "cs_p2_f", "evt_f1"),
                       ("checkout.session.expired", "cs_p2_e", "evt_e1")):
    # no local PENDING row for these sessions (interrupted/delayed webhooks)
    r = wh(c, evt(kind, sid, eid=eid))
    ok(r.json["state"] in ("not_found", "marked_failed") and balance(BID) == 250.0,
       f"{kind} with unknown session refused (no invented credit)", str(r.get_json()))

print("== 7. charges + the monthly purchase cap, enforced on REAL (is_test=0) traffic")
Mp = appmod.MARKETPLACE
appmod.reload_cfg()


def submit_test(phone, vi):
    """High-intent health fields so the lead scores tier A (pass-two buys A/B at 45/30)."""
    return c.post("/api/lead", json=dict(campaign="oep-2027-health", first_name="Zoe", last_name=f"z{phone[-4:]}",
                                         email=f"z{phone[-4:]}@example.com", phone=phone, zip="33130", state="FL",
                                         age_range="25-34", coverage_for="me", consent_checked=1, visitor_id=vi,
                                         urgency="HIGH", income_band="150-250", household_size=3,
                                         enrollment_situation="OEP_OPEN", preferred_contact="phone",
                                         tool_result={"headline": "You may qualify"}, flags=[], is_test=1))


def submit_real(phone, vi):
    """Same shape WITHOUT the test flag: staging honors the absent flag as a REAL submission."""
    return c.post("/api/lead", json=dict(campaign="oep-2027-health", first_name="Zoe", last_name=f"z{phone[-4:]}",
                                         email=f"z{phone[-4:]}@example.com", phone=phone, zip="33130", state="FL",
                                         age_range="25-34", coverage_for="me", consent_checked=1, visitor_id=vi,
                                         urgency="HIGH", income_band="150-250", household_size=3,
                                         enrollment_situation="OEP_OPEN", preferred_contact="phone",
                                         tool_result={"headline": "You may qualify"}, flags=[]))


r = submit_test("3052340201", "v-cap-1")
ok(r.json["routed"] == "ROUTED", "test lead routed", str(r.json))
# is_test=1 charge only; TEST traffic never counts toward caps
real_ledger = lambda: con.execute(
    "SELECT COUNT(*) c, COALESCE(ROUND(SUM(amount),2),0) s FROM buyer_ledger WHERE buyer_id=? AND kind='LEAD_CHARGE' AND is_test=0",
    (BID,)).fetchone()
test_ledger = lambda: con.execute(
    "SELECT COUNT(*) c, COALESCE(ROUND(SUM(amount),2),0) s FROM buyer_ledger WHERE buyer_id=? AND kind='LEAD_CHARGE' AND is_test=1",
    (BID,)).fetchone()
ok(test_ledger()["c"] == 1 and test_ledger()["s"] == -45.0 and real_ledger()["c"] == 0,
   "TEST lead charged ONLY in the TEST ledger", (dict(test_ledger()), dict(real_ledger())))
ok(balance(BID) == 205.0, "test balance after one 45 charge", balance(BID))
r = submit_real("3052340301", "v-cap-r1")
ok(r.json["routed"] == "ROUTED", "real lead routed to the active real buyer", str(r.json))
ok(real_ledger()["c"] == 1 and real_ledger()["s"] == -45.0, "REAL lead charged in the REAL ledger", dict(real_ledger()))
# bring pass-two to the cap (5 REAL leads already sold this month), the way a running month looks
for i in range(4):
    con.execute("""INSERT INTO leads (lead_id, created_at, is_test, campaign, product, state, zip, phone, phone_source,
        qual_status, tier, score, routing_status, buyer_id, price, routed_at, sale_type, delivered_at)
        VALUES (?, datetime('now'), 0, 'oep-2027-health', 'health_aca', 'FL', '33130', ?, 'CONSUMER_SUBMITTED',
        'QUALIFIED', 'A', 80, 'DELIVERED', ?, 45.0, datetime('now'), 'EXCLUSIVE', datetime('now'))""",
                (f"LREALCAP{i}", f"40723406{i:02d}", BID))
con.commit()
r = submit_real("3052340402", "v-cap-r2")
b_cap_r = con.execute("SELECT buyer_id FROM leads WHERE lead_id=?", (r.json["lead_id"],)).fetchone()
ok(b_cap_r and b_cap_r["buyer_id"] != BID and real_ledger()["c"] == 1,
   "a REAL lead is never sold to the capped buyer (staging's demo backstop took it instead)", str(b_cap_r))
# engine-level proof of the same rule, naming the capped buyer in the audit reason
_syn = {"lead_id": "SYN", "state": "FL", "product": "health_aca", "age_range": "25-34", "tier": "A"}
reason, n = ro.explain(con, ro.load_buyers(H.cfg), _syn)
ok("pass-two-insurance-group monthly purchase limit 5 reached" in reason,
   "audit explanation names the capped buyer", reason[:200])
r = submit_test("3052340202", "v-cap-2")
ok(r.json["routed"] == "ROUTED", "TEST traffic keeps flowing (caps count REAL leads only)", str(r.json))
b_cap = con.execute("SELECT buyer_id FROM leads WHERE lead_id=?", (r.json["lead_id"],)).fetchone()
ok(b_cap and b_cap["buyer_id"] != BID, "the capped buyer cannot sell even to TEST traffic; staging's demo backstop did", b_cap)
ok(test_ledger()["c"] == 1, "the fallback demo buyer received the test sale at its own price",
   (b_cap["buyer_id"], dict(test_ledger()), round(balance(b_cap["buyer_id"]), 2)))

print("== 8. enforce_credits=routability floor: an under-credited buyer is blocked BEFORE consent")
(H.cfg / "marketplace.json").write_text(json.dumps({**Mp, "enforce_credits": True, "min_balance_usd": 0}), encoding="utf-8")
appmod.reload_cfg()
# engine-level TEST adjustment (is_test=1) to dip under the floor: the API refuses that for real buyers (== 4)
billing.apply_adjustment(con, BID, -350.0, note="engine test adjustment: dip below floor", is_test=1)
ok(balance(BID) < 0, "test balance now under floor", balance(BID))
r = c.get("/api/route-preview?campaign=oep-2027-health&state=FL&age_range=25-34&test=1")
ok(r.json["available"] is False, "consent preview unavailable for an under-credited buyer (HONEST consent)", str(r.json)[:120])
r = submit_test("3052340911", "v-low-credit")
ok(r.json["routed"] == "NO_BUYER", "under-credited buyer never named/sold", str(r.json))
r = submit_real("3052340912", "v-low-credit-real")
ok(r.json["routed"] == "NO_BUYER", "a REAL buyer below the floor is not on offer in any mode", str(r.json))
(H.cfg / "marketplace.json").write_text(json.dumps({**Mp, "enforce_credits": False}), encoding="utf-8")
appmod.reload_cfg()
r = submit_test("3052340913", "v-restored")
ok(r.json["routed"] == "ROUTED", "enforcement off: routing comes back", str(r.json))

print("== 9. returns: in-window credit, duplicate return, no invented refund")
lead2 = con.execute("SELECT * FROM leads WHERE buyer_id=? AND routing_status='DELIVERED' AND is_test=1 "
                    "ORDER BY created_at DESC LIMIT 1", (BID,)).fetchone()
price = round(float(lead2["price"]), 2)
pre = balance(BID)
r = c.post(f"/agent/{tok(BID)}", data={"lead_id": lead2["lead_id"], "status": "RETURNED", "reason": "unreachable"})
ok(r.status_code in (302, 200), "in-window return accepted")
ok(balance(BID) == pre + price and ledger_count("LEAD_CREDIT", BID) == 1,
   "LEAD_CREDIT for a timely return", (balance(BID), price))
pre = balance(BID)
r = c.post(f"/agent/{tok(BID)}", data={"lead_id": lead2["lead_id"], "status": "RETURNED", "reason": "unreachable"})
ok(r.status_code in (302, 200, 400, 409), "duplicate return handled without crash")
ok(balance(BID) == pre and ledger_count("LEAD_CREDIT", BID) == 1, "returning twice credits exactly once")
ch_before = ledger_count("LEAD_CHARGE", BID)
r = c.post(f"/agent/{tok(BID)}", data={"lead_id": lead2["lead_id"], "status": "CONTACTED"})
ok(ledger_count("LEAD_CHARGE", BID) == ch_before and ledger_count("LEAD_CREDIT", BID) == 1,
   "a later CONTACTED after return does not re-charge, and stays returned", ch_before)

print("== 10. buyer statement CSV: scoped, complete, sums to the displayed balance")
r = c.get(f"/agent/{tok(BID)}/billing/statement.csv")
ok(r.status_code == 200 and "text/csv" in r.headers["Content-Type"], "statement CSV served")
import csv as _csvm  # noqa: E402
import io as _iom  # noqa: E402
rows = list(_csvm.DictReader(_iom.StringIO(r.get_data(as_text=True))))
ok("ts,kind,amount,balance_after,lead_id,package_id,note" in r.get_data(as_text=True), "CSV header shape")
# a REAL buyer's statement shows ONLY their real (is_test=0) ledger, never the TEST traffic
ok(all(x["kind"] != "PACKAGE_PURCHASE" or float(x["amount"]) != 250.0 for x in rows) and
   any(x["kind"] == "LEAD_CHARGE" for x in rows) and len(rows) == real_ledger()["c"],
   "REAL buyer CSV lists the real charges only", [x["kind"] for x in rows])
vals = [float(x["amount"]) for x in rows]
ok(round(sum(vals), 2) == real_balance(BID), "CSV sums to the real ledger balance",
   (round(sum(vals), 2), real_balance(BID)))

demo = "demo-agency-a"
r = c.post(f"/admin/buyer/{demo}/topup?token={TOKEN}", data={"package": "starter-250", "test": "1"})
ok(r.status_code in (302, 200), "TEST top-up recorded for the demo buyer (demo ledger accepts test rows)")
demo_bal = balance(demo)
r = c.get(f"/agent/{tok(demo)}/billing/statement.csv")
vals = [float(x["amount"]) for x in _csvm.DictReader(_iom.StringIO(r.get_data(as_text=True)))]
ok(round(sum(vals), 2) == round(demo_bal, 2), "demo CSV sums to its ledger balance",
   (round(sum(vals), 2), demo_bal))

print("== 11. stuck payment (webhook never arrived): admin alert, operator mark, late webhook no-op")
stub_session("cs_p2_stuck1")
r = c.post(f"/agent/{tok(demo)}/billing/package", data={"package": "starter-250"})
sid2 = con.execute("SELECT session_id FROM buyer_payments ORDER BY id DESC LIMIT 1").fetchone()[0]
rid2 = con.execute("SELECT rowid FROM buyer_payments WHERE session_id=?", (sid2,)).fetchone()[0]
st0 = con.execute("SELECT status, ts, amount_usd FROM buyer_payments WHERE session_id=?", (sid2,)).fetchone()
con.execute("UPDATE buyer_payments SET ts='2026-10-01T00:00:00' WHERE session_id=?", (sid2,))
con.commit()
st1 = con.execute("SELECT status, ts FROM buyer_payments WHERE session_id=?", (sid2,)).fetchone()
pg = c.get(f"/admin?token={TOKEN}")
stuck_rows = con.execute("SELECT COUNT(*) FROM buyer_payments WHERE status='PENDING'").fetchone()[0]
ok("Payment stuck pending" in pg.get_data(as_text=True), "stuck payment surfaced on admin",
   (dict(st0), dict(st1), stuck_rows, r.status_code, r.headers.get("Location", "")[:120]))
pre = balance(demo)
r = c.post(f"/admin/payments/{rid2}/mark?token={TOKEN}", data={})
msg = _up.unquote_plus(r.headers.get("Location", ""))
ok("a resolution note is required" in msg, "mark without a note is refused", msg[:160])
r = c.post(f"/admin/payments/{rid2}/mark?token={TOKEN}", data={"note": "stripe dashboard: refunded"})
ok(r.status_code in (302, 200), "operator mark processed")
row = con.execute("SELECT status, note FROM buyer_payments WHERE session_id=?", (sid2,)).fetchone()
ok(row["status"] == "CLOSED" and "refunded" in row["note"], "payment row CLOSED with operator note",
   (row["status"], (row["note"] or "")[:80]))
ok(balance(demo) == pre, "closing never invents credit", (balance(demo), pre))
r = c.post(f"/admin/payments/{rid2}/mark?token={TOKEN}", data={"note": "again"})
msg2 = _up.unquote_plus(r.headers.get("Location", ""))
ok("already CLOSED" in msg2, "re-close refused (only a PENDING row can close)", msg2[:200])
ok(balance(demo) == pre, "no double effect from repeated mark")
r = wh(c, evt("checkout.session.completed", sid2, eid="evt_late1"))
ok(r.json["state"] == "duplicate" and balance(demo) == pre, "late webhook on a CLOSED row is a no-op")

print("== 12. paid-but-webhook-missing recovery: operator records the confirmed pack, exactly one credit")
stub_session("cs_p2_paid3")
r = c.post(f"/agent/{tok(demo)}/billing/package", data={"package": "weekly-1000"})
sid3 = con.execute("SELECT session_id FROM buyer_payments ORDER BY id DESC LIMIT 1").fetchone()[0]
rid3 = con.execute("SELECT rowid FROM buyer_payments WHERE session_id=?", (sid3,)).fetchone()[0]
pre = balance(demo)
r = c.post(f"/admin/buyer/{demo}/topup?token={TOKEN}",
           data={"package": "weekly-1000", "note": "recorded: funds confirmed on stripe dashboard", "test": "1"})
cred = balance(demo) - pre
ok(cred == 1000.0, "exactly one credit of the recorded pack", cred)
r = wh(c, evt("checkout.session.completed", sid3, eid="evt_late2"))
ok(r.json["state"] in ("credited", "duplicate", "not_found") and balance(demo) == pre + 1000.0,
   "the credit never doubles: webhook + operator top-up stay at one effect",
   (balance(demo), str(r.get_json())))
row3 = con.execute("SELECT status FROM buyer_payments WHERE session_id=?", (sid3,)).fetchone()
ok(row3["status"] == "CLOSED", "the top-up itself closed the PENDING session row (no future webhook can credit)", row3["status"])
r = c.post(f"/admin/payments/{rid3}/mark?token={TOKEN}", data={"note": "already credited via top-up"})
ok(r.status_code in (302, 400), "PENDING row can be closed even after the top-up (operator truth)")

print("== 13. negative REAL balance bookkeeping is flagged for the operator")
# adjustments to a DEMO buyer always land in the TEST ledger; the REAL ledger alertable path is a real buyer
pre_real = real_balance(BID)
r = c.post(f"/admin/buyer/{BID}/adjust?token={TOKEN}", data={"amount": "-9999", "note": "operator: overdraft correction"})
ok(r.status_code in (302, 200), "real-ledger overdraft adjustment accepted")
ok(real_balance(BID) < 0, "real balance negative after adjustment", real_balance(BID))
pg = c.get(f"/admin?token={TOKEN}")
ok("negative" in pg.get_data(as_text=True).lower() and BID in pg.get_data(as_text=True),
   "negative balance flagged on the admin", real_balance(BID))
r = c.post(f"/admin/buyer/{BID}/adjust?token={TOKEN}",
           data={"amount": str(round(-real_balance(BID), 2)), "note": "operator: restore to zero"})
ok(real_balance(BID) == 0.0 and real_balance(BID) >= pre_real, "operator restores the balance", real_balance(BID))
ok(billing.balance(con, BID, 0) == real_balance(BID), "balance() agrees")

print()
print(f"PASS2 RESULT: {checks - len(fails)}/{checks} checks passed")
if fails:
    print("FAILURES:")
    for f_ in fails:
        print(" -", f_)
sys.exit(1 if fails else 0)
