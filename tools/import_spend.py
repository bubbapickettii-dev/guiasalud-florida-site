"""Import ad spend so every engine's cost per lead is real, not guessed.

Accepts either:
  - a Meta Ads Manager export (CSV with "Campaign name", "Amount spent (USD)" and "Day" or "Reporting starts"), or
  - our simple CSV: day,channel,campaign_name,amount[,experiment_id][,category]

Naming convention that links spend to experiments: start every ad campaign name with the experiment id,
e.g. "X022 | ES carrier exit | Miami-Dade". Category is read from the name (health/life) or the experiment.

  python tools/import_spend.py path/to/export.csv [--channel paid_meta]

Re-importing the same day+campaign replaces the old row (no double counting). Experiment cost totals are
updated so the Lab shows cost per lead too.
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine.db import connect, now  # noqa: E402

ENGINE = {"paid_meta": "PAID", "paid_other": "PAID", "partner": "PARTNER", "social_community": "ORGANIC", "search": "ORGANIC"}


def _num(s: str) -> float:
    return float(re.sub(r"[^0-9.\-]", "", s or "0") or 0)


def run(path: str, channel: str = "paid_meta", db_path: str | None = None) -> dict:
    root = Path(__file__).resolve().parents[1]
    con = connect(db_path or root / "data" / "leads.db")
    rows = list(csv.DictReader(open(path, newline="", encoding="utf-8-sig")))
    n, total = 0, 0.0
    for r in rows:
        name = r.get("Campaign name") or r.get("campaign_name") or ""
        amt = _num(r.get("Amount spent (USD)") or r.get("amount") or "0")
        day = (r.get("Day") or r.get("Reporting starts") or r.get("day") or "")[:10]
        if not name or not day or amt == 0:
            continue
        ch = r.get("channel") or channel
        m = re.match(r"\s*(X\d{3})\b", name)
        eid = r.get("experiment_id") or (m.group(1) if m else None)
        cat = r.get("category") or ("life" if re.search(r"\blife\b|vida|final expense|mortgage", name, re.I) else "health")
        con.execute("""INSERT INTO spend (day, channel, engine, category, experiment_id, campaign_name, amount, source, imported_at)
                       VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(day, channel, campaign_name) DO UPDATE SET amount=excluded.amount,
                       experiment_id=excluded.experiment_id, category=excluded.category, imported_at=excluded.imported_at""",
                    (day, ch, ENGINE.get(ch, "PAID"), cat, eid, name, amt, Path(path).name, now()))
        n += 1
        total += amt
    for (eid, s) in con.execute("SELECT experiment_id, SUM(amount) FROM spend WHERE experiment_id IS NOT NULL GROUP BY 1").fetchall():
        con.execute("UPDATE experiments SET cost=? WHERE experiment_id=?", (round(s, 2), eid))
    con.commit()
    return {"rows": n, "amount": round(total, 2)}


if __name__ == "__main__":
    a = sys.argv[1:]
    ch = a[a.index("--channel") + 1] if "--channel" in a else "paid_meta"
    print(run(a[0], ch))
