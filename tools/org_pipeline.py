#!/usr/bin/env python3
"""ORG PIPELINE — run the organization public-contact discovery + verify + rank loop.

The organization side of discovery: businesses, clinics, nonprofits and other community
organizations that VOLUNTARILY publish contact details for inquiries. This is the outreach
candidate pipeline — it never produces consumer leads and never contacts anyone.

Commands (all explicit operator actions; `run` is the only network user):
  python tools/org_pipeline.py run           batch: NPI registry + robots-checked org seeds
  python tools/org_pipeline.py bulk          official HRSA health-center site file (data.hrsa.gov
                                             bulk CSV, free/no key) — active sites with a
                                             published phone for ONE state [--state FL|GA|...]
  python tools/org_pipeline.py verify        recheck DISCOVERED rows against their source page
  python tools/org_pipeline.py status        per-source metrics + ladder counts
  python tools/org_pipeline.py perf          sources ranked by verified-records-per-unit
  python tools/org_pipeline.py list          latest org contact rows [--type t]
  python tools/org_pipeline.py partner-sync  write VERIFIED candidates into the existing
                                             partners table (leads.db) as
                                             "DISCOVERED - not contacted" — sends nothing

Seeds come from config/sources.json > org_contacts.seeds. Everything reuses the existing
DBs: org_contacts.db (engine/org_contacts.py) and the project leads.db partners table.
"""
from __future__ import annotations

import json
import secrets
import sys
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import org_contacts as org              # noqa: E402
from engine.db import connect as lead_connect       # noqa: E402

CFG = ROOT / "config" / "sources.json"
ORG_DB = ROOT / "data" / "org_contacts.db"
LEAD_DB = ROOT / "data" / "leads.db"


def load_seeds() -> list[dict]:
    try:
        cfg = json.loads(CFG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return list((cfg.get("org_contacts") or {}).get("seeds") or [])


def cmd_run() -> int:
    con = org.connect(ORG_DB)
    rows = []
    rows.append({"adapter": "npi_registry", **org.fetch_npi_organizations(con, sleeper=time.sleep)})
    for seed in load_seeds():
        try:
            r = org.fetch_org_seed(con, seed, sleeper=time.sleep)
            rows.append({"adapter": f"seed:{urllib.parse.urlsplit(seed['url']).netloc}", **r})
        except Exception as e:                              # noqa: BLE001 - one seed never kills the batch
            rows.append({"adapter": f"seed:{urllib.parse.urlsplit(seed['url']).netloc}", "error": f"{type(e).__name__}: {e}"})
    print(json.dumps(rows, indent=1, ensure_ascii=False))
    return 0


def cmd_bulk(state: str = "FL") -> int:
    """Official HRSA health-center site file: organizations that serve uninsured consumers and
    publish a site telephone. Organization contacts only — never consumer leads."""
    con = org.connect(ORG_DB)
    r = org.fetch_hrsa_sites(con, state=state)
    print(json.dumps({"adapter": "hrsa_health_centers", **r}, indent=1))
    return 0


def cmd_verify() -> int:
    con = org.connect(ORG_DB)
    print(json.dumps(org.verify_contacts(con), indent=1))
    return 0


def cmd_status() -> int:
    con = org.connect(ORG_DB)
    out = {
        "ladder": {
            org.KIND_DISCOVERED: con.execute("SELECT COUNT(*) FROM org_contacts WHERE record_kind=?", (org.KIND_DISCOVERED,)).fetchone()[0],
            org.KIND_VERIFIED: con.execute("SELECT COUNT(*) FROM org_contacts WHERE record_kind=?", (org.KIND_VERIFIED,)).fetchone()[0],
            org.KIND_PARTNER: con.execute("SELECT COUNT(*) FROM org_contacts WHERE record_kind=?", (org.KIND_PARTNER,)).fetchone()[0],
        },
        "by_source": org.perf_rank(con, limit=50),
    }
    print(json.dumps(out, indent=1, ensure_ascii=False))
    return 0


def cmd_perf() -> int:
    con = org.connect(ORG_DB)
    for r in org.perf_rank(con):
        print(f"{r['source']:<28} fetched={r['fetched'] or 0:<5} accepted={r['accepted'] or 0:<4} "
              f"extracted={r['extracted'] or 0:<4} verified={r['verified'] or 0:<4} "
              f"dupes={r['dupes'] or 0:<3} errors={r['errors'] or 0:<2} {r['restrictions'] or ''}" [:160])
    return 0


def cmd_list(org_type: str | None = None) -> int:
    con = org.connect(ORG_DB)
    q = ("SELECT org_name, org_type, source, source_url, emails, phones, address, record_kind, "
         "verify_status, terms_note FROM org_contacts")
    args = ()
    if org_type:
        q += " WHERE org_type=?"
        args = (org_type,)
    q += " ORDER BY id DESC LIMIT 40"
    for r in con.execute(q, args):
        print(json.dumps(dict(r), ensure_ascii=False)[:400])
    return 0


def cmd_partner_sync() -> int:
    """Feed VERIFIED org contacts into the EXISTING partners table (operator-outreach queue).
    Creates NO outreach, NO link, NO contact — just rows actionable in /admin/partners."""
    org_con = org.connect(ORG_DB)
    lead_con = lead_connect(LEAD_DB)
    added = skipped = 0
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for r in org_con.execute("SELECT * FROM org_contacts WHERE record_kind IN (?,?) ORDER BY id",
                             (org.KIND_VERIFIED, org.KIND_PARTNER)):
        pid = r["org_key"]                      # reuse the stable dedup key as partner_id
        phones = json.loads(r["phones"] or "[]")
        emails = json.loads(r["emails"] or "[]")
        contact = "; ".join([f"phone {p['e164']}" for p in phones]
                            + [f"email {e['value']}" for e in emails])
        hint = (f"[PUBLIC ORG CONTACT from {r['source']}] {contact} — verify at {r['source_url']}. "
                f"Terms: {r['terms_note']}. Outreach requires explicit operator approval.")
        cur = lead_con.execute(
            "INSERT OR IGNORE INTO partners (partner_id, display_name, type, audience, default_angle, "
            "language, status, token, cobrand_approved, evidence, contact_hint, created_at) "
            "VALUES (?,?,?, 'community', NULL, ?, 'DISCOVERED - not contacted', ?, 0, ?, ?, ?)",
            (f"org-{pid}", (r["org_name"] or "")[:100], r["org_type"] or "other",
             r["lang"] or "en", secrets.token_urlsafe(10),
             f"{r['source_url']}", hint, now))
        if cur.rowcount:
            added += 1
        else:
            skipped += 1
    lead_con.commit()
    print(f"partner rows created: {added}, already present: {skipped}")
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "run":
        return cmd_run()
    if cmd == "bulk":
        st = "FL"
        if "--state" in sys.argv:
            i = sys.argv.index("--state")
            st = (sys.argv[i + 1] if i + 1 < len(sys.argv) else "FL").upper()
        return cmd_bulk(st)
    if cmd == "verify":
        return cmd_verify()
    if cmd == "status":
        return cmd_status()
    if cmd == "perf":
        return cmd_perf()
    if cmd == "list":
        return cmd_list(sys.argv[2] if len(sys.argv) > 2 else None)
    if cmd == "partner-sync":
        return cmd_partner_sync()
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
