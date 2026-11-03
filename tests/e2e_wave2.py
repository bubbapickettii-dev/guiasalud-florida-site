"""Wave-2 E2E (TEST DATA ONLY): the opportunity tests work end to end in a real browser.
- Google-referred reader of the Spanish Molina page -> 'plan ending' Check -> phone lead credited to X016 / google / organic
- ChatGPT-referred reader of the Florida Blue page -> 'price going up' Check -> lead credited to X017 / ai_answer
- Reader of an employer layoff page -> 60-day Check -> lead credited to X018 with that page as entry
- WhatsApp share button -> friend opens the shared link -> credited to X016 via whatsapp/share
- Late-season visitor on the 'still open?' page -> Check -> credited to X021 (X020 merged)
Run against a fresh DB with the app on localhost:8000. Screenshots -> tests/screens_wave2/"""
import sqlite3
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

B = "http://localhost:8000"
DB = Path(__file__).resolve().parents[1] / "data" / "leads.db"
SH = Path(__file__).parent / "screens_wave2"
SH.mkdir(exist_ok=True)
fails = []


def check(c, msg):
    print(("PASS " if c else "FAIL ") + msg)
    if not c:
        fails.append(msg)


def answer(p, picks, zip_="33012"):
    p.wait_for_selector("#steps .step.on")
    for _ in range(14):
        if p.locator("#want").count():
            return True
        if p.locator("#zipin").count():
            p.fill("#zipin", zip_); p.click("#zipgo"); p.wait_for_timeout(120); continue
        vals = [p.locator(".opt").nth(i).get_attribute("data-v") for i in range(p.locator(".opt").count())]
        p.click(f'.opt[data-v="{next(v for v in picks if v in vals)}"]'); p.wait_for_timeout(100)
    return False


def submit(p, last, phone):
    p.click("#want"); p.wait_for_selector("#phone")
    p.fill("#first_name", "Wave"); p.fill("#last_name", last); p.fill("#phone", phone); p.click("#tocon")
    p.wait_for_selector("#cbox"); p.check("#cbox"); p.click("#submit")
    p.wait_for_selector('.result .lbl:text-matches("^(Done|Listo)$")')


def main():
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        mob = dict(viewport={"width": 390, "height": 844}, device_scale_factor=2)
        # 1. Google -> Spanish Molina page -> plan_change
        ctx = br.new_context(**mob); p = ctx.new_page()
        p.goto(B + "/aprende/molina-se-va-de-florida-2027?test=1", referer="https://www.google.com/")
        p.screenshot(path=str(SH / "01_molina_es_page.png"), full_page=True)
        p.click(".ctabox a.btn")
        ok = answer(p, ["molina", "ending", "marketplace", "3", "35-44", "150-250"])
        p.screenshot(path=str(SH / "02_plan_change_result_es.png"), full_page=True)
        check(ok and "termina el 31 de diciembre" in p.content() and "otra compañía" in p.content(), "ES plan-ending result: Dec 31 end, auto-move to another company, Dec 15 deadline")
        submit(p, "MolinaGoogle", "3055550151"); ctx.close()
        # 2. ChatGPT -> Florida Blue page -> price_up
        ctx = br.new_context(**mob); p = ctx.new_page()
        p.goto(B + "/learn/florida-blue-2027-renewal-increase?test=1", referer="https://chatgpt.com/")
        p.click(".ctabox a.btn")
        ok = answer(p, ["florida_blue", "price_up", "2", "55-64", "250-400"], zip_="33401")
        p.screenshot(path=str(SH / "03_plan_change_result_en_renewal.png"), full_page=True)
        check(ok and "renewed at the new price" in p.content(), "EN renewal result explains auto-renewal at the new price")
        submit(p, "FlBlueAI", "3055550152"); ctx.close()
        # 3. Layoff page -> 60-day Check
        ctx = br.new_context(**mob); p = ctx.new_page()
        p.goto(B + "/layoffs/assistrx-orlando-2026?test=1&today=2026-12-01")
        p.screenshot(path=str(SH / "04_layoff_page.png"), full_page=True)
        check("window is likely open" in p.content(), "layoff page computes the open 60-day window")
        p.click(".ctabox a.btn")
        ok = answer(p, ["soon", "recent", "job", "1", "26-34", "150-250"], zip_="32801")
        submit(p, "AssistRX", "3055550153"); ctx.close()
        # 4. WhatsApp share -> friend
        ctx = br.new_context(**mob); p = ctx.new_page()
        p.goto(B + "/go/x016?c=ig-story&test=1"); p.wait_for_load_state()
        p.screenshot(path=str(SH / "05_es_hub_with_share.png"), full_page=True)
        href = p.get_attribute("#wa", "href")
        check(href and "wa.me" in href and "x%3DX016" in href and "utm_source%3Dwhatsapp" in href, "WhatsApp share link carries X016 + whatsapp/share tags")
        p.click("#wa"); p.wait_for_timeout(300); ctx.close()
        import urllib.parse as U
        shared = U.unquote(href.split("text=")[1]).split(" ")[-1]
        ctx = br.new_context(**mob); p = ctx.new_page()
        p.goto(shared.replace("http://localhost:8000", B) + "&test=1"); p.click(".ctabox a.btn")
        ok = answer(p, ["cigna", "ending", "direct", "1", "45-54", "100-150"])
        check(ok and "nadie te pasa a otro plan" in p.content(), "off-exchange (bought direct) result: no automatic move, plan just ends")
        submit(p, "WhatsAppFriend", "3055550154"); ctx.close()
        # 5. Late-season 'still open' page
        ctx = br.new_context(**mob); p = ctx.new_page()
        p.goto(B + "/inscripcion-abierta-2027?test=1&today=2026-12-20", referer="https://www.bing.com/")
        p.screenshot(path=str(SH / "06_still_open_es.png"), full_page=True)
        check("Sigue abierta hasta el 15 de enero" in p.content(), "late-season page answers 'still open until Jan 15'")
        p.click(".ctabox a.btn"); ok = answer(p, ["individual", "1", "26-34", "100-150", "none", "asap"])
        submit(p, "LateOEP", "3055550155"); ctx.close()
        ctx = br.new_context(viewport={"width": 1360, "height": 900}); p = ctx.new_page()
        p.goto(B + "/admin/opportunities?token=demo"); p.screenshot(path=str(SH / "10_admin_opportunities.png"), full_page=True)
        p.goto(B + "/admin/lab/X016?token=demo&test=only"); p.screenshot(path=str(SH / "11_x016_detail.png"), full_page=True)
        ctx.close(); br.close()

    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    L = {r["last_name"]: dict(r) for r in con.execute("SELECT * FROM leads WHERE first_name='Wave'")}
    exp = {"MolinaGoogle": ("X016", "google", "organic", "/aprende/molina-se-va-de-florida-2027"),
           "FlBlueAI": ("X017", "chatgpt", "ai_answer", "/learn/florida-blue-2027-renewal-increase"),
           "AssistRX": ("X018", None, None, "/layoffs/assistrx-orlando-2026"),
           "WhatsAppFriend": ("X016", "whatsapp", "share", "/aprende/cambios-seguro-medico-florida-2027"),
           "LateOEP": ("X021", "bing", "organic", "/inscripcion-abierta-2027")}
    for k, (x, src, med, entry) in exp.items():
        l = L.get(k) or {}
        got = (l.get("experiment_id"), l.get("utm_source"), l.get("utm_medium"), l.get("entry_page"))
        check(got == (x, src, med, entry), f"{k} attributed {x}/{src}/{med}/{entry}: got {got}")
    m = L.get("MolinaGoogle") or {}
    check(m.get("need_path") == "plan_change" and m.get("urgency") == "HIGH" and "plan_ending" in (m.get("flags") or ""), "plan-ending lead: need plan_change, urgency HIGH, flag plan_ending")
    check((L.get("FlBlueAI") or {}).get("urgency") == "MEDIUM", "renewal lead urgency MEDIUM")
    w = L.get("WhatsAppFriend") or {}
    check("plan_ending_offexchange" in (w.get("flags") or "") and w.get("urgency") == "HIGH", "off-exchange lead flagged plan_ending_offexchange, urgency HIGH")
    import json as _j
    check(_j.loads((L.get("FlBlueAI") or {}).get("answers") or "{}").get("bought_where") is None, "renewal-only path never asks where the plan was bought")
    check(all(l["is_test"] == 1 and l["phone_source"] == "CONSUMER_SUBMITTED" and l["lead_type"] == "PHONE_LEAD" for l in L.values()), "all TEST, consumer-submitted phone leads")
    check(con.execute("SELECT COUNT(*) FROM events WHERE is_test=0").fetchone()[0] == 0, "zero REAL events")
    check(con.execute("SELECT COUNT(*) FROM events WHERE event='share_click'").fetchone()[0] == 1, "share click recorded")
    print("\nALL PASS" if not fails else f"\n{len(fails)} FAILED")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
