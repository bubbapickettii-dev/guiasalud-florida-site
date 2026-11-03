"""Opportunity discovery layer: researched acquisition theses, scored, linked to experiments.

Status ladder: HYPOTHESIS -> RESEARCHED -> READY TO TEST -> TESTING -> PROVEN / DISPROVEN.
Research can move an opportunity up to READY TO TEST. Only REAL consumer behavior moves it further:
- TESTING: a linked experiment has real (non-test) visits
- PROVEN: linked experiments produced >= 3 real consented phone leads AND an agent reported >= 1 as contacted/appointment/sold
- DISPROVEN: set by a human after the experiment's read point (with the result written down) - never automatic
"""
from __future__ import annotations

import json
from pathlib import Path

from . import lab
from .db import now

ORDER = ["HYPOTHESIS", "RESEARCHED", "READY TO TEST", "TESTING", "PROVEN", "DISPROVEN"]
PROVEN_MIN_LEADS = 3


def load(cfg_dir: Path) -> dict:
    p = cfg_dir / "opportunities.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"opportunities": [], "weights": {}, "criteria": []}


def ensure_table(con):
    con.execute("""CREATE TABLE IF NOT EXISTS opportunity_status (opp_id TEXT PRIMARY KEY, status TEXT, result TEXT, updated_at TEXT)""")


def evaluate(con, cfg_dir: Path) -> list[dict]:
    ensure_table(con)
    data = load(cfg_dir)
    manual = {r["opp_id"]: dict(r) for r in con.execute("SELECT * FROM opportunity_status")}
    out = []
    for o in data["opportunities"]:
        o = dict(o)
        real = {"visits": 0, "phone_step_views": 0, "consented_phone_leads": 0, "qualified_leads": 0, "contacted": 0}
        linked = []
        for eid in o.get("experiments", []):
            if not con.execute("SELECT 1 FROM experiments WHERE experiment_id=?", (eid,)).fetchone():
                continue
            m = lab.metrics(con, eid)
            linked.append({"id": eid, "status": m["experiment"]["status"], "evidence": m["evidence"]})
            r = m["real"]
            real["visits"] += r["landing_page_visits"] or 0
            real["phone_step_views"] += r["phone_step_views"] or 0
            real["consented_phone_leads"] += r["consented_phone_leads"] or 0
            real["qualified_leads"] += r["qualified_leads"] or 0
            real["contacted"] += con.execute("""SELECT COUNT(*) FROM leads WHERE experiment_id=? AND is_test=0
                AND agent_status IN ('CONTACTED','APPOINTMENT','SOLD')""", (eid,)).fetchone()[0]
        status, basis = o["status"], "research"
        if real["consented_phone_leads"] >= PROVEN_MIN_LEADS and real["contacted"] >= 1:
            status, basis = "PROVEN", "real consumer behavior"
        elif real["visits"] > 0:
            status, basis = "TESTING", "real traffic on a linked experiment"
        m_ = manual.get(o["id"])
        if m_ and m_["status"] == "DISPROVEN":
            status, basis = "DISPROVEN", "human decision after read point"
            o["result"] = m_.get("result") or o.get("result", "")
        elif m_ and m_["status"] in ORDER and ORDER.index(m_["status"]) > ORDER.index(status) and m_["status"] != "PROVEN":
            status, basis = m_["status"], "manual"
        o.update(status_now=status, status_basis=basis, real=real, linked=linked)
        out.append(o)
    return out


def set_status(con, opp_id: str, status: str, result: str = ""):
    ensure_table(con)
    if status == "PROVEN":
        raise ValueError("PROVEN can only come from real consumer behavior")
    con.execute("INSERT OR REPLACE INTO opportunity_status VALUES (?,?,?,?)", (opp_id, status, result, now()))
    con.commit()
