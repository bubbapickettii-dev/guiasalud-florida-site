"""Acquisition CLI: build the referral network and read the eight-stage consumer funnel.

  python tools/leadgen.py report                    # the eight stages + blocking dependencies
  python tools/leadgen.py report --test             # TEST rows only
  python tools/leadgen.py network-build             # org ledger -> partner rows (idempotent)
  python tools/leadgen.py network-build --dry-run    # what it WOULD create, writes nothing
  python tools/leadgen.py network-report            # activation + measured referral yield
  python tools/leadgen.py kit <partner_id>          # copy-ready card/outreach text (never sends)
  python tools/leadgen.py directory --city Miami    # what the public /free-help page shows

Nothing here sends outreach, publishes, or creates a lead. Partner creation is bookkeeping: the org
is a distribution partner at stage IDENTIFIED until a human approves and logs real contact.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import leadgen, network as network_mod          # noqa: E402

CFG = Path(os.environ.get("LEADENGINE_CFG", ROOT / "config"))
DB = Path(os.environ.get("LEADENGINE_DB", ROOT / "data" / "leads.db"))
ORG = Path(os.environ.get("ORG_CONTACTS_DB", ROOT / "data" / "org_contacts.db"))


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    return con


def _int(v) -> str:
    return "-" if v is None else f"{v:,}"


def cmd_report(args) -> int:
    con = connect()
    rep = leadgen.report(con, CFG, 1 if args.test else 0)
    st = rep["stages"]
    print(f"\nCONSUMER ACQUISITION — {'TEST DATA' if args.test else 'REAL'}\n")
    for k, label, desc in leadgen.STAGES:
        print(f"  {label:<32} {_int(st.get(k)):>8}")
    print(f"  {'  duplicates removed':<32} {_int(st.get('duplicates')):>8}")
    print(f"  {'  email-only (not sellable)':<32} {_int(st.get('email_only')):>8}\n")
    print("BY CHANNEL")
    if not rep["by_channel"]:
        print("  (no channel rows yet)")
    for r in rep["by_channel"]:
        print(f"  {r['k']:<18} visits={_int(r['visits']):>6}  done={_int(r['check_completions']):>5}  "
              f"consented={_int(r['consented_phone_leads']):>4}  qualified={_int(r['qualified']):>4}  "
              f"delivered={_int(r['delivered']):>3}")
    print("\nBLOCKING DEPENDENCIES")
    if not rep["blocking"]:
        print("  none reported")
    for b in rep["blocking"]:
        print(f"  [ ] {b['label']}\n      {b['detail']}\n      -> {b['fix']}")
    print(f"\nNETWORK: {rep['network']['partners_total']} organizations, "
          f"{rep['network']['activated']} activated, {rep['network']['with_activity']} with tracked visits\n")
    if args.json:
        print(json.dumps({"stages": st, "blockers": rep["blockers"], "gate": rep["gate"]}, indent=1, default=str))
    return 0


def cmd_network_build(args) -> int:
    con = connect()
    res = network_mod.build(con, args.org_db or ORG, state=args.state, limit=args.limit,
                            dry_run=args.dry_run)
    s = res["stats"]
    print(("\nDRY RUN — nothing written\n" if args.dry_run else "\nNETWORK BUILD\n"))
    for k, v in s.items():
        print(f"  {k:<24} {v:>7}")
    if res["created"]:
        print("\n  example:")
        for r in res["created"][:5]:
            print(f"    {r['partner_id']:<46} {r['display_name'][:40]:<40} {r['phone']}")
    print("\n  Organizations only. No consumer lead is created, nothing is sent, and no page is\n"
          "  co-branded until the organization itself approves (cobrand_approved=0).\n")
    return 0


def cmd_network_report(args) -> int:
    con = connect()
    rep = network_mod.report(con)
    print(f"\nREFERRAL NETWORK — {rep['partners_total']} network partners "
          f"({rep['all_partners']} partners total)\n")
    print("  stages: " + "  ".join(f"{k}={v}" for k, v in rep["stages"].items() if v))
    t = rep["totals"]
    print(f"  visits={t['visits']}  checks_done={t['check_done']}  consented={t['consented']}  "
          f"qualified={t['qualified']}  delivered={t['delivered']}  revenue=${t['revenue']:,.2f}")
    for r in rep["by_type"]:
        print(f"    {r['type']:<16} partners={r['partners']:>4} visits={r['visits']:>5} "
              f"consented={r['consented']:>3} qualified={r['qualified']:>3}")
    lr = rep["last_run"]
    if lr:
        print(f"\n  last build {lr['ts']}: considered={lr['records_considered']} created={lr['partners_created']} "
              f"existing={lr['partners_existing']} no_phone={lr['skipped_no_phone']} "
              f"unverified={lr['skipped_unverified']} dupes={lr['skipped_duplicate']} errors={lr['errors']}")
    if args.json:
        print(json.dumps(rep, indent=1, default=str))
    return 0


def cmd_kit(args) -> int:
    con = connect()
    r = con.execute("SELECT * FROM partners WHERE partner_id=?", (args.partner_id,)).fetchone()
    if not r:
        print(f"no partner {args.partner_id!r}")
        return 1
    site = json.loads((CFG / "site.json").read_text(encoding="utf-8")) if (CFG / "site.json").exists() else {}
    k = network_mod.kit(dict(r), site.get("base_url", ""), site.get("brand", "Guía Salud"))
    print(f"\n{k['display_name']}  ({k['partner_id']})")
    print(f"  link       {k['link']}")
    print(f"  QR         {k['qr_url']}")
    print(f"  stats page {k['stats_link']}")
    print(f"  co-branded {'YES' if k['cobrand_approved'] else 'no (their approval required)'}\n")
    for f in ("card_en", "card_es", "outreach_en", "outreach_es"):
        print(f"--- {f} ---\n{k[f]}\n")
    print(k["note"])
    return 0


def cmd_network_export(args) -> int:
    con = connect()
    site = json.loads((CFG / "site.json").read_text(encoding="utf-8")) if (CFG / "site.json").exists() else {}
    res = network_mod.export_kits(con, site.get("base_url", ""), site.get("brand", "Guía Salud"),
                                  args.out, args.limit, not args.all_partners)
    print(f"\n  {res['partners']} activation kit(s) -> {res['path']}\n"
          "  Each row carries link + QR + card copy + outreach draft and an APPROVAL REQUIRED marker.\n"
          "  Copying text into an email is a human action; the platform sends nothing.\n")
    return 0


def cmd_directory(args) -> int:
    d = network_mod.directory(args.org_db or ORG, state=args.state, city=args.city, page=args.page)
    print(f"\n{args.city or 'all cities'}: {d['total']} location(s) of {d['grand_total']} "
          f"(page {d['page']}/{d['pages']})")
    for r in d["rows"][:args.show]:
        print(f"  {r['name'][:52]:<52} {r['city'][:16]:<16} {r['phone'] or '-'}")
    print()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="GS Allude consumer acquisition")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report"); r.add_argument("--test", action="store_true"); r.add_argument("--json", action="store_true"); r.set_defaults(fn=cmd_report)
    b = sub.add_parser("network-build"); b.add_argument("--state", default="FL"); b.add_argument("--limit", type=int); b.add_argument("--org-db"); b.add_argument("--dry-run", action="store_true"); b.set_defaults(fn=cmd_network_build)
    n = sub.add_parser("network-report"); n.add_argument("--json", action="store_true"); n.set_defaults(fn=cmd_network_report)
    k = sub.add_parser("kit"); k.add_argument("partner_id"); k.set_defaults(fn=cmd_kit)
    x = sub.add_parser("network-export"); x.add_argument("--out"); x.add_argument("--limit", type=int); x.add_argument("--all-partners", action="store_true"); x.set_defaults(fn=cmd_network_export)
    d = sub.add_parser("directory"); d.add_argument("--city", default=""); d.add_argument("--state", default="FL"); d.add_argument("--page", type=int, default=1); d.add_argument("--show", type=int, default=10); d.add_argument("--org-db"); d.set_defaults(fn=cmd_directory)
    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
