"""E2E for the current Health Coverage Check in real Chromium (TEST DATA ONLY).
Personas are fictional, every URL carries ?test=1, phones are 555-01XX, emails @example.com.
Covers: branching paths (self-employed EN via experiment link, lost-coverage ES, family email-only via partner QR),
Medicare + out-of-area exits (nothing saved), consent naming one agent, agent desk one-click statuses, admin screens.
Run with the app on localhost:8000 (or set E2E_BASE=http://<host>:<port> like e2e_browser.py).  Screenshots -> tests/screens_v3/"""
import os
import sqlite3
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("E2E_BASE", "http://localhost:8000")
SHOTS = Path(__file__).parent / "screens_v3"
SHOTS.mkdir(exist_ok=True)
DB = Path(__file__).resolve().parents[1] / "data" / "leads.db"

PERSONAS = [
    dict(name="en_self_employed", url="/go/x001?test=1", need=None, zip="33130",
         taps={"work_type": "realestate", "household": "1", "age": "35-44", "income": "150-250", "coverage_now": "none", "timing": "jan1"},
         contact=dict(first_name="Dana", last_name="Rivers", email="dana.rivers@example.com", phone="3055550121"), full=True, expect="saved"),
    dict(name="es_lost_coverage", url="/check?a=es_life_change&utm_source=instagram&utm_medium=organic&test=1", need=None, zip="33012",
         taps={"lost_when": "recent", "lost_reason": "job", "household": "3", "age": "26-34", "income": "100-150"},
         contact=dict(first_name="Javier", last_name="Paz", email="javier.paz@example.com", phone="3055550122"), full=True, expect="saved"),
    dict(name="en_partner_phone", url="/p/sch-gold-coast-professional-schools?src=guide&test=1", need=None, zip="33024",
         taps={"household": "4", "age": "35-44", "income": "250-400", "coverage_now": "employer", "timing": "renewal",
               "work_type": "realestate"},
         contact=dict(first_name="Priya", last_name="Shah", phone="3055550125"), full=True, expect="saved"),
    dict(name="en_family", url="/check?a=family&utm_source=google&utm_medium=cpc&test=1", need=None, zip="33024",
         taps={"household": "4", "age": "35-44", "income": "250-400", "coverage_now": "none", "timing": "asap"},
         contact=dict(first_name="Rosa", last_name="Medina", email="rosa.medina@example.com", phone="3055550123"), full=True, expect="saved"),
    dict(name="en_bad_phone_then_fixed", url="/check?a=timing&utm_source=reddit&utm_medium=organic&test=1", need="individual", zip="33139",
         taps={"age": "26-34", "income": "150-250", "coverage_now": "none", "timing": "asap"}, bad_phone="800 446 1234",
         contact=dict(first_name="Leo", last_name="Grant", phone="3055550124"), full=True, expect="saved"),
    dict(name="medicare_exit", url="/check?test=1", need="medicare", expect="exit"),
    dict(name="age65_exit", url="/check?test=1", need="individual", zip="33139", taps={"age": "65+"}, expect="exit"),
    dict(name="out_of_area_exit", url="/check?test=1", need="individual", zip="30301", expect="exit"),
]


def shot(page, name):
    page.wait_for_timeout(300)
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=False)


def run(page, p):
    page.goto(BASE + p["url"])
    page.wait_for_selector("#steps .step.on, #app .step.on")
    shot(page, p["name"] + "_01_start")
    for _ in range(14):
        if page.locator(".result b").count() and page.locator("#want").count() == 0:   # exit screen
            shot(page, p["name"] + "_exit")
            return "exit"
        if page.locator("#want").count():
            break
        if page.locator("#zipin").count():
            page.fill("#zipin", p["zip"]); page.click("#zipgo"); page.wait_for_timeout(150); continue
        opts = page.locator(".opt")
        vals = [opts.nth(i).get_attribute("data-v") for i in range(opts.count())]
        if "individual" in vals and "medicare" in vals:      # need screen
            page.click(f'.opt[data-v="{p["need"]}"]'); page.wait_for_timeout(150); continue
        hit = next((v for v in p.get("taps", {}).values() if v in vals), None)
        if hit is None:
            raise AssertionError(f"{p['name']}: no planned answer for options {vals}")
        page.click(f'.opt[data-v="{hit}"]'); page.wait_for_timeout(150)
    shot(page, p["name"] + "_02_result")
    if page.locator("#emailme").is_visible():               # lead-gen only: we never offer to email consumers by default
        raise AssertionError(f"{p['name']}: email-summary option should be hidden")
    page.click("#want" if p["full"] else "#emailme")
    if p.get("bad_phone"):                                   # inline validation: wrong number is caught before consent
        for k, v in p["contact"].items():
            page.fill(f"#{k}", p["bad_phone"] if k == "phone" else v)
        page.click("#tocon"); page.wait_for_function("document.querySelector('#perr').textContent.length > 0")
        page.locator("#phone").scroll_into_view_if_needed(); page.evaluate("window.scrollBy(0, -120)")
        shot(page, p["name"] + "_03a_phone_error")
    for k, v in p["contact"].items():
        page.fill(f"#{k}", v)
    page.locator("#first_name").scroll_into_view_if_needed(); page.evaluate("window.scrollBy(0, -160)")
    shot(page, p["name"] + "_03_contact")
    page.click("#tocon")
    page.wait_for_selector("#cbox")
    shot(page, p["name"] + "_04_consent")
    page.check("#cbox"); page.click("#submit")
    page.wait_for_selector('.result .lbl:text-matches("^(Done|Listo)$")')
    shot(page, p["name"] + "_05_done")
    return "saved"


def main():
    con = sqlite3.connect(DB)
    before = con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    failures = []
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        for p in PERSONAS:
            ctx = b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2)
            page = ctx.new_page(); errs = []
            page.on("pageerror", lambda e: errs.append(str(e)))
            got = run(page, p)
            if got != p["expect"] or errs:
                failures.append((p["name"], got, errs))
            ctx.close()
        after = con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        saved = sum(1 for p in PERSONAS if p["expect"] == "saved")
        if after - before != saved:
            failures.append(("lead count", after - before, f"expected {saved} (exits must save nothing)"))
        rows = con.execute("SELECT first_name, lang, need_path, lead_type, phone_e164, phone_source, phone_valid, consent_version, tracking_id, "
                           "qual_status, tier, routing_status, buyer_id, partner_id, experiment_id, is_test FROM leads ORDER BY created_at DESC LIMIT ?", (saved,)).fetchall()
        for r in rows:
            print(r)
            if r[-1] != 1:
                failures.append(("is_test", r))
            if r[3] == "PHONE_LEAD" and (r[5] != "CONSUMER_SUBMITTED" or r[6] != 1 or not r[4]):
                failures.append(("phone provenance", r))
            if r[3] == "EMAIL_ONLY_INQUIRY" and r[4]:
                failures.append(("email-only kept a phone", r))
        # agent desk: one click per status
        ctx = b.new_context(viewport={"width": 1280, "height": 900}); page = ctx.new_page()
        page.goto(f"{BASE}/agent/demo-health-token"); shot(page, "agent_01_desk")
        lid = con.execute("SELECT lead_id FROM leads WHERE buyer_id='demo-health-agent' AND is_test=1 ORDER BY created_at DESC LIMIT 1").fetchone()[0]
        page.goto(f"{BASE}/agent/demo-health-token/lead/{lid}"); page.screenshot(path=str(SHOTS / "agent_02_lead.png"), full_page=True)
        page.click('button[name=status][value="APPOINTMENT"]'); page.wait_for_load_state()
        # desk-home rows show STATUS pills; one-click statuses live on the per-lead desk page
        page.goto(f"{BASE}/agent/demo-health-token/lead/{lid}")
        page.click('button[name=status][value="SOLD"]'); page.wait_for_load_state()
        page.screenshot(path=str(SHOTS / "agent_03_after_status.png"), full_page=True)
        st = sqlite3.connect(DB).execute("SELECT agent_status FROM leads WHERE lead_id=?", (lid,)).fetchone()[0]
        if st != "SOLD":
            failures.append(("agent status", st))
        for path, name in [("/admin/funnel?token=demo&test=only&dim=source", "admin_funnel_test"), ("/admin/lab?token=demo", "admin_lab"), ("/admin/quality?token=demo&dim=angle&test=only", "admin_quality_test"),
                           ("/admin/partners?token=demo", "admin_partners"), ("/admin/intel/briefs?token=demo", "admin_briefs"),
                           ("/admin/content?token=demo", "admin_content"), ("/learn/health-insurance-self-employed", "learn_en"),
                           ("/aprende/perdi-seguro-medico-del-trabajo", "aprende_es"), ("/partner/" + sqlite3.connect(DB).execute(
                               "SELECT token FROM partners WHERE partner_id='sch-gold-coast-professional-schools'").fetchone()[0], "partner_stats")]:
            page.goto(BASE + path); page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)
        ctx.close(); b.close()
    if failures:
        print("FAILURES:", failures); sys.exit(1)
    print("E2E OK:", len(PERSONAS), "personas,", saved, "TEST leads saved, exits saved nothing")


if __name__ == "__main__":
    main()
