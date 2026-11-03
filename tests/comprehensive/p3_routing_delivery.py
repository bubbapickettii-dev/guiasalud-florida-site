"""PASS 3 — ROUTING, EXCLUSIVITY, DELIVERY, AND RECOVERY (isolated DB + local webhook stub).

Drives the REAL delivery code path (LEADENGINE_LIVE_DELIVERY=1) at a localhost stub that
records every POST body and verifies the x-ticket-signature HMAC. Everything is TEST-mode:
fresh config copy, no network beyond 127.0.0.1, no real buyers, no real charges.
"""
import hashlib
import hmac as hmac_mod
import json
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import Harness  # noqa: E402

H = Harness("p3", fresh_cfg=True)
H.env["LEADENGINE_LIVE_DELIVERY"] = "1"          # engine-level live delivery, to the localhost stub ONLY

# --------------------------- local webhook stub (records + keeps everything for HMAC verification)
REC = []            # each: {path, body(bytes), sig(header)}
FLAKY = [999999]    # countdown of failures for /flaky


class Stub(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n)
        REC.append({"path": self.path, "body": raw, "sig": self.headers.get("x-ticket-signature", "")})
        code = 500 if (self.path.startswith("/fail") or (self.path.startswith("/flaky") and FLAKY[0] > 0)) else 200
        if self.path.startswith("/flaky") and FLAKY[0] > 0:
            FLAKY[0] -= 1
        self.send_response(code)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


SRV = HTTPServer(("127.0.0.1", 18443), Stub)
threading.Thread(target=SRV.serve_forever, daemon=True).start()


def hit(path):
    urllib.request.urlopen(f"http://127.0.0.1:18443{path}", data=b"").read()


appmod = H.load_app()
appmod.CFG = H.cfg
appmod.OUTBOX = H.wdir / "outbox"
appmod.SNAP = H.wdir / "snap"
from engine import billing, route as ro, qualify  # noqa: E402

TOKEN = appmod.ADMIN_TOKEN
con = appmod.con
fails, checks = [], 0


def ok(cond, what, extra=""):
    global checks
    checks += 1
    if not cond:
        fails.append(f"{what} {extra}")
        print(f"  FAIL {what} {extra}")
    else:
        print(f"  ok   {what}")


BID = "p3-crm-agency"
SECRET = "p3-webhook-secret-000000000"


def buyers_file(extra=None, active=True, url="http://127.0.0.1:18443/ok", solo=False):
    """Rebuild the harness buyers.json: our fixture buyer + the file's demo placeholders untouched.
    solo=True also deactivates EVERY other buyer (fixture-isolation tests: deactivation, no-buyer queue)."""
    bs = json.loads((H.cfg / "buyers.json").read_text(encoding="utf-8"))
    bs = [b for b in bs if b["id"] != BID] + [b for b in (extra or [])] + [dict(
        id=BID, name="P3 CRM Agency", is_demo=False, active=active, priority=1, exclusive=True,
        portal_token="pt-p3-crm-agency",
        license_number="FL-L-300001", npn="3000001", licensed_states=["FL"], products=["health_aca"],
        tiers=["A", "B"], price_by_tier={"A": 40, "B": 25, "C": 0},
        daily_cap=50, monthly_cap=100, age_min=18, age_max=80,
        delivery={"method": "webhook", "webhook_url": url, "webhook_secret": SECRET} if active else
        {"method": "webhook", "webhook_url": url})]
    if solo:
        for b in bs:
            if b["id"] != BID:
                nb = dict(b)
                nb["active"] = False
                bs = [x for x in bs if x["id"] != nb["id"]] + [nb]
    (H.cfg / "buyers.json").write_text(json.dumps(bs), encoding="utf-8")


buyers_file()


def submit(phone, vi, campaign="oep-2027-health"):
    return appmod.app.test_client().post("/api/lead", json=dict(
        campaign=campaign, first_name="Zoe", last_name=f"z{phone[-4:]}", email=f"z{phone[-4:]}@example.com",
        phone=phone, zip="33130", state="FL", age_range="25-34", coverage_for="me", consent_checked=1,
        visitor_id=vi, urgency="HIGH", income_band="150-250", household_size=3,
        enrollment_situation="OEP_OPEN", preferred_contact="phone", tool_result={"headline": "You may qualify"}))


def delivery_rows(lead_id):
    return [dict(r) for r in con.execute(
        "SELECT * FROM deliveries WHERE lead_id=? AND method='webhook' ORDER BY id", (lead_id,))]


def charges(lead_id):
    return [dict(r) for r in con.execute(
        "SELECT * FROM buyer_ledger WHERE lead_id=? AND kind='LEAD_CHARGE'", (lead_id,))]


def all_sig_ok():
    bad = []
    for r in REC:
        want = "sha256=" + hmac_mod.new(SECRET.encode(), r["body"], hashlib.sha256).hexdigest()
        if not hmac_mod.compare_digest(want, r["sig"]):
            bad.append((r["path"], r["sig"][:12], want[:12]))
    return bad


def submit_redirect_buyer(tok, lead_id):
    """Agent portal access to someone else's lead must be a 404, never data."""
    c = appmod.app.test_client()
    return c.get(f"/agent/{tok}/lead/{lead_id}").status_code


print("== 1. exclusive sale: exactly one delivery, one signature, one charge")
r = submit("3052341001", "v-p3-1")
L1 = r.get_json()["lead_id"]
ok(r.get_json()["routed"] == "ROUTED" and con.execute("SELECT buyer_id FROM leads WHERE lead_id=?", (L1,)).fetchone()[0] == BID,
   "assigned to the fixture buyer", str(r.get_json()))
row = con.execute("SELECT routing_status, price, sale_type FROM leads WHERE lead_id=?", (L1,)).fetchone()
ok(row["routing_status"] == "DELIVERED" and row["sale_type"] == "EXCLUSIVE", "EXCLUSIVE sale delivered", dict(row))
ok(len(REC) == 1 and REC[0]["path"] == "/ok" and charges(L1)[0]["amount"] == -40.0,
   "exactly ONE stub POST, one ledger charge at the tier price",
   (len(REC), len(charges(L1)), charges(L1)[0]["amount"] if charges(L1) else "none"))
drows = delivery_rows(L1)
ok(len(drows) == 1 and drows[0]["status"] == "SENT", "one SENT deliveries row", [d["status"] for d in drows])
ok(not all_sig_ok(), "initial delivery signature verifies (HMAC-SHA256 over the exact bytes)", all_sig_ok())
pay_file = Path(drows[0]["payload_path"])
ok(pay_file.exists(), "payload recorded on disk")
# double sale is impossible: re-deliver is a no-op, a second charge is deduped
c2 = appmod.app.test_client()
ok(ro.deliver(con, ro.load_buyers(H.cfg), dict(con.execute("SELECT * FROM leads WHERE lead_id=?", (L1,)).fetchone()), appmod.OUTBOX) == "ALREADY_DELIVERED",
   "re-deliver refused: the exclusive lead cannot be sold twice")
ok(len(REC) == 1, "no second webhook POST", len(REC))
_ok2 = appmod.billing  # engine module alias
ok(_ok2.charge(con, BID, dict(con.execute("SELECT * FROM leads WHERE lead_id=?", (L1,)).fetchone())) is None,
   "second charge attempt deduped (no row)")
rec1 = len(REC)
ok(len(charges(L1)) == 1, "ledger still exactly one charge")

print("== 2. failed webhook + retry budget + recovery (same bytes, same signature)")
buyers_file(url="http://127.0.0.1:18443/fail")
r = submit("3052341002", "v-p3-2")
L2 = r.get_json()["lead_id"]
ok(con.execute("SELECT routing_status FROM leads WHERE lead_id=?", (L2,)).fetchone()[0] == "DELIVERED",
   "sale stands even when the CRM POST fails (exclusivity + billing are not undone by a delivery outage)")
d2 = delivery_rows(L2)
ok(d2[0]["status"] == "FAILED" and len(REC) == rec1 + 1, "FAILED delivery row recorded",
   (rec1, len(REC)))
lead2 = dict(con.execute("SELECT * FROM leads WHERE lead_id=?", (L2,)).fetchone())
rets = []
for i in range(6):
    rets.append(ro.retry_webhook(con, ro.load_buyers(H.cfg), lead2))
ok(all(x == "FAILED" for x in rets[:5]), "five automatic retries, all FAILED", [r for r in rets[:6]])
ok(rets[5] == "", "the 6th automatic retry is NOT attempted (budget of 5 exhausted)", rets[5])
ok(len(REC) == rec1 + 6, "every attempted retry posted once", (rec1, len(REC)))
d2b = delivery_rows(L2)
ok(len([d for d in d2b if d["status"] == "FAILED"]) == 6, "six FAILED rows (initial + 5 retries)",
   [(d["status"], d["response"][:40]) for d in d2b])
ok(len(delivery_rows(L2)) == 6 and len(charges(L2)) == 1,
   "retries never re-sell or re-charge: one charge, sale stands", len(delivery_rows(L2)))
# budget is fully spent here (the initial failure + 5 retries = 6 FAILED rows): every further
# automatic retry stays off permanently, even with a healthy endpoint again — recovery must go
# through the operator escalation (== 3). A lease-in-budget lead still recovers automatically (2b).
buyers_file(url="http://127.0.0.1:18443/ok")
spoke = ro.retry_webhook(con, ro.load_buyers(H.cfg), lead2)
ok(spoke == "" and len(REC) == rec1 + 6, "budget spent: automatic retries stay off even with a healthy CRM",
   (spoke, rec1, len(REC)))
ok(not all_sig_ok(), "every recorded signature (initial + retries) verifies", all_sig_ok())
ok(len(charges(L2)) == 1, "still exactly one charge after the outage")
# byte-level: every retried body is the recorded payload, unregenerated (and the disk copy is the
# EXACT wire copy: write_bytes, not a text-mode variant that would differ per platform)
pL2 = Path(delivery_rows(L2)[0]["payload_path"])
ok(all(REC[rec1]["body"] == rb["body"] for rb in REC[rec1 + 1:]),
   "retries post the EXACT recorded bytes", (len(REC[rec1]["body"]), len(REC[-1]["body"])))
ok(REC[rec1]["body"] == pL2.read_bytes(), "body matches the recorded file on disk", pL2.name)

print("== 2b. a lead whose failures remain within budget recovers automatically once the CRM is back")
buyers_file(url="http://127.0.0.1:18443/fail")
r = submit("3052341007", "v-p3-2b")
L2b = r.get_json()["lead_id"]
lead2b = dict(con.execute("SELECT * FROM leads WHERE lead_id=?", (L2b,)).fetchone())
sp = delivery_rows(L2b)
ok(sp[0]["status"] == "FAILED" and len(sp) == 1, "outage lead: one FAILED delivery row", [d["status"] for d in sp])
buyers_file(url="http://127.0.0.1:18443/ok")
spoke2 = ro.retry_webhook(con, ro.load_buyers(H.cfg), lead2b)
sp = delivery_rows(L2b)
ok(spoke2 == "RESENT" and sp[-1]["status"] == "RESENT" and len(sp) == 2,
   "a retry within budget succeeds once the CRM is back", (spoke2, [d["status"] for d in sp]))
ok(len(REC) == rec1 + 8, "exactly one new POST for the successful recovery", (rec1, len(REC)))
ok(len(charges(L2b)) == 1 and con.execute("SELECT routing_status FROM leads WHERE lead_id=?", (L2b,)).fetchone()[0] == "DELIVERED",
   "the recovered lead was never re-charged or re-routed")
ok(not all_sig_ok(), "recovery signature verifies", all_sig_ok())

print("== 3. operator resend is unbudgeted and never re-sells")
r = appmod.app.test_client().post(f"/admin/lead/{L2}/resend?token={TOKEN}")
spoke = urllib.parse.unquote_plus(r.headers.get("Location", ""))
ok("re-posted" in spoke and "successfully" in spoke, "operator resend re-posted the payload", spoke[:160])
ok(len([d for d in delivery_rows(L2) if d["status"] != "FAILED"]) == 1,
   "a successful re-post updates the ONE non-FAILED row (unique index holds)")
ok(len(charges(L2)) == 1, "still one charge")

print("== 4. the buyer cannot reach another buyer's lead")
toks = {b["id"]: dict(b).get("portal_token") for b in ro.load_buyers(H.cfg)}
ok(toks.get(BID) == "pt-p3-crm-agency", "the fixture buyer has a portal token", toks.get(BID))
other = next(d["lead_id"] for d in con.execute(
    "SELECT lead_id FROM leads WHERE buyer_id=? AND lead_id!=?", (BID, L1)))
demo = next(b for b in ro.load_buyers(H.cfg) if b["id"] != BID)
demo_tok = demo["portal_token"]
ok(demo_tok and submit_redirect_buyer(demo_tok, L1) == 404,
   "another buyer's portal token gets 404 on a foreign lead", demo_tok[:6] if demo_tok else "?")
ok(demo_tok and submit_redirect_buyer(toks[BID], L1) == 200, "the owning buyer can open their own lead")

print("== 5. re-offer sweep: unmatched queue + assignment audit + batch/no-repeat")
rec5 = len(REC)
# nobody active: the lead flows into the unmatched queue, never routed
buyers_file(active=False, solo=True)
r = submit("3052341003", "v-p3-5")
L5 = r.get_json()["lead_id"]
ok(r.get_json()["routed"] == "NO_BUYER" and con.execute(
    "SELECT routing_status FROM leads WHERE lead_id=?", (L5,)).fetchone()[0] == "NO_BUYER",
   "no active buyer -> NO_BUYER (nothing was sold or named)", str(r.get_json()))
ok(len(REC) == rec5, "no webhook was POSTed by an unmatched lead")
out = appmod.app.test_client().post(f"/admin/sweep?token={TOKEN}")
nums = urllib.parse.unquote_plus(out.headers.get("Location", ""))
ok(con.execute("SELECT routing_status FROM leads WHERE lead_id=?", (L5,)).fetchone()[0] == "NO_BUYER",
   "sweep with zero eligible buyers keeps the lead unmatched", nums[:160])
# re-activate: the SAME sweep re-assigns the SAME platform consent, without a re-consent
buyers_file(active=True)
out = appmod.app.test_client().post(f"/admin/sweep?token={TOKEN}")
rd = con.execute("SELECT decision, reason FROM routing_decisions WHERE lead_id=? ORDER BY id DESC LIMIT 1", (L5,)).fetchone()
ok(rd["decision"] == "RE_ASSIGNED" and "operator sweep" in rd["reason"], "sweep re-assignment audited with source", dict(rd))
ok(con.execute("SELECT routing_status, buyer_id FROM leads WHERE lead_id=?", (L5,)).fetchone()["buyer_id"] == BID,
   "re-offered to the one eligible buyer")
ok(len(REC) == rec5 + 1 and not all_sig_ok(), "sweep delivery posted once with a valid signature", (len(REC), all_sig_ok()))
out = appmod.app.test_client().post(f"/admin/sweep?token={TOKEN}")
n_now = con.execute("SELECT COUNT(*) FROM routing_decisions WHERE lead_id=? AND decision='RE_ASSIGNED' AND reason LIKE '%operator sweep%'", (L5,)).fetchone()[0]
ok(n_now == 1, "a delivered sale is never re-touched by the sweep", n_now)

print("== 6. mid-sale crash: the sale strands in ROUTED and the operator heals it exactly once")
import engine.billing as _bl
_orig = _bl.charge
_stranded = {"n": 0}


def _boom(con, buyer_id, lead):
    _stranded["n"] += 1
    raise RuntimeError("simulated mid-sale crash")


_bl.charge = _boom
appmod.con = con
appmod.billing = _bl  # app.py calls the module-level name
r = submit("3052341004", "v-p3-6")
L6 = r.get_json()["lead_id"]
_bl.charge = _orig
st = con.execute("SELECT routing_status FROM leads WHERE lead_id=?", (L6,)).fetchone()[0]
ok(st == "ROUTED", "the crashed sale strands in ROUTED (never lost, never re-sold)", st)
res = appmod.app.test_client().post(f"/admin/lead/{L6}/deliver?token={TOKEN}")
st6 = con.execute("SELECT routing_status FROM leads WHERE lead_id=?", (L6,)).fetchone()[0]
ok(st6 == "DELIVERED", "operator deliver-now completes the stranded sale", st6)
ok(len(charges(L6)) == 1, "charged exactly once after the heal", len(charges(L6)))
mo = con.execute("SELECT outcome, note FROM lead_outcomes WHERE lead_id=? ORDER BY id LIMIT 1", (L6,)).fetchone()
ok(mo and mo["outcome"] == "DELIVERY_RETRY", "the failure is audited on the lead", dict(mo) if mo else "?")
res = appmod.app.test_client().post(f"/admin/lead/{L6}/deliver?token={TOKEN}")   # second heal attempt
ok(len(charges(L6)) == 1, "re-running deliver-now cannot double-bill")

print("== 7. deactivation: no NEW routing for a deactivated buyer; sold leads are unaffected")
buyers_file(active=False, solo=True)
r = submit("3052341005", "v-p3-7")
ok(r.get_json()["routed"] == "NO_BUYER", "deactivated buyer receives nothing new", str(r.get_json()))
ok(con.execute("SELECT routing_status, buyer_id FROM leads WHERE lead_id=?", (L6,)).fetchone()["buyer_id"] == BID,
   "the already-sold lead still belongs to the now-deactivated buyer (their consent stands)")
buyers_file(active=True)
r = submit("3052341006", "v-p3-8")
ok(r.get_json()["routed"] == "ROUTED", "re-activation restores routing", str(r.get_json()))

print("== 8. duplicate consumer submission is refused, not re-routed")
r1 = submit("3052341001", "v-p3-dup")     # the SAME phone as L1
ok(r1.get_json()["status"] == "DUPLICATE", "30-day dedupe matches on phone", str(r1.get_json()))

print()
print(f"PASS3 RESULT: {checks - len(fails)}/{checks} checks passed")
if fails:
    print("FAILURES:")
    for f_ in fails:
        print(" -", f_)
SRV.shutdown()
sys.exit(1 if fails else 0)
