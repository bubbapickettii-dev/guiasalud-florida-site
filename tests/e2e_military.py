"""Separating-service-member path (TEST DATA ONLY): tracked partner link -> life guide -> life estimator -> consented lead.
Asserts attribution = X026 / partner / mil-macdill / life. Run against a fresh DB on localhost:8000."""
import sqlite3
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
import e2e_engines as E  # noqa: E402

B, DB = E.B, E.DB

if __name__ == "__main__":
    with sync_playwright() as pw:
        br = pw.chromium.launch(); ctx = br.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2); p = ctx.new_page()
        p.goto(B + "/learn/leaving-military-life-insurance-florida?utm_source=military_transition&utm_medium=partner&utm_campaign=X026&utm_content=mil-macdill&x=X026&test=1")
        p.wait_for_load_state()
        E.check("120 days" in p.content() and "240 days" in p.content(), "guide shows both dates")
        E.check("not affiliated" in p.content().lower(), "guide says we are not affiliated with VA/DoD/TRICARE")
        p.screenshot(path=str(E.SH / "06_military_guide.png"), full_page=True)
        p.click(".ctabox a.btn"); p.wait_for_load_state()
        E.life(p, "MilLife", "3055550171")
        ctx.close(); br.close()
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    l = con.execute("SELECT * FROM leads WHERE last_name='MilLife'").fetchone()
    l = dict(l) if l else {}
    E.check((l.get("utm_source"), l.get("utm_medium"), l.get("utm_content"), l.get("experiment_id")) == ("military_transition", "partner", "mil-macdill", "X026"),
            f"lead attribution: {(l.get('utm_source'), l.get('utm_medium'), l.get('utm_content'), l.get('experiment_id'))}")
    E.check(l.get("product") == "term_life" and l.get("campaign") == "military-life-ends" and l.get("is_test") == 1, f"life lead, TEST: {l.get('product'), l.get('campaign')}")
    sys.path.insert(0, str(E.ROOT))
    from engine import funnel
    rows = {x["k"]: x["consented_phone_leads"] for x in funnel.by(con, "channel", "only")}
    cat = {x["k"]: x["consented_phone_leads"] for x in funnel.by(con, "category", "only")}
    E.check(rows.get("partner") == 1 and cat.get("life") == 1, f"funnel: channel {rows}, category {cat}")
    print("FAILED" if E.fails else "ALL PASS", len(E.fails))
