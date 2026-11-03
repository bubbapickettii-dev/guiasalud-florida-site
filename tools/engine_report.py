"""The decision report: which engines, channels and experiments to SCALE, FIX, KILL or keep WATCHing.

REAL traffic only (test data never decides anything). Rules live in config/engines.json -> decision_rules.

  python tools/engine_report.py            -> prints the report and writes out/engine_report.md

Decision logic (per row):
  - not enough data            -> WATCH (says what's missing)
  - paid: spend >= kill multiple x target CPL and 0 consented leads -> KILL
  - paid: CPL <= target, >= N accepted leads, acceptance >= min      -> SCALE
  - organic/partner: >= min visits and 0 leads                        -> FIX OFFER (page/CTA isn't converting)
  - any: acceptance below min after >= 5 delivered                    -> FIX QUALITY (agents return them)
  - organic/partner: >= N leads and acceptance >= min                 -> SCALE (make more of this)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from engine import funnel  # noqa: E402
from engine.db import connect  # noqa: E402

R = json.loads((ROOT / "config" / "engines.json").read_text())["decision_rules"]


def decide(r: dict, paid: bool, category: str = "health") -> tuple[str, str]:
    tcpl = R["target_cpl"].get(category, 25)
    leads, acc, dl, spend, visits = r["consented_phone_leads"], r["accepted"], r["delivered"], r.get("spend") or 0, r["visits"]
    accp = (100 * acc / dl) if dl else None
    if dl >= 5 and accp is not None and accp < R["min_acceptance_rate_pct"]:
        return "FIX QUALITY", f"agents accept only {accp:.0f}% of delivered leads"
    if paid:
        if spend >= R["paid_kill_spend_multiple"] * tcpl and leads == 0:
            return "KILL", f"${spend:.0f} spent, 0 consented leads"
        if leads and spend / leads <= tcpl and acc >= R["paid_scale_min_accepted"] and (accp or 0) >= R["min_acceptance_rate_pct"]:
            return "SCALE", f"CPL ${spend / leads:.2f} <= ${tcpl} target, {acc} accepted"
        return "WATCH", f"${spend:.0f} spent, {leads} leads" + (f", CPL ${spend / leads:.2f}" if leads else "")
    if visits >= R["organic_min_visits_to_judge"] and leads == 0:
        return "FIX OFFER", f"{visits} visits, 0 leads"
    if leads >= R["organic_scale_min_leads"] and (accp is None or accp >= R["min_acceptance_rate_pct"]):
        return "SCALE", f"{leads} leads" + (f", {accp:.0f}% accepted" if accp is not None else "")
    return "WATCH", f"{visits} visits, {leads} leads (needs {R['organic_min_visits_to_judge']} visits to judge)"


def report(con) -> str:
    L = ["# Engine decision report (REAL traffic only)", ""]
    for dim, title in (("engine", "By engine"), ("channel", "By channel"), ("category", "By category"), ("experiment", "By experiment")):
        rows = funnel.by(con, dim, "real")
        L += [f"## {title}", "", "| | Visits | Check done | Leads | Delivered | Accepted | Returned | Dupes | Spend | CPL | Revenue | Decision |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        if not rows:
            L.append("| (no real traffic yet) | | | | | | | | | | | UNTESTED |")
        for r in rows:
            paid = (dim == "engine" and r["k"] == "PAID") or (dim == "channel" and str(r["k"]).startswith("paid")) or \
                   (dim == "experiment" and (r.get("spend") or 0) > 0)
            cat = r["k"] if dim == "category" and r["k"] in ("health", "life") else "health"
            dec, why = decide(r, paid, cat)
            L.append(f"| {r['k']} | {r['visits']} | {r['check_completions']} | {r['consented_phone_leads']} | {r['delivered']} | {r['accepted']} | "
                     f"{r['returned']} | {r['duplicates']} | ${r.get('spend') or 0:.0f} | {('$' + format(r['cost_per_lead'], '.2f')) if r['cost_per_lead'] else '-'} | "
                     f"${r['revenue']:.0f} | **{dec}**: {why} |")
        L.append("")
    L += queue_section(con) + paid_gate(con)
    return "\n".join(L)


def queue_section(con) -> list[str]:
    from engine import ops
    hourly = R.get("hourly_value_usd", 20)
    rows = [r for r in ops.board(con, ROOT / "config", "", 0) if r["live"] or r["minutes"] or r["leads"]]
    L = ["## Organic queue (actions actually taken; REAL results)", "",
         "| Item | Status | Effort | Cost | Visits | Leads | Accepted | Effort cost / lead |", "|---|---|---|---|---|---|---|---|"]
    if not rows:
        L.append("| (nothing posted or sent yet) | | | | | | | |")
    for r in rows:
        c = r["minutes"] / 60 * hourly + r["cost_usd"]
        L.append(f"| {r['id']} | {r['status']} | {r['minutes']} min | ${r['cost_usd']:.0f} | {r['visits']} | {r['leads']} | {r['accepted']} | "
                 f"{('$' + format(c / r['leads'], '.2f')) if r['leads'] else '-'} |")
    return L + [""]


def paid_gate(con) -> list[str]:
    """What a paid test must beat, computed from REAL organic results and what agents pay."""
    meta = json.loads((ROOT / "config" / "meta_campaigns.json").read_text())["economics"]
    rows = {r["k"]: r for r in funnel.by(con, "engine", "real")}
    org = [rows[k] for k in ("ORGANIC", "PARTNER") if k in rows]
    leads = sum(r["consented_phone_leads"] for r in org)
    dl = sum(r["delivered"] for r in org)
    acc = sum(r["accepted"] for r in org)
    from engine import ops
    hourly = R.get("hourly_value_usd", 20)
    eff = sum(r["minutes"] / 60 * hourly + r["cost_usd"] for r in ops.board(con, ROOT / "config", "", 0))
    accp = acc / dl if dl else None
    L = ["## Paid gate (Meta stays off until this says GO and Bubba approves)", ""]
    for cat, price in meta["assumed_price_per_lead_usd"].items():
        a = accp if accp is not None else meta["assumed_acceptance_rate"]
        L.append(f"- {cat}: agents pay ${price}/lead (assumed until real prices are in config). Paid CPL must be <= ${price * a * 0.7:.2f} "
                 f"(price x acceptance {a:.0%}{' measured' if accp is not None else ' ASSUMED'} x 0.7 for margin).")
    L.append(f"- Organic baseline: {leads} real leads, {dl} delivered, {acc} accepted"
             + (f", effort cost per lead ${eff / leads:.2f}" if leads else ", no baseline yet") + ".")
    ok = leads >= 10 and dl >= 10 and accp is not None and accp >= R["min_acceptance_rate_pct"] / 100
    L.append(f"- Gate: {'GO - a small paid test can be proposed' if ok else 'NOT YET'} (needs >= 10 real organic leads delivered and acceptance >= "
             f"{R['min_acceptance_rate_pct']}% so we know lead quality before buying traffic).")
    return L + [""]


if __name__ == "__main__":
    con = connect(sys.argv[1] if len(sys.argv) > 1 else ROOT / "data" / "leads.db")
    txt = report(con)
    (ROOT / "out").mkdir(exist_ok=True)
    (ROOT / "out" / "engine_report.md").write_text(txt)
    print(txt)
