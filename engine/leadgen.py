"""The consumer acquisition ledger: ONE view of the eight stages that stand between a stranger and a
buyer's invoice, plus the exact dependencies that are blocking the next stage.

    visitors -> started inquiries -> completed inquiries -> consented leads
      -> verified contact details -> qualified leads -> eligible for buyer routing
      -> purchased & delivered

Rules this module refuses to break:
  * Every number is a COUNT of recorded rows. Nothing is estimated, projected or extrapolated, and
    TEST traffic is excluded unless you explicitly ask for it.
  * A stage that has not happened is 0; an unknown metric is None (rendered '-'). They are never
    the same value.
  * An organization contact is never a consumer. The org ledger lives in its own database and its
    report is a separate block (`network`), never summed into these stages.
  * `consented` requires the three things that make consent real: a consumer-submitted phone that
    validated, a consent timestamp on the lead, and the stored consent row (text + time) that proves
    what they agreed to. A phone number typed without that is not a consented lead.
  * `contact_verified` means server-validated E.164 plus that stored consent record. Carrier-level
    ownership verification is NOT implemented (it needs a paid lookup nobody approved) and is
    reported as such, never implied.

Nothing here sends, routes or sells anything. It reads recorded rows and explains the gap.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import channels as ch
from . import funnel as funnel_mod
from . import network as network_mod
from . import route as route_mod

ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "config"

STAGES = [
    ("visitors", "Visitors", "A real person loaded a page (unique visitor, TEST excluded)."),
    ("started", "Started inquiries", "They opened the coverage check and answered the first step."),
    ("completed", "Completed inquiries", "They reached the end of the check."),
    ("submitted", "Submitted inquiries", "A lead row exists - they actually sent the form."),
    ("consented", "Consented leads", "Consumer-submitted + valid phone + stored consent record."),
    ("contact_verified", "Verified contact details", "E.164 phone and the consent text/timestamp both on record."),
    ("qualified", "Qualified leads", "Passed the qualification rules (duplicates excluded)."),
    ("routing_eligible", "Eligible for buyer routing", "Qualified AND a licensed buyer exists for that product/state today."),
    ("delivered", "Purchased & delivered", "Delivered to a buyer and charged on the ledger."),
]

CONSENT_SQL = ("l.lead_type='PHONE_LEAD' AND COALESCE(l.phone_valid,0)=1 AND l.consent_ts IS NOT NULL "
               "AND EXISTS (SELECT 1 FROM consents c WHERE c.lead_id=l.lead_id "
               "AND COALESCE(c.consent_text,'') != '' AND COALESCE(c.ts,'') != '')")


def stage_counts(con, test: int = 0) -> dict:
    """The eight stages, each from an explicit recorded source. Unknown stays None."""
    cap = funnel_mod.stage_totals(funnel_mod.by(con, "all", "real" if not test else "test"))
    ev = con.execute("""SELECT COUNT(DISTINCT CASE WHEN event='page_view' THEN visitor_id END) visitors,
          COUNT(DISTINCT CASE WHEN event='tool_start' THEN visitor_id END) started,
          COUNT(DISTINCT CASE WHEN event='tool_complete' THEN visitor_id END) completed
          FROM events WHERE is_test=?""", (test,)).fetchone()
    ld = con.execute(f"""SELECT COUNT(*) submitted,
          SUM({CONSENT_SQL}) consented,
          SUM({CONSENT_SQL} AND COALESCE(l.phone_e164,'') != '') contact_verified,
          SUM(qual_status='QUALIFIED') qualified_all,
          SUM({CONSENT_SQL} AND qual_status='QUALIFIED') qualified,
          SUM(qual_status='DUPLICATE') duplicates,
          SUM(lead_type='EMAIL_ONLY_INQUIRY') email_only,
          SUM(COALESCE(l.phone_verified,0)=1) carrier_verified
          FROM leads l WHERE l.is_test=?""", (test,)).fetchone()
    # A delivery to a DEMO placeholder never happened commercially: no agency received it and nobody
    # was charged. Counting it as "purchased & delivered" would inflate the only metric a buyer
    # cares about, so demo deliveries are reported separately as suspect REAL rows (QA traffic that
    # escaped the test flag). Production excludes demo buyers entirely, so this must stay 0 there.
    g = lambda v: int(v or 0)  # noqa: E731
    demo_delivered = g(con.execute(
        "SELECT COUNT(*) FROM leads WHERE is_test=? AND routing_status='DELIVERED' "
        "AND (buyer_id LIKE 'demo%' OR buyer_id IS NULL OR buyer_id='')", (test,)).fetchone()[0])
    out = {"visitors": g(ev["visitors"]), "started": g(ev["started"]), "completed": g(ev["completed"]),
           "submitted": g(ld["submitted"]), "consented": g(ld["consented"]),
           "contact_verified": g(ld["contact_verified"]), "qualified": g(ld["qualified"]),
           "qualified_any": g(ld["qualified_all"]), "duplicates": g(ld["duplicates"]),
           "email_only": g(ld["email_only"]), "carrier_verified": g(ld["carrier_verified"]),
           "delivered": max(0, g(cap.get("delivered")) - demo_delivered),
           "demo_delivered": demo_delivered, "sold": g(cap.get("sales")),
           "revenue": round(cap.get("revenue") or 0, 2)}
    # routing_eligible is deferred to report(): it depends on which buyers exist TODAY.
    out["routing_eligible"] = None
    return out


def buyer_gate(cfg_dir: Path = CFG, served_states: list[str] | None = None) -> dict:
    """Which products can actually be routed right now, and why not when the answer is none.

    A buyer is usable only if it is active, is not a demo placeholder, is licensed in the state, has
    a positive contracted price, and has any capacity. `route.preview` makes the final call per lead;
    this is the registry-level truth the acquisition report needs before promising volume.
    """
    try:
        buyers = route_mod.load_buyers(cfg_dir)
    except (OSError, ValueError, KeyError):
        buyers = []
    states = served_states or _served_states(cfg_dir)
    real = [b for b in buyers if not b.get("is_demo")]
    demo = [b for b in buyers if b.get("is_demo")]
    usable = []
    for b in real:
        products = [p for p in (b.get("products") or []) if p]
        priced = any(float(v or 0) > 0 for v in (b.get("price_by_tier") or {}).values())
        covers = any(s in states for s in (b.get("licensed_states") or []))
        if products and priced and covers and int(b.get("daily_cap") or 0) > 0:
            usable.append({"id": b["id"], "name": b.get("name"), "products": products,
                           "states": b.get("licensed_states"), "cap": b.get("daily_cap")})
    products = sorted({p for b in usable for p in b["products"]})
    return {"buyers_total": len(buyers), "buyers_real": len(real), "buyers_demo": len(demo),
            "buyers_usable": len(usable), "usable": usable, "routable_products": products,
            "states": states}


def _served_states(cfg_dir: Path) -> list[str]:
    try:
        return json.loads((cfg_dir / "site.json").read_text(encoding="utf-8")).get("served_states") or ["FL"]
    except (OSError, ValueError):
        return ["FL"]


def _site_cfg(cfg_dir: Path) -> dict:
    try:
        return json.loads((cfg_dir / "site.json").read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}


def blockers(con, cfg_dir: Path = CFG, stages: dict | None = None, gate: dict | None = None,
             net: dict | None = None, test: int = 0) -> list[dict]:
    """The ordered list of what is actually stopping consumer leads from turning into revenue.
    Each entry names the dependency, whether it blocks TODAY, and the one concrete action."""
    st = stages if stages is not None else stage_counts(con, test)
    gate = gate if gate is not None else buyer_gate(cfg_dir)
    net = net if net is not None else network_mod.report(con)
    site = _site_cfg(cfg_dir)
    live = os.environ.get("LEADENGINE_LIVE_DELIVERY", "0") == "1"
    indexing = os.environ.get("ALLOW_INDEXING", "0") == "1"
    out = []
    health_routable = "health_aca" in (gate.get("routable_products") or [])
    out.append({
        "key": "named_buyer", "label": "A named licensed agency for health leads",
        "blocking": not health_routable,
        "detail": (f"{gate['buyers_real']} non-demo buyer(s) configured; "
                   f"{gate['buyers_usable']} usable; health_aca routable: {'yes' if health_routable else 'no'}. "
                   f"Demo placeholders: {gate['buyers_demo']} - never offered to real consumers."),
        "fix": "Add one real agency to config/buyers.json (legal name, FL license, products incl. "
               "health_aca, price per tier, daily cap). No code change needed.",
        "why": "The consent text may only name a buyer that can receive the lead. Without one, the "
               "funnel stops at 'completed' by design - that guard is unit-tested and stays.",
    })
    counsel = bool(site.get("consent_reviewed_by_counsel"))
    out.append({
        "key": "consent_counsel", "label": "Counsel review of the consent wording",
        "blocking": not counsel,
        "detail": "config/site.json consent_reviewed_by_counsel = %s." % ("true" if counsel else "false"),
        "fix": "Have counsel sign off on config/consent.json (v5 draft already sits in "
               "config/consent_v5_DRAFT.json) and set the flag.",
        "why": "Real phone leads carry TCPA/FTSA exposure. The flag exists so radio silence is never "
               "mistaken for approval.",
    })
    out.append({
        "key": "indexing", "label": "Search indexing on",
        "blocking": not indexing,
        "detail": f"ALLOW_INDEXING={os.environ.get('ALLOW_INDEXING', '0')} (robots.txt currently disallows everything).",
        "fix": "Publish the site on its real domain with ALLOW_INDEXING=1 and submit /sitemap.xml.",
        "why": "Organic search and the new free-help directory are the only zero-cost channels that "
               "produce inbound visitors without contacting anyone.",
    })
    out.append({
        "key": "live_delivery", "label": "Live delivery to buyers",
        "blocking": not live,
        "detail": f"LEADENGINE_LIVE_DELIVERY={os.environ.get('LEADENGINE_LIVE_DELIVERY', '0')} (dry-run to outbox/).",
        "fix": "Switch on only after a buyer accepts a test delivery and payments are approved.",
        "why": "Until then 'delivered' counts dry-run deliveries, and nothing is charged.",
    })
    activated = (net or {}).get("activated", 0)
    out.append({
        "key": "partners_activated", "label": "At least one referral partner live",
        "blocking": activated == 0,
        "detail": (f"{activated} activated partner(s); {net.get('partners_total', 0)} in the network, "
                   f"{net.get('with_activity', 0)} with tracked activity."),
        "fix": "Approve outreach to the highest-fit organizations in /admin/partners, then log the "
               "action. Nothing is sent by the platform.",
        "why": "A clinic counter card is the only channel here that reaches people at the moment they "
               "need coverage, and it costs nothing per lead.",
    })
    out.append({
        "key": "carrier_verification", "label": "Carrier ownership check on phone numbers",
        "blocking": False,
        "detail": f"{st.get('carrier_verified', 0)} leads carry a carrier-verified flag (no provider connected).",
        "fix": "Approve a phone-intelligence provider, then implement engine/phone.py's lookup hook.",
        "why": "Not a launch blocker - numbers are validated for shape and consent is on record - but "
               "return rates stay higher without it.",
    })
    return out


def report(con, cfg_dir: Path = CFG, test: int = 0) -> dict:
    """The whole acquisition picture: stages, per-channel rows, the buyer gate, network block, blockers."""
    stages = stage_counts(con, test)
    gate = buyer_gate(cfg_dir)
    routable = gate.get("routable_products") or []
    # Eligible for routing = qualified, consumed by a product that has a usable buyer today.
    elig = con.execute(f"""SELECT COUNT(*) n, COALESCE(SUM(product),'') p FROM leads l
          WHERE l.is_test=? AND l.qual_status='QUALIFIED' AND {CONSENT_SQL}""", (test,)).fetchone()
    per = con.execute(f"""SELECT COALESCE(product,'-') k, COUNT(*) n FROM leads l
          WHERE l.is_test=? AND l.qual_status='QUALIFIED' AND {CONSENT_SQL} GROUP BY 1""", (test,)).fetchall()
    stages["routing_eligible"] = sum(r["n"] for r in per if r["k"] in routable)
    stages["qualified_products"] = {r["k"]: r["n"] for r in per}
    rows = funnel_mod.by(con, "channel", "real" if not test else "test")
    net = network_mod.report(con)
    bl = blockers(con, cfg_dir, stages, gate, net, test)
    steps = []
    order = [k for k, _l, _d in STAGES]
    for a, b in zip(order, order[1:]):
        up, dn = stages.get(a) or 0, stages.get(b) or 0
        steps.append({"a": a, "b": b, "up": up, "dn": dn, "lost": max(0, up - dn) if up else 0,
                      "drop_pct": round(100 * (up - dn) / up, 1) if up and dn <= up else None})
    return {"stages": stages, "stage_defs": STAGES, "steps": steps, "by_channel": rows,
            "gate": gate, "network": net, "blockers": bl,
            "test": test,
            "blocking": [b for b in bl if b["blocking"]],
            "environment": {"indexing": os.environ.get("ALLOW_INDEXING", "0") == "1",
                            "live_delivery": os.environ.get("LEADENGINE_LIVE_DELIVERY", "0") == "1"}}


def csv_rows(rep: dict) -> list[dict]:
    """Flatten the stage model to rows a spreadsheet can hold (stage, count, share of visitors)."""
    out = []
    base = (rep["stages"].get("visitors") or 0)
    for key, label, desc in STAGES:
        v = rep["stages"].get(key)
        out.append({"stage": key, "label": label, "count": "" if v is None else v,
                    "pct_of_visitors": (round(100 * v / base, 2) if (base and v is not None) else ""),
                    "definition": desc})
    return out
