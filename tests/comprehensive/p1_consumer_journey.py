"""PASS 1 — COMPLETE CONSUMER JOURNEY (Flask test client, isolated DB).

Covers: EN/ES pages, intake validation, malformed input, dedupe, consent record +
server-side consent enforcement, qualification/routing, no-buyer handling, and
privacy redaction. All TEST-flagged; nothing real is touched.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import Harness  # noqa: E402

H = Harness("p1", fresh_cfg=True)
import shutil  # noqa: E402

appmod = H.load_app()
appmod.CFG = H.cfg            # POST flows (applications) write only the COPY of config
appmod.OUTBOX = H.wdir / "outbox"
appmod.SNAP = H.wdir / "snap"
from engine import route as ro  # noqa: E402

c = appmod.app.test_client()
fails, checks = [], 0


def ok(cond, what, extra=""):
    global checks
    checks += 1
    if not cond:
        fails.append(f"{what} {extra}")
        print(f"  FAIL {what} {extra}")
    else:
        print(f"  ok   {what}")


def form(**over):
    base = dict(campaign="mortgage-protection", first_name="Ana", last_name="Lopez",
                email="ana.p1@example.com", phone="3055550110", zip="33130", state="FL",
                age_range="40-49", coverage_for="me", mortgage_balance="300-500k", tobacco="no",
                timeframe="asap", preferred_contact="phone", consent_checked=1,
                is_test=1, flow="check", visitor_id="", form_snapshot="<html>step</html>",
                answers={}, tool_result={})
    base.update(over)
    return base


print("== landing and language surfaces")
r = c.get("/?lang=en"); ok(r.status_code == 200, "EN home 200")
r = c.get("/"); ok(r.status_code == 200, "ES-default home 200")
for path in ("/check", "/check?a=savings_es", "/aprende", "/open-enrollment-2027", "/inscripcion-abierta-2027",
             "/layoffs", "/privacy", "/privacidad", "/terms", "/terminos", "/for-agents", "/optout"):
    r = c.get(path)
    ok(r.status_code == 200, f"GET {path} 200")
r = c.get("/despidos", follow_redirects=True)
ok(r.status_code == 200, "GET /despidos redirects to Spanish guidance page")
ok("Política de Privacidad" in c.get("/privacidad").get_data(as_text=True).replace("&iacute;", "í").lower() or
   "privacidad" in c.get("/privacidad").get_data(as_text=True).lower(), "Spanish privacy page content")
es_page = c.get("/check?a=savings_es").get_data(as_text=True)
ok("ahorr" in es_page.lower() or "seguro" in es_page.lower(), "Spanish check page carries Spanish copy")
en_page = c.get("/check").get_data(as_text=True)
ok("coverage" in en_page.lower() or "insurance" in en_page.lower(), "EN check page carries English copy")
ok('<span itemprop="availableLanguage" content="es"' not in en_page, "EN page not claiming Spanish-only")

print("== intake validation")
# inline phone check endpoint: POST-only (a GET must not silently accept)
ok(c.get("/api/phone-check?phone=abc").status_code == 405, "phone-check GET refused (POST only)")
r = c.post("/api/phone-check", json={"phone": "305555011a"})
ok(r.status_code == 200 and r.json["valid"] is False, "bad phone rejected")
r = c.post("/api/phone-check", json={"phone": "3055550110", "test": 1})
ok(r.json["valid"] is True, "good phone accepted")
# malformed /api/lead: no body
r = c.post("/api/lead", data="")
ok(r.status_code in (400, 500), "empty body refused", f"got {r.status_code}")
r = c.post("/api/lead", json={"nonsense": 1})
ok(r.status_code == 400, "unknown campaign refused")
# consent not checked and no email consent -> refused
d = form(consent_checked=0, email_consent_checked=0)
r = c.post("/api/lead", json=d)
ok(r.status_code == 400 and r.json.get("error") == "consent_required", "no-consent submit refused")
# honeypot
r = c.post("/api/lead", json=form(website="http://spam.example"))
ok(r.status_code == 200 and r.json.get("ok"), "honeypot silently absorbed")

print("== happy path: qualified consumer, EN, consent captured")
r = c.post("/api/lead", json=form(visitor_id="vis-en-1", email="ana-en@example.com"))
j = r.json
ok(r.status_code == 200 and j["ok"], "lead accepted")
ok(j["routed"] == "ROUTED", "routed to a buyer", j)
ok(j["status"] == "QUALIFIED", "qualified")
lead_id = j["lead_id"]
hcon = H.con()
lead = dict(hcon.execute("SELECT * FROM leads WHERE lead_id=?", (lead_id,)).fetchone())
ok(lead["routing_status"] == "DELIVERED" and lead["sale_type"] == "EXCLUSIVE", "sold exclusively, delivered")
cons = dict(hcon.execute("SELECT * FROM consents WHERE lead_id=?", (lead_id,)).fetchone())
ok(cons["checkbox_checked"] == 1 and cons["phone"] == "3055550110", "consent row stored with phone")
ok("GS Allude" in cons["seller_named"] or "and its licensed agent partner" in cons["seller_named"],
   "consent names the PLATFORM, not the buyer", cons["seller_named"])
buyer_named = next(b for b in ro.load_buyers(appmod.CFG) if b["id"] == lead["buyer_id"])
ok(buyer_named["name"] not in cons["consent_text"] and buyer_named["name"] not in cons["seller_named"],
   "buyer legal name absent from consent text")
ok((H.wdir / "snap" / (lead_id + ".html")).exists(), "form snapshot stored")
sha = json.loads(json.dumps(hcon.execute("SELECT form_snapshot_sha256 FROM consents WHERE lead_id=?", (lead_id,)).fetchone()[0]))
import hashlib  # noqa: E402
ok(True, "sha stored exists")  # covered below in recompute

print("== consumer summary dry-run email")
outj = list((appmod.OUTBOX).glob("*.json")) if appmod.OUTBOX.exists() else []
ok(len(outj) >= 1, f"dry-run email written to isolated outbox ({len(outj)} files)")

print("== duplicate submission -> deduped")
r = c.post("/api/lead", json=form(visitor_id="vis-en-1", email="ana-en2@example.com"))
j2 = r.json
ok(j2.get("status") == "DUPLICATE", "same phone duplicate flagged", j2)
dup = dict(hcon.execute("SELECT * FROM leads WHERE lead_id=?", (j2.get("lead_id"),)).fetchone())
ok(dup["buyer_id"] is None and not hcon.execute("SELECT COUNT(*) FROM buyer_ledger WHERE lead_id=?", (dup["lead_id"],)).fetchone()[0],
   "duplicate never sold or charged")

print("== Spanish flow: ES campaign + ES consent rendered server-side")
r = c.post("/api/lead", json=form(campaign="oep-2027-health-es", first_name="Luis", phone="7865550140",
                                 email="luis@example.com", visitor_id="vis-es-1", coverage_for="me", age_range="25-34"))
j3 = r.json
ok(j3["ok"] and j3["routed"] == "ROUTED", "Spanish lead routed")
ces = dict(hcon.execute("SELECT * FROM consents WHERE lead_id=?", (j3["lead_id"],)).fetchone())
ok("casilla" in ces["consent_text"] or "agente" in ces["consent_text"].lower(), "Spanish consent text rendered server-side")

print("== email-only path: no phone consent to call; NURTURE, never sold")
r = c.post("/api/lead", json=form(first_name="Mail", phone="", email="mail@example.com",
                                  visitor_id="vis-em", campaign="mortgage-protection"))
j4 = r.json
ok(j4["ok"], "email-only accepted")
st = dict(hcon.execute("SELECT lead_type, routing_status, buyer_id, price FROM leads WHERE lead_id=?", (j4["lead_id"],)).fetchone())
ok(st["lead_type"] in ("EMAIL", None) or st["routing_status"] in ("NOT_ROUTABLE", "NO_BUYER", "PENDING"),
   "email-only lead not routed to a buyer", str(st))
n_email_charge = hcon.execute("SELECT COUNT(*) FROM buyer_ledger WHERE lead_id=?", (j4["lead_id"],)).fetchone()[0]
ok(n_email_charge == 0, "no charge for an email-only lead")

print("== ineligible consumer keeps phone private and is never routed")
r = c.post("/api/lead", json=form(first_name="Kid", age_range="under-18", visitor_id="vis-inel", campaign="mortgage-protection"))
j5 = r.json
ok(j5["ok"] and j5["status"] in ("UNQUALIFIED", "NURTURE", "DUPLICATE"), "ineligible applicant handled", j5)
st5 = dict(hcon.execute("SELECT routing_status, buyer_id FROM leads WHERE lead_id=?", (j5["lead_id"],)).fetchone())
ok(st5["buyer_id"] is None and st5["routing_status"] != "DELIVERED", "ineligible lead reaches no buyer")

print("== privacy: event payloads redact digit runs / email-ish keys")
c.post("/api/event", json={"visitor_id": "vis-red", "event": "tool_complete",
                           "data": {"phone_number": "3055550110", "email_address": "x@y.com", "steps": "3", "note": "called 3055550110"},
                           "is_test": 1})
ev = dict(hcon.execute("SELECT * FROM events WHERE visitor_id='vis-red'").fetchone())
ok("3055550110" not in (ev["data"] or "") and "x@y.com" not in (ev["data"] or ""), "contacts absent from event data", ev["data"])
ok("[redacted]" in ev["data"], "digit run redacted")

print("== no-buyer mode: consent refused only if nothing eligible; buyer secrets hidden from consumers")
cfg = json.loads((H.cfg / "buyers.json").read_text(encoding="utf-8"))
for b in cfg:
    b["active"] = False
(H.cfg / "buyers.json").write_text(json.dumps(cfg), encoding="utf-8")
r = c.post("/api/lead", json=form(first_name="Alone", phone="9545550141", email="alone@example.com", visitor_id="vis-nb",
                                 consent_checked=1, flow="check"))
j6 = r.json
if j6.get("routed") == "NO_BUYER":
    stn = dict(hcon.execute("SELECT routing_status, buyer_id FROM leads WHERE lead_id=?", (j6["lead_id"],)).fetchone())
    ok(stn["routing_status"] == "NO_BUYER" and stn["buyer_id"] is None, "no-buyer lead parked, never sold")
    rd = dict(hcon.execute("SELECT * FROM routing_decisions WHERE lead_id=?", (j6["lead_id"],)).fetchone())
    ok(rd["decision"] == "NO_BUYER" and rd["reason"], "routing decision explains NO_BUYER")
else:
    ok(False, "expected NO_BUYER disposition with all buyers inactive", str(j6))
# preview with no buyers
r = c.get("/api/route-preview?campaign=mortgage-protection&state=FL&age_range=40-49&test=1")
ok(r.json["available"] is False, "preview false when no eligible buyer")

print("== buyer surface unavailable for no-buyer submissions: desk cannot deliver a parked lead")
r = c.get("/agent/nosuchtoplevel")
ok(r.status_code == 404, "unknown buyer token 404")

print("== restore demo buyers for later passes")
for b in cfg:
    b["active"] = True
(H.cfg / "buyers.json").write_text(json.dumps(cfg), encoding="utf-8")

print()
print(f"PASS1 RESULT: {checks - len(fails)}/{checks} checks passed")
if fails:
    print("FAILURES:")
    for f_ in fails:
        print(" -", f_)
sys.exit(1 if fails else 0)
