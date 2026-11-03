"""Engine 1 operations queue: every organic / partner action (post, group, page owner, employer HR, CareerSource,
media, flyer) as ONE list with its own tracked link, a status history (approved / posted / sent / replied / removed),
the effort it took (minutes, $ spent) and the results it produced (REAL visits, Check completions, leads, accepted).

Results are matched by utm_content = item id (every tracked link carries it), so each action is measured on its own.
Nothing here posts or sends anything: a human does the action and logs it.
"""
from __future__ import annotations

import json
from pathlib import Path

from .db import now

SCHEMA = """CREATE TABLE IF NOT EXISTS ops_log (id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, ts TEXT, action TEXT,
  url TEXT, minutes REAL DEFAULT 0, cost_usd REAL DEFAULT 0, note TEXT)"""
ACTIONS = ["APPROVED", "POSTED", "SENT", "REPLIED_YES", "REPLIED_NO", "REMOVED", "NOTE"]
LIVE_ACTIONS = ("POSTED", "SENT", "REPLIED_YES")


def ensure(con):
    con.execute(SCHEMA)


def items(cfg_dir: Path, base_url: str = "") -> list[dict]:
    out = []
    dist = json.loads((cfg_dir / "distribution.json").read_text(encoding="utf-8"))["placements"] if (cfg_dir / "distribution.json").exists() else []
    for p in dist:
        if p.get("kind") == "setup":
            link = None
        else:
            link = f"{base_url}/go/{p['code']}"
        out.append({"id": p["id"], "engine": p["engine"], "channel": p["channel"], "type": p.get("kind", "post"), "where": p["platform"] + ": " + p["where"],
                    "category": p["category"], "default_status": p["status"], "link": link, "copy_text": p.get("copy_es") or p.get("copy_en") or "",
                    "how": p.get("how_to_find", ""), "rules": p.get("rules", ""), "priority": "HIGH" if p["engine"] == "ORGANIC" and p["status"] != "SCHEDULED_JAN" else "MEDIUM"})
    out_cfg = cfg_dir / "outreach.json"
    for o in (json.loads(out_cfg.read_text(encoding="utf-8"))["items"] if out_cfg.exists() else []):
        out.append({"id": o["id"], "engine": "PARTNER", "channel": "partner", "type": o["type"], "where": o["target"], "category": o["category"],
                    "default_status": o["status"], "link": base_url + o["link"], "copy_text": o["message"], "how": o.get("how_to_find", ""), "rules": "",
                    "priority": o["priority"], "window": o.get("life_window_closes")})
    return out


def log(con, item_id: str, action: str, url: str = "", minutes: float = 0, cost: float = 0, note: str = "") -> bool:
    ensure(con)
    if action not in ACTIONS:
        return False
    con.execute("INSERT INTO ops_log (item_id, ts, action, url, minutes, cost_usd, note) VALUES (?,?,?,?,?,?,?)",
                (item_id, now(), action, (url or "")[:500], float(minutes or 0), float(cost or 0), (note or "")[:500]))
    con.commit()
    return True


def board(con, cfg_dir: Path, base_url: str = "", test: int = 0) -> list[dict]:
    """Every item with its current status, effort and REAL results (test=1 shows TEST results instead)."""
    ensure(con)
    effort = {r[0]: (r[1] or 0, r[2] or 0) for r in con.execute("SELECT item_id, SUM(minutes), SUM(cost_usd) FROM ops_log GROUP BY 1")}
    last = {r[0]: (r[1], r[2], r[3]) for r in con.execute(
        "SELECT item_id, action, ts, url FROM ops_log o WHERE id = (SELECT MAX(id) FROM ops_log WHERE item_id=o.item_id AND action != 'NOTE')")}
    ev = {r[0]: (r[1], r[2]) for r in con.execute("""SELECT utm_content, COUNT(DISTINCT CASE WHEN event='page_view' THEN visitor_id END),
          COUNT(DISTINCT CASE WHEN event='tool_complete' THEN visitor_id END) FROM events WHERE is_test=? AND utm_content IS NOT NULL GROUP BY 1""", (test,))}
    ld = {r[0]: (r[1] or 0, r[2] or 0, r[3] or 0) for r in con.execute("""SELECT utm_content,
          SUM(lead_type='PHONE_LEAD' AND phone_valid=1), SUM(routing_status='DELIVERED' AND COALESCE(agent_status,'NEW') NOT IN ('RETURNED','BAD_INFO','DNC')),
          SUM(agent_status IN ('RETURNED','BAD_INFO','DNC')) FROM leads WHERE is_test=? AND utm_content IS NOT NULL GROUP BY 1""", (test,))}
    out = []
    for it in items(cfg_dir, base_url):
        a = last.get(it["id"])
        mins, cost = effort.get(it["id"], (0, 0))
        v, done = ev.get(it["id"], (0, 0))
        leads, acc, ret = ld.get(it["id"], (0, 0, 0))
        status = a[0] if a else it["default_status"]
        out.append(dict(it, status=status, live=bool(a and a[0] in LIVE_ACTIONS), last_ts=a[1] if a else None, post_url=a[2] if a else None,
                        minutes=round(mins, 1), cost_usd=round(cost, 2), visits=v, check_done=done, leads=leads, accepted=acc, returned=ret))
    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    out.sort(key=lambda r: (not r["live"], order.get(r["priority"].split(" ")[0], 3), r["id"]))
    return out


def performance(con, cfg_dir: Path, base_url: str = "", test: int = 0) -> tuple[list[dict], list[dict]]:
    """Acquisition performance by TRACKED PLACEMENT: the ops item id is the utm_content carried on the
    consumer's link, so item id == attribution key. Every configured placement gets its REAL business
    funnel (unique visitors, Check completions, consented phone leads, QUALIFIED leads, delivered,
    dispositions RETURNED / BAD_INFO / DNC as separate outcomes, sales, revenue) plus the operator-
    recorded effort and cost from ops_log. Rules:
      - duplicate-flagged leads are reported as `dup` and NEVER enter `qualified` (no double counting)
      - cost comes ONLY from ops_log rows the operator actually recorded; cost-per-qualified is None
        (shown as unknown, never 0) when no spend was recorded — a placement cannot acquire its first
        free lead by magic and it isn't charged imaginary dollars either
      - test traffic is excluded entirely (pass test=1 to see the TEST rows instead)
      - returns: (rows, untracked) — untracked utm_content values with real leads but no configured
        placement, so attribution that exists is never silently dropped
    """
    ensure(con)
    listing = {it["id"]: it for it in items(cfg_dir, base_url)}
    ev = {r["k"]: dict(r) for r in con.execute("""SELECT utm_content k,
          COUNT(DISTINCT CASE WHEN event='page_view' THEN visitor_id END) visits,
          COUNT(DISTINCT CASE WHEN event='tool_start' THEN visitor_id END) form_starts,
          COUNT(DISTINCT CASE WHEN event='tool_complete' THEN visitor_id END) check_done
          FROM events WHERE is_test=? AND utm_content IS NOT NULL GROUP BY 1""", (test,))}
    ld = {r["k"]: dict(r) for r in con.execute("""SELECT utm_content k, COUNT(*) leads_all,
          SUM(lead_type='PHONE_LEAD' AND COALESCE(phone_valid,0)=1) consented,
          SUM(qual_status='QUALIFIED') qualified, SUM(qual_status='DUPLICATE') dup,
          SUM(routing_status='DELIVERED') delivered,
          SUM(routing_status='DELIVERED' AND agent_status='SOLD') sold,
          SUM(routing_status='DELIVERED' AND agent_status='RETURNED') returned,
          SUM(routing_status='DELIVERED' AND agent_status='BAD_INFO') bad_info,
          SUM(routing_status='DELIVERED' AND agent_status='DNC') dnc,
          COALESCE(SUM(CASE WHEN routing_status='DELIVERED' AND COALESCE(agent_status,'NEW') NOT IN ('RETURNED','BAD_INFO','DNC') THEN price END),0) revenue
          FROM leads WHERE is_test=? AND utm_content IS NOT NULL GROUP BY 1""", (test,))}
    eff = {r[0]: (r[1] or 0, r[2] or 0) for r in con.execute(
        "SELECT item_id, SUM(minutes), SUM(cost_usd) FROM ops_log GROUP BY 1")}
    out = []
    for iid, it in listing.items():
        e, volume = ev.get(iid, {}), ld.get(iid, {})
        minutes, cost = eff.get(iid, (0, 0))
        g = lambda k: volume.get(k) or 0  # noqa: E731
        d = dict(it, visits=e.get("visits", 0), form_starts=e.get("form_starts", 0), check_done=e.get("check_done", 0),
                 consented=g("consented"), qualified=g("qualified"), dup=g("dup"), delivered=g("delivered"),
                 sold=g("sold"), returned=g("returned"), bad_info=g("bad_info"), dnc=g("dnc"),
                 revenue=round(g("revenue"), 2), minutes=round(minutes, 1), cost_usd=round(cost, 2) if cost else None,
                 cost_per_qualified=round(cost / g("qualified"), 2) if cost and g("qualified") else None,
                 qual_pct=round(100 * g("qualified") / g("consented"), 1) if g("consented") else None)
        out.append(d)
    out.sort(key=lambda r: (-r["qualified"], -r["consented"], -r["visits"], r["id"]))
    known = set(listing) | set(eff)   # operator-recorded ids count as known placements even when unlisted
    untracked = [{"k": k, **{c: (v.get(c) or 0) for c in ("leads_all", "consented", "qualified", "dup", "delivered", "sold", "revenue")}}
                 for k, v in sorted(ld.items()) if k not in known]
    for u in untracked:
        u["revenue"] = round(u["revenue"], 2)
    return out, untracked


def sale_alerts(con) -> dict:
    """Sale-integrity alerts, computed from RECORDED rows only — the same kernel the /admin overview
    renders, exposed for the CLI daily run. Every list is a read query; nothing here repairs or posts.
      stranded: sales recorded ROUTED but never DELIVERED (a crash between route and deliver)
      webhook_out: webhook deliveries that exhausted their automatic retries
      negative: REAL-lead ledger sums per buyer below zero
      stuck_purchases: self-serve payments PENDING for over 24h (webhook never confirmed)
      unbilled: REAL delivered leads priced > 0 with NO LEAD_CHARGE ledger row (deliver chose a crash,
        or enforcement was OFF — the buyer's desk self-heals the charge on next load)
      credit_requests: buyers asking for a charge to be re-examined (pending, unhedged)
    """
    from . import billing                                    # local import: kernel stays self-contained
    from datetime import datetime, timezone, timedelta
    until = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
    stranded = [dict(r) for r in con.execute(
        "SELECT lead_id, buyer_id, created_at FROM leads WHERE routing_status='ROUTED' ORDER BY created_at DESC LIMIT 10")]
    webhook_out = [dict(r) for r in con.execute(
        """SELECT lead_id, COUNT(*) n FROM deliveries WHERE method='webhook' AND status='FAILED' GROUP BY lead_id
            HAVING n >= 5 ORDER BY 1 DESC LIMIT 10""")]
    negative = [dict(r) for r in con.execute(
        """SELECT buyer_id, ROUND(SUM(amount), 2) bal, COUNT(*) txns FROM buyer_ledger WHERE is_test=0
            GROUP BY buyer_id HAVING bal < 0 ORDER BY bal LIMIT 10""")]
    stuck = [dict(r) for r in con.execute(
        "SELECT rowid, buyer_id, amount_usd, ts, provider FROM buyer_payments WHERE status='PENDING' AND ts <= ? ORDER BY ts LIMIT 10", (until,))]
    unbilled = [dict(r) for r in con.execute(
        """SELECT l.lead_id, l.buyer_id, l.created_at, l.price FROM leads l
            WHERE l.routing_status='DELIVERED' AND l.is_test=0 AND COALESCE(l.price,0) > 0
              AND NOT EXISTS (SELECT 1 FROM buyer_ledger g WHERE g.lead_id=l.lead_id AND g.kind='LEAD_CHARGE')
            ORDER BY l.created_at DESC LIMIT 10""")]
    live = os_now_live()
    email_out: list = []
    if live == "1":
        email_out = [dict(r) for r in con.execute("""SELECT d.lead_id, d.buyer_id, d.ts, d.response FROM deliveries d
            WHERE d.method='email_notification' AND d.status != 'SENT'
            AND EXISTS (SELECT 1 FROM leads l WHERE l.lead_id=d.lead_id AND l.routing_status='DELIVERED' AND l.is_test=0)
            AND d.id = (SELECT MAX(x.id) FROM deliveries x WHERE x.lead_id=d.lead_id AND x.method='email_notification')
            ORDER BY d.id DESC LIMIT 10""")]
    alerts = {"stranded": stranded, "webhook_out": webhook_out, "negative": negative,
              "stuck_purchases": stuck, "email_out": email_out, "unbilled": unbilled}
    alerts["credit_requests"] = billing.pending_credit_requests(con)
    alerts["any"] = bool(stranded or webhook_out or negative or stuck or email_out or unbilled or alerts["credit_requests"])
    return alerts


def os_now_live() -> str:
    """One source for the live-delivery flag (import os avoids a reload of the env shape)."""
    import os as _os
    return _os.environ.get("LEADENGINE_LIVE_DELIVERY", "0")
