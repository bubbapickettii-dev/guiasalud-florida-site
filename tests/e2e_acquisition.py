"""Acquisition attribution E2E (TEST DATA ONLY) - proves, in a real browser, that every visit, Check step,
phone-step view, phone submission and lead lands on the right experiment / placement / landing page / language /
phone wording, and that none of it ever counts as REAL traffic.

Run with the app on localhost:8000 against a fresh DB (wave-1 experiments X013-X015 are created at startup).
All personas are fictional, every phone is 555-01XX. Screenshots -> tests/screens_acq/"""
import sqlite3
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

B = "http://localhost:8000"
DB = Path(__file__).resolve().parents[1] / "data" / "leads.db"
SHOTS = Path(__file__).parent / "screens_acq"
SHOTS.mkdir(exist_ok=True)
ANS = ["cigna", "ending", "marketplace", "self_employed", "realestate", "beauty", "1", "3", "26-34", "35-44", "100-150", "150-250", "none", "asap", "jan1", "recent", "job"]
fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def run_check(p, zip_="33012", phone=None, bad_phone=None, stop_at_phone=False, name="T"):
    p.wait_for_selector("#steps .step.on")
    for _ in range(14):
        if p.locator("#want").count() or p.locator(".result b").count() and not p.locator("#want").count() and not p.locator(".opt").count() and not p.locator("#zipin").count():
            break
        if p.locator("#zipin").count():
            p.fill("#zipin", zip_); p.click("#zipgo"); p.wait_for_timeout(120); continue
        vals = [p.locator(".opt").nth(i).get_attribute("data-v") for i in range(p.locator(".opt").count())]
        p.click(f'.opt[data-v="{next(v for v in ANS if v in vals)}"]'); p.wait_for_timeout(100)
    if not p.locator("#want").count():
        return "exit"
    p.click("#want"); p.wait_for_selector("#phone")
    if stop_at_phone:
        p.wait_for_timeout(400); return "abandoned_at_phone"
    p.fill("#first_name", "Acq"); p.fill("#last_name", name)
    if bad_phone:
        p.fill("#phone", bad_phone); p.click("#tocon"); p.wait_for_function("document.querySelector('#perr').textContent.length > 0")
    p.fill("#phone", phone); p.click("#tocon"); p.wait_for_selector("#cbox"); p.check("#cbox"); p.click("#submit")
    p.wait_for_selector('.result .lbl:text-matches("^(Done|Listo)$")')
    return "lead"


def main():
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    check(all(con.execute("SELECT 1 FROM experiments WHERE experiment_id=?", (e,)).fetchone() for e in ("X013", "X014", "X015")), "wave-1 experiments X013-X015 exist")
    # an extra experiment whose landing page is a Spanish article and whose phone wording is forced to B
    data = urllib.parse.urlencode({"experiment_id": "X901", "name": "E2E learn-page landing", "audience": "Spanish-speaking self-employed (Miami-Dade)",
                                   "angle": "es_self_employed", "channel": "seo_content", "source": "instagram", "medium": "organic",
                                   "landing_page": "/aprende/seguro-medico-trabajador-1099", "phone_variant": "B"}).encode()
    urllib.request.urlopen(urllib.request.Request(B + "/admin/lab?token=demo", data=data, method="POST"))
    clicks0 = {r["code"]: r["clicks"] for r in con.execute("SELECT code, clicks FROM links")}

    with sync_playwright() as pw:
        br = pw.chromium.launch()
        # QA device: cookie only, no ?test=1 anywhere afterwards
        ctx = br.new_context(viewport={"width": 390, "height": 844}); p = ctx.new_page()
        p.goto(B + "/qa?on=1"); p.screenshot(path=str(SHOTS / "00_qa_on.png"))
        p.goto(B + "/go/x013?c=story"); p.click(".ctabox a.btn"); r1 = run_check(p, phone="3055550141", name="StoryLead")
        p.screenshot(path=str(SHOTS / "01_x013_story_done.png")); ctx.close()
        check(r1 == "lead", "X013 story visitor (QA cookie, no ?test) completes with a phone lead")

        ctx = br.new_context(viewport={"width": 390, "height": 844}); p = ctx.new_page()
        p.goto(B + "/go/x013?c=reel&test=1"); p.click(".ctabox a.btn"); r2 = run_check(p, stop_at_phone=True, name="ReelAbandon")
        p.screenshot(path=str(SHOTS / "02_x013_reel_phone_step.png")); ctx.close()
        check(r2 == "abandoned_at_phone", "X013 reel visitor reaches the phone step and leaves")

        ctx = br.new_context(viewport={"width": 390, "height": 844}); p = ctx.new_page()
        p.goto(B + "/go/x014?test=1"); p.wait_for_selector("#steps .step.on")       # lands, leaves
        p.goto("about:blank"); p.goto(B + "/check?a=es_self_employed&test=1")          # comes back later with no tracking params
        r3 = run_check(p, phone="3055550142", name="ReturnES"); ctx.close()
        check(r3 == "lead", "X014 visitor returns later without params and converts")

        ctx = br.new_context(viewport={"width": 390, "height": 844}); p = ctx.new_page()
        p.goto(B + "/go/x901?test=1"); p.wait_for_load_state()
        check("/aprende/seguro-medico-trabajador-1099" in p.url, "experiment landing page is honored (/aprende article)")
        p.screenshot(path=str(SHOTS / "03_x901_article.png")); p.click(".ctabox a.btn"); p.wait_for_load_state()
        r4 = run_check(p, phone="3055550143", bad_phone="8004461234", name="ArticleB")
        p.screenshot(path=str(SHOTS / "04_x901_done.png")); ctx.close()
        check(r4 == "lead", "article reader clicks through, fixes a bad phone, converts")

        ctx = br.new_context(viewport={"width": 390, "height": 844}); p = ctx.new_page()
        p.goto(B + "/go/x015?a=es_check&c=es-adgroup&test=1"); r5 = run_check(p, phone="3055550144", name="GoogleES")
        p.screenshot(path=str(SHOTS / "05_x015_es_done.png")); ctx.close()
        check(r5 == "lead", "X015 Spanish ad group visitor converts")

        ctx = br.new_context(viewport={"width": 390, "height": 844}); p = ctx.new_page()
        p.goto(B + "/go/x015?c=en-adgroup&test=1"); r6 = run_check(p, zip_="30301", name="OutOfArea")
        p.screenshot(path=str(SHOTS / "06_x015_out_of_area.png")); ctx.close()
        check(r6 == "exit", "X015 English visitor outside Florida exits (nothing saved)")

        # admin views
        ctx = br.new_context(viewport={"width": 1360, "height": 900}); p = ctx.new_page()
        for path, n in [("/admin/lab?token=demo", "10_lab"), ("/admin/lab/X013?token=demo&test=only", "11_x013_detail_test"),
                        ("/admin/lab/X901?token=demo&test=only", "12_x901_detail_test"), ("/admin/audiences?token=demo", "13_audiences"),
                        ("/admin/funnel?token=demo&test=only&dim=phone_variant", "14_funnel_phone_variant"),
                        ("/admin/funnel?token=demo&test=only&dim=landing", "15_funnel_landing")]:
            p.goto(B + path); p.screenshot(path=str(SHOTS / f"{n}.png"), full_page=True)
        ctx.close(); br.close()

    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    leads = {r["last_name"]: dict(r) for r in con.execute("SELECT * FROM leads WHERE first_name='Acq'")}
    L = leads.get
    check(set(leads) == {"StoryLead", "ReturnES", "ArticleB", "GoogleES"}, f"exactly 4 leads saved (exits/abandons save nothing): {sorted(leads)}")
    check(all(l["is_test"] == 1 for l in leads.values()), "every lead is TEST DATA")
    check(con.execute("SELECT COUNT(*) FROM events WHERE is_test=0").fetchone()[0] == 0, "zero REAL events created by any of this")
    check({c: r for c, r in ((r["code"], r["clicks"]) for r in con.execute("SELECT code, clicks FROM links"))} == clicks0, "no tracking-link click counted (QA/test)")
    s = L("StoryLead") or {}
    check((s.get("experiment_id"), s.get("utm_source"), s.get("utm_content"), s.get("entry_page")) == ("X013", "instagram", "story", "/learn/florida-health-plan-changes-2027"),
          f"StoryLead attributed X013 / instagram / story / entry = redirected hub: {s.get('experiment_id'), s.get('utm_source'), s.get('utm_content'), s.get('entry_page')}")
    s = L("ReturnES") or {}
    check((s.get("experiment_id"), s.get("lang"), s.get("utm_source")) == ("X014", "es", "instagram"), f"ReturnES still credited to X014 / es / instagram: {s.get('experiment_id'), s.get('lang'), s.get('utm_source')}")
    s = L("ArticleB") or {}
    check((s.get("experiment_id"), s.get("entry_page"), s.get("phone_variant"), s.get("utm_source")) == ("X901", "/aprende/seguro-medico-trabajador-1099", "B", "instagram"),
          f"ArticleB credited X901 / entry article / phone wording B / instagram: {s.get('experiment_id'), s.get('entry_page'), s.get('phone_variant'), s.get('utm_source')}")
    s = L("GoogleES") or {}
    check((s.get("experiment_id"), s.get("lang"), s.get("utm_content"), s.get("angle")) == ("X015", "es", "es-adgroup", "es_check"),
          f"GoogleES credited X015 / es / es-adgroup / es_check: {s.get('experiment_id'), s.get('lang'), s.get('utm_content'), s.get('angle')}")
    check(all(l["phone_source"] == "CONSUMER_SUBMITTED" and l["phone_valid"] == 1 and l["phone_variant"] in ("A", "B") for l in leads.values()),
          "every lead: consumer-submitted valid phone + recorded phone wording")
    import importlib; sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from engine import funnel, lab
    m13 = lab.metrics(con, "X013")["test"]
    check((m13["landing_page_visits"], m13["phone_step_views"], m13["phones_submitted"], m13["valid_phones"], m13["consented_phone_leads"]) == (2, 2, 1, 1, 1),
          f"X013 TEST funnel visits 2 / phone step 2 / submitted 1 / valid 1 / phone leads 1: {m13['landing_page_visits'], m13['phone_step_views'], m13['phones_submitted'], m13['valid_phones'], m13['consented_phone_leads']}")
    check(lab.metrics(con, "X013")["real"]["landing_page_visits"] == 0 and lab.metrics(con, "X013")["evidence"] == "TEST DATA ONLY", "X013 REAL side is empty; evidence = TEST DATA ONLY")
    split = {r["k"]: r for r in funnel.by(con, "source", "only", experiment="X013")}
    check(set(split) == {"instagram"}, "X013 split by source = instagram only")
    m901 = lab.metrics(con, "X901")["test"]
    check((m901["phones_submitted"], m901["valid_phones"]) == (1, 1), f"X901 invalid-then-fixed phone counts as 1 submitter, 1 valid: {m901['phones_submitted'], m901['valid_phones']}")
    m15 = funnel.by(con, "language", "only", experiment="X015")
    check({r["k"]: r["consented_phone_leads"] for r in m15}.get("es") == 1 and {r["k"]: r["visits"] for r in m15}.get("en") == 1, "X015 compares EN vs ES within one experiment")
    land = {r["k"] for r in funnel.by(con, "landing", "only")}
    check("/aprende/seguro-medico-trabajador-1099" in land and "/check" in land, f"landing-page dimension separates article vs Check: {sorted(land)}")
    pvs = {r["k"] for r in funnel.by(con, "phone_variant", "only")}
    check(pvs <= {"A", "B"} and "B" in pvs, f"phone-wording dimension populated: {sorted(pvs)}")
    print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
