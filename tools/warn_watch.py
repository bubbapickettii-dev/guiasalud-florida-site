"""WARN watcher: turns new Florida WARN notices into DRAFT employer pages for the job-loss engine.

Public, company-level data only (employer, city, county, # affected, dates). Never individuals.
Florida's WARN database (FloridaCommerce / reactwarn.floridajobs.org) isn't reachable from the build sandbox,
so this reads a CSV export instead. Run it wherever the CSV can be downloaded (the server or your PC):

    python tools/warn_watch.py path/to/warn_export.csv          # adds drafts to config/layoffs.json
    python tools/warn_watch.py export.csv --min 50 --days 90     # only notices >= 50 workers, effective within 90 days

Column names are matched loosely (Company/Employer, City, County, Employees/Affected, Notice Date, Layoff/Effective Date).
New pages are added with status "draft"; review them at /layoffs/<slug>?token=ADMIN_TOKEN and set status "ready" to publish.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

CFG = Path(__file__).resolve().parents[1] / "config" / "layoffs.json"
ALIASES = {"company": ["company", "employer", "company name", "employer name", "business"], "city": ["city", "location", "worksite city"],
           "county": ["county", "region"], "employees": ["employees", "affected", "employees affected", "number affected", "no. of employees", "workers"],
           "notice_date": ["notice date", "date received", "received", "notice"], "effective_date": ["layoff date", "effective date", "separation date", "date of layoff", "layoff start"]}


def _col(headers, key):
    norm = {h.strip().lower(): h for h in headers}
    for a in ALIASES[key]:
        if a in norm:
            return norm[a]
    return next((h for h in headers if any(a in h.lower() for a in ALIASES[key])), None)


def _date(v):
    v = (v or "").strip().split(" ")[0]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            pass
    return None


def slugify(*parts):
    return re.sub(r"[^a-z0-9]+", "-", " ".join(p for p in parts if p).lower()).strip("-")[:70]


def run(csv_path: Path, min_workers=25, days=120, today: date | None = None, cfg: Path = CFG) -> list[dict]:
    today = today or date.today()
    data = json.loads(cfg.read_text()) if cfg.exists() else {"events": []}
    have = {e["slug"] for e in data["events"]}
    added = []
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        rd = csv.DictReader(fh)
        cols = {k: _col(rd.fieldnames or [], k) for k in ALIASES}
        if not cols["company"] or not cols["effective_date"]:
            raise SystemExit(f"Can't find company/effective-date columns in {rd.fieldnames}")
        for r in rd:
            company = (r.get(cols["company"]) or "").strip()
            eff = _date(r.get(cols["effective_date"]))
            n = int(re.sub(r"\D", "", r.get(cols["employees"]) or "0") or 0) if cols["employees"] else 0
            if not company or not eff or n < min_workers or not (today - timedelta(days=60) <= eff <= today + timedelta(days=days)):
                continue
            city = (r.get(cols["city"]) or "").strip().title() if cols["city"] else ""
            slug = slugify(company, city, str(eff.year))
            if slug in have:
                continue
            ev = {"slug": slug, "company": company.title() if company.isupper() else company, "city": city or "Florida",
                  "county": (r.get(cols["county"]) or "").strip().title() if cols["county"] else "", "employees": n,
                  "effective_date": eff.isoformat(), "detail": f"WARN notice: {n} workers", "source": "https://floridajobs.org/office-directory/division-of-workforce-services/workforce-programs/reemployment-and-emergency-assistance-coordination-team-react/warn-notices",
                  "source_label": "FloridaCommerce WARN notices", "status": "draft", "added": today.isoformat()}
            data["events"].append(ev); have.add(slug); added.append(ev)
    cfg.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    return added


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("csv"); ap.add_argument("--min", type=int, default=25); ap.add_argument("--days", type=int, default=120)
    a = ap.parse_args()
    for e in run(Path(a.csv), a.min, a.days):
        print("DRAFT", e["slug"], e["employees"], e["effective_date"])
