"""SCOUT: the opportunity-discovery loop for the Florida health lead engine.

Scout (English, and a loanword in Spanish: "escáut"): the one sent ahead to read the terrain and find the route. That is
the job: find overlooked demand, record the route to the people who have it, and move each route one rung up a ladder
that only REAL evidence can climb.

Ladder (never skip a rung, never mark a rung without evidence in the action log):
  identified > verified > route_established > outreach_prepared > outreach_sent > distribution_confirmed >
  content_published > real_visits > voluntary_inquiries > leads_accepted > revenue

Commands (all local and safe: nothing is sent, posted, fetched or spent by this file):
  python tools/scout.py rank                 ranked queue with blockers, urgency and the next action
  python tools/scout.py metrics              real (non-test) counts pulled from data/leads.db
  python tools/scout.py log ID RUNG "note"   append an action to data/scout_log.jsonl (needs a note with evidence)
  python tools/scout.py report               writes out/scout_queue.md
  python tools/scout.py scoreboard           completed vs planned, 8 acquisition numbers; writes out/scout_scoreboard.md
  python tools/scout.py set KEY VALUE        record a hand-copied number (e.g. videos_published 1) in data/scout_manual.json

State: config/scout_routes.json (routes, evidence, blockers, next action). Scores come from config/opportunities.json.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTES = ROOT / "config" / "scout_routes.json"
OPPS = ROOT / "config" / "opportunities.json"
LOG = ROOT / "data" / "scout_log.jsonl"
DB = ROOT / "data" / "leads.db"
LADDER = ["identified", "verified", "route_established", "outreach_prepared", "outreach_sent", "distribution_confirmed",
          "content_published", "real_visits", "voluntary_inquiries", "leads_accepted", "revenue"]
SYSTEM = "Scout"


def load_routes():
    return json.loads(ROUTES.read_text())["routes"]


def opp_scores():
    d = json.loads(OPPS.read_text())
    return {o["id"]: float(o.get("score") or 0) for o in d["opportunities"]}


def log_rungs():
    """Highest rung per route that has a logged action with a note."""
    got = {}
    if LOG.exists():
        for line in LOG.read_text().splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            if e.get("note", "").strip() and e["rung"] in LADDER:
                got.setdefault(e["route"], set()).add(e["rung"])
    return got


def metrics():
    m = {"real_visitors": 0, "real_page_views": 0, "real_clicks": 0, "check_starts": 0, "check_completes": 0,
         "real_leads": 0, "test_events_ignored": 0}
    if not DB.exists():
        return m
    c = sqlite3.connect(DB)
    q = lambda sql: c.execute(sql).fetchone()[0]
    m["real_visitors"] = q("select count(distinct visitor_id) from events where is_test=0")
    m["real_page_views"] = q("select count(*) from events where is_test=0 and event='page_view'")
    m["check_starts"] = q("select count(*) from events where is_test=0 and event='tool_start'")
    m["check_completes"] = q("select count(*) from events where is_test=0 and event='tool_complete'")
    m["real_leads"] = q("select count(*) from leads where is_test=0")
    m["test_events_ignored"] = q("select count(*) from events where is_test=1")
    try:
        m["real_clicks"] = q("select count(*) from clicks")
    except sqlite3.Error:
        pass
    return m


def rung_reached(route, logged):
    have = set(route.get("rungs", [])) | logged.get(route["id"], set())
    reached = None
    for r in LADDER:
        if r in have:
            reached = r
        else:
            break
    return reached


def urgency(route, today=None):
    today = today or date.today()
    dl = route.get("deadline")
    if not dl:
        return 0, None
    days = (date.fromisoformat(dl) - today).days
    if days < 0:
        return -5, days
    return (10 if days <= 30 else 5 if days <= 75 else 0), days


def ranked():
    scores, logged = opp_scores(), log_rungs()
    out = []
    for r in load_routes():
        base = max([scores.get(o, 0) for o in r.get("opps", [])] or [0])
        bonus, days = urgency(r)
        blockers = [b for b in r.get("blockers", []) if not b.get("resolved")]
        hard = [b for b in blockers if b.get("hard")]
        priority = base + bonus - 4 * len(hard)
        out.append(dict(r, base=base, bonus=bonus, days=days, blockers=blockers, hard=hard, priority=round(priority, 1),
                        reached=rung_reached(r, logged)))
    out.sort(key=lambda x: -x["priority"])
    return out


def cmd_rank():
    for i, r in enumerate(ranked(), 1):
        dl = f"{r['days']}d to {r['deadline']}" if r["days"] is not None else "no deadline"
        print(f"{i}. [{r['priority']}] {r['id']}  {r['title']}  (reached: {r['reached']}; {dl})")
        for b in r["blockers"]:
            print(f"     blocker{' (HARD)' if b.get('hard') else ''}: {b['what']}  <- {b.get('who', '?')}")
        print(f"     next: {r['next_action']}")


def cmd_log(argv):
    if len(argv) < 3 or argv[1] not in LADDER or not argv[2].strip():
        sys.exit("usage: log ROUTE_ID RUNG 'note with evidence'  (rung one of: " + ", ".join(LADDER) + ")")
    if argv[0] not in {r["id"] for r in load_routes()}:
        sys.exit("unknown route id")
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), "route": argv[0], "rung": argv[1], "note": argv[2]}) + "\n")
    print("logged")


def cmd_report():
    m = metrics()
    lines = [f"# {SYSTEM} queue ({date.today().isoformat()})", "",
             "REAL numbers only (test traffic excluded): " + ", ".join(f"{k} {v}" for k, v in m.items()), "",
             "| # | Route | Priority | Reached | Hard blockers | Next action |", "|---|---|---|---|---|---|"]
    for i, r in enumerate(ranked(), 1):
        lines.append(f"| {i} | {r['title']} | {r['priority']} | {r['reached']} | {'; '.join(b['what'] for b in r['hard']) or 'none'} | {r['next_action']} |")
    (ROOT / "out" / "scout_queue.md").write_text("\n".join(lines) + "\n")
    print("wrote out/scout_queue.md")

MANUAL = ROOT / "data" / "scout_manual.json"
MANUAL_KEYS = {"videos_produced": "videos rendered and uploaded (fact)", "videos_published": "videos live on IG/TikTok (copy from the platform)",
               "people_reached": "accounts reached, summed from platform Insights (copy by hand; leave unset if not read)",
               "hosted_site_clicks": "clicks logged by the hosted Worker (read from its database; leave unset until read)",
               "revenue_usd": "money actually received from a buyer (copy from the invoice/bank; never estimate)"}


def manual():
    try:
        return json.loads(MANUAL.read_text())
    except (OSError, ValueError):
        return {}


def cmd_set(argv):
    if len(argv) != 2 or argv[0] not in MANUAL_KEYS:
        sys.exit("keys: " + ", ".join(MANUAL_KEYS))
    d = manual()
    d[argv[0]] = float(argv[1]) if "." in argv[1] else int(argv[1])
    d[argv[0] + "_set_at"] = datetime.now().isoformat(timespec="seconds")
    MANUAL.parent.mkdir(exist_ok=True)
    MANUAL.write_text(json.dumps(d, indent=1))
    print("recorded", argv[0], d[argv[0]])


def cmd_scoreboard():
    m, mn, rs, lg = metrics(), manual(), load_routes(), log_rungs()
    def reached(rung):
        return sum(1 for r in rs if LADDER.index(rung) <= LADDER.index(rung_reached(r, lg)))
    leads = {"inquiries": 0, "qualified": 0}
    if DB.exists():
        c = sqlite3.connect(DB)
        leads["inquiries"] = c.execute("select count(*) from leads where is_test=0").fetchone()[0]
        leads["qualified"] = c.execute("select count(*) from leads where is_test=0 and qual_status='QUALIFIED'").fetchone()[0]
    nv = lambda k: mn[k] if k in mn else "not recorded"
    rows = [
        ("Videos published", nv("videos_published") if "videos_published" in mn else 0, f"{mn.get('videos_produced', 0)} produced; 6 packaged (A-F); 0 posted", "manual: platform"),
        ("Distribution routes activated (partner confirmed distribution)", reached("distribution_confirmed"), f"{reached('outreach_prepared')} of {len(rs)} routes have outreach prepared; {reached('outreach_sent')} sent", "ledger"),
        ("People reached", nv("people_reached"), "no platform Insights read yet; no partner has distributed anything", "manual: platform Insights"),
        ("Visits (engine DB, real)", m["real_visitors"], "hosted site not deployed; engine on localhost only", "engine DB"),
        ("Clicks (engine DB, real)", m["real_clicks"], f"hosted-site clicks: {nv('hosted_site_clicks')}", "engine DB + manual"),
        ("Voluntary inquiries", leads["inquiries"], "capture is OFF by design until a named buyer, entity and counsel-approved consent exist", "engine DB"),
        ("Qualified leads", leads["qualified"], "none possible yet", "engine DB"),
        ("Revenue (USD received)", nv("revenue_usd") if "revenue_usd" in mn else 0, "no buyer exists", "manual: invoice"),
    ]
    out = [f"# {SYSTEM} scoreboard ({date.today().isoformat()})", "", "COMPLETED = verified fact today. PLANNED = prepared, not done. Unknown stays 'not recorded'; nothing here is estimated.", "",
           "| Metric | Completed (actual) | Planned / context | Source |", "|---|---|---|---|"]
    out += [f"| {a} | {b} | {c_} | {d} |" for a, b, c_, d in rows]
    out += ["", f"Test events ignored: {m['test_events_ignored']}. Routes by highest rung reached:"]
    out += [f"- {r['id']}: {rung_reached(r, lg)}" for r in rs]
    txt = "\n".join(out) + "\n"
    (ROOT / "out" / "scout_scoreboard.md").write_text(txt)
    print(txt)


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a or a[0] == "rank":
        cmd_rank()
    elif a[0] == "metrics":
        print(json.dumps(metrics(), indent=1))
    elif a[0] == "log":
        cmd_log(a[1:])
    elif a[0] == "report":
        cmd_report()
    elif a[0] == "scoreboard":
        cmd_scoreboard()
    elif a[0] == "set":
        cmd_set(a[1:])
    else:
        sys.exit(__doc__)
