"""Admit third-party contact records — WITHOUT claiming consent.

    python tools/import_contacts.py import --source licensed-panel-2026-04 \
        --name "Panel export" --url https://example.com/panel --license "vendor agreement §3" \
        --type CONSUMER --file exports/panel.csv

    python tools/import_contacts.py report
    python tools/import_contacts.py sources
    python tools/import_contacts.py promote --id 12 --lead 8f3c...-a1

`import` stores every row in `imported_contacts` as NOT_ELIGIBLE with its provenance. It never
writes a lead, a consent or an eligibility. `promote` links ONE record to the same person's own
/check submission; it refuses unless that lead exists, has a consent row, and its own submitted
phone/email matches the record — which is what makes it evidence rather than an assertion.

Nothing here sends, dials, emails or sells.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import admission as adm                    # noqa: E402
from engine import prospector                          # noqa: E402
from engine import source_eval                          # noqa: E402

DB = Path(os.environ.get("LEADENGINE_DB", ROOT / "data" / "leads.db"))


def _con():
    return prospector.connect(DB)


def cmd_import(a) -> int:
    con = _con()
    source = {"id": a.source, "name": a.name or "", "url": a.url or "", "license": a.license or "",
              "license_note": a.license_note or ""}
    r = adm.import_file(con, source, a.file, record_type=a.type, batch=a.batch or "",
                        is_test=1 if a.test else 0)
    print(json.dumps({k: v for k, v in r.items() if k != "reasons"}, indent=2))
    if r["reasons"]:
        print("rejected rows:", json.dumps(r["reasons"]))
    for w in r["warnings"]:
        print("WARNING:", w)
    print(f"\nEvery imported row is NOT_ELIGIBLE: possession only, never routed or sold.")
    print(f"Review them at /admin/contacts (operator token required).")
    return 0


def cmd_evaluate(a) -> int:
    """Measure what each source ACTUALLY contains (counts, field coverage, validity, duplicates)."""
    ids = a.source if a.source else list(source_eval.SOURCES)
    if ids == ["all"]:
        ids = list(source_eval.SOURCES)
    reports = []
    for sid in ids:
        try:
            r = source_eval.run(sid, limit=a.limit, state=a.state or "FL",
                                fetcher=None)
        except Exception as e:                        # noqa: BLE001 - reported, not hidden
            reports.append({"source_id": sid, "title": source_eval.SOURCES[sid]["title"],
                            "record_type": source_eval.SOURCES[sid]["record_type"],
                            "data_status": source_eval.SOURCES[sid]["data_status"],
                            "access": source_eval.SOURCES[sid]["access"],
                            "license": source_eval.SOURCES[sid]["license"],
                            "measured": False,
                            "finding": f"fetch failed here: {type(e).__name__}: {str(e)[:160]}"})
            continue
        r.pop("rows", None)
        reports.append(r)
        e = r.get("evaluation")
        if e:
            print(f"{sid:<24} records={e['records']:<5} name={e['has_name']:<5} "
                  f"phone={e['valid_phones']:<5} email={e['valid_emails']:<5} "
                  f"exact-dups={e['duplicate_rows']:<4} shared-channels={e['shared_contact_channels']:<4} "
                  f"no-identity={e['no_identity_rows']}")
        else:
            print(f"{sid:<24} not measured — {r.get('finding', '')[:90]}")
    out = ROOT / "out" / "source_eval"
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text(source_eval.to_markdown(reports), encoding="utf-8")
    (out / "report.json").write_text(json.dumps(reports, indent=2), encoding="utf-8")
    print(f"\nwrote {out/'report.md'} and report.json")
    return 0


def cmd_fetch(a) -> int:
    """Fetch real records from a public source and (unless --dry-run) admit them as imported contacts."""
    if a.source not in source_eval.SOURCES:
        print(f"unknown source {a.source!r}; known: {sorted(source_eval.SOURCES)}")
        return 2
    meta = source_eval.run(a.source, limit=a.limit, state=a.state or "FL")
    rows = meta.pop("rows", []) or []
    e = meta.get("evaluation") or {}
    print(f"{a.source}: fetched {len(rows)} row(s); name {e.get('has_name', 0)}, "
          f"valid phone {e.get('valid_phones', 0)}, valid email {e.get('valid_emails', 0)}, "
          f"duplicates {e.get('duplicate_rows', 0)}")
    if meta.get("synthetic") and not a.allow_synthetic:
        print("REFUSED: this source generates records. Re-run with --allow-synthetic only if you "
              "deliberately want synthetic rows stored as flagged test data.")
        return 1
    if a.dry_run:
        print("dry run: nothing stored")
        return 0
    source = {"id": a.source_id or f"{a.source}-{datetime.now(timezone.utc):%Y%m%d}",
              "name": meta.get("title", a.source), "url": meta.get("url", ""),
              "license": meta.get("license", ""), "license_note": meta.get("license_note", ""),
              "data_status": meta.get("data_status", "UNVERIFIED")}
    con = _con()
    r = adm.import_rows(con, source, rows, record_type=meta.get("record_type", "UNKNOWN"),
                        batch=a.batch or "")
    print(json.dumps({k: v for k, v in r.items() if k != "reasons"}, indent=2))
    print(f"\nsource: {source['id']} ({source['data_status']}) — every row is NOT_ELIGIBLE: "
          f"held, with provenance, and not routable or sellable.")
    return 0


def cmd_report(a) -> int:
    con = _con()
    print(json.dumps(adm.report(con), indent=2, default=str))
    return 0


def cmd_sources(a) -> int:
    con = _con()
    for s in adm.report(con)["sources"]:
        print(f"{s['source_id']:<32} records={s['total']:<6} with_contact={s['with_contact'] or 0:<6} "
              f"licence={s['license'] or '—none recorded—'}")
    return 0


def cmd_promote(a) -> int:
    con = _con()
    r = adm.promote(con, a.id, a.lead, a.note or "")
    print(json.dumps(r, indent=2))
    return 0 if r["ok"] else 1


def main() -> int:
    p = argparse.ArgumentParser(description="Imported-contact admission store")
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("import", help="admit a CSV/JSON export as NOT_ELIGIBLE records")
    i.add_argument("--source", required=True, help="source id (provenance, required)")
    i.add_argument("--name", default="", help="human source name")
    i.add_argument("--url", default="", help="dataset / provenance URL")
    i.add_argument("--license", default="", help="licence or terms basis recorded for the source")
    i.add_argument("--license-note", default="", help="licence note / permitted-use clause")
    i.add_argument("--type", default="UNKNOWN", choices=adm.RECORD_TYPES, help="record type")
    i.add_argument("--batch", default="", help="batch label (default: generated)")
    i.add_argument("--test", action="store_true", help="mark rows as TEST")
    i.add_argument("--file", dest="file", required=True, help="CSV or JSON export path")
    i.set_defaults(func=cmd_import)

    ev = sub.add_parser("evaluate", help="measure what a source actually contains (counts/coverage)")
    ev.add_argument("--source", nargs="*", default=["all"], help="source id(s); default all")
    ev.add_argument("--limit", type=int, default=200)
    ev.add_argument("--state", default="FL")
    ev.set_defaults(func=cmd_evaluate)

    fe = sub.add_parser("fetch", help="fetch real records from a public source and admit them")
    fe.add_argument("--source", required=True)
    fe.add_argument("--limit", type=int, default=200)
    fe.add_argument("--state", default="FL")
    fe.add_argument("--source-id", default="", help="override the recorded source id")
    fe.add_argument("--batch", default="")
    fe.add_argument("--dry-run", action="store_true")
    fe.add_argument("--allow-synthetic", action="store_true")
    fe.set_defaults(func=cmd_fetch)

    r = sub.add_parser("report", help="counts, sources and the 'no imported lead' audit")
    r.set_defaults(func=cmd_report)

    s = sub.add_parser("sources", help="one line per source held")
    s.set_defaults(func=cmd_sources)

    pr = sub.add_parser("promote", help="link a record to the same person's consented submission")
    pr.add_argument("--id", type=int, required=True)
    pr.add_argument("--lead", required=True)
    pr.add_argument("--note", default="")
    pr.set_defaults(func=cmd_promote)

    a = p.parse_args()
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
