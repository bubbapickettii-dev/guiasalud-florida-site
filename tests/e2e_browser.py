"""End-to-end test in a real Chromium: click through tracked links -> tool -> form -> consent -> submit.
All submissions use ?test=1, fictional 555-01XX numbers and @example.com emails, and are flagged is_test=1.
Run with the app on localhost:8000.  Screenshots -> tests/screens/"""
import json
import sqlite3
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

import os
BASE = os.environ.get("E2E_BASE", "http://localhost:8000")   # WSL/Docker port-relay can hold loopback 8000: run with E2E_BASE=http://<LAN-IP>:8000
SHOTS = Path(__file__).parent / "screens"
SHOTS.mkdir(exist_ok=True)
DB = Path(__file__).resolve().parents[1] / "data" / "leads.db"

PEOPLE = [  # fictional test personas. Tap values are the CURRENT option keys of the v5 flow
    # (work_type -> age -> state -> employer plan -> income band -> covered now -> timing)
    dict(code="se-tt", first="Dana", last="Rivers", email="dana.rivers@example.com", phone="3055550101", zip="33130",
         taps=["realtor", "30-39", "FL", "no", "250-500k", "no", "asap", "no"], tool={},
         full=True, mobile=True, shots="se_", done="You're all set"),
    dict(code="mp-close", first="Marco", last="Ellis", email="marco.ellis@example.com", phone="3055550102", zip="33156",
         taps=["me_spouse_kids", "40-49", "FL", "300-500k", "no", "asap"], tool={"balance": "380000", "payment": "2900"},
         full=True, mobile=True, shots="mp_", done="You're all set"),
    dict(code="fe-mail", first="Gloria", last="Banks", email="gloria.banks@example.com", phone="3055550103", zip="33012",
         taps=["me", "60-69", "FL", "10-15k", "no", "good", "asap"], tool={}, full=True, done="You're all set"),
    # full Spanish journey on the CURRENT product (v5 Health Coverage Check, es_life_change angle)
    dict(url="/check?a=es_life_change&utm_source=instagram&utm_medium=organic&", first="Lucia", last="Paz",
         email="lucia.paz@example.com", phone="3055550106", zip="33012", es=True, full=True,
         taps=["recent", "job", "3", "26-34", "100-150"], done="Listo"),
    # partner-QR entry: /p/<partner> -> Health Coverage Check (EN, phone-only save)
    dict(url="/p/sch-gold-coast-professional-schools?src=guide&", first="Priya", last="Shah", email="", phone="3055550110", zip="33024",
         full=True, taps=["realestate", "4", "35-44", "250-400", "employer", "renewal"], done="Done"),
    dict(code="se-tt", first="Dana", last="Rivers", email="dana.rivers@example.com", phone="3055550101", zip="33130",
         taps=["realtor", "30-39", "FL", "no", "250-500k", "no", "asap", "no"], tool={},
         full=True, done="We already have your request"),  # duplicate: 30-day phone dedupe says it directly
    dict(code="mp-gs", first="Kevin", last="Moss", email="kevin.moss@example.com", phone="3055550107", zip="33178",
         taps=["me_spouse", "60-69", "FL", "150-300k", "yes", "asap"], tool={}, full=True, done="You're all set"),
    dict(code="fe-fb", first="Tyler", last="Grant", email="tyler.grant@example.com", phone="3055550108", zip="33139",
         taps=["me", "50-59", "FL", "5-10k", "no", "excellent", "asap"], tool={}, full=True, done="You're all set"),
]


def snap(page, name, prefix=""):
    name = prefix + name
    page.wait_for_timeout(450)
    page.locator("#app").scroll_into_view_if_needed()
    page.wait_for_timeout(250)
    page.screenshot(path=str(SHOTS / name), full_page=False)


def run_person(page, p, idx):
    page.goto(f"{BASE}{p['url']}&test=1" if p.get("url") else f"{BASE}/go/{p['code']}?test=1")
    page.wait_for_selector("#steps .step.on, #app .step.on")
    shot = p.get("shots") or ""
    if page.locator("#zipin").count():        # v5 Health Coverage Check: ZIP gate before the questions
        page.fill("#zipin", p["zip"]); page.click("#zipgo")
    if shot:
        page.wait_for_timeout(400); page.screenshot(path=str(SHOTS / (shot + "01_landing_first_tap.png")), full_page=False)
    for i, v in enumerate(p["taps"]):
        # either a tap question or we hit the tool screen
        if page.locator("#calc").count():
            for k, val in p["tool"].items():
                sel = page.locator(f'[data-k="{k}"]')
                if sel.count():
                    if sel.evaluate("e=>e.tagName") == "SELECT":
                        sel.select_option(val)
                    else:
                        sel.fill(val)
            if shot:
                snap(page, shot + "02_tool.png")
            page.click("#calc")
            page.wait_for_selector("#want")
            if shot:
                snap(page, shot + "03_result.png")
            page.click("#want" if p["full"] else "#emailme")
        btn = page.locator(f'.opt[data-v="{v}"]')
        if not btn.count():
            opts = page.locator(".opt")
            avail = [opts.nth(i).get_attribute("data-v") for i in range(opts.count())]
            raise AssertionError(f"persona#{idx} ({p.get('code') or p.get('url')}): tap '{v}' not on screen; options={avail}")
        btn.first.click()
        page.wait_for_timeout(250)
    # tool may come after the taps (answer-driven tools), or the /check result screen waits for a choose
    if page.locator("#calc").count():
        if shot:
            snap(page, shot + "02_tool.png")
        page.click("#calc")
        page.wait_for_selector("#want")
        if shot:
            snap(page, shot + "03_result.png")
        page.click("#want" if p["full"] else "#emailme")
    if page.locator("#want").count():          # /check result screen (no tool screen)
        page.click("#want" if p["full"] else "#emailme")
    page.wait_for_selector("#tocon")
    page.fill("#first_name", p["first"]); page.fill("#last_name", p["last"])
    if p.get("email") and page.locator("#email").count():
        page.fill("#email", p["email"])
    if page.locator("#zip").count():
        page.fill("#zip", p["zip"])
    if p["full"]:
        page.fill("#phone", p["phone"])
    if shot:
        snap(page, shot + "04_contact.png")
    page.click("#tocon")
    page.wait_for_selector("#cbox")
    if shot:
        snap(page, shot + "05_consent.png")
    page.check("#cbox")
    page.click("#submit")
    page.wait_for_selector("text=" + p["done"])   # legacy flows: "You're all set"; /check: "Done" (EN) / "Listo" (ES)
    if shot:
        snap(page, shot + "06_done.png")


def main():
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        for i, p in enumerate(PEOPLE):
            ctx = b.new_context(viewport={"width": 390, "height": 844} if p.get("mobile") else {"width": 1280, "height": 860},
                                device_scale_factor=2 if p.get("mobile") else 1)
            page = ctx.new_page()
            errs = []
            page.on("pageerror", lambda e: errs.append(str(e)))
            run_person(page, p, i)
            if errs:
                print("JS errors:", errs); sys.exit(1)
            ctx.close()
        # desktop landing + admin screenshots
        ctx = b.new_context(viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        page.goto(f"{BASE}/c/mortgage-protection"); page.screenshot(path=str(SHOTS / "07_landing_desktop.png"))
        ADMIN = os.environ.get("ADMIN_TOKEN", "DEVLOCAL")
        page.goto(f"{BASE}/admin?token={ADMIN}"); page.screenshot(path=str(SHOTS / "08_admin_overview.png"), full_page=True)
        lid = sqlite3.connect(DB).execute("SELECT lead_id FROM leads WHERE routing_status='DELIVERED' ORDER BY created_at LIMIT 1").fetchone()[0]
        page.goto(f"{BASE}/admin/lead/{lid}?token={ADMIN}"); page.screenshot(path=str(SHOTS / "09_admin_lead.png"), full_page=True)
        page.goto(f"{BASE}/admin/intel?token={ADMIN}"); page.screenshot(path=str(SHOTS / "10_admin_intel.png"), full_page=False)
        # agent feedback loop: one-click statuses live on the PER-LEAD desk page now
        lid2 = sqlite3.connect(DB).execute("SELECT lead_id FROM leads WHERE buyer_id='demo-agency-a' AND is_test=1 ORDER BY created_at DESC LIMIT 1").fetchone()[0]
        page.goto(f"{BASE}/agent/demo-agency-a-token")
        page.screenshot(path=str(SHOTS / "11_agent_portal.png"), full_page=True)
        page.goto(f"{BASE}/agent/demo-agency-a-token/lead/{lid2}")
        page.locator('button[name=status][value="APPOINTMENT"]').first.click()
        page.wait_for_load_state()
        st = sqlite3.connect(DB).execute("SELECT agent_status FROM leads WHERE lead_id=?", (lid2,)).fetchone()[0]
        assert st == "APPOINTMENT", st
        b.close()
    con = sqlite3.connect(DB)
    rows = con.execute("SELECT first_name, campaign, utm_source, link_code, qual_status, tier, score, contact_scope, routing_status, buyer_id, price, qual_reasons FROM leads ORDER BY created_at").fetchall()
    for r in rows:
        print(r)
    print(con.execute("SELECT stage, COUNT(*) FROM visitors GROUP BY 1").fetchall())
    print(con.execute("SELECT COUNT(*) FROM consents").fetchone(), con.execute("SELECT status, COUNT(*) FROM deliveries GROUP BY 1").fetchall())


if __name__ == "__main__":
    main()
