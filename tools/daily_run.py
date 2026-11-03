"""One command for the daily loop (Engine 4). Safe: it never posts, sends or spends anything.
  python tools/daily_run.py [--warn path/to/warn.csv] [--spend path/to/meta_export.csv]
1. WARN watcher: new Florida layoff notices -> DRAFT employer pages (health 60-day + life 31-day CTAs), for review.
2. Spend import: ad-platform CSV -> cost per lead per engine/channel/experiment.
3. Copy lint: every ad and post checked against platform + insurance-advertising rules.
4. Posting pack: refreshed copy + tracked links + flyers (uses BASE_URL).
5. Decision report: SCALE / FIX / KILL / WATCH per engine, channel, category and experiment (REAL data only).
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(argv):
    if "--discover" in argv:
        # ACTIVE lead finding (opt-in: it makes real requests to third-party public pages).
        # Prospects land in data/leads.db and are reviewed at /admin/people; nothing is contacted.
        from engine import discovery, prospector
        from engine.db import connect as _connect
        _con = _connect(ROOT / "data" / "leads.db")
        try:
            prospector.ensure(_con)
            try:
                import sqlite3 as _sq
                _sig = _sq.connect(str(ROOT / "data" / "signals.db"))
                _sig.row_factory = _sq.Row
                print("SIGNALS->PROSPECTS:", prospector.signals_ingest(_con, _sig))
                _sig.close()
            except Exception as _e:               # noqa: BLE001
                print("SIGNALS->PROSPECTS skipped:", type(_e).__name__)
            for _r in discovery.run(_con):
                print(f"DISCOVERY {_r['source']}: fetched={_r['fetched']} new={_r['new']} "
                      f"dupes={_r['duplicates']} errors={_r['errors']}")
        finally:
            _con.close()
    if "--warn" in argv:
        from tools import warn_watch
        print("WARN:", warn_watch.run(argv[argv.index("--warn") + 1]))
    if "--spend" in argv:
        from tools import import_spend
        print("SPEND:", import_spend.run(argv[argv.index("--spend") + 1]))
    lint = subprocess.run([sys.executable, str(ROOT / "tools" / "ad_lint.py")], capture_output=True, text=True)
    print("LINT:", lint.stdout.strip().splitlines()[-1])
    subprocess.run([sys.executable, str(ROOT / "tools" / "distribution_pack.py")], check=True, capture_output=True)
    print("PACK: out/organic_pack/ refreshed")
    from tools import engine_report
    from engine.db import connect
    txt = engine_report.report(connect(ROOT / "data" / "leads.db"))
    (ROOT / "out").mkdir(exist_ok=True)
    (ROOT / "out" / "engine_report.md").write_text(txt)
    print("REPORT: out/engine_report.md")
    # 6. Outstanding tasks: the SAME sale-integrity kernel /admin renders (stranded sales, webhook
    #    retries exhausted, negative real balances, stuck payments, unbilled deliveries, credit
    #    requests) plus overdue prospect-buyer follow-ups from the recorded trail. READ-ONLY: writes
    #    a markdown list for the operator — it never repairs, sends, contacts or charges anything.
    from engine.ops import sale_alerts
    import json as _json
    from engine import buyer_pipeline as _bp
    al = sale_alerts(connect(ROOT / "data" / "leads.db"))
    buyers = _json.loads((ROOT / "config" / "buyers.json").read_text(encoding="utf-8"))
    tasks = [f"- stranded ROUTED sale: {a['lead_id']} -> {a['buyer_id']} ({a['created_at'][:16]})" for a in al["stranded"]]
    tasks += [f"- webhook retries exhausted: {a['lead_id']} x{a['n']}" for a in al["webhook_out"]]
    tasks += [f"- negative REAL balance: {a['buyer_id']}  {a['bal']:.2f} over {a['txns']} txns" for a in al["negative"]]
    tasks += [f"- payment stuck PENDING >24h: {a['buyer_id']} ${a['amount_usd']} ({a['ts'][:16]})" for a in al["stuck_purchases"]]
    tasks += [f"- delivered but unbilled: {a['lead_id']} -> {a['buyer_id']} (${a['price'] or 0:.2f})" for a in al["unbilled"]]
    tasks += [f"- buyer notification not sent (live mode): {a['lead_id']} -> {a['buyer_id']}" for a in al["email_out"]]
    tasks += [f"- credit request pending: {a}" for a in al["credit_requests"]]
    today = __import__("datetime").date.today().isoformat()
    con2 = connect(ROOT / "data" / "leads.db")
    for r in _bp.rows(con2, buyers):
        if r["bucket"] == "OVERDUE":
            tasks.append(f"- prospect follow-up OVERDUE: {r['buyer']['id']} ({r['stage']}) next={r['next_followup']}")
    lines = (["# Outstanding tasks — from recorded state only", "", f"Generated {today} UTC. Full detail:"
              f" /admin (alerts), /admin/buyer-pipeline, /admin/credits.", ""])
    if not tasks:
        lines.append("Nothing outstanding. (Empty really means empty: every list is computed from the "
                     "DB and config, not a cache.)")
    else:
        lines += [t for t in tasks]
    (ROOT / "out" / "ops_alerts.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"TASKS: {len(tasks)} outstanding -> out/ops_alerts.md")
    return 1 if lint.returncode else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
