"""One-command first-buyer pilot rehearsal (docs/FIRST_BUYER_PILOT.md §3, automated).

  python tools/pilot_rehearsal.py

Runs the ENTIRE validation sequence an operator should be able to show, in one isolated temp
app (its own database + its own copy of config; staging defaults: live delivery OFF, payment
rail OFF, test-flagged leads only). Nothing here contacts anyone or spends anything.

It prints a PASS/FAIL checklist and exits non-zero if anything fails:

  1. buyer configured as an inactive draft (admin onboarding form)
  2. independent license/NPN verification recorded (never auto-derived, never activating)
  3. activation succeeds and issues a private portal link
  4. a TEST funnel lead: consent -> platform routing -> desk -> ONE ledger charge
  5. buyer desk outcomes recorded (CONTACTED, SOLD)
  6. in-policy return credited EXACTLY once (a repeat click cannot charge again)
  7. late return refused: reason recorded, sits in the admin credit queue, no ledger movement
  8. operator GRANT posts one LEAD_CREDIT; DENY leaves the ledger untouched
  9. stranded ROUTED sale heals on the buyer's next desk load (deliver + charge, once)
 10. ledger ends consistent: balance == purchases + credits + adjustments - charges
"""
import json, os, shutil, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILS = []


def step(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="pilot_rehearsal_"))
    os.environ["LEADENGINE_DB"] = str(tmp / "rehearsal.db")
    os.environ["ADMIN_TOKEN"] = "t"
    os.environ["INTEL_DB"] = str(tmp / "none.db")
    os.environ["LEADENGINE_OUTBOX"] = str(tmp / "outbox")
    os.environ["LEADENGINE_LIVE_DELIVERY"] = "0"
    for k in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"):
        os.environ.pop(k, None)                     # no payment rail in a rehearsal
    import importlib
    import app as appmod
    importlib.reload(appmod)
    import engine.billing as billing
    import engine.route as route

    # the rehearsal copies config/ to the temp dir and owns that copy: deactivating the DEMO
    # buyers in the COPY (never in config/) makes the isolated flow route every TEST lead to
    # the rehearsed buyer — exactly how a real buyer will sit alone on the desk on day 1.
    d = tmp / "cfg"
    shutil.copytree(appmod.CFG, d)
    buyers = json.loads((d / "buyers.json").read_text(encoding="utf-8"))
    for b in buyers:
        if b.get("is_demo"):
            b["active"] = False
    (d / "buyers.json").write_text(json.dumps(buyers, indent=1), encoding="utf-8")
    appmod.CFG = d
    appmod.OUTBOX = tmp / "outbox"
    c = appmod.app.test_client()

    print("Pilot rehearsal — isolated temp app · staging defaults · TEST data only\n")

    tok = _rehearse(c, appmod, billing, route)
    _consistency(appmod, billing, tok)

    shutil.rmtree(tmp, ignore_errors=True)
    if FAILS:
        print(f"\nResult: {len(FAILS)} check(s) FAILED — see above")
        return 1
    print("\nResult: ALL CHECKS PASSED — the pilot mechanics are rehearsed end to end"
          " (isolated temp app; nothing was sent, spent, activated or deployed)")
    return 0


def _rehearse(c, appmod, billing, route) -> str:
    BUY = dict(buyer_id="pilot-buyer", action="save", name="Pilot Health Advisors LLC", license_number="D1234567",
               npn="9876543", licensed_states="FL", products="mortgage_protection", tiers="A,B",
               price_A="60", price_B="35", daily_cap="5", age_min="30", age_max="64")

    def lead(**kw):
        d = dict(campaign="mortgage-protection", first_name="Ana", last_name="Rehearsal",
                 email="ana.rehearsal@example.test", phone="3055550101", zip="33130", state="FL",
                 age_range="40-49", coverage_for="me_spouse_kids", mortgage_balance="300-500k",
                 tobacco="no", timeframe="asap", preferred_contact="phone", is_test=1,
                 tool_result={"headline": "$300,000"}, consent_checked=True)
        d.update(kw)
        return c.post("/api/lead", json=d).get_json()

    # 1 draft (NOT activated)
    c.post("/admin/buyers?token=t", data=BUY)
    row = _by_id(appmod)
    step("buyer configured as an inactive draft", bool(row) and not row.get("active"))
    # 2 independent verification recordable
    c.post("/admin/buyer/pilot-buyer/verify?token=t", data={"what": "license", "ref": "FL DFS licensee search — rehearsal"})
    c.post("/admin/buyer/pilot-buyer/verify?token=t", data={"what": "npn", "ref": "NPN lookup — rehearsal"})
    step("license + NPN verification recorded independently",
         bool((_by_id(appmod).get("_verification") or {}).get("license")
              and (_by_id(appmod).get("_verification") or {}).get("npn")))
    # 3 activation + portal token
    c.post("/admin/buyers?token=t", data={"buyer_id": "pilot-buyer", "action": "activate"})
    row = _by_id(appmod)
    tok = row.get("portal_token") or ""
    step("activation succeeds and issues the private portal link", row.get("active") is True and bool(tok))
    # 4 one TEST lead through consent -> routing -> desk -> ledger
    r = lead()
    lid = r.get("lead_id", "")
    lr = _lead(appmod, lid)
    step("TEST lead routed by the platform to the rehearsed buyer ONLY",
         r.get("routed") == "ROUTED" and lr is not None and lr["buyer_id"] == "pilot-buyer" and lr["routing_status"] == "DELIVERED",
         json.dumps(r)[:160])
    n = appmod.con.execute("SELECT COUNT(*) FROM buyer_ledger WHERE lead_id=? AND kind='LEAD_CHARGE'", (lid,)).fetchone()[0]
    step("delivered and charged exactly once", n == 1)
    step("lead visible on the buyer's private desk", "Ana" in c.get(f"/agent/{tok}").get_data(as_text=True))
    # 5 outcomes recorded
    c.post(f"/agent/{tok}", data={"lead_id": lid, "status": "CONTACTED"})
    c.post(f"/agent/{tok}", data={"lead_id": lid, "status": "SOLD"})
    outs = [dict(x)["outcome"] for x in appmod.con.execute(
        "SELECT outcome FROM lead_outcomes WHERE lead_id=? ORDER BY id", (lid,))]
    step("CONTACTED and SOLD recorded on the lead's history", outs == ["CONTACTED", "SOLD"], str(outs))
    # 6 in-policy return credited once
    lid2 = lead(first_name="Bob", phone="3055550102", email="bob.rehearsal@example.test").get("lead_id", "")
    c.post(f"/agent/{tok}", data={"lead_id": lid2, "status": "RETURNED", "reason": "unreachable"})
    step("in-policy return posts ONE LEAD_CREDIT",
         appmod.con.execute("SELECT COUNT(*) FROM buyer_ledger WHERE lead_id=? AND kind='LEAD_CREDIT'", (lid2,)).fetchone()[0] == 1)
    c.post(f"/agent/{tok}", data={"lead_id": lid2, "status": "RETURNED", "reason": "unreachable"})
    step("a repeated return click can NEVER credit again",
         appmod.con.execute("SELECT COUNT(*) FROM buyer_ledger WHERE lead_id=? AND kind='LEAD_CREDIT'", (lid2,)).fetchone()[0] == 1)
    # 7 late return -> refusal + queue, no movement
    lid3 = lead(first_name="Cara", phone="3055550103", email="cara.rehearsal@example.test").get("lead_id", "")
    _age(appmod, lid3, days=4)
    c.post(f"/agent/{tok}", data={"lead_id": lid3, "status": "RETURNED"})
    note = appmod.con.execute("SELECT note FROM lead_outcomes WHERE lead_id=? AND outcome='RETURNED'", (lid3,)).fetchone()
    step("late return REFUSED with its reason, no ledger credit",
         bool(note) and note["note"].startswith("no credit: ") and
         appmod.con.execute("SELECT COUNT(*) FROM buyer_ledger WHERE lead_id=? AND kind='LEAD_CREDIT'", (lid3,)).fetchone()[0] == 0)
    step("refusal sits in the operator credit queue", lid3[-8:] in c.get("/admin/credits?token=t").get_data(as_text=True))
    # 8 operator grant / deny
    c.post(f"/admin/lead/{lid3}/credit_decision?token=t", data={"decision": "grant", "note": "pilot rehearsal grant"})
    step("operator GRANT posts one LEAD_CREDIT",
         appmod.con.execute("SELECT COUNT(*) FROM buyer_ledger WHERE lead_id=? AND kind='LEAD_CREDIT'", (lid3,)).fetchone()[0] == 1)
    lid4 = lead(first_name="Dan", phone="3055550104", email="dan.rehearsal@example.test").get("lead_id", "")
    _age(appmod, lid4, days=4)
    c.post(f"/agent/{tok}", data={"lead_id": lid4, "status": "RETURNED"})
    c.post(f"/admin/lead/{lid4}/credit_decision?token=t", data={"decision": "deny", "note": "pilot rehearsal deny"})
    step("operator DENY leaves the ledger untouched",
         appmod.con.execute("SELECT COUNT(*) FROM buyer_ledger WHERE lead_id=? AND kind='LEAD_CREDIT'", (lid4,)).fetchone()[0] == 0)
    # 9 stranded ROUTED sale heals on the next desk load, deliver+charge once
    lid5 = "LSTRANDED1"
    appmod.con.execute("""INSERT INTO leads (lead_id, created_at, is_test, campaign, product, first_name, last_name,
        email, phone, phone_source, zip, state, age_range, contact_scope, qual_status, score, tier,
        routing_status, buyer_id, consent_ts, price, tool_result)
        VALUES (?, datetime('now'), 1, 'mortgage-protection', 'mortgage_protection', 'Eve', 'Strand',
        'eve.strand@example.test', '3055550105', 'CONSUMER_SUBMITTED', '33101', 'FL', '40-49', 'PHONE_AND_EMAIL',
        'QUALIFIED', 80, 'A',        'ROUTED', 'pilot-buyer', datetime('now'), 60, '{}')""", (lid5,))
    appmod.con.execute("""INSERT INTO consents (lead_id, ts, consent_version, consent_text, seller_named,
        checkbox_checked, scope, ip, user_agent, page_url, form_snapshot_sha256, phone, email)
        VALUES (?, datetime('now'), 'rehearsal', 'rehearsal consent text (isolated temp app)', '', 1,
        'PHONE_AND_EMAIL', '127.0.0.1', 'rehearsal', 'tools/pilot_rehearsal.py', '-', '3055550105', 'eve.strand@example.test')""", (lid5,))
    appmod.con.commit()
    c.get(f"/agent/{tok}")                      # the desk's documented self-healing path
    lr5 = _lead(appmod, lid5)
    step("stranded ROUTED sale heals: delivered + charged exactly once via the desk",
         lr5["routing_status"] == "DELIVERED" and
         appmod.con.execute("SELECT COUNT(*) FROM buyer_ledger WHERE lead_id=? AND kind='LEAD_CHARGE'", (lid5,)).fetchone()[0] == 1)
    return tok


def _consistency(appmod, billing, tok):
    # 10 the ledger agrees with itself: balance == purchases + credits + adjustments - charges
    rows = [dict(x) for x in appmod.con.execute("SELECT kind, amount FROM buyer_ledger WHERE buyer_id='pilot-buyer' AND is_test=1")]
    sums = {"PACKAGE_PURCHASE": 0.0, "LEAD_CREDIT": 0.0, "ADJUSTMENT": 0.0, "LEAD_CHARGE": 0.0}
    for r in rows:
        sums[r["kind"]] = sums.get(r["kind"], 0.0) + float(r["amount"])
    expected = sums["PACKAGE_PURCHASE"] + sums["LEAD_CREDIT"] + sums["ADJUSTMENT"] + sums["LEAD_CHARGE"]
    actual = billing.balance(appmod.con, "pilot-buyer", 1)
    step(f"ledger consistent (expected ${expected:.2f}, balance ${actual:.2f})",
         round(expected, 2) == round(actual, 2))
    page = appmod.app.test_client().get(f"/agent/{tok}/billing").get_data(as_text=True)
    step("billing page renders the same ledger", f"${actual:.2f}" in page)


def _by_id(appmod):
    return next((x for x in json.loads((appmod.CFG / "buyers.json").read_text(encoding="utf-8"))
                 if x["id"] == "pilot-buyer"), None)


def _lead(appmod, lid):
    r = appmod.con.execute("SELECT * FROM leads WHERE lead_id=?", (lid,)).fetchone() if lid else None
    return dict(r) if r else None


def _age(appmod, lid, days):
    appmod.con.execute("UPDATE leads SET delivered_at=datetime('now', ?) WHERE lead_id=?",
                       (f"-{int(days)} days", lid))
    appmod.con.commit()


if __name__ == "__main__":
    sys.exit(main())
