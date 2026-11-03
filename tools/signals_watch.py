"""SIGNAL WATCH CLI: operator command for the local intent-signal discovery loop.

Everything here is LOCAL and READ-ONLY with respect to people: nothing is sent, posted,
replied to, or distributed. Network access happens ONLY when you pass --network to the
fetch subcommand; everything else is offline (classification, review, reports).

  python tools/signals_watch.py summary                   counts per review status + last fetches
  python tools/signals_watch.py queue [STATUS]            print the review queue (NEW by default)
  python tools/signals_watch.py review ID ACTION 'note'   record a decision (REVIEWED|ACTIONED|IRRELEVANT)
  python tools/signals_watch.py ingest file.json          offline ingestion (see schema below)
  python tools/signals_watch.py fetch --network           POLITE fetch: Reddit + HN public JSON,
                                                          Google News RSS (queries in
                                                          config/sources.json), plus YouTube
                                                          upload feeds when channel ids listed
  python tools/signals_watch.py web --network [URL]       public-page extraction through the
                                                          SAME signal loop: stdlib reader with
                                                          robots.txt compliance (fallback_reader
                                                          mode) or ScrapeGraphAI structured
                                                          extraction when installed with a local
                                                          model. Seeds: config/sources.json
                                                          > webreader; a URL arg runs just that
                                                          page.
  python tools/signals_watch.py prune [--days N]          retention: delete IRRELEVANT older than N
                                                          days (default 90); NEW never auto-deleted
  python tools/signals_watch.py report                    writes out/signals_report.md

Offline ingest file schema (one JSON object): {"signals": [
  {"source": "manual", "community": "some-forum.example", "title": "...",
   "url": "https://...", "snippet": "...", "observed_at": "2026-10-27T10:00:00Z"}]}
  - url: REQUIRED. A durable public permalink, not a screenshot, not a DM.
  - The author field of anything you paste is DISCARDED by design: signals store place + moment,
    never people.

Evidence rule: a signal only rises to the same status the Scout ladder respects when the
reviewer note says WHY (which existing page answers it, which test it should join, or why it
is irrelevant). A bare status flip is not a review.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import signals as sig          # noqa: E402
from engine import platforms as plat       # noqa: E402
from engine import web_extract as webx     # noqa: E402

DB = Path(__file__).resolve().parents[1] / "data" / "signals.db"


def _con():
    return sig.connect(DB)


def cmd_summary(con) -> None:
    c = sig.counts(con)
    last = con.execute("SELECT ts, source, fetched, accepted, note FROM fetch_log ORDER BY id DESC LIMIT 5").fetchall()
    print(json.dumps(c, indent=1))
    for r in last:
        print(f"  fetch {r['ts']} {r['source']}: fetched={r['fetched']} accepted={r['accepted']} {r['note'][:80]}")


def cmd_queue(con, status="NEW") -> None:
    rows = sig.queue(con, status, 100)
    print(f"{status}: {len(rows)}")
    for r in rows:
        print(f"\n[{r['intent']}] {r['topic']} ({r['lang']}) {r['community']}  {r['observed_at']}")
        print(f"  {r['title']}")
        print(f"  {r['url']}")
        if r["snippet"]:
            print(f"  ...{r['snippet'][:180]}")
        print(f"  angle: {r['angle']}  opps: {r['opps']}")


def cmd_review(con, argv: list[str]) -> None:
    if len(argv) < 3 or argv[0] not in ("REVIEWED", "ACTIONED", "IRRELEVANT") or not argv[2].strip():
        sys.exit('usage: review ID ACTION "note with evidence"  (ACTION: REVIEWED | ACTIONED | IRRELEVANT)')
    try:
        sid = int(argv[1])
    except ValueError:
        sys.exit("signal id must be a number")
    row = con.execute("SELECT id FROM signals WHERE id=?", (sid,)).fetchone()
    if not row:
        sys.exit(f"no signal #{sid}")
    sig.review(con, sid, argv[0], argv[2])
    print("recorded")


def cmd_ingest(con, path: Path) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        sys.exit(f"can't read {path}: {e}")
    rows = data.get("signals") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        sys.exit("expect {\"signals\": [...]} or a bare list")
    res = sig.ingest(con, rows, data.get("source", "manual") if isinstance(data, dict) else "manual")
    print(f"added {res['added']}, skipped {res['skipped']} (duplicates / missing URLs)")


def cmd_fetch(con) -> None:
    """Delegate to tools.collect.run_all: ONE sweep implementation shared by this CLI,
    the collect CLI, and the scheduler (tools/collect.py run --network)."""
    from tools.collect import run_all
    rows = run_all(con)
    for r in rows:
        if r.get("note"):
            print(f"{r['source']}: {r.get('status', '')} fetched={r.get('fetched', 0)} "
                  f"added={r.get('added', 0)}  {r['note'][:110]}")
        else:
            print(f"{r['source']}: fetched={r.get('fetched', 0)} accepted={r.get('accepted', r.get('added', 0))}")
    cmd_summary(con)


def cmd_web(con, url_arg: str) -> None:
    cfg_path = ROOT / "config" / "sources.json"
    wr = {}
    if cfg_path.exists():
        try:
            wr = json.loads(cfg_path.read_text(encoding="utf-8")).get("webreader", {})
        except (OSError, ValueError):
            pass
    if url_arg:
        r = webx.collect_page(con, url_arg)
        print(url_arg, "->", r)
    for seed in (wr.get("seeds") or []):
        u = (seed.get("url") or "").strip()
        if not u:
            continue
        r = webx.collect_site(con, u, max_pages=int(wr.get("max_pages_per_seed") or 5))
        print(u, "->", json.dumps(r)[:220])
    st = webx.sga_status()
    print(f"webreader done (extract mode: {st['effective_mode']})")


def cmd_report(con) -> None:
    c = sig.counts(con)
    lines = [f"# Signal watch report ({str(DB)})", "",
             f"Totals: " + ", ".join(f"{k} {v}" for k, v in c.items()), "",
             "SIGNALS ARE NOT LEADS. Each is a public post that may indicate the moment to answer",
             "publicly (content page) or to add a channel; no author is identified and nobody is",
             "contacted from this report. Actioned signals MUST name the content/test they contributed to.", "",
             "| # | intent | topic | lang | where | post | decision |", "|---|---|---|---|---|---|---|"]
    for r in sig.queue(con, "NEW", 40):
        t = (r["title"] or "")[:90].replace("|", "\\|")
        lines.append(f"| {r['id']} | {r['intent']} | {r['topic']} | {r['lang']} | {r['community']} | [{t}]({r['url']}) | - |")
    for status in ("ACTIONED", "IRRELEVANT"):
        for r in sig.queue(con, status, 40):
            t = (r["title"] or "")[:70].replace("|", "\\|")
            lines.append(f"| {r['id']} | {r['intent']} | {r['topic']} | {r['lang']} | {r['community']} | [{t}]({r['url']}) | {status} |")
    out = ROOT / "out" / "signals_report.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out}")


def main() -> None:
    a = sys.argv[1:]
    con = _con()
    try:
        if not a or a[0] == "summary":
            cmd_summary(con)
        elif a[0] == "queue":
            cmd_queue(con, a[1] if len(a) > 1 else "NEW")
        elif a[0] == "review":
            cmd_review(con, a[1:])
        elif a[0] == "ingest":
            if len(a) != 2 or not Path(a[1]).exists():
                sys.exit("usage: ingest file.json")
            cmd_ingest(con, Path(a[1]))
        elif a[0] == "fetch":
            if "--network" not in a:
                sys.exit("refused: fetching is a REAL network action; pass --network explicitly")
            cmd_fetch(con)
        elif a[0] == "prune":
            try:
                days = int(a[a.index("--days") + 1]) if "--days" in a else 90
            except (IndexError, ValueError):
                sys.exit("usage: prune [--days N]")
            n = sig.prune(con, days)
            print(f"deleted {n} irrelevant signal(s) older than {days} days")
        elif a[0] == "web":
            if "--network" not in a:
                sys.exit("refused: web extraction is a REAL network action; pass --network explicitly")
            url_arg = next((x for x in a[1:] if not x.startswith("--")), "")
            cmd_web(con, url_arg)
            cmd_summary(con)
        elif a[0] == "report":
            cmd_report(con)
        else:
            sys.exit(__doc__)
    finally:
        con.close()


if __name__ == "__main__":
    main()
