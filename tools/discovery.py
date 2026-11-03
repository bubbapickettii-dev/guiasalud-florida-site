"""PUBLIC PROSPECT DISCOVERY CLI — the automated lead-finding loop.

  python tools/discovery.py run --network                 one sweep over every configured source
  python tools/discovery.py run --network --sources citydata --max 6
  python tools/discovery.py run --network --sources reddit_archive_search \
         --queries "need health insurance|lost my health insurance|no puedo pagar seguro"
  python tools/discovery.py report                        statuses + measured yield per adapter
  python tools/discovery.py list --status CONTACT_PERMISSION_REQUIRED
  python tools/discovery.py inspect --id 42 --save        open the original post, store the excerpt
  python tools/discovery.py verify --id 42 --note "…"     operator: real person, current need
  python tools/discovery.py qualify --id 42 --note "…"    operator: may be offered the /check link
  python tools/discovery.py queue                         ACTIVE/STRONG consumers, contact optional
  python tools/discovery.py kit --id 42                   ready-to-post public reply (never sent)
  python tools/discovery.py enrich --live [--ids 303,324]  identity + contact resolution for the queue
  python tools/discovery.py identity                      what identity resolution has produced so far
  python tools/discovery.py status                        the ladder: prospects -> kits -> visits -> leads
  python tools/discovery.py regrade [--dry-run]           re-grade the backlog, collapse duplicates
  python tools/discovery.py issue --id 42                 create the tracked /check link for one prospect
  python tools/discovery.py attribute --lead L2026...     link a consented lead back to its prospect
  python tools/discovery.py migrate                       move legacy prospect_people.db rows in

Why `--network` is required: a sweep is a REAL network action against third-party sites. It reads
public pages only, honours robots.txt, paces requests, and never logs in, replies, DMs or emails.
Findings land in data/leads.db (table public_prospects) with evidence and status; nothing becomes a
saleable lead until that person submits /check themselves.

Cron (an example, not installed):
  0 */6 * * *  cd /srv/leadengine && python tools/discovery.py run --network >> out/discovery.log 2>&1
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import discovery, identity, prospector   # noqa: E402

DB = Path(os.environ.get("LEADENGINE_DB", ROOT / "data" / "leads.db"))
PEOPLE_DB = Path(os.environ.get("PEOPLE_DB", ROOT / "data" / "prospect_people.db"))


def _con():
    return prospector.connect(DB)


def _base_url() -> str:
    """The origin every kit link is built on. BASE_URL wins, exactly like app.py: that is the single
    switch flipped at go-live, and a kit built from config/site.json after the switch would hand the
    operator a localhost URL to paste into a public thread - a dead link nobody can diagnose."""
    env = (os.environ.get("BASE_URL") or "").rstrip("/")
    if env:
        return env
    return (json.loads((ROOT / "config" / "site.json").read_text(encoding="utf-8")).get("base_url") or "").rstrip("/")


def cmd_run(con, args) -> int:
    if "--network" not in args:
        print("refused: reading third-party sites is a REAL network action; pass --network explicitly")
        return 2
    sources = tuple(discovery.DEFAULT_SOURCES)
    if "--sources" in args:
        picked = tuple(x.strip() for x in args[args.index("--sources") + 1].split(",") if x.strip())
        sources = tuple(discovery.DEFAULT_SOURCES) if picked == ("all",) else picked
    mx = None
    if "--max" in args:
        try:
            mx = int(args[args.index("--max") + 1])
        except (IndexError, ValueError):
            mx = None
    cfg = discovery.load_cfg()
    if "--queries" in args:
        # operator-supplied phrase pack, pipe-separated: --queries "need health insurance|no puedo pagar seguro"
        qs = [q.strip() for q in args[args.index("--queries") + 1].split("|") if q.strip()]
        rc = dict(cfg.get("reddit_archive") or {})
        rc["search_queries"] = qs
        rc["max_queries"] = max(int(rc.get("max_queries") or 12), len(qs) * 3)
        cfg = {**cfg, "reddit_archive": rc}
    results = discovery.run(con, sources=sources, max_threads=mx, cfg=cfg)
    print(f"\nDISCOVERY SWEEP — {len(results)} source(s)\n")
    for r in results:
        print(f"  {r['source']:<22} fetched={r['fetched']:>3}  accepted={r['accepted']:>3}  "
              f"new={r['new']:>3}  dupes={r['duplicates']:>2}  skipped={r['skipped']:>3}  "
              f"errors={r['errors']}  engagement={r.get('engagement_ready', 0)}  "
              f"contacts: {r.get('contact_rows', 0)} row(s), "
              f"{r.get('emails', 0)} email(s), {r.get('phones', 0)} phone(s)")
        if r.get("note"):
            print(f"    note: {r['note'][:160]}")
        if r.get("reasons"):
            top = sorted(r["reasons"].items(), key=lambda kv: -kv[1])[:4]
            print("    skipped because: " + ", ".join(f"{k} x{v}" for k, v in top))
    c = prospector.counts(con)
    print("\n  stored now: " + ", ".join(f"{k}={v}" for k, v in c["stages"].items() if v) or "  stored now: none")
    # ONE CONTINUOUS PROCESS: discovery -> identity resolution -> contact matching. Opt-in because it
    # is a second network pass, but when asked for it runs against exactly what the sweep just stored.
    if "--enrich" in args:
        lim = 25
        if "--enrich-limit" in args:
            try:
                lim = max(1, int(args[args.index("--enrich-limit") + 1]))
            except (IndexError, ValueError):
                lim = 25
        s = identity.resolve_queue(con, limit=lim, min_intent="STRONG",
                                   probes=("--probes" in args))
        print(f"\n  IDENTITY RESOLUTION — {s['rows_scanned']} row(s): names {s['names_found']} "
              f"({s['names_verified']} verified), phones {s['phones_found']} "
              f"({s['phones_verified']} verified), claims refused {s['claims_rejected']}, "
              f"errors {s['errors']}")
    print("  every row is a PUBLIC POST, not a lead. Review: /admin/people?token=...\n")
    return 0


def cmd_signals(con, args) -> int:
    """Feed rows already collected by engine/signals.py into the lead database (offline, no network)."""
    import sqlite3
    sig_path = Path(os.environ.get("SIGNALS_DB", ROOT / "data" / "signals.db"))
    if not sig_path.exists():
        print(f"no signal store at {sig_path}")
        return 0
    scon = sqlite3.connect(str(sig_path))
    scon.row_factory = sqlite3.Row
    try:
        stats = prospector.signals_ingest(con, scon)
    finally:
        scon.close()
    print(f"\nSIGNAL STORE -> prospects: read={stats['fetched']} accepted={stats['accepted']} "
          f"new={stats['new']} dupes={stats['duplicates']} skipped={stats['skipped']}\n")
    c = prospector.counts(con)
    print("  stored now: " + ", ".join(f"{k}={v}" for k, v in c["stages"].items() if v))
    return 0


def cmd_queue(con, args) -> int:
    """The working queue: ACTIVE/STRONG consumers in a servable state, freshest first.
    Deliberately NOT filtered on a published contact - a public thread is the route."""
    rows = prospector.actionable(con)
    print(f"\nACTIONABLE CONSUMER QUEUE — {len(rows)} row(s)\n")
    for r in rows:
        route = "reply in thread" if r["engagement"] == "THREAD_REPLY" else \
                ("public contact, no permission" if r["contact_status"] == "CONTACT_AVAILABLE" else "none yet")
        print(f"  #{r['id']:<4} {r['intent_strength']:<7} {r['state_relevant']:<8} {r['source']:<20} route={route}")
        print(f"        {(r['stated_need'] or '')[:160]}")
        print(f"        {r['source_url']}")
    print()
    return 0


def cmd_regrade(con, args) -> int:
    """Backfill intent/state/engagement on every stored row (no network)."""
    s = prospector.regrade(con, dry_run="--dry-run" in args)
    print(f"\nREGRADE {'(dry run) ' if '--dry-run' in args else ''}scanned={s['scanned']} graded={s['graded']} "
          f"actionable={s['promoted']} restored={s.get('restored', 0)} rejected={s['rejected']} "
          f"stale={s['stale']} out_of_state={s['out_of_state']}\n")
    d = s.get("dedupe") or {}
    print(f"  cross-adapter duplicates: {d.get('marked', 0)} row(s) collapsed across "
          f"{d.get('groups', 0)} repeated permalink(s)")
    for k, v in sorted(s["reasons"].items(), key=lambda kv: -kv[1])[:8]:
        print(f"    {k:<60} {v}")
    print()
    return 0


def cmd_report(con, args) -> int:
    c = prospector.counts(con)
    print("\nPUBLIC PROSPECTS (data/leads.db -> public_prospects)\n")
    for s in prospector.STATUSES:
        print(f"  {s:<28} {c['stages'].get(s, 0):>6}")
    print(f"  {'with a public contact':<28} {c['with_contact']:>6}")
    print(f"  {'engagement route open':<28} {c.get('engagement_ready', 0):>6}")
    print(f"  {'check links issued':<28} {c.get('links_issued', 0):>6}")
    if c.get("by_strength"):
        print("  intent: " + ", ".join(f"{k}={v}" for k, v in sorted(c["by_strength"].items())))
    print(f"  {'duplicates':<28} {c['duplicates']:>6}\n")
    yields = prospector.source_yield(con)
    if not yields:
        print("  no discovery runs recorded yet\n")
    else:
        print("  MEASURED YIELD PER ADAPTER")
        for y in yields:
            print(f"    {y['source']:<22} pages={y['fetched']:>4} prospects={y['new_rows']:>3} "
                  f"engagement={y.get('engagement_ready') or 0:>3} out_of_state={y.get('out_of_state') or 0:>3} "
                  f"emails={y['emails'] or 0:>3} phones={y['phones'] or 0:>3} "
                  f"dupes={y['duplicates']:>2} errors={y['errors']:>2}  last={y['last_run']}")
    if "--json" in args:
        print(json.dumps({"counts": c, "yield": yields}, indent=1, default=str))
    return 0


def cmd_list(con, args) -> int:
    status = args[args.index("--status") + 1] if "--status" in args else ""
    source = args[args.index("--source") + 1] if "--source" in args else ""
    rows = prospector.list_rows(con, status=status, source=source, limit=60)
    print(f"\n{len(rows)} row(s)\n")
    for r in rows:
        contact = ", ".join(f"{m['kind']}:{m['value']}" for m in r["methods"]) or "-"
        print(f"  #{r['id']:<4} {r['status']:<28} {r['source']:<12} contact={contact[:40]}")
        print(f"        {(r['stated_need'] or '')[:150]}")
        print(f"        {r['source_url']}")
    print()
    return 0


def cmd_inspect(con, args) -> int:
    """Open ONE stored prospect's original post and record what was seen.

    Network action against the public archive only (reddit.com's robots disallows our agent). With
    --save the excerpt and thread facts are merged into the row's evidence, so the operator's later
    decision is backed by text on disk rather than a link they may not reopen.
    """
    pid = int(args[args.index("--id") + 1]) if "--id" in args else 0
    row = con.execute("SELECT * FROM public_prospects WHERE id=?", (pid,)).fetchone()
    if not row:
        print(f"no prospect #{pid}")
        return 1
    post_id = discovery.post_id_from_url(row["source_url"])
    if not post_id:
        print(f"#{pid}: not a post URL ({row['source_url']})"); return 1
    d = discovery.archive_post(post_id)
    if not d:
        print(f"#{pid}: archive returned nothing for {post_id}"); return 1
    when = datetime.datetime.fromtimestamp(d["created_utc"], datetime.timezone.utc) if d["created_utc"] else None
    print(f"\n#{pid}  r/{d['subreddit']}  u/{d['author']}  {when:%Y-%m-%d}  "
          f"comments={d['num_comments']}  score={d['score']}  removed={d['removed'] or 'no'}")
    print(f"permalink: {d['permalink']}")
    print(f"title: {d['title']}")
    print("body:\n  " + (d["selftext"] or "")[:1600].replace("\n", "\n  "))
    print(f"\nstored row: status={row['status']} intent={row['intent_strength']} "
          f"state={row['state_relevant']} geo={row['geo']} route={row['engagement']}")
    if "--save" in args:
        ev = json.loads(row["evidence"] or "{}")
        ev["verified_excerpt"] = (d["selftext"] or "")[:4000]
        ev["thread"] = {"subreddit": d["subreddit"], "author": d["author"],
                        "created": when.isoformat() if when else "",
                        "num_comments": d["num_comments"], "score": d["score"],
                        "removed": d["removed"], "read_by": "archive_post"}
        con.execute("UPDATE public_prospects SET evidence=?, updated_at=? WHERE id=?",
                    (json.dumps(ev), prospector._now(), pid))
        con.commit()
        print("  saved: verified_excerpt + thread facts merged into evidence")
    return 0


def cmd_verify(con, args) -> int:
    """Record the operator's VERIFIED decision with a written note (the note is mandatory)."""
    pid = int(args[args.index("--id") + 1]) if "--id" in args else 0
    note = args[args.index("--note") + 1] if "--note" in args else ""
    ok = prospector.verify(con, pid, note)
    print(f"#{pid}: {'VERIFIED' if ok else 'refused (need a real note >= 10 chars on a live row)'}")
    return 0 if ok else 1


def cmd_qualify(con, args) -> int:
    """QUALIFIED = this person may be OFFERED the /check link. It authorises nothing else.

    Refused unless the row is VERIFIED (public reply route) or CONSENTED (a contact with a recorded
    permission basis) - the two lawful doors. Nobody is contacted by this command.
    """
    pid = int(args[args.index("--id") + 1]) if "--id" in args else 0
    note = args[args.index("--note") + 1] if "--note" in args else ""
    ok = prospector.qualify(con, pid, note)
    if ok:
        print(f"#{pid}: QUALIFIED - the tracked /check link may be placed in that public thread")
        return 0
    row = con.execute("SELECT status, engagement, contact_status FROM public_prospects WHERE id=?", (pid,)).fetchone()
    print(f"#{pid}: refused (status={row['status'] if row else 'missing'}, "
          f"route={row['engagement'] if row else '-'}). Needs a written note and either a VERIFIED "
          f"row with a public reply route, or CONSENTED with a published contact.")
    return 1


def cmd_kit(con, args) -> int:
    """Build the ready-to-post public reply for a QUALIFIED prospect (never sent)."""
    pid = int(args[args.index("--id") + 1]) if "--id" in args else 0
    base = _base_url()
    kit = prospector.prepare_kit(con, pid, base)
    if not kit:
        print(f"refused: prospect #{pid} must be QUALIFIED first (verify -> qualify with a note)")
        return 1
    print(f"\nKIT for prospect #{pid} — situation: {kit['situation']}")
    print(f"  bridge   {kit['invite_url']}")
    print(f"  source   {kit['source_url']}")
    print("\n  EN reply (post as a normal comment in that thread):\n    " +
          kit["reply_en"] + "\n    " + kit["disclosure_en"])
    print("\n  ES reply:\n    " + kit["reply_es"] + "\n    " + kit["disclosure_es"])
    print("\n  rules:")
    for r_ in kit["rules"]:
        print("    - " + r_)
    print("\n  NOTHING HAS BEEN SENT. An operator posts it by hand after checking that community's rules.\n")
    return 0


def cmd_enrich(con, args) -> int:
    """IDENTITY + CONTACT RESOLUTION for the ACTIVE/STRONG queue (or explicit --ids).

    This is the stage after discovery: it reads the same public account's OTHER public posts, the
    platform profile and (with --probes) a short allowlist of public profile surfaces, then stores a
    name only from explicit self-identification and a phone only when the same identity published it
    with ownership wording. Nothing is contacted, nothing is guessed, and every stored value carries
    the URL it came from.
    """
    if "--live" not in args:
        print("refused: resolving identities reads third-party public pages; pass --live explicitly")
        return 2
    ids: list[int] = []
    if "--ids" in args:
        try:
            ids = [int(x) for x in args[args.index("--ids") + 1].split(",") if x.strip()]
        except (IndexError, ValueError):
            print("--ids takes a comma list, e.g. --ids 303,324")
            return 2
    limit = 25
    if "--limit" in args:
        try:
            limit = max(1, int(args[args.index("--limit") + 1]))
        except (IndexError, ValueError):
            limit = 25
    intent = "STRONG"
    if "--intent" in args:
        intent = args[args.index("--intent") + 1].upper()
    probes = "--probes" in args
    s = identity.resolve_queue(con, ids=ids or None, limit=limit, min_intent=intent,
                               probes=probes)
    print(f"\nIDENTITY RESOLUTION — {s['rows_scanned']} row(s), probes={'on' if probes else 'off'}\n")
    for r in s["results"]:
        print(f"  #{r['id']:<4} name={r['name'] or '—':<22} {r['name_status']:<10} "
              f"phone={r['phone'] or '—':<13} {r['phone_status']:<10} "
              f"match={r['match_status']:<10} loc={r['location'] or '—'}")
        if r.get("name_source"):
            print(f"        name from: {r['name_source']}")
        if r.get("phone_source"):
            print(f"        phone from: {r['phone_source']}")
        for n in (r.get("notes") or [])[:2]:
            print(f"        note: {n[:140]}")
    print(f"\n  rows resolved          {s['rows_resolved']:>4}")
    print(f"  names found            {s['names_found']:>4}   verified {s['names_verified']}")
    print(f"  phones found           {s['phones_found']:>4}   verified {s['phones_verified']}")
    print(f"  claims refused         {s['claims_rejected']:>4}   errors {s['errors']}")
    if s["by_source"]:
        print("  names verified via: " + ", ".join(f"{k} x{v}" for k, v in s["by_source"].items()))
    rep = identity.report(con)
    print(f"\n  STORED: name verified {rep['name_verified']}, phone verified "
          f"{rep['phone_verified']}, both {rep['usable']}, marketable (consented) {rep['marketed']}")
    if rep["reject_reasons"]:
        print("  refusals by reason:")
        for x in rep["reject_reasons"][:6]:
            print(f"    {(x['reject_reason'] or '?')[:70]:<72} {x['n']}")
    if "--json" in args:
        print(json.dumps({"run": {k: v for k, v in s.items() if k != 'results'}, "report": rep},
                         indent=1, default=str))
    print()
    return 0


def cmd_identity(con, args) -> int:
    """No network: what identity resolution has produced, by evidence source and refusal reason."""
    rep = identity.report(con)
    print("\nIDENTITY RESOLUTION — stored state (no network)\n")
    print(f"  prospect rows              {rep['rows_total']:>5}")
    print(f"  rows attempted             {rep['rows_attempted']:>5}")
    print(f"  names  verified/matched    {rep['name_verified']:>5}")
    print(f"  phones verified            {rep['phone_verified']:>5}")
    print(f"  usable name+phone          {rep['usable']:>5}")
    print(f"  marketable (consented)     {rep['marketed']:>5}")
    for k in ('matches', 'names', 'phones'):
        if rep[k]:
            print(f"  {k}: " + ", ".join(f"{a}={b}" for a, b in sorted(rep[k].items())))
    if rep["by_source"]:
        print("\n  CLAIMS BY SOURCE")
        for r in rep["by_source"]:
            print(f"    {r['source_kind']:<20} {r['claim_kind']:<9} claims={r['claims']:>3} "
                  f"accepted={r['accepted']:>3} rejected={r['rejected']:>3}")
    if rep["reject_reasons"]:
        print("\n  REFUSED, BY REASON")
        for x in rep["reject_reasons"]:
            print(f"    {(x['reject_reason'] or '?')[:70]:<72} {x['n']}")
    if rep["runs"]:
        print("\n  RUNS")
        for r in rep["runs"]:
            print(f"    {r['ts'][:16]} rows={r['rows_scanned']:>3} names={r['names_found']:>2} "
                  f"({r['names_verified']} verified) phones={r['phones_found']:>2} "
                  f"({r['phones_verified']} verified) errors={r['errors']}")
    print()
    return 0


def cmd_status(con, args) -> int:
    """The acquisition ladder, with TEST and REAL kept apart. Prospects are not leads."""
    one = lambda sql, a=(): con.execute(sql, a).fetchone()[0]
    live = one("SELECT COUNT(*) FROM public_prospects WHERE status NOT IN ('REJECTED','DUPLICATE')")
    # the ladder is cumulative: a QUALIFIED row was also verified, so count "at least this far"
    ver = one("SELECT COUNT(*) FROM public_prospects WHERE status IN "
              "('VERIFIED','CONSENTED','QUALIFIED','ROUTING_ELIGIBLE')")
    qual = one("SELECT COUNT(*) FROM public_prospects WHERE status IN ('QUALIFIED','ROUTING_ELIGIBLE')")
    routed_eligible = one("SELECT COUNT(*) FROM public_prospects WHERE status='ROUTING_ELIGIBLE'")
    kits = one("SELECT COUNT(*) FROM public_prospects WHERE COALESCE(kit_json,'')!=''")
    links = one("SELECT COUNT(*) FROM public_prospects WHERE COALESCE(tracked_link,'')!=''")
    clicks = one("SELECT COALESCE(SUM(l.clicks),0) FROM links l JOIN public_prospects p ON p.tracked_link=l.code")
    bridge = one("SELECT COUNT(*) FROM events WHERE event='bridge_view' AND COALESCE(link_code,'') GLOB 'p[0-9]*' AND COALESCE(is_test,0)=0")
    cta = one("SELECT COUNT(*) FROM events WHERE event='bridge_cta' AND COALESCE(link_code,'') GLOB 'p[0-9]*' AND COALESCE(is_test,0)=0")
    starts = one("SELECT COUNT(*) FROM events WHERE event IN ('tool_start','form_start') AND COALESCE(link_code,'') GLOB 'p[0-9]*' AND COALESCE(is_test,0)=0")
    subs = one("SELECT COUNT(*) FROM leads WHERE COALESCE(link_code,'') GLOB 'p[0-9]*' AND COALESCE(is_test,0)=0")
    subs_test = one("SELECT COUNT(*) FROM leads WHERE COALESCE(link_code,'') GLOB 'p[0-9]*' AND COALESCE(is_test,0)=1")
    consented = one("SELECT COUNT(DISTINCT l.lead_id) FROM leads l JOIN consents c ON c.lead_id=l.lead_id "
                    "WHERE COALESCE(l.link_code,'') GLOB 'p[0-9]*' AND COALESCE(l.is_test,0)=0")
    routed = one("SELECT COUNT(*) FROM leads WHERE COALESCE(link_code,'') GLOB 'p[0-9]*' "
                 "AND COALESCE(is_test,0)=0 AND routing_status IN ('ROUTED','DELIVERED')")
    print("\nACQUISITION STATUS — discovery is not the finish line\n")
    print("  PROSPECTS (public posts, not leads)")
    print(f"    live rows                         {live:>4}")
    print(f"    verified by an operator           {ver:>4}")
    print(f"    qualified (may be offered a link) {qual:>4}")
    print(f"  OPPORTUNITIES PREPARED FOR ENGAGEMENT")
    print(f"    tracked links issued              {links:>4}")
    print(f"    reply kits prepared               {kits:>4}")
    print(f"  CONSUMERS WHO ARRIVED (REAL, test excluded)")
    print(f"    bridge page views                 {bridge:>4}")
    print(f"    clicked through to /check         {cta:>4}")
    print(f"    started the check                 {starts:>4}")
    print(f"    link clicks recorded              {clicks:>4}")
    print("  CONTACT RESOLUTION (public sources — evidence, never consent)")
    print(f"    prospects attempted               {one('SELECT COUNT(*) FROM public_prospects WHERE resolve_attempts>0'):>4}")
    print("    names verified / probable         {:>4} / {}".format(
        one("SELECT COUNT(*) FROM public_prospects WHERE identity_name_status='VERIFIED'"),
        one("SELECT COUNT(*) FROM public_prospects WHERE identity_name_status='PROBABLE'")))
    print("    phones verified / held            {:>4} / {}".format(
        one("SELECT COUNT(*) FROM public_prospects WHERE identity_phone_status='VERIFIED'"),
        one("SELECT COUNT(*) FROM public_prospects WHERE identity_phone_status='PROBABLE'")))
    print("    usable name+phone                 {:>4}".format(one(
        "SELECT COUNT(*) FROM public_prospects WHERE identity_name_status='VERIFIED' "
        "AND identity_phone_status='VERIFIED'")))
    print("    marketable (consented)            {:>4}".format(one(
        "SELECT COUNT(*) FROM public_prospects WHERE marketing_eligibility='ELIGIBLE_CONSENTED'")))
    print(f"  INQUIRIES")
    print(f"    submissions via a prospect link   {subs:>4}   (TEST copies excluded: {subs_test})")
    print(f"    with a consent record             {consented:>4}")
    print(f"    routed / delivered to a buyer      {routed:>4}")
    print(f"    prospect rows now ROUTING_ELIGIBLE {routed_eligible:>4}")
    print("\n  A prospect is not a lead. A lead exists only after that person submits /check with consent.\n")
    return 0


def cmd_issue(con, args) -> int:
    """Issue the tracked /check link for a QUALIFIED prospect (an action an operator takes after
    reading the thread). It does not contact anyone: it produces the URL to place in that thread."""
    from engine import discovery as _d
    pid = int(args[args.index("--id") + 1]) if "--id" in args else 0
    base = _base_url()
    link = prospector.issue_link(con, pid, base)
    if not link:
        print(f"refused: prospect #{pid} must be QUALIFIED first (verify -> qualify with a note)")
        return 1
    print(f"prospect #{pid}: {'existing' if link['existing'] else 'created'} {link['url']}")
    return 0


def cmd_attribute(con, args) -> int:
    lead_id = args[args.index("--lead") + 1] if "--lead" in args else ""
    ok = prospector.attribute_lead(con, lead_id)
    print(f"{lead_id}: {'linked to its prospect (ROUTING_ELIGIBLE)' if ok else 'no prospect link on this lead'}")
    return 0 if ok else 1


def cmd_migrate(con, args) -> int:
    import sqlite3
    if not PEOPLE_DB.exists():
        print(f"no legacy store at {PEOPLE_DB}")
        return 0
    legacy = sqlite3.connect(str(PEOPLE_DB))
    legacy.row_factory = sqlite3.Row
    try:
        moved = prospector.migrate_legacy(con, legacy)
    finally:
        legacy.close()
    print(f"migrated {moved} legacy row(s) into {DB}")
    return 0


def main() -> int:
    try:                                  # Windows consoles default to cp1252; keep output readable
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                     # noqa: BLE001
        pass
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 0
    con = _con()
    try:
        cmd = args[0]
        if cmd == "run":
            return cmd_run(con, args[1:])
        if cmd == "signals":
            return cmd_signals(con, args[1:])
        if cmd == "report":
            return cmd_report(con, args[1:])
        if cmd == "queue":
            return cmd_queue(con, args[1:])
        if cmd == "regrade":
            return cmd_regrade(con, args[1:])
        if cmd == "list":
            return cmd_list(con, args[1:])
        if cmd == "inspect":
            return cmd_inspect(con, args[1:])
        if cmd == "verify":
            return cmd_verify(con, args[1:])
        if cmd == "qualify":
            return cmd_qualify(con, args[1:])
        if cmd == "kit":
            return cmd_kit(con, args[1:])
        if cmd == "status":
            return cmd_status(con, args[1:])
        if cmd == "issue":
            return cmd_issue(con, args[1:])
        if cmd == "enrich":
            return cmd_enrich(con, args[1:])
        if cmd == "identity":
            return cmd_identity(con, args[1:])
        if cmd == "attribute":
            return cmd_attribute(con, args[1:])
        if cmd == "migrate":
            return cmd_migrate(con, args[1:])
        print(__doc__)
        return 1
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
