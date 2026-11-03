"""PUBLIC CONTACT DISCOVERY — profile-surface collection with measured per-source yield.

  python tools/public_contacts.py scan --network [--sources bluesky,mastodon,lemmy] [--limit 40]
        Runs the PUBLIC-PROFILE contact adapters (engine/public_profiles.py) against the
        credential-free public endpoints, stores evidence-bearing consumer rows in
        data/prospect_people.db, and writes out/public_profile_yield.json.
  python tools/public_contacts.py yield
        Read-only: what the people store actually holds, split post vs profile contact,
        consumer rows vs the organisation surfaces that were deliberately NOT stored here.

Nothing in this tool contacts anyone, buys anything, or creates a lead. A discovered and
even an operator-verified public contact is not consent: the consumer still has to submit
the /check journey themselves (see engine/prospect_contacts.check_link).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import public_profiles as pp   # noqa: E402
from engine import prospect_contacts as pcon  # noqa: E402
from engine import web_extract as webx      # noqa: E402 - robots-checked one-hop page reader

OUT = ROOT / "out"


def cmd_scan(sources: list[str], limit: int) -> int:
    cfg = pp.load_cfg()
    ppl = pcon.connect()
    try:
        stats = pp.scan_all(ppl, sources=tuple(sources), cfg=cfg, limit=limit,
                            fetch_text=webx._real_fetch_text)
        summary = pp.yield_summary(ppl)
        counts = pcon.counts(ppl)
    finally:
        ppl.close()
    # KEEP the last measured run per source: running one source must not erase what the
    # others produced (the report is the evidence the yield claims rest on).
    report_path = OUT / "public_profile_yield.json"
    try:
        prev = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prev = {}
    runs = {r.get("source"): r for r in prev.get("runs") or [] if isinstance(r, dict)}
    for r in stats:
        runs[r.get("source") or "unknown"] = dict(r, measured_at=pp._now())
    report = {"generated_at": pp._now(), "limit_per_query": limit,
              "sources_run": sorted(set(prev.get("sources_run") or []) | set(sources)),
              "runs": list(runs.values()), "store": summary, "counts": counts,
              "note": ("Profile rows are SELF_PUBLISHED contacts tied to a public need post; "
                       "they are not consent and not leads. Organisation-shaped profiles are "
                       "counted per run under org_skipped and never inserted here. A contact "
                       "found via contact_origin=profile_link lives on a page the account "
                       "itself linked (robots-checked, one hop).")}
    OUT.mkdir(exist_ok=True)
    report_path.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n",
                           encoding="utf-8")
    for r in stats:
        if "skipped" in r:
            print(f"{r['source']}: SKIPPED {r['skipped']}")
            continue
        print(f"{r['source']}: fetched={r['posts_fetched']} need_posts={r['need_posts']} "
              f"profiles_read={r['profiles_read']} accepted={r['accepted']} "
              f"unique={r['unique_individuals']} email={r['with_email']} phone={r['with_phone']} "
              f"dup={r['duplicates']} org_skipped={r['org_skipped']} no_bio={r['no_bio']} "
              f"no_contact={r['no_contact']} page_hops={r.get('page_hops', 0)} "
              f"page_contacts={r.get('page_contacts', 0)} stale={r['stale_skipped']} "
              f"restricted={r['access_restricted']} errors={r['errors']}")
        for e in r.get("error_notes", []):
            print(f"    ! {e}")
        for o in r.get("org_examples", []):
            print(f"    org (not a consumer): {o['profile_url']} {o.get('published')}")
    print("wrote out/public_profile_yield.json")
    print(json.dumps(summary, indent=1))
    return 0


def cmd_yield() -> int:
    ppl = pcon.connect()
    try:
        summary = pp.yield_summary(ppl)
        counts = pcon.counts(ppl)
        rows = [dict(r) for r in ppl.execute(
            "SELECT id, source, lang, stage, permission_status, contact_status, dup_status, "
            "contact_methods, profile_url, source_url, stated_need, contact_evidence "
            "FROM prospect_people WHERE contact_status='CONTACT_AVAILABLE' "
            "ORDER BY rank_score DESC LIMIT 25")]
    finally:
        ppl.close()
    print(json.dumps({"counts": counts, "store": summary}, indent=1))
    for r in rows:
        methods = json.loads(r["contact_methods"] or "[]")
        print(f"\n#{r['id']} [{r['stage']}/{r['permission_status']}] {r['source']} {r['lang']} "
              f"dup={r['dup_status']}\n"
              f"    published: {', '.join(m['kind'] + ':' + m['value'] for m in methods)}\n"
              f"    evidence : {(r['contact_evidence'] or '')[:120]}\n"
              f"    need     : {(r['stated_need'] or '')[:120]}\n"
              f"    profile  : {r['profile_url']}\n"
              f"    post     : {r['source_url']}")
    return 0


def main() -> None:
    a = sys.argv[1:]
    if not a or a[0] in ("help", "-h", "--help"):
        print(__doc__)
        return
    if a[0] == "yield":
        sys.exit(cmd_yield())
    if a[0] != "scan":
        sys.exit("usage: public_contacts.py scan --network [--sources bluesky,mastodon,lemmy] "
                 "[--limit 40] | public_contacts.py yield")
    if "--network" not in a:
        sys.exit("refused: profile collection is a REAL network action; pass --network explicitly")
    sources = tuple(pp.ADAPTERS)
    limit = 40
    if "--sources" in a:
        i = a.index("--sources")
        sources = tuple(x.strip() for x in (a[i + 1] if i + 1 < len(a) else "").split(",") if x.strip())
        if not sources or any(s not in pp.ADAPTERS for s in sources):
            sys.exit(f"unknown source; choose from {sorted(pp.ADAPTERS)}")
    if "--limit" in a:
        i = a.index("--limit")
        try:
            limit = max(1, min(100, int(a[i + 1])))
        except (IndexError, ValueError):
            sys.exit("--limit needs a number 1..100")
    sys.exit(cmd_scan(list(sources), limit))


if __name__ == "__main__":
    main()
