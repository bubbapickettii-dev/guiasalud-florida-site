"""Optimization engine: which sources / campaigns / partners / languages / audiences / experiments produce
leads that agents actually contact, book and close. Driven by agent statuses in the buyer desk."""
from __future__ import annotations

DIMENSIONS = {"source": "COALESCE(utm_source,'(direct)')", "campaign": "campaign", "partner": "COALESCE(partner_id,'-')",
              "language": "COALESCE(lang,'en')", "audience": "COALESCE(need_path, '-')", "angle": "COALESCE(angle,'-')",
              "experiment": "COALESCE(experiment_id,'-')", "buyer": "COALESCE(buyer_id,'-')"}


def by(con, dim: str, test: str = "real") -> list[dict]:
    expr = DIMENSIONS.get(dim, DIMENSIONS["source"])
    w = "is_test=0" if test == "real" else "is_test=1" if test == "only" else "1=1"
    rows = con.execute(f"""
      SELECT {expr} k, COUNT(*) submitted, SUM(qual_status='QUALIFIED') qualified,
        SUM(routing_status='DELIVERED') delivered,
        SUM(agent_status IN ('CONTACTED','APPOINTMENT','SOLD','NOT_QUALIFIED')) contacted,
        SUM(agent_status IN ('APPOINTMENT','SOLD')) appointments, SUM(agent_status='SOLD') sold,
        SUM(agent_status='BAD_INFO') bad_info, SUM(agent_status='NOT_QUALIFIED') not_qualified,
        SUM(agent_status IS NOT NULL AND agent_status NOT IN ('NEW','VIEWED')) with_feedback,
        COALESCE(SUM(CASE WHEN routing_status='DELIVERED' THEN price END),0) value
      FROM leads WHERE {w} GROUP BY 1 ORDER BY delivered DESC, submitted DESC""").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        dv = d["delivered"] or 0
        fb = d["with_feedback"] or 0
        d["qualified_rate"] = round(100 * (d["qualified"] or 0) / d["submitted"], 1) if d["submitted"] else None
        d["contact_rate"] = round(100 * (d["contacted"] or 0) / fb, 1) if fb else None
        d["appointment_rate"] = round(100 * (d["appointments"] or 0) / fb, 1) if fb else None
        d["close_rate"] = round(100 * (d["sold"] or 0) / fb, 1) if fb else None
        d["bad_info_rate"] = round(100 * (d["bad_info"] or 0) / fb, 1) if fb else None
        d["value_per_lead"] = round(d["value"] / dv, 2) if dv else None
        d["feedback_coverage"] = round(100 * fb / dv, 1) if dv else None
        out.append(d)
    if dim == "experiment":
        costs = {r[0]: r[1] for r in con.execute("SELECT experiment_id, cost FROM experiments")}
        for d in out:
            c = costs.get(d["k"])
            d["cost"] = c
            d["cost_per_qualified"] = round(c / d["qualified"], 2) if c and d["qualified"] else None
    return out
