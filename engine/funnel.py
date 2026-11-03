"""The business funnel - the KPIs this company optimizes, in order:

visits -> form starts -> Check completions -> phones submitted -> valid phones -> consented phone leads
-> qualified -> delivered -> contacted by agent -> appointments -> sales -> revenue per lead

Not KPIs here: number of public records, public phones or emails, or anything about contacting people.
Visits/starts/completions/phone steps come from first-party events; everything after comes from the lead
table and agent statuses. Phone steps are logged as pass/fail only - a phone typed without consent is
never stored. Real and TEST traffic are always separated.
"""
from __future__ import annotations

from . import channels as ch

EV_DIM = {"source": "COALESCE(e.utm_source, v.first_utm_source, '(direct)')", "experiment": "COALESCE(e.experiment_id, '-')",
          "angle": "COALESCE(e.angle, '-')", "language": "COALESCE(e.lang, 'en')", "partner": "COALESCE(e.partner_id, v.partner_id, '-')",
          "campaign": "COALESCE(e.campaign, '-')", "landing": "COALESCE(e.entry_page, v.entry_page, '-')",
          "phone_variant": "COALESCE(e.phone_variant, v.phone_variant, '-')", "all": "'all'",
          "channel": ch.EV_CHANNEL, "engine": ch.engine_sql(ch.EV_CHANNEL),
          "category": ch.category_event_sql("e.campaign", "COALESCE(e.entry_page, v.entry_page)")}
LD_DIM = {"source": "COALESCE(utm_source, '(direct)')", "experiment": "COALESCE(experiment_id, '-')", "angle": "COALESCE(angle, '-')",
          "language": "COALESCE(lang, 'en')", "partner": "COALESCE(partner_id, '-')", "campaign": "COALESCE(campaign, '-')", "landing": "COALESCE(entry_page, '-')",
          "phone_variant": "COALESCE(phone_variant, '-')", "all": "'all'",
          "channel": ch.LD_CHANNEL, "engine": ch.engine_sql(ch.LD_CHANNEL), "category": ch.category_lead_sql()}
STAGES = ["visits", "form_starts", "check_completions", "phone_step_views", "phones_submitted", "valid_phones", "consented_phone_leads", "qualified",
          "routing_eligible", "delivered", "accepted", "contacted", "appointments", "sales"]
# Development, QA and owner traffic arrives over loopback or a private LAN address. Those are not
# consumers, so they are excluded from every number instead of inflating "real" visits. The rule is
# computed from the recorded URL, not from a flag somebody has to remember to set.
_INTERNAL_URL = ("(COALESCE(e.page_url,'') LIKE '%//localhost%' OR COALESCE(e.page_url,'') LIKE '%//127.0.0.1%' "
                 "OR COALESCE(e.page_url,'') LIKE '%//192.168.%' OR COALESCE(e.page_url,'') LIKE '%//10.%' "
                 "OR COALESCE(v.landing_url,'') LIKE '%//localhost%' OR COALESCE(v.landing_url,'') LIKE '%//127.0.0.1%' "
                 "OR COALESCE(v.landing_url,'') LIKE '%//192.168.%' OR COALESCE(v.landing_url,'') LIKE '%//10.%')")
_INTERNAL_LANDING = ("(COALESCE(landing_url,'') LIKE '%//localhost%' OR COALESCE(landing_url,'') LIKE '%//127.0.0.1%' "
                     "OR COALESCE(landing_url,'') LIKE '%//192.168.%' OR COALESCE(landing_url,'') LIKE '%//10.%')")

RETURN_STATUSES = ("RETURNED", "BAD_INFO", "DNC")
SPEND_KEY = {"channel": "channel", "engine": "engine", "category": "category", "experiment": "COALESCE(experiment_id, '-')",
             "campaign": "campaign_name", "all": "'all'"}     # agent says the lead isn't usable -> not accepted, revenue credited back
LABELS = {"visits": "Visits", "form_starts": "Form starts", "check_completions": "Check completed", "phone_step_views": "Phone step viewed", "phones_submitted": "Phones submitted",
          "valid_phones": "Valid phones", "consented_phone_leads": "Consented phone leads", "qualified": "Qualified",
          "routing_eligible": "Routing-eligible",
          "delivered": "Delivered to agent", "accepted": "Accepted by agent", "contacted": "Contacted by agent", "appointments": "Appointments", "sales": "Sales"}


def by(con, dim: str = "all", test: str = "real", experiment: str | None = None, external_only: bool = False) -> list[dict]:
    """Raw funnel counts. external_only=True additionally drops rows whose landing/event URL is a
    development origin (localhost/127.0.0.1/private ranges) — the client-facing arrival report uses it
    so a developer opening the staging build never shows up as a consumer."""
    dim = dim if dim in EV_DIM else "all"
    t = 0 if test == "real" else 1
    _ex_ev = f"AND NOT {_INTERNAL_URL}" if external_only else ""
    _ex_ld = f"AND NOT {_INTERNAL_LANDING}" if external_only else ""
    ev = con.execute(f"""
      SELECT {EV_DIM[dim]} k,
        COUNT(DISTINCT CASE WHEN e.event='page_view' THEN e.visitor_id END) visits,
        COUNT(DISTINCT CASE WHEN e.event='tool_start' THEN e.visitor_id END) form_starts,
        COUNT(DISTINCT CASE WHEN e.event='tool_complete' THEN e.visitor_id END) check_completions,
        COUNT(DISTINCT CASE WHEN e.event='phone_step_view' THEN e.visitor_id END) phone_step_views,
        COUNT(DISTINCT CASE WHEN e.event='contact_submit' AND e.step LIKE 'phone_%' THEN e.visitor_id END) phones_submitted,
        COUNT(DISTINCT CASE WHEN e.event='contact_submit' AND e.step='phone_valid' THEN e.visitor_id END) valid_phones
      FROM events e LEFT JOIN visitors v ON v.visitor_id = e.visitor_id
      WHERE e.is_test = ? {_ex_ev} {"AND e.experiment_id = ?" if experiment else ""} GROUP BY 1""", (t, experiment) if experiment else (t,)).fetchall()
    ld = con.execute(f"""
      SELECT {LD_DIM[dim]} k,
        SUM(lead_type='PHONE_LEAD' AND phone_valid=1) consented_phone_leads,
        SUM(lead_type='EMAIL_ONLY_INQUIRY') email_only_inquiries,
        SUM(qual_status='QUALIFIED') qualified,
        SUM(qual_status='QUALIFIED' AND routing_status<>'NOT_ROUTABLE') routing_eligible,
        SUM(routing_status='DELIVERED') delivered,
        SUM(routing_status='DELIVERED' AND COALESCE(agent_status,'NEW') NOT IN ('RETURNED','BAD_INFO','DNC')) accepted,
        SUM(routing_status='DELIVERED' AND agent_status IN ('RETURNED','BAD_INFO','DNC')) returned,
        SUM(qual_status='DUPLICATE') duplicates,
        SUM(agent_status IN ('CONTACTED','APPOINTMENT','SOLD','NOT_QUALIFIED')) contacted,
        SUM(agent_status IN ('APPOINTMENT','SOLD')) appointments,
        SUM(agent_status='SOLD') sales,
        SUM(agent_status IN ('BAD_INFO')) bad_info,
        COALESCE(SUM(CASE WHEN routing_status='DELIVERED' AND COALESCE(agent_status,'NEW') NOT IN ('RETURNED','BAD_INFO','DNC') THEN price END), 0) revenue
      FROM leads WHERE is_test = ? {_ex_ld} {"AND experiment_id = ?" if experiment else ""} GROUP BY 1""", (t, experiment) if experiment else (t,)).fetchall()
    rows: dict[str, dict] = {}
    for r in ev:
        rows.setdefault(r["k"], {"k": r["k"]}).update({k: r[k] or 0 for k in r.keys() if k != "k"})
    for r in ld:
        rows.setdefault(r["k"], {"k": r["k"]}).update({k: r[k] or 0 for k in r.keys() if k != "k"})
    if t == 0 and dim in SPEND_KEY:            # ad spend is always real money; TEST views never carry spend
        try:
            for r in con.execute(f"SELECT {SPEND_KEY[dim]} k, SUM(amount) amt FROM spend {'WHERE experiment_id = ?' if experiment else ''} GROUP BY 1",
                                 (experiment,) if experiment else ()):
                rows.setdefault(r["k"], {"k": r["k"]})["spend"] = round(r["amt"] or 0, 2)
        except Exception:  # noqa: BLE001 - older DB without the spend table
            pass
    out = []
    for d in rows.values():
        for s in STAGES + ["email_only_inquiries", "bad_info", "returned", "duplicates", "revenue", "spend"]:
            d.setdefault(s, 0)
        v, dl = d["visits"], d["delivered"]
        d["visit_to_phone_lead_pct"] = round(100 * d["consented_phone_leads"] / v, 1) if v else None
        d["completion_to_phone_pct"] = round(100 * d["phones_submitted"] / d["check_completions"], 1) if d["check_completions"] else None
        d["phone_valid_pct"] = round(100 * d["valid_phones"] / d["phones_submitted"], 1) if d["phones_submitted"] else None
        d["revenue_per_lead"] = round(d["revenue"] / dl, 2) if dl else None
        d["contact_rate_pct"] = round(100 * d["contacted"] / dl, 1) if dl else None
        d["acceptance_pct"] = round(100 * d["accepted"] / dl, 1) if dl else None
        d["cost_per_lead"] = round(d["spend"] / d["consented_phone_leads"], 2) if d["spend"] and d["consented_phone_leads"] else None
        d["cost_per_accepted"] = round(d["spend"] / d["accepted"], 2) if d["spend"] and d["accepted"] else None
        d["profit"] = round(d["revenue"] - d["spend"], 2)
        out.append(d)
    out.sort(key=lambda d: (-d["consented_phone_leads"], -d["visits"]))
    return out


def evidence(row: dict, test: str) -> str:
    if test != "real":
        return "TEST DATA"
    if row.get("qualified"):
        return "VALIDATED"
    return "LIVE - NO LEADS YET" if row.get("visits") else "UNTESTED"


STAGE_KEYS = ["visits", "form_starts", "check_completions", "phone_step_views", "phones_submitted",
              "valid_phones", "consented_phone_leads", "qualified", "routing_eligible", "delivered",
              "accepted", "contacted", "appointments", "sales"]


def stage_totals(rows: list[dict]) -> dict:
    """Aggregate one dim's funnel rows into a single stage-total dict (sums, spend kept out)."""
    tot = {k: 0 for k in STAGE_KEYS}
    for r in rows:
        for k in STAGE_KEYS:
            tot[k] += r.get(k) or 0
    return tot


def stop_points(tot: dict) -> list[dict]:
    """Where people stop: the consecutive leak table over ONE aggregate funnel. Each row is a real
    transition between recorded stage counts (unique visitors crossing both stages) — no conversion
    rate is invented for a transition whose upper stage never happened. This is the operator-facing
    answer to 'where do visitors abandon?' without any new tracking.
    """
    out = []
    for a, b in zip(STAGE_KEYS, STAGE_KEYS[1:]):
        up = tot.get(a, 0)
        dn = tot.get(b, 0)
        if not up:
            out.append({"a": a, "b": b, "up": 0, "dn": 0, "lost": 0, "drop_pct": None, "reachable": False})
            continue
        out.append({"a": a, "b": b, "up": up, "dn": dn, "lost": max(0, up - dn),
                    "drop_pct": round(100 * (up - dn) / up, 1) if dn <= up else 0, "reachable": True})
    return out


# ------------------------------------------------------------------ arrival report
# The exact list an operator asks for, in order, per acquisition source. Everything here is counted
# from recorded rows only (first-party events for the top half, the lead row for the bottom half) and
# TEST traffic is never mixed in: `test="real"` starts from is_test=0.
ARRIVAL_STAGES = [("visits", "Visitors"), ("form_starts", "Check started"),
                  ("check_completions", "Check completed"), ("phones_submitted", "Contact submitted"),
                  ("valid_phones", "Valid contact"), ("consented_phone_leads", "Consent + phone"),
                  ("qualified", "Qualified"), ("routing_eligible", "Routing-eligible"),
                  ("delivered", "Delivered to agency")]


def source_table(con, test: str = "real", dim: str = "source") -> list[dict]:
    """Arrival funnel per acquisition dimension (source by default, also channel/engine/campaign) with
    the single biggest drop named, so a source that brings visits but no requests is visibly different
    from a source that brings requests."""
    # Real numbers exclude development origins; the TEST tab keeps them, because a QA submission made on
    # localhost is exactly what that tab exists to show. traffic_split() still reports the split.
    rows = by(con, dim, test=test, external_only=(test == "real"))
    out = []
    for r in rows:
        drops = [(a, b, (r.get(a) or 0) - (r.get(b) or 0))
                 for (a, _), (b, _) in zip(ARRIVAL_STAGES, ARRIVAL_STAGES[1:])]
        worst = max(drops, key=lambda x: x[2]) if drops else None
        r = dict(r)
        r["worst_drop"] = (f"{dict(ARRIVAL_STAGES)[worst[0]]} → {dict(ARRIVAL_STAGES)[worst[1]]}: "
                           f"-{worst[2]}") if worst and worst[2] > 0 else ""
        out.append(r)
    out.sort(key=lambda d: (-(d.get("consented_phone_leads") or 0), -(d.get("visits") or 0)))
    return out


def traffic_split(con) -> dict:
    """How much of the recorded traffic is real consumer activity vs TEST/QA vs internal or bot.

    Internal screens (admin/agent/partner/qa) are dropped at ingest, and bot user-agents are dropped
    there too, so they never become a visit. TEST/QA is a recorded flag (is_test), never a guess."""
    def _n(sql, args=()):
        try:
            return con.execute(sql, args).fetchone()[0]
        except Exception:                      # noqa: BLE001 - older DB without a column
            return 0
    internal_v = _n("SELECT COUNT(*) FROM visitors WHERE is_test=0 AND (COALESCE(landing_url,'') LIKE '%//localhost%' "
                    "OR COALESCE(landing_url,'') LIKE '%//127.0.0.1%' OR COALESCE(landing_url,'') LIKE '%//192.168.%' "
                    "OR COALESCE(landing_url,'') LIKE '%//10.%')")
    return {"real_visitors": _n("SELECT COUNT(*) FROM visitors WHERE is_test=0") - internal_v,
            "internal_visitors": internal_v,
            "test_visitors": _n("SELECT COUNT(*) FROM visitors WHERE is_test=1"),
            "real_leads": _n("SELECT COUNT(*) FROM leads WHERE is_test=0 AND COALESCE(landing_url,'') NOT LIKE '%//localhost%' "
                             "AND COALESCE(landing_url,'') NOT LIKE '%//127.0.0.1%'"),
            "test_leads": _n("SELECT COUNT(*) FROM leads WHERE is_test=1")}
