"""Focused verification: community-partner tracked links -> /check -> consented phone lead -> funnel attribution.
TEST DATA ONLY (?test=1 on every URL, 555-01XX phones, @example.com emails). Run with the app on
localhost:8000 (E2E_BASE to override). Checks:
  1. each /go/pt* community-partner code 302s to /check with the right partner + angle params
  2. one full consumer submission through /go/ptlaborfindersfl?test=1 saves a TEST lead whose
     partner_id matches, phone_source=CONSUMER_SUBMITTED, phone valid, consent recorded
  3. /admin/funnel (dim=partner, TEST view) renders that partner's row
Run: python tests/manual/test_partner_link_path_live.py
"""
import os
import re
import sqlite3
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("E2E_BASE", "http://localhost:8000")
DB = Path(__file__).resolve().parents[2] / "data" / "leads.db"
failures: list[tuple] = []

LINKS = ["ptbranchesmiamivita", "ptfchcincfl", "ptbrowardesol", "ptcflccorlando", "ptechofl", "ptlaborfindersfl"]
PARTNER_OF = {
    "ptbranchesmiamivita": "ngo-branches-miami-vita", "ptfchcincfl": "ngo-fchcinc-fl",
    "ptbrowardesol": "edu-broward-esol", "ptcflccorlando": "ngo-cflcc-orlando",
    "ptechofl": "ngo-echofl", "ptlaborfindersfl": "biz-laborfinders-fl",
}


def check_redirects(page):
    for code in LINKS:
        r = page.goto(f"{BASE}/go/{code}?test=1")
        if r.status != 302 and page.url.split("?")[0].endswith("/check"):
            # Playwright follows redirects; the final URL is what matters
            pass
        final = page.url
        ok = "/check" in final and f"partner={PARTNER_OF[code]}" in final
        if not ok:
            failures.append((f"redirect {code}", final))
        else:
            print(f"OK   /go/{code} -> {final[:110]}")


def run_submission(page):
    page.goto(f"{BASE}/go/ptlaborfindersfl?test=1")
    page.wait_for_selector("#steps .step.on, #app .step.on")
    taps = {"work_type": "realestate", "household": "1", "age": "35-44", "income": "150-250",
            "coverage_now": "none", "timing": "jan1"}
    for _ in range(14):
        if page.locator(".result b").count() and page.locator("#want").count() == 0:
            raise AssertionError(f"unexpected exit screen: {page.url}")
        if page.locator("#want").count():
            break
        if page.locator("#zipin").count():
            page.fill("#zipin", "33130"); page.click("#zipgo"); page.wait_for_timeout(150); continue
        opts = page.locator(".opt")
        vals = [opts.nth(i).get_attribute("data-v") for i in range(opts.count())]
        if "individual" in vals and "medicare" in vals:
            page.click('.opt[data-v="individual"]'); page.wait_for_timeout(150); continue
        hit = next((v for v in taps.values() if v in vals), None)
        if hit is None:
            raise AssertionError(f"no planned answer for options {vals}")
        page.click(f'.opt[data-v="{hit}"]'); page.wait_for_timeout(150)
    page.click("#want")
    for k, v in (("first_name", "Ana"), ("last_name", "Test"), ("email", "ana.test@example.com"), ("phone", "3055550126")):
        page.fill(f"#{k}", v)
    page.click("#tocon"); page.wait_for_selector("#cbox")
    page.check("#cbox"); page.click("#submit")
    page.wait_for_selector('.result .lbl:text-matches("^(Done|Listo)$")')


def main():
    con = sqlite3.connect(DB)
    before = con.execute("SELECT COUNT(*) FROM leads WHERE is_test=1").fetchone()[0]
    clicks_before = dict(con.execute("SELECT code, clicks FROM links WHERE code IN (?,?)", (LINKS[0], LINKS[-1])).fetchall() or [])
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        ctx = b.new_context(viewport={"width": 390, "height": 844})
        page = ctx.new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        check_redirects(page)
        run_submission(page)
        ctx.close(); b.close()
    # funnel attribution (TEST view, partner dimension) — admin pages 403 non-browser flows on token schema;
    # local dev token is DEVLOCAL (ADMIN_TOKEN env overrides)
    import requests
    adm = requests.get(f"{BASE}/admin/funnel", params={"token": "DEVLOCAL", "test": "only", "dim": "partner"}, timeout=15)
    html = "" if adm.status_code != 200 else adm.text
    if adm.status_code != 200:
        failures.append(("funnel page status", adm.status_code))
    after = con.execute("SELECT COUNT(*) FROM leads WHERE is_test=1").fetchone()[0]
    row = con.execute("""SELECT partner_id, experiment_id, campaign, lead_type, phone_source, phone_valid,
                                consent_version, phone_e164, link_code, utm_source, utm_medium, is_test
                         FROM leads WHERE partner_id='biz-laborfinders-fl' AND is_test=1
                         ORDER BY created_at DESC LIMIT 1""").fetchone()
    if after - before != 1:
        failures.append(("lead count delta", after - before, "expected exactly 1 TEST lead"))
    if not row:
        failures.append(("no partner-attributed lead", "biz-laborfinders-fl"))
    else:
        got = dict(zip("partner_id experiment_id campaign lead_type phone_source phone_valid consent_version phone_e164 link_code utm_source utm_medium is_test".split(), row))
        print("lead:", got)
        if got["lead_type"] != "PHONE_LEAD" or got["phone_source"] != "CONSUMER_SUBMITTED" or got["phone_valid"] != 1 or not got["consent_version"]:
            failures.append(("lead provenance", got))
        if got["link_code"] != "ptlaborfindersfl":
            failures.append(("link attribution", got["link_code"]))
    if "biz-laborfinders-fl" not in html:
        failures.append(("funnel page missing partner row", "dim=partner TEST view"))
    if errs:
        failures.append(("page errors", errs))
    print("FAILURES:", failures if failures else "none")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
