"""PROSPECTS CLI: operator workflow for the BUYER-ACQUISITION prospect registry.

Business discovery only. Rows are public business records (data/prospects.db); nothing
here contacts anyone, activates a buyer, or touches the consumer lead database. The
pros registry is intentionally separate from data/signals.db and data/leads.db.

  python tools/prospects.py list [DECISION]      print prospects (NEW by default)
  python tools/prospects.py decide ID STATE 'n'  decision: NEW|UNDER_REVIEW|PRIORITIZED|
                                                 NOT_A_FIT|ARCHIVED|CONTACTS_AGREEMENT_DRAFTED
  python tools/prospects.py verify ID STATE 'n'  verify: UNVERIFIED|PUBLIC_DETAILS_CHECKED|
                                                 OPERATOR_VERIFIED|DENIED (operator record,
                                                 named source required; NEVER auto-derived)
  python tools/prospects.py import csv --file f --source manual
                                                 import operator-supplied public records.
                                                 Required CSV column: name; optional:
                                                 category phone website address city state zip5
  python tools/prospects.py import geoleads --file export.csv [--source geoleadscraper]
                                                 translate a GeoLeadScraper export (CSV or JSON)
                                                 into the same rows: title->name,
                                                 categories->category, address->city/state/zip5,
                                                 maps_url->evidence. Emails/socials in the
                                                 export are deliberately dropped (business
                                                 identity only). Source caveat: the export is
                                                 Maps-derived — docs/GEOLEADSCRAPER_REVIEW.md.
  python tools/prospects.py places [--file queries.json | --query "text"]
                                                 Google Places sweep (official, billed per request).
                                                 Refuses with the exact missing configuration unless
                                                 GOOGLE_PLACES_API_KEY is set AND sources.json >
                                                 places > approved: true — no API call happens otherwise.
  python tools/prospects.py status               counts by decision + verification
  python tools/prospects.py lbt [--limit N]      Miami-Dade Local Business Tax receipts (official county
                                                 open data): business name, published OWNERNAME,
                                                 address, city, ZIP, county activity class.
  python tools/prospects.py npi [--max N]        NPPES org practices in FL cities: practice name,
                                                 authorized official (registered decision-maker),
                                                 published practice telephone, taxonomy.
  python tools/prospects.py npi-individuals [--max N] [--cities "A,B"] [--all-cities] [--all-taxonomies]
                                                 NPPES INDIVIDUAL providers (NPI-1) in the same FL
                                                 cities: solo/self-employed professionals, name +
                                                 published practice telephone + taxonomy.
  python tools/prospects.py osm [--area miami-dade|broward|palm-beach] [--limit N] [--extended]
                                                 OpenStreetMap business POIs that publish a phone
                                                 or email (Overpass, ODbL, no key). Chains skipped.
                                                 --extended sweeps the long tail of shops, trades
                                                 and professional offices (second pass).
  python tools/prospects.py match                attach a phone to a phone-less record only when
                                                 another record proves it (same name + same ZIP).
  python tools/prospects.py enrich [--limit N] [--source s] [--workers N] [--newest]
                                                 fill biz_email from each business's OWN website
                                                 (robots.txt honoured; never guessed).
                                                 --workers N fetches sites concurrently.
  python tools/prospects.py stats                measurable acquisition state: phones, emails,
                                                 named decision-makers, records missing contact.
  python tools/prospects.py export --out FILE [--limit N] [--require-phone]
                                                 write the operator's working list as CSV (only
                                                 rows with a real contact channel, named first).
  python tools/prospects.py import-worklist [--dir DIR] [--dry-run]
                                                 import the verified worklist INTO this registry:
                                                 stamps each row with its segment, audience,
                                                 priority, phone evidence and outreach status,
                                                 keyed by record id. Never adds rows, never
                                                 touches contacts/provenance, never contacts
                                                 anyone, never writes consumer leads.
  python tools/prospects.py segments [--out DIR]
                                                 the segmented working list: named owner+phone,
                                                 phone without owner, email-only, plus the
                                                 excluded rows (duplicate / incomplete /
                                                 irrelevant) and SUMMARY.md. Every registry row
                                                 falls in exactly one bucket; counts reconcile.

Google Places (official, billed): engine/prospects.fetch_places() runs ONLY with
GOOGLE_PLACES_API_KEY set AND config/sources.json > places > approved: true. Nothing in
this CLI triggers billing. google-maps-scraper-kit is deliberately NOT integrated
(headless scraping of Google Maps = against Google's ToS) — documented in
engine/sources.py > PROSPECT_SOURCES.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import prospects as pr        # noqa: E402

DB = Path(os.environ.get("PROSPECTS_DB", ROOT / "data" / "prospects.db"))   # same override app.py uses


def _con():
    return pr.connect(DB)


def cmd_list(con, decision: str) -> None:
    rows = pr.queue(con, decision=decision, limit=100)
    print(f"{decision or 'ALL'}: {len(rows)}")
    for r in rows:
        loc = ", ".join(x for x in (r["city"], r["state"], r["zip5"]) if x)
        print(f"\n#{r['id']} [{r['decision']}/{r['verify_state']}] {r['name']}  ({r['source']})")
        print(f"  {r['category'] or '-'} · {loc or '-'}")
        if r["website"]:
            print(f"  {r['website']}")
        if r["evidence_url"]:
            print(f"  evidence: {r['evidence_url']}")


def cmd_status(con) -> None:
    import json
    print(json.dumps(pr.counts(con), indent=1))


def cmd_places(con, vals: list[str]) -> None:
    """Run the Google Places adapter against the existing prospect registry. Every refusal
    names the exact missing configuration; success prints what was fetched/added/skipped
    plus the per-query outcome trail recorded in prospects.collect_log."""
    if "--file" in vals:
        path = Path(vals[vals.index("--file") + 1])
        try:
            queries = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            sys.exit(f"can't read {path}: {e}")
        if isinstance(queries, dict):
            queries = queries.get("queries", [])
        if not isinstance(queries, list) or not all(isinstance(q, dict) for q in queries):
            sys.exit("--file must hold a JSON list of {\"text_query\": \"...\"} objects")
    elif "--query" in vals:
        queries = [{"text_query": vals[vals.index("--query") + 1]}]
    else:
        sys.exit('usage: places --file queries.json   or   places --query "insurance agencies in Miami, FL"')
    try:
        res = pr.fetch_places(con, queries)
    except NotImplementedError as e:
        print("PLACES: NOT RUN -- blocked by configuration (honest state, no call made):")
        print(f"  {e}")
        sys.exit(2)
    print(f"places: fetched={res['fetched']} added={res['added']} updated={res['updated']} "
          f"skipped={res['skipped']} errors={res['errors']}")


def main() -> None:
    a = sys.argv[1:]
    con = _con()
    try:
        if not a or a[0] == "status":
            cmd_status(con)
        elif a[0] == "list":
            cmd_list(con, a[1] if len(a) > 1 else "")
        elif a[0] == "decide":
            if len(a) < 4 or a[2] not in pr.DECISIONS or not a[3].strip():
                sys.exit('usage: decide ID STATE "note"  (one of: ' + ", ".join(pr.DECISIONS) + ")")
            if not pr.decide(con, int(a[1]), a[2], a[3]):
                sys.exit(f"no prospect #{a[1]}")
            print("recorded")
        elif a[0] == "verify":
            if len(a) < 4 or a[2] not in pr.VERIFY_STATES or not a[3].strip():
                sys.exit('usage: verify ID STATE "named source / how checked"  (one of: '
                         + ", ".join(pr.VERIFY_STATES) + ")")
            if not pr.verify(con, int(a[1]), a[2], a[3]):
                sys.exit(f"no prospect #{a[1]}")
            print("recorded (verification never activates a buyer)")
        elif a[0] == "places":
            cmd_places(con, a[1:])
        elif a[0] == "lbt":
            lim = int(a[a.index("--limit") + 1]) if "--limit" in a else 2000
            res = pr.fetch_mdc_lbt(con, limit=lim)
            print(json.dumps(res, indent=1))
        elif a[0] == "npi":
            mx = int(a[a.index("--max") + 1]) if "--max" in a else 1500
            cities = pr.NPI_CITIES[:12] if "--all-cities" not in a else pr.NPI_CITIES
            taxs = pr.NPI_TAXONOMIES[:8] if "--all-taxonomies" not in a else pr.NPI_TAXONOMIES
            res = pr.fetch_npi_practices(con, cities=cities, taxonomies=taxs, max_rows=mx)
            print(json.dumps(res, indent=1))
        elif a[0] == "npi-individuals":
            mx = int(a[a.index("--max") + 1]) if "--max" in a else 3000
            if "--cities" in a:
                cities = tuple(c.strip() for c in a[a.index("--cities") + 1].split(",") if c.strip())
            else:
                cities = pr.NPI_CITIES if "--all-cities" in a else pr.NPI_CITIES[:12]
            taxs = (pr.NPI_INDIVIDUAL_TAXONOMIES if "--all-taxonomies" in a
                    else pr.NPI_INDIVIDUAL_TAXONOMIES[:8])
            res = pr.fetch_npi_individuals(con, cities=cities, taxonomies=taxs, max_rows=mx)
            print(json.dumps(res, indent=1))
        elif a[0] == "osm":
            lim = int(a[a.index("--limit") + 1]) if "--limit" in a else 8000
            areas = pr.OSM_BBOXES
            if "--area" in a:
                one = a[a.index("--area") + 1]
                if one not in pr.OSM_BBOXES:
                    sys.exit("areas: " + ", ".join(pr.OSM_BBOXES))
                areas = {one: pr.OSM_BBOXES[one]}
            if "--extended" in a:
                print(json.dumps(pr.fetch_osm_businesses(
                    con, areas=areas, limit=lim, selectors=pr.OSM_SELECTORS_EXT,
                    tag="extended"), indent=1))
            else:
                print(json.dumps(pr.fetch_osm_businesses(con, areas=areas, limit=lim), indent=1))
        elif a[0] == "match":
            print(json.dumps(pr.match_phones(con), indent=1))
        elif a[0] == "enrich":
            lim = int(a[a.index("--limit") + 1]) if "--limit" in a else 50
            src = a[a.index("--source") + 1] if "--source" in a else ""
            wrk = int(a[a.index("--workers") + 1]) if "--workers" in a else 1
            print(json.dumps(pr.enrich_emails(con, limit=lim, source=src, workers=wrk,
                                              newest="--newest" in a), indent=1))
        elif a[0] == "stats":
            print(json.dumps(pr.acquisition_stats(con), indent=1))
        elif a[0] == "import-worklist":
            outdir = Path(a[a.index("--dir") + 1]) if "--dir" in a else (
                ROOT / "out" / "prospect_worklist_2026-11-02")
            res = pr.import_worklist_dir(con, outdir, dry_run="--dry-run" in a)
            print(json.dumps(res, indent=1))
        elif a[0] == "segments":
            outdir = Path(a[a.index("--out") + 1]) if "--out" in a else (
                ROOT / "out" / "prospect_worklist_2026-11-02")
            res = pr.export_worklist(con, outdir)
            print(json.dumps(res["written"], indent=1))
            print(json.dumps({k: v for k, v in res["stats"].items()
                              if k not in ("by_industry", "by_county")}, indent=1))
            print(f"written to {res['outdir']}")
        elif a[0] == "export":
            if "--out" not in a:
                sys.exit("usage: export --out FILE.csv [--limit N] [--require-phone]")
            out = Path(a[a.index("--out") + 1])
            lim = int(a[a.index("--limit") + 1]) if "--limit" in a else 0
            print(json.dumps(pr.export_usable(con, out, limit=lim,
                                              require_phone="--require-phone" in a), indent=1))
        elif a[0] == "import":
            kind = a[1] if len(a) > 1 else "csv"
            if kind not in ("csv", "geoleads"):
                sys.exit("usage: import csv|geoleads --file results.csv [--source manual]")
            vals = a[2:]
            path = Path(vals[vals.index("--file") + 1]) if "--file" in vals else None
            default_src = "geoleadscraper" if kind == "geoleads" else "manual"
            src = vals[vals.index("--source") + 1] if "--source" in vals else default_src
            if not path or not path.exists():
                sys.exit("--file must name an existing CSV/JSON export")
            if kind == "geoleads":
                res = pr.import_geoleads_csv(con, path, src)
                print(f"added {res['added']}, updated {res['updated']}, skipped {res['skipped']} "
                      f"(mapped {res['mapped']} export rows -> business identity only)")
            else:
                res = pr.import_csv(con, path, src)
                print(f"added {res['added']}, updated {res['updated']}, skipped {res['skipped']}")
        else:
            sys.exit(__doc__)
    finally:
        con.close()


if __name__ == "__main__":
    main()
