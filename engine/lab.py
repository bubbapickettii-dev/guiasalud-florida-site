"""Distribution Lab: every acquisition test is an experiment with its own tracking link, so new
channels/angles/partners launch without touching the funnel. Metrics are computed from real events,
leads and agent outcomes - nothing is assumed. Experiments with no real data say so.
"""
from __future__ import annotations

import re

from .db import now

STATUSES = ["PLANNED", "READY", "LIVE", "PAUSED", "DONE", "KILLED"]


def new_id(con) -> str:
    n = con.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] + 1
    while True:
        eid = f"X{n:03d}"
        if not con.execute("SELECT 1 FROM experiments WHERE experiment_id=?", (eid,)).fetchone():
            return eid
        n += 1


def create(con, d: dict) -> str:
    eid = d.get("experiment_id") or new_id(con)
    lang = d.get("language") or ("es" if (d.get("angle") or "").startswith("es_") else "en")
    row = {k: d.get(k) for k in ("name", "campaign", "audience", "angle", "channel", "creative", "source", "medium",
                                 "partner_id", "hypothesis", "start_date", "end_date", "demand_trigger", "content_concept", "offer_cta",
                                 "pages", "opportunity_id")}
    pv = (d.get("phone_variant") or "").strip().upper()
    row["phone_variant"] = pv if len(pv) == 1 and pv.isalpha() else None   # blank = A/B split per visitor
    row.update(experiment_id=eid, language=lang, status=d.get("status") or "PLANNED", cost=float(d.get("cost") or 0),
               assumed_lead_value=float(d["assumed_lead_value"]) if d.get("assumed_lead_value") not in (None, "") else None,
               landing_page=d.get("landing_page") or f"/check?a={d.get('angle') or 'health_check'}", created_at=now())
    con.execute(f"INSERT OR REPLACE INTO experiments ({','.join(row)}) VALUES ({','.join('?' * len(row))})", list(row.values()))
    code = re.sub(r"[^a-z0-9]", "", eid.lower())
    row["tracking_id"] = code
    con.execute("UPDATE experiments SET tracking_id=?, phone_variant=? WHERE experiment_id=?", (code, row["phone_variant"], eid))
    custom_lp = row["landing_page"] if not row["landing_page"].startswith("/check?a=") else None
    camp = row["campaign"] or ("oep-2027-health-es" if lang == "es" else "oep-2027-health")
    con.execute("""INSERT OR REPLACE INTO links (code,campaign,channel,utm_source,utm_medium,utm_campaign,utm_content,partner_id,note,created_at,clicks,experiment_id,angle,lang,landing_page,phone_variant)
                   VALUES (?,?,?,?,?,?,?,?,?,?,COALESCE((SELECT clicks FROM links WHERE code=?),0),?,?,?,?,?)""",
                (code, camp, row["channel"], row["source"], row["medium"], camp, row["creative"] or code, row["partner_id"], row["name"], now(), code, eid,
                 row["angle"], lang, custom_lp, row["phone_variant"]))
    con.commit()
    return eid


def ensure_placement_link(con, pl: dict) -> str:
    """One tracked link per distribution placement (post, group, partner, flyer). Keeps click counts on re-runs."""
    e = con.execute("SELECT * FROM experiments WHERE experiment_id=?", (pl.get("experiment_id"),)).fetchone() if pl.get("experiment_id") else None
    lang = pl.get("lang") or "en"
    camp = (e["campaign"] if e and e["campaign"] else None) or ("oep-2027-health-es" if lang == "es" else "oep-2027-health")
    con.execute("""INSERT INTO links (code,campaign,channel,utm_source,utm_medium,utm_campaign,utm_content,partner_id,note,created_at,clicks,experiment_id,angle,lang,landing_page,phone_variant)
                   VALUES (?,?,?,?,?,?,?,?,?,?,COALESCE((SELECT clicks FROM links WHERE code=?),0),?,?,?,?,NULL)
                   ON CONFLICT(code) DO UPDATE SET utm_source=excluded.utm_source, utm_medium=excluded.utm_medium, landing_page=excluded.landing_page,
                   experiment_id=excluded.experiment_id, note=excluded.note""",
                (pl["code"], camp, pl["channel"], pl["utm_source"], pl["utm_medium"], camp, pl["id"], None, pl["where"][:200], now(), pl["code"],
                 pl.get("experiment_id"), e["angle"] if e else None, lang, pl.get("landing_page")))
    return pl["code"]


DECISION_FIELDS = ("name", "status", "audience", "angle", "landing_page", "pages", "demand_trigger", "content_concept", "learning")


def apply_decision(con, d: dict) -> bool:
    """Apply a recorded operator decision (kill / redirect / merge) to an existing experiment. Idempotent;
    keeps the tracking link alive (old printed/shared links still resolve) but points it at the new landing page."""
    eid = d.get("experiment_id")
    cur = con.execute("SELECT * FROM experiments WHERE experiment_id=?", (eid,)).fetchone()
    if not cur:
        return False
    ch = {k: d[k] for k in DECISION_FIELDS if k in d and d[k] != cur[k]}
    if ch.get("status") and ch["status"] not in STATUSES:
        raise ValueError(f"bad status {ch['status']}")
    if not ch:
        return False
    con.execute(f"UPDATE experiments SET {','.join(k + '=?' for k in ch)} WHERE experiment_id=?", [*ch.values(), eid])
    if "landing_page" in ch or "angle" in ch:
        lp = d.get("landing_page", cur["landing_page"])
        con.execute("UPDATE links SET landing_page=?, angle=? WHERE experiment_id=?",
                    (None if lp.startswith("/check?a=") else lp, d.get("angle", cur["angle"]), eid))
    con.commit()
    return True


def _div(a, b):
    return round(a / b, 2) if b else None


def metrics(con, eid: str) -> dict:
    e = dict(con.execute("SELECT * FROM experiments WHERE experiment_id=?", (eid,)).fetchone())
    q1 = lambda sql, *a: con.execute(sql, a).fetchone()[0] or 0  # noqa: E731
    out = {}
    for real, label in ((0, "real"), (1, "test")):
        m = {}
        m["clicks"] = q1("SELECT COALESCE(SUM(clicks),0) FROM links WHERE experiment_id=?", eid) if label == "real" else None
        m["landing_page_visits"] = q1("SELECT COUNT(DISTINCT visitor_id) FROM events WHERE experiment_id=? AND event='page_view' AND is_test=?", eid, real)
        m["form_starts"] = q1("SELECT COUNT(DISTINCT visitor_id) FROM events WHERE experiment_id=? AND event IN ('tool_start') AND is_test=?", eid, real)
        m["check_completions"] = q1("SELECT COUNT(DISTINCT visitor_id) FROM events WHERE experiment_id=? AND event='tool_complete' AND is_test=?", eid, real)
        m["phone_step_views"] = q1("SELECT COUNT(DISTINCT visitor_id) FROM events WHERE experiment_id=? AND event='phone_step_view' AND is_test=?", eid, real)
        m["phones_submitted"] = q1("SELECT COUNT(DISTINCT visitor_id) FROM events WHERE experiment_id=? AND event='contact_submit' AND step LIKE 'phone_%' AND is_test=?", eid, real)
        m["valid_phones"] = q1("SELECT COUNT(DISTINCT visitor_id) FROM events WHERE experiment_id=? AND event='contact_submit' AND step='phone_valid' AND is_test=?", eid, real)
        m["consented_phone_leads"] = q1("SELECT COUNT(*) FROM leads WHERE experiment_id=? AND is_test=? AND lead_type='PHONE_LEAD' AND phone_valid=1", eid, real)
        m["form_completions"] = q1("SELECT COUNT(*) FROM leads WHERE experiment_id=? AND is_test=?", eid, real)
        m["qualified_leads"] = q1("SELECT COUNT(*) FROM leads WHERE experiment_id=? AND is_test=? AND qual_status='QUALIFIED'", eid, real)
        m["invalid_leads"] = q1("""SELECT COUNT(*) FROM leads WHERE experiment_id=? AND is_test=? AND (qual_status IN ('UNQUALIFIED','DUPLICATE')
                                   OR agent_status IN ('BAD_INFO'))""", eid, real)
        m["delivered"] = q1("SELECT COUNT(*) FROM leads WHERE experiment_id=? AND is_test=? AND routing_status='DELIVERED'", eid, real)
        m["agent_feedback"] = q1("SELECT COUNT(*) FROM leads WHERE experiment_id=? AND is_test=? AND agent_status IS NOT NULL AND agent_status NOT IN ('NEW','VIEWED')", eid, real)
        m["appointments"] = q1("SELECT COUNT(*) FROM leads WHERE experiment_id=? AND is_test=? AND agent_status IN ('APPOINTMENT','SOLD')", eid, real)
        m["sales"] = q1("SELECT COUNT(*) FROM leads WHERE experiment_id=? AND is_test=? AND agent_status='SOLD'", eid, real)
        m["lead_value"] = q1("SELECT COALESCE(SUM(price),0) FROM leads WHERE experiment_id=? AND is_test=? AND routing_status='DELIVERED'", eid, real)
        if label == "real":
            cost = e["cost"] or 0
            m["cost"] = cost
            m["cost_per_lead"] = _div(cost, m["form_completions"])
            m["cost_per_phone_lead"] = _div(cost, m["consented_phone_leads"])
            m["revenue_per_lead"] = _div(m["lead_value"], q1("SELECT COUNT(*) FROM leads WHERE experiment_id=? AND is_test=0 AND routing_status='DELIVERED'", eid))
            m["cost_per_qualified_lead"] = _div(cost, m["qualified_leads"])
            m["roi"] = _div(m["lead_value"] - cost, cost)
            m["visit_to_qualified_pct"] = _div(100 * m["qualified_leads"], m["landing_page_visits"])
        out[label] = m
    r = out["real"]
    evidence = ("VALIDATED" if r["qualified_leads"] > 0 else "LIVE - NO LEADS YET" if (r["landing_page_visits"] or r["clicks"])
                else "TEST DATA ONLY" if out["test"]["form_completions"] > 0 else "UNTESTED")
    return {"experiment": e, "real": r, "test": out["test"], "evidence": evidence}


def all_metrics(con) -> list[dict]:
    return [metrics(con, r[0]) for r in con.execute("SELECT experiment_id FROM experiments ORDER BY experiment_id")]


def audience_status(con, playbook: dict, counts: dict) -> list[dict]:
    """Market-intelligence segments joined to real test results. A segment's 'where to reach / what they search /
    what offer' stays a HYPOTHESIS until a real (non-test) experiment aimed at it produces consented phone leads."""
    out = []
    for s in playbook.get("segments", []):
        exps = [dict(r) for r in con.execute("SELECT experiment_id, name, status, channel FROM experiments WHERE audience=? ORDER BY experiment_id",
                                             (s["segment"],))]
        real = {"visits": 0, "phone_step_views": 0, "consented_phone_leads": 0, "qualified_leads": 0}
        for e in exps:
            m = metrics(con, e["experiment_id"])["real"]
            for k in real:
                real[k] += m.get(k) or 0
        if real["consented_phone_leads"]:
            status = f"SUPPORTED BY DATA - {real['consented_phone_leads']} real consented phone lead(s)"
        elif real["visits"]:
            status = f"TESTING - {real['visits']} real visits, no phone leads yet"
        elif exps:
            status = "HYPOTHESIS - test planned, no real traffic yet"
        else:
            status = "HYPOTHESIS - no experiment yet"
        out.append({**s, "signals": counts.get("segments", {}).get(s["segment"], {}), "experiments_linked": exps, "real": real, "evidence": status})
    return out
