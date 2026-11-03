"""Buyer routing + delivery.

Rule that makes the consent valid: the buyer is chosen BEFORE submit (routing preview) and
that buyer's name is written into the consent text. After submit the lead may only go to
that same buyer. If they can't take it (cap hit, tier not accepted) the lead is held as
NO_BUYER - it is never silently re-sold to a different company the consumer didn't agree to.

Delivery is DRY-RUN by default: a JSON file in outbox/ + a deliveries row. Real webhook /
email delivery only runs when LEADENGINE_LIVE_DELIVERY=1 (you switch that on, not the code).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db import now
from .qualify import AGE_HIGH, AGE_LOW

SIGN_HEADER = "x-ticket-signature"          # HMAC-SHA256 over the EXACT posted body bytes

# Single source of truth for the calling rules every delivery payload and the buyers' desk print.
CONTACT_RULES = "Florida: 8am-8pm consumer local time, max 3 calls per 24h on the same subject; honor STOP/opt-out immediately."

# Quality-aware routing: a buyer's own recent outcomes (their desk dispositions) weigh on who gets
# the NEXT fresh consumer. The platform assigns every buyer; chronic rejected/unconsumed demand is
# the marketplace breaking its promise to consumers (and to the paying buyer alike).


def buyer_health(con, cfg: dict | None) -> dict:
    """Rolling REAL-lead probe of buyer behaviour, driving ordering (and the hard exclusion) in preview.
    Counts against a buyer: RETURNED, BAD_INFO, and delivered-but-uncontacted after the grace window.
    DNC / NOT_QUALIFIED / CONTACTED + are the buyer DOING their job — counted, never penalized.
    TEST traffic and disablement (cfg falsy / enabled:false) always produce an empty map (no bias).
    rank: 0 normal, 1 demoted (ranked after healthy buyers), 2 blocked (excluded from the preview
    entirely — the consent can never name them). Requires min_sample delivered leads before either."""
    if not cfg or not cfg.get("enabled"):
        return {}
    days = int(cfg.get("window_days", 14))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    grace = (datetime.now(timezone.utc) - timedelta(hours=float(cfg.get("grace_hours", 48)))).isoformat(timespec="seconds")
    rows = con.execute("""SELECT buyer_id, COUNT(*) n,
        SUM(agent_status='RETURNED') returned, SUM(agent_status='BAD_INFO') bad,
        SUM((agent_status IS NULL OR agent_status IN ('NEW','VIEWED')) AND delivered_at IS NOT NULL AND delivered_at < ?) stale
      FROM leads WHERE routing_status='DELIVERED' AND is_test=0 AND buyer_id IS NOT NULL AND substr(delivered_at,1,10) >= ?
      GROUP BY buyer_id""", (grace, cutoff)).fetchall()
    mn, demote, block = int(cfg.get("min_sample", 4)), float(cfg.get("demote_rate", 0.34)), float(cfg.get("block_rate", 0.66))
    out: dict[str, dict] = {}
    for r in rows:
        pen = (r["returned"] or 0) + (r["bad"] or 0) + (r["stale"] or 0)
        rate = round(pen / r["n"], 2)
        if r["n"] < mn:
            rank = 0
        elif rate >= block:
            rank = 2
        elif rate >= demote:
            rank = 1
        else:
            rank = 0
        out[r["buyer_id"]] = dict(n=r["n"], penalty=pen, rate=rate, rank=rank, window_days=days,
                                  detail=f"{pen} of {r['n']} delivered leads in the last {days}d "
                                         "were returned, bad-info or stayed uncontacted after the grace window")
    return out


def sign(secret: str, body: bytes) -> str | None:
    """The exact signature a buyer-side webhook can verify: sha256=HMAC(secret, raw_body_bytes).
    The secret never travels in the request — the buyer holds it (issued at onboarding), so they can
    prove a POST really came from this platform with the payload unmodified in transit."""
    if not secret:
        return None
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def post_signed(url: str, secret: str, body: bytes, timeout: int = 15):
    """POST the raw body with Content-Type application/json and, when the buyer holds a signing secret,
    the SIGN_HEADER. Returns (ok, response_text)."""
    import requests  # live delivery only when explicitly enabled
    headers = {"Content-Type": "application/json"}
    sig = sign(secret, body)
    if sig:
        headers[SIGN_HEADER] = sig
    r = requests.post(url, data=body, headers=headers, timeout=timeout)
    return r.ok, f"{r.status_code} {r.text[:200]}"


def load_buyers(cfg_dir: Path) -> list[dict]:
    return [b for b in json.loads((cfg_dir / "buyers.json").read_text(encoding="utf-8")) if b.get("active")]


def delivered_today(con, buyer_id: str) -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    return con.execute("SELECT COUNT(*) FROM leads WHERE buyer_id=? AND substr(routed_at,1,10)=? AND is_test=0",
                       (buyer_id, today)).fetchone()[0]


def delivered_in_month(con, buyer_id: str) -> int:
    """REAL leads routed to this buyer since the 1st of the current month (UTC). TEST traffic never
    counts toward a purchase limit, so a demo buyer can play through the whole funnel."""
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    return con.execute("SELECT COUNT(*) FROM leads WHERE buyer_id=? AND substr(routed_at,1,7)=? AND is_test=0",
                       (buyer_id, month)).fetchone()[0]


def _over_monthly_cap(con, b: dict) -> bool:
    cap = b.get("monthly_cap")
    return bool(cap) and delivered_in_month(con, b["id"]) >= int(cap)


def _tier_price(b: dict, tier: str | None) -> float:
    """The contracted price a buyer carries for the requested tier (0 when they never priced it).
    A buyer with no positive price can never CHARGE for the lead, so they can never be selected:
    charge()/reconcile both skip price<=0, which would otherwise hand out the sale free in silence."""
    if not tier:
        return 0.0
    try:
        return float((b.get("price_by_tier") or {}).get(tier) or 0)
    except (TypeError, ValueError):
        return 0.0


def eligible(b: dict, product: str, state: str, age_range: str | None) -> bool:
    if state not in b["licensed_states"] or product not in b["products"]:
        return False
    if age_range in AGE_LOW and (AGE_HIGH[age_range] < b["age_min"] or AGE_LOW[age_range] > b["age_max"]):
        return False
    return True


def preview(con, buyers: list[dict], product: str, state: str, age_range: str | None, tier: str | None = None,
            blocked_ids: frozenset | None = None, health: dict | None = None) -> dict | None:
    """THE ONE buyer-selection algorithm — the platform assigns the buyer that fulfills a lead.
    BUSINESS RULE: consumers never choose which agent receives them. They consent to
    GS Allude connecting them with a licensed agent; the platform picks WHICH one here from
    each buyer's contracted criteria (states, products, tiers, age band), his daily cap,
    and current prepaid balance (blocked_ids, credit enforcement in billing). Onboarded buyers
    outrank demo placeholders; ties then go by priority, then fewest leads today (round-robin).
    Quality-aware routing: buyers whose recent outcomes are poor rank after healthy ones, and a
    buyer beyond the configured ceiling is EXCLUDED — the consent can never name them (app passes
    the precomputed map from buyer_health)."""
    blocked_ids = blocked_ids or frozenset()
    h = health or {}

    cands = [b for b in buyers if eligible(b, product, state, age_range) and b["id"] not in blocked_ids
             and delivered_today(con, b["id"]) < b["daily_cap"] and not _over_monthly_cap(con, b)
             and h.get(b["id"], {}).get("rank") != 2
             # a buyer with no POSITIVE price for this tier can never charge for the lead —
             # selecting them would deliver it free in silence (charge/reconcile both skip price<=0)
             and (not tier or _tier_price(b, tier) > 0)]
    if tier:
        cands = [b for b in cands if tier in b["tiers"]]
    if not cands:
        return None
    cands.sort(key=lambda b: (1 if b.get("is_demo") else 0, h.get(b["id"], {}).get("rank", 0),
                              b["priority"], delivered_today(con, b["id"])))
    return cands[0]


def explain(con, buyers: list[dict], lead: dict, blocked_ids: frozenset | None = None, health: dict | None = None) -> tuple[str, int]:
    """Human-readable reason a lead was NOT assigned (or, when assigned, one line about WHO and WHY).
    Recorded in routing_decisions so the operator can audit withheld demand without guesswork.
    Health-aware: a buyer blocked/demoted by recent outcomes gets that as its reason too."""
    blocked_ids = blocked_ids or frozenset()
    h = health or {}
    if not buyers:
        return "no buyer is configured at all", 0
    notes = []
    ar = lead.get("age_range")
    for b in buyers[:10]:
        info = h.get(b["id"], {})
        if b["id"] in blocked_ids:
            notes.append(f"{b['id']} lacks prepaid credits")
        elif info.get("rank") == 2:
            notes.append(f"{b['id']} BLOCKED by recent buyer history - {info.get('detail')}")
        elif delivered_today(con, b["id"]) >= b["daily_cap"]:
            notes.append(f"{b['id']} daily cap {b['daily_cap']} reached")
        elif _over_monthly_cap(con, b):
            notes.append(f"{b['id']} monthly purchase limit {b.get('monthly_cap')} reached")
        elif lead["state"] not in b["licensed_states"]:
            notes.append(f"{b['id']} not licensed in {lead['state']}")
        elif lead["product"] not in b["products"]:
            notes.append(f"{b['id']} does not buy {lead['product']}")
        elif ar in AGE_LOW and (AGE_HIGH[ar] < b["age_min"] or AGE_LOW[ar] > b["age_max"]):
            notes.append(f"{b['id']} age band {b['age_min']}-{b['age_max']} excludes {ar}")
        elif lead.get("tier") and lead["tier"] not in b["tiers"]:
            notes.append(f"{b['id']} does not buy tier {lead['tier']}")
        elif lead.get("tier") and _tier_price(b, lead["tier"]) <= 0:
            notes.append(f"{b['id']} carries no positive price for tier {lead['tier']}")
        else:
            # no hard exclusion, but this buyer is not the pick: if their recent record DEMOTED
            # them, say so instead of a misleading "eligible but unassigned" (demotion is only
            # a tie-breaker behind healthy buyers - the operator sees the real ordering reason)
            if info.get("rank") == 1:
                notes.append(f"{b['id']} DEMOTED by recent buyer history (ranked after healthy buyers) - {info.get('detail')}")
            else:
                notes.append(f"{b['id']} eligible but unassigned")
    return ("no fully eligible buyer - " + "; ".join(notes))[:450], len(buyers)


def route(con, buyers: list[dict], lead: dict, blocked_ids: frozenset | None = None, health: dict | None = None) -> dict:
    """Assign a lead to ONE buyer (EXCLUSIVE) for delivery + billing. The buyer is picked here by the
    platform — any buyer id in the consumer's request payload is IGNORED (a consumer never chooses who
    receives them; a crafted payload cannot steer the sale). blocked_ids: the same exclusion the
    availability preview used (e.g. billing enforcement: no prepaid credits left). health: the
    buyer_health map (same quality-aware exclusion/ordering the consent naming preview used)."""
    blocked_ids = blocked_ids or frozenset()
    # `phone_source` is a defence in depth, not the primary guard: the DB trigger in db.py already
    # makes a non-CONSUMER_SUBMITTED lead impossible, so this only catches a hand-built dict that
    # admits its number came from somewhere else (an imported/appended contact — see
    # engine/admission.py). Absent provenance stays routable so existing callers are unaffected.
    if lead.get("phone_source") not in (None, "", "CONSUMER_SUBMITTED"):
        return {"routing_status": "NOT_ROUTABLE", "buyer_id": None, "price": None, "sale_type": ""}
    if lead["qual_status"] != "QUALIFIED" or lead.get("lead_type", "PHONE_LEAD") != "PHONE_LEAD" or not lead.get("phone"):
        return {"routing_status": "NOT_ROUTABLE", "buyer_id": None, "price": None, "sale_type": ""}  # never sold
    b = preview(con, buyers, lead["product"], lead["state"], lead["age_range"], tier=lead.get("tier"), blocked_ids=blocked_ids, health=health)
    if not b:
        return {"routing_status": "NO_BUYER", "buyer_id": None, "price": None, "sale_type": ""}
    # final billing guard (backed by preview's filter): the picked buyer MUST carry a positive price
    # for this tier — a sale priced at/under 0 would deliver free in silence (charge and reconcile
    # both skip price<=0), so an unpriced tier is treated exactly like an unaffordable buyer.
    price = b["price_by_tier"].get(lead.get("tier"))
    try:
        ok_price = float(price or 0) > 0
    except (TypeError, ValueError):
        ok_price = False
    if not ok_price:
        return {"routing_status": "NO_BUYER", "buyer_id": None, "price": None, "sale_type": ""}  # never sold free
    # the commercial product: EXCLUSIVE. One lead, one buyer, never re-sold — the consent (made to the
    # platform, per its on-screen text) is fulfilled with exactly this buyer and the delivery unique
    # index (ux_deliveries_lead) backs the same promise in the DB.
    return {"routing_status": "ROUTED", "buyer_id": b["id"], "price": b["price_by_tier"].get(lead["tier"]),
            "routed_at": now(), "sale_type": "EXCLUSIVE"}


def deliver(con, buyers: list[dict], lead: dict, outbox: Path) -> str:
    already = con.execute("SELECT 1 FROM leads WHERE lead_id=? AND routing_status='DELIVERED'", (lead["lead_id"],)).fetchone()
    if already:
        return "ALREADY_DELIVERED"          # a routed lead is sold to exactly one buyer; never re-posted
    b = next(x for x in buyers if x["id"] == lead["buyer_id"])
    payload = {k: lead.get(k) for k in ("lead_id", "created_at", "lead_type", "campaign", "product", "first_name", "last_name",
                                        "zip", "state", "lang", "age_range", "need_path", "household_size", "income_band",
                                        "enrollment_situation", "urgency", "product_category", "coverage_for",
                                        "coverage_amount", "tobacco", "health_rating", "timeframe",
                                        "preferred_contact", "best_time", "qual_status", "tier", "score")}
    # the product: the phone the consumer typed + proof they asked for this agent to use it
    payload["phone"] = {"number_e164": lead.get("phone_e164"), "national": lead.get("phone"), "source": lead.get("phone_source"),
                        "format_valid": bool(lead.get("phone_valid")), "check": lead.get("phone_check"),
                        "area_code_region": lead.get("phone_area_region"), "line_type": lead.get("phone_line_type"),
                        "line_type_source": lead.get("phone_line_type_source")}
    payload["email"] = lead.get("email") or None
    payload["provenance"] = {"landing_page": lead.get("landing_url"), "campaign": lead.get("campaign"), "source": lead.get("utm_source"),
                             "medium": lead.get("utm_medium"), "content": lead.get("utm_content"), "tracking_id": lead.get("tracking_id"),
                             "experiment_id": lead.get("experiment_id"), "partner_id": lead.get("partner_id"),
                             "consent_ts": lead.get("consent_ts"), "consent_version": lead.get("consent_version")}
    # full transparency for the agent: the exact page the consumer read and the answers that made them a lead
    payload["what_they_saw"] = {"entry_page": lead.get("entry_page"), "angle": lead.get("angle"), "need": lead.get("need_path"),
                                "urgency": lead.get("urgency"), "flags": lead.get("flags"), "phone_step_wording": lead.get("phone_variant")}
    payload["contact_rules"] = CONTACT_RULES
    payload["consent"] = dict(con.execute("SELECT consent_version, ts, seller_named, consent_text, ip, page_url, "
                                          "form_snapshot_sha256 FROM consents WHERE lead_id=?",
                                          (lead["lead_id"],)).fetchone())
    outbox.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, indent=2)
    p = outbox / f"{lead['lead_id']}__{b['id']}.json"
    # EXACT bytes, never a text-mode copy: on Windows write_text silently translates \n to \r\n,
    # which would make the recorded payload differ from the wire bytes the deliveries signature
    # was computed over (and every later retry would post a translated variant). Write bytes.
    p.write_bytes(body.encode("utf-8"))
    method = b.get("delivery", {}).get("method", "outbox")
    status, resp = "OUTBOX_DRY_RUN", ""
    if os.environ.get("LEADENGINE_LIVE_DELIVERY") == "1" and method == "webhook" and b["delivery"].get("webhook_url"):
        ok, resp = post_signed(b["delivery"]["webhook_url"], b["delivery"].get("webhook_secret", ""),
                               body.encode("utf-8"))
        status = "SENT" if ok else "FAILED"
    con.execute("INSERT INTO deliveries (lead_id,buyer_id,ts,method,status,payload_path,response) VALUES (?,?,?,?,?,?,?)",
                (lead["lead_id"], b["id"], now(), method, status, str(p), resp))
    con.execute("UPDATE leads SET routing_status='DELIVERED', delivered_at=? WHERE lead_id=?", (now(), lead["lead_id"]))
    return status


def _record_repost(con, lead_id: str, buyer_id: str, payload_path: str, status: str, resp: str) -> None:
    """Record a re-post of a sold lead's payload. A failed attempt is its own FAILED row (the partial
    unique index exempts FAILED). A SUCCESSFUL re-post must NOT become a second non-FAILED row:
    ux_deliveries_lead guarantees at most one successful sale delivery per lead, so the existing
    success row is updated in place — otherwise every later desk load would crash on the index and
    the buyer would lose access to their dashboard entirely."""
    if status != "FAILED":
        row = con.execute("SELECT id FROM deliveries WHERE lead_id=? AND buyer_id=? AND method='webhook' AND status != 'FAILED'",
                          (lead_id, buyer_id)).fetchone()
        if row:
            con.execute("UPDATE deliveries SET ts=?, status=?, response=? WHERE id=?", (now(), status, resp, row["id"]))
            return
    con.execute("INSERT INTO deliveries (lead_id,buyer_id,ts,method,status,payload_path,response) VALUES (?,?,?,?,?,?,?)",
                (lead_id, buyer_id, now(), "webhook", status, payload_path, resp))


def retry_webhook(con, buyers: list[dict], lead: dict, retry_budget: int = 5) -> str:
    """Redeliver a FAILED webhook delivery for a sold lead. The sale stands (the consent named this buyer)
    and the payload exists in the buyer's portal, but their CRM never received it. This re-posts the EXACT
    payload recorded on disk (never regenerated data, so the evidence trail stays intact), appends ONE new
    deliveries row (RESENT on success) and stops after retry_budget failures - from then on it is an
    operator conversation, not an automatic retry. Live delivery only (LEADENGINE_LIVE_DELIVERY=1)."""
    if os.environ.get("LEADENGINE_LIVE_DELIVERY") != "1":
        return ""
    fails = [dict(r) for r in con.execute(
        "SELECT payload_path FROM deliveries WHERE lead_id=? AND buyer_id=? AND method='webhook' AND status='FAILED'",
        (lead["lead_id"], lead["buyer_id"]))]
    if not fails or len(fails) > retry_budget:
        return ""
    b = next((x for x in buyers if x["id"] == lead["buyer_id"]), None)
    if not b or b.get("delivery", {}).get("method") != "webhook" or not b["delivery"].get("webhook_url"):
        return ""
    import requests  # live delivery only when explicitly enabled
    try:
        raw = Path(fails[0]["payload_path"]).read_bytes()   # EXACT recorded bytes: the signature is computed over what we keep on disk
        json.loads(raw.decode("utf-8"))                     # never post a payload we couldn't prove we recorded
        r = requests.post(b["delivery"]["webhook_url"], data=raw, headers={"Content-Type": "application/json",
                          "x-ticket-signature": sign(b["delivery"].get("webhook_secret", ""), raw)}, timeout=15)
        status, resp = ("RESENT" if r.ok else "FAILED"), f"retry: {r.status_code} {r.text[:200]}"
    except Exception as exc:
        status, resp = "FAILED", f"retry: {repr(exc)[:200]}"
    _record_repost(con, lead["lead_id"], b["id"], fails[0]["payload_path"], status, resp)
    return status


def operator_resend(con, buyers: list[dict], lead: dict) -> str:
    """Operator-initiated redelivery of a SOLD lead's ORIGINAL payload file (never regenerated data) to the
    buyer's CURRENT webhook URL. Unbudgeted — the automatic retry_webhook() stops after its budget and this
    is the manual escalation the admin outage alert points to. Appends one deliveries row (RESENT/FAILED)
    and never creates a new sale, re-charges a buyer, or touches routing state.
    Returns a status spoke for the admin UI: RESENT | FAILED | LIVE_DELIVERY_OFF | NO_WEBHOOK | NO_PAYLOAD."""
    b = next((x for x in buyers if x["id"] == lead.get("buyer_id")), None)
    if not b or b.get("delivery", {}).get("method") != "webhook" or not b["delivery"].get("webhook_url"):
        return "NO_WEBHOOK"
    row = con.execute("SELECT payload_path FROM deliveries WHERE lead_id=? AND buyer_id=? AND method='webhook' "
                      "ORDER BY id LIMIT 1", (lead["lead_id"], b["id"])).fetchone()
    if not row or not Path(row["payload_path"]).exists():
        return "NO_PAYLOAD"
    if os.environ.get("LEADENGINE_LIVE_DELIVERY") != "1":
        return "LIVE_DELIVERY_OFF"     # staging default: nothing leaves the machine
    try:
        import requests
        raw = Path(row["payload_path"]).read_bytes()
        json.loads(raw.decode("utf-8"))
        r = requests.post(b["delivery"]["webhook_url"], data=raw, headers={"Content-Type": "application/json",
                          "x-ticket-signature": sign(b["delivery"].get("webhook_secret", ""), raw)}, timeout=15)
        status, resp = ("RESENT" if r.ok else "FAILED"), f"operator resend: {r.status_code} {r.text[:200]}"
    except Exception as exc:
        status, resp = "FAILED", f"operator resend: {repr(exc)[:200]}"
    _record_repost(con, lead["lead_id"], b["id"], row["payload_path"], status, resp)
    return status
