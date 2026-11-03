"""Engines E2E (TEST DATA ONLY): three acquisition engines feed one system, in a real browser.
1. ENGINE 1 organic: Reddit placement link -> hub page -> Check -> consented phone lead -> channel social_community / engine ORGANIC
2. ENGINE 2 paid: the exact Meta ad URL from config/meta_campaigns.json -> Spanish page -> Check -> lead credited to X022 / paid_meta
3. ENGINE 3 sauce: a WARN employer layoff page -> LIFE CTA (31-day group-life conversion) -> life estimator -> term_life lead (category life)
Then the decision report reads all of it (as TEST, so it never decides anything). Run against a fresh DB on localhost:8000."""
import json
import sqlite3
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

B = "http://localhost:8000"
ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "leads.db"
SH = Path(__file__).parent / "screens_engines"
SH.mkdir(exist_ok=True)
fails = []


def check(c, msg):
    print(("PASS " if c else "FAIL ") + msg)
    if not c:
        fails.append(msg)


def health(p, picks, last, phone, zip_="33012"):
    p.wait_for_selector("#steps .step.on")
    for _ in range(14):
        if p.locator("#want").count():
            break
        if p.locator("#zipin").count():
            p.fill("#zipin", zip_); p.click("#zipgo"); p.wait_for_timeout(120); continue
        vals = [p.locator(".opt").nth(i).get_attribute("data-v") for i in range(p.locator(".opt").count())]
        p.click(f'.opt[data-v="{next(v for v in picks if v in vals)}"]'); p.wait_for_timeout(100)
    check(not p.locator("#emailme").is_visible(), f"{last}: no 'email me' option (we don't contact consumers)")
    p.click("#want"); p.wait_for_selector("#phone")
    p.fill("#first_name", "Eng"); p.fill("#last_name", last); p.fill("#phone", phone); p.click("#tocon")
    p.wait_for_selector("#cbox"); p.check("#cbox"); p.click("#submit")
    p.wait_for_selector('.result .lbl:text-matches("^(Done|Listo)$")')


def life(p, last, phone):
    p.wait_for_selector("#steps .step.on")
    taps = ["me_kids", "30-39", "FL", "500k-1m", "no", "asap"]
    for _ in range(20):
        if p.locator("#tocon").count():
            break
        if p.locator("#calc").count():
            for k, val in {"income": "85000", "kids": "2"}.items():
                if p.locator(f'[data-k="{k}"]').count():
                    p.locator(f'[data-k="{k}"]').fill(val)
            p.click("#calc"); p.wait_for_timeout(200); continue
        if p.locator("#want").count():
            check(not p.locator("#emailme").is_visible(), "life flow: no 'email me' option")
            p.screenshot(path=str(SH / "05_life_result.png"), full_page=True)
            p.click("#want"); p.wait_for_timeout(200); continue
        vals = [p.locator(".opt").nth(i).get_attribute("data-v") for i in range(p.locator(".opt").count())]
        p.click(f'.opt[data-v="{next(v for v in taps if v in vals)}"]'); p.wait_for_timeout(150)
    for k, v in (("first_name", "Eng"), ("last_name", last), ("phone", phone), ("zip", "33166"), ("email", "eng.life@example.com")):
        if p.locator(f"#{k}").count():
            p.fill(f"#{k}", v)
    p.click("#tocon"); p.wait_for_selector("#cbox"); p.check("#cbox"); p.click("#submit")
    p.wait_for_selector("text=You're all set")


def main():
    meta = json.loads((ROOT / "config" / "meta_campaigns.json").read_text())
    ad_url = meta["campaigns"][0]["ads"][0]["url"]
    ev = next(e for e in json.loads((ROOT / "config" / "layoffs.json").read_text())["events"] if e.get("status") == "ready")
    with sync_playwright() as pw:
        br = pw.chromium.launch(); mob = dict(viewport={"width": 390, "height": 844}, device_scale_factor=2)
        ctx = br.new_context(**mob); p = ctx.new_page()                       # 1. organic: Reddit placement
        p.goto(B + "/go/rdhealthinsexit?test=1"); p.wait_for_load_state()
        p.screenshot(path=str(SH / "01_reddit_landing.png"), full_page=True)
        p.click(".ctabox a.btn"); health(p, ["cigna", "ending", "marketplace", "1", "45-54", "150-250"], "Reddit", "3055550161", "33301"); ctx.close()
        ctx = br.new_context(**mob); p = ctx.new_page()                       # 2. paid: Meta ad URL
        p.goto(B + ad_url + "&test=1", referer="https://l.facebook.com/"); p.wait_for_load_state()
        p.screenshot(path=str(SH / "02_meta_landing_es.png"), full_page=True)
        p.click(".ctabox a.btn"); health(p, ["molina", "ending", "direct", "2", "35-44", "100-150"], "MetaAd", "3055550162"); ctx.close()
        ctx = br.new_context(**mob); p = ctx.new_page()                       # 3. sauce: layoff page -> life
        p.goto(B + f"/layoffs/{ev['slug']}?test=1"); p.wait_for_load_state()
        p.screenshot(path=str(SH / "03_layoff_page_two_ctas.png"), full_page=True)
        p.click("a[href^='/c/work-life-insurance-ending']"); p.wait_for_load_state()
        p.screenshot(path=str(SH / "04_life_estimator.png"), full_page=True)
        life(p, "LayoffLife", "3055550163"); ctx.close(); br.close()
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    L = {r["last_name"]: dict(r) for r in con.execute("SELECT * FROM leads WHERE first_name='Eng'")}
    r, mt, lf = L.get("Reddit") or {}, L.get("MetaAd") or {}, L.get("LayoffLife") or {}
    check((r.get("utm_source"), r.get("utm_medium"), r.get("utm_content"), r.get("experiment_id")) == ("reddit", "community", "rd-healthins-exit", "X017"),
          f"Reddit lead: reddit/community/rd-healthins-exit/X017: {(r.get('utm_source'), r.get('utm_medium'), r.get('utm_content'), r.get('experiment_id'))}")
    check((mt.get("utm_source"), mt.get("utm_medium"), mt.get("experiment_id"), mt.get("utm_content")) == ("facebook", "paid_social", "X022", "x022-a"),
          f"Meta lead: facebook/paid_social/X022/x022-a: {(mt.get('utm_source'), mt.get('utm_medium'), mt.get('experiment_id'), mt.get('utm_content'))}")
    check("plan_ending_offexchange" in (mt.get("flags") or ""), "Meta lead answered 'bought direct' -> off-exchange flag")
    check(lf.get("product") == "term_life" and lf.get("campaign") == "work-life-ends" and lf.get("lead_type") == "PHONE_LEAD", f"layoff page produced a LIFE lead: {lf.get('product'), lf.get('campaign')}")
    check(lf.get("experiment_id") == "X023" or (lf.get("entry_page") or "").startswith("/layoffs/"), f"life lead traces back to the layoff page: {lf.get('experiment_id'), lf.get('entry_page')}")
    check(all(l["is_test"] == 1 and l["phone_source"] == "CONSUMER_SUBMITTED" for l in L.values()) and len(L) == 3, "3 TEST leads, consumer-submitted phones")
    check(con.execute("SELECT COUNT(*) FROM events WHERE is_test=0").fetchone()[0] == 0, "zero REAL events")
    sys.path.insert(0, str(ROOT))
    from engine import funnel
    eng = {x["k"]: x["consented_phone_leads"] for x in funnel.by(con, "engine", "only")}
    cat = {x["k"]: x["consented_phone_leads"] for x in funnel.by(con, "category", "only")}
    check(eng.get("ORGANIC", 0) >= 1 and eng.get("PAID") == 1, f"leads by engine: {eng}")
    check(cat.get("health") == 2 and cat.get("life") == 1, f"leads by category: {cat}")
    print("\nALL PASS" if not fails else f"\n{len(fails)} FAILED")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
