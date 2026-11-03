"""Prospective-buyer follow-up pipeline — the commercial counterpart to engine/partners.py.

Buyers arrive as public applications (/for-agents/apply -> ONE inactive draft in buyers.json) or
operator-configured drafts. Between "application received" and "activated buyer" there is a real
sales process (kit sent, pricing discussed, pilot scheduled), and UNTIL NOW nothing recorded it —
the deal lived in the operator's head. This module is an APPEND-ONLY, internal audit trail of what
the operator actually did with each prospect; it never contacts anyone, never activates a buyer,
never touches consumer data, and is completely disjoint from partner_activity: partners distribute,
buyers purchase.

Stage progression is derived from the buyer_activity trail (buyers live in config/buyers.json, so
unlike partners there is no DB row to hold a columns mirror; the LATEST STAGE row in the trail is
the current stage, defaulting to the recorded configured state). Buckets reuse the partner
pipeline's semantics: OVERDUE / DUE_TODAY from a real recorded next_followup date, nothing
fabricated.

  python-side users: app.py /admin/buyer-pipeline + tests/test_buyer_pipeline.py
"""
from __future__ import annotations

import hashlib
import datetime as dt

from engine.db import now

# The buyer-prospect stage ladder. Applications land at APPLIED; the operator walks a prospect
# rightward. DECLINED / PAUSED leave the active follow-up flow. ACTIVATED is recorded when the
# buyer actually goes active on /admin/buyers (the app route records it automatically, so the trail
# answers "when did this prospect come live" without a separate notion of success).
STAGES = ["APPLIED", "REVIEWED", "KIT_SENT", "PRICING_DISCUSSED", "PILOT_READY", "ACTIVATED", "DECLINED", "PAUSED"]
STAGE_LABEL = {"APPLIED": "Application received", "REVIEWED": "Reviewed application", "KIT_SENT": "Buyer kit sent",
               "PRICING_DISCUSSED": "Pricing discussed", "PILOT_READY": "Pilot agreed", "ACTIVATED": "Activated buyer",
               "DECLINED": "Declined", "PAUSED": "Paused"}
METHODS = ["EMAIL", "PHONE", "IN_PERSON", "EVENT", "OTHER"]
# Stages that end (or suspend) the active follow-up flow — mirrors partner DECLINED/PAUSED handling.
LEAVING = ("ACTIVATED", "DECLINED", "PAUSED")


def stage_of(buyer: dict, con) -> str:
    """Current stage of a buyer draft: the latest STAGE row in the append-only trail, else the
    recorded configured state (an active buyer has always effectively been ACTIVATED)."""
    if buyer.get("active"):
        return "ACTIVATED"
    r = con.execute("SELECT stage_to FROM buyer_activity WHERE buyer_id=? AND kind='STAGE' ORDER BY id DESC LIMIT 1",
                    (buyer["id"],)).fetchone()
    if r and r["stage_to"] in STAGES:
        return r["stage_to"]
    return "APPLIED"


def buckets(rows: list[dict]) -> dict[str, int]:
    b: dict[str, int] = {}
    for r in rows:
        b[r["bucket"]] = b.get(r["bucket"], 0) + 1
    return b


def _valid_date(v: str) -> str | None:
    v = (v or "").strip()
    try:
        return dt.date.fromisoformat(v).isoformat()
    except ValueError:
        return None


def rows(con, buyers: list[dict]) -> list[dict]:
    """Every non-demo buyer draft as one pipeline action row, most important first.
    OVERDUE / DUE_TODAY come from a recorded next_followup date vs today (UTC); a prospect with a
    recorded block on their routing config shows that blocker line, never a fabricated phase; a
    never-touched prospect with no follow-up date is AWAITING_FIRST_STEP."""
    today = dt.date.today().isoformat()
    out = []
    for b in buyers:
        if b.get("is_demo"):
            continue                                    # demo placeholders are software fixtures, not prospects
        d = dict(b)
        stg = stage_of(d, con)
        act = con.execute("SELECT ts, method, note, next_followup FROM buyer_activity WHERE buyer_id=? "
                          "AND kind='OUTREACH' ORDER BY id DESC LIMIT 1", (d["id"],)).fetchone()
        nf_row = con.execute("SELECT next_followup FROM buyer_activity WHERE buyer_id=? AND next_followup != '' "
                             "ORDER BY id DESC LIMIT 1", (d["id"],)).fetchone()
        nf = (nf_row["next_followup"] or "")[:10] if nf_row else ""
        stage_now = stg
        if nf and nf < today:
            bucket = "OVERDUE"
        elif nf == today:
            bucket = "DUE_TODAY"
        elif stg == "PILOT_READY" and not nf:
            bucket = "PILOT_READY"
        elif stage_now == "APPLIED" and not act and not nf:
            bucket = "AWAITING_FIRST_STEP"
        elif nf > today:
            bucket = "SCHEDULED"
        else:
            bucket = "RECENT"
        app = d.get("_application") or {}
        out.append({"buyer": d, "stage": stg, "bucket": bucket, "next_followup": nf,
                    "last_outreach_at": act["ts"] if act else "",
                    "outreach_method": act["method"] if act else "",
                    "last_note": act["note"] if act else "",
                    "contact_email": app.get("contact_email") or (d.get("delivery") or {}).get("email") or "",
                    "contact_name": app.get("contact_name") or "",
                    "note": app.get("note") or ""})
    order = ["OVERDUE", "DUE_TODAY", "PILOT_READY", "AWAITING_FIRST_STEP", "SCHEDULED", "RECENT"]
    out.sort(key=lambda r: (order.index(r["bucket"]), r["next_followup"] or "9999-99-99", r["buyer"]["id"]))
    return out


def record_action(con, bid: str, buyers: list[dict], *, stage: str = "", method: str = "",
                  next_followup: str = "", note: str = "", operator: str = "admin") -> tuple[list[str], list[str]]:
    """Validated, append-only pipeline update for one buyer prospect. Returns (messages, errors);
    errors mean NOTHING was written. Identical retries within the same minute (double-clicks, form
    re-posts) are detected with dedupe_key and reported instead of creating a duplicate record."""
    b = next((x for x in buyers if x["id"] == bid), None)
    if not b:
        return [], ["unknown buyer"]
    if b.get("is_demo"):
        return [], ["demo placeholders are not tracked prospects"]
    errs, msgs = [], []
    stage = (stage or "").strip().upper()
    method = (method or "").strip().upper()
    note = (note or "").strip()[:500]
    stage_now = stage_of(dict(b), con)
    if stage and stage not in STAGES:
        errs.append(f"unknown stage '{stage}'")
    if method and method not in METHODS:
        errs.append(f"unknown method '{method}'")
    nf = _valid_date(next_followup) if (next_followup or "").strip() else ""
    if (next_followup or "").strip() and nf is None:
        errs.append("follow-up date must be YYYY-MM-DD")
    if errs:
        return [], errs
    out_rows = []
    if stage and stage != stage_now:
        out_rows.append(("STAGE", stage_now, stage))
        msgs.append(f"stage {stage_now} -> {stage}")
    if method or note:
        out_rows.append(("OUTREACH", stage_now, ""))
        msgs.append("outreach recorded")
    if nf:
        out_rows.append(("FOLLOWUP", stage_now, ""))
        msgs.append(f"next follow-up {nf}")
    if not out_rows:
        return ["nothing to record - set a stage, method, note or follow-up date"], []
    ts = now()
    minute = ts[:16]
    inserted = skipped = 0
    for kind, _from, to in out_rows:
        raw = f"v1|{bid}|{kind}|{stage_now}|{to}|{method}|{nf}|{note}|{minute}"
        dk = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        cur = con.execute("SELECT 1 FROM buyer_activity WHERE dedupe_key=?", (dk,)).fetchone()
        if cur:
            skipped += 1
            continue
        con.execute("INSERT INTO buyer_activity (ts, buyer_id, kind, stage_from, stage_to, method, next_followup, note, dedupe_key, operator)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)", (ts, bid, kind, _from, to, method, nf, note, dk, operator))
        inserted += 1
    con.commit()
    if skipped:
        msgs.append(str(skipped) + " duplicate form re-submit(s) ignored - the record already existed")
    return msgs, []


def history(con, bid: str) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT ts, kind, stage_from, stage_to, method, next_followup, note, operator FROM buyer_activity "
        "WHERE buyer_id=? ORDER BY id DESC LIMIT 100", (bid,))]


def funnel(con, buyers: list[dict]) -> dict:
    """Drop-off view over the SAME recorded trail, never invented:
    for every non-demo buyer (a prospect OR an active buyer that came up the pipeline)
    each stage shows how many ever reached it, how many are standing there now,
    how many moved onward, and the recorded time-in-stage medians.
    Stage markers are what the operator RECORDED — a configured draft that skipped
    markers counts only for the stage it stands on. Exits (DECLINED / PAUSED) and
    ACTIVATED success are reported as terminal rows, not hidden.
    """
    ladder = [s for s in STAGES if s not in ("DECLINED", "PAUSED", "ACTIVATED")]
    idx = {s: i for i, s in enumerate(STAGES)}
    st = {s: {"reached": 0, "now": 0, "onward": 0, "days": []} for s in STAGES}
    arrived = 0
    for b in buyers:
        if b.get("is_demo"):
            continue
        arrived += 1
        trail = [dict(r) for r in con.execute(
            "SELECT ts, stage_to FROM buyer_activity WHERE buyer_id=? AND kind='STAGE' ORDER BY id", (b["id"],))]
        path = [r["stage_to"] for r in trail if r["stage_to"] in STAGES]
        if b.get("active"):
            current = "ACTIVATED"
        elif path:
            current = path[-1]
        else:
            current = "APPLIED"                # everyone arrives at least at the application
        reached = set(path) | {current}
        for s in reached:
            st[s]["reached"] += 1
        st[current]["now"] += 1
        max_idx = max(idx[s] for s in reached)
        for s in list(reached):
            if idx[s] < max_idx:
                st[s]["onward"] += 1
        # recorded time-in-stage: the ts of the row that PUT the buyer on the current stage
        if b.get("active") and not path:
            pass                                # configured-active buyer without trail: no recorded entry
        else:
            entry = next((r["ts"] for r in reversed(trail) if r["stage_to"] == current), "")
            if current == "APPLIED" and not path:
                entry = ""
            if entry:
                try:
                    st[current]["days"].append(max(0, (dt.date.fromisoformat(entry[:10]) - dt.date.today()).days * -1))
                except ValueError:
                    pass
    def _med(days):
        days = sorted(days)
        if not days:
            return None
        m = len(days) // 2
        return (days[m] if len(days) % 2 else round((days[m - 1] + days[m]) / 2))
    return {
        "arrived": arrived,
        "ladder": [{"stage": s, "label": STAGE_LABEL[s], "reached": st[s]["reached"],
                    "now": st[s]["now"], "onward": st[s]["onward"],
                    "days": _med(st[s]["days"])} for s in ladder],
        "exits": {"declined": st["DECLINED"]["now"], "paused": st["PAUSED"]["now"],
                  "declined_ever": st["DECLINED"]["reached"], "paused_ever": st["PAUSED"]["reached"],
                  "activated": st["ACTIVATED"]["now"], "activated_ever": st["ACTIVATED"]["reached"]},
        "stalled": sum(st[s]["now"] for s in ("APPLIED",)),
    }
