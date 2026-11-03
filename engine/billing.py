"""Buyer billing ledger — bookkeeping ONLY, no money moves in code.

Model: prepaid credits measured in USD.
  PACKAGE_PURCHASE  +amount  recorded by the OPERATOR only after funds were actually received
                              (bank transfer / invoice payment confirmed offline). Never automatic.
  LEAD_CHARGE       -price    recorded when a lead is DELIVERED to the buyer (price = the buyer's
                              configured price_by_tier from config/buyers.json — never estimated here).
  LEAD_CREDIT       +price    recorded when the delivered lead is RETURNED as unusable (same price back).
  ADJUSTMENT        signed    operator hand-correction, always with a note.

Idempotency: a lead is charged at most once per buyer and credited at most once per
buyer (guarded in SQL), so re-delivery or repeated status clicks can't double-bill.

Enforcement: when config/marketplace.json sets enforce_credits=true, buyers whose real
(non-TEST) balance is below min_balance_usd are excluded from routing BEFORE the routing
preview — so a buyer is never named in the consumer's consent if they can't pay for the
resulting lead, and no lead is delivered to a buyer with an empty account. The default
config keeps enforcement OFF so the existing consumer funnel behaves exactly as before
until a real buyer exists and the operator flips the switch.

Unit economics ("$1000/week buyer") are an UNVERIFIED commercial hypothesis: this module
only makes pricing/package parameters configurable — nothing in here assumes revenue.
"""
from __future__ import annotations

import json
from pathlib import Path

from .db import now

PACKAGE_PURCHASE = "PACKAGE_PURCHASE"
LEAD_CHARGE = "LEAD_CHARGE"
LEAD_CREDIT = "LEAD_CREDIT"
ADJUSTMENT = "ADJUSTMENT"
KINDS = (PACKAGE_PURCHASE, LEAD_CHARGE, LEAD_CREDIT, ADJUSTMENT)

DEFAULTS = {"enforce_credits": False, "min_balance_usd": 0.0, "packages": [], "mode": "prepaid_credits"}


def load_marketplace(cfg_dir: Path) -> dict:
    p = cfg_dir / "marketplace.json"
    # encoding='utf-8': on Windows the default cp1252 mangles accented text
    cfg = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    return {**DEFAULTS, **cfg}


def defaults() -> dict:
    return dict(DEFAULTS)


def balance(con, buyer_id: str, is_test: int | None = None) -> float:
    """Signed USD balance. is_test=0 -> real-lead ledger only; 1 -> TEST traffic only; None -> both."""
    q, args = "SELECT COALESCE(SUM(amount),0) FROM buyer_ledger WHERE buyer_id=?", [buyer_id]
    if is_test is not None:
        q += " AND is_test=?"
        args.append(is_test)
    return round(con.execute(q, args).fetchone()[0] or 0.0, 2)


def _record(con, buyer_id: str, kind: str, amount: float, is_test: int,
            lead_id: str = "", package_id: str = "", note: str = "", dedupe: bool = False) -> dict | None:
    """Insert one ledger row, then stamp the running balance. Returns the row, or None when deduped."""
    if kind not in KINDS:
        raise ValueError(f"unknown ledger kind {kind}")
    if package_id and kind != PACKAGE_PURCHASE:
        raise ValueError("package_id only on PACKAGE_PURCHASE")
    vals = (now(), buyer_id, lead_id, "" if not package_id else package_id, kind, round(float(amount), 2), 0.0, note[:300], is_test)
    if dedupe:  # one charge / one credit per lead+buyer, enforced by the partial unique index (ux_ledger_lead)
        cur = con.execute(
            "INSERT OR IGNORE INTO buyer_ledger (ts,buyer_id,lead_id,package_id,kind,amount,balance_after,note,is_test) "
            "VALUES (?,?,?,?,?,?,?,?,?)", vals)
        if cur.rowcount == 0:  # already charged/credited
            con.commit()
            return None
    else:
        cur = con.execute(
            "INSERT INTO buyer_ledger (ts,buyer_id,lead_id,package_id,kind,amount,balance_after,note,is_test) "
            "VALUES (?,?,?,?,?,?,?,?,?)", vals)
    lid = cur.lastrowid
    con.execute("UPDATE buyer_ledger SET balance_after=? WHERE id=?", (balance(con, buyer_id, is_test), lid))
    con.commit()
    return dict(con.execute("SELECT * FROM buyer_ledger WHERE id=?", (lid,)).fetchone())


def record_package(con, buyer_id: str, package: dict, note: str = "", is_test: int = 0) -> dict:
    """Record a prepaid credit pack. THE CALLER MUST CHECK THAT MONEY WAS RECEIVED — this only writes the ledger."""
    amount = float(package.get("amount_usd") or 0)
    if amount <= 0:
        raise ValueError("package amount_usd must be positive")
    return _record(con, buyer_id, PACKAGE_PURCHASE, amount, is_test, package_id=package.get("id", ""), note=note)


def charge(con, buyer_id: str, lead: dict, buyer_demo: bool | None = None) -> dict | None:
    """Record the per-lead charge once, at DELIVERY (price was fixed by the buyer's config at routing).

    Channel invariant: the ledger row's lane follows the TRANSACTION, not just the lead. Activity
    inside a DEMO buyer's account is rehearsal traffic (no real money can ever move there), so the
    row must ride the demo lane or the buyer's own balance/ledger/cards exclude a sale that really
    happened. buyer_demo, when known, wins over the lead's is_test flag."""
    price = lead.get("price")
    if not price or float(price) <= 0:
        return None
    is_test = 1 if (buyer_demo or lead.get("is_test")) else 0
    return _record(con, buyer_id, LEAD_CHARGE, -abs(float(price)), is_test,
                   lead_id=lead["lead_id"], note=f"tier {lead.get('tier')}", dedupe=True)


def credit_return(con, buyer_id: str, lead: dict, buyer_demo: bool | None = None) -> dict | None:
    """Credit a delivered lead back (agent marked RETURNED). At most once per lead+buyer.
    Same channel invariant as charge(): a demo buyer's return always rides the demo lane."""
    price = lead.get("price")
    if not price or float(price) <= 0:
        return None
    is_test = 1 if (buyer_demo or lead.get("is_test")) else 0
    return _record(con, buyer_id, LEAD_CREDIT, abs(float(price)), is_test,
                   lead_id=lead["lead_id"], note=f"returned tier {lead.get('tier')}", dedupe=True)


def apply_adjustment(con, buyer_id: str, amount: float, note: str, is_test: int = 0) -> dict:
    if not note.strip():
        raise ValueError("an adjustment always carries a note")
    return _record(con, buyer_id, ADJUSTMENT, float(amount), is_test, note=note)


def reconcile_lead(con, lead: dict) -> dict | None:
    """Self-healing: a lead that IS delivered but has no LEAD_CHARGE row gets charged exactly once
    (the SQL-guarded dedupe makes reruns no-ops). Called on buyer-facing page loads so a crash or
    partial write between delivery and charge can never leave the buyer paying nothing in silence."""
    if lead.get("routing_status") != "DELIVERED" or not lead.get("price") or float(lead["price"]) <= 0:
        return None
    row = con.execute("SELECT 1 FROM buyer_ledger WHERE buyer_id=? AND lead_id=? AND kind='LEAD_CHARGE'",
                      (lead["buyer_id"], lead["lead_id"])).fetchone()
    return None if row else charge(con, lead["buyer_id"], lead,
                                   buyer_demo=lead.get("_buyer_demo"))


def enforcement_on(marketplace: dict, production: bool = False) -> bool:
    """Is prepaid credit enforcement active for this lane?

    Two independent reasons, deliberately: the operator's config switch (staging rehearsals and
    any deployment that wants ledger-only bookkeeping) OR running live. A production deployment
    always enforces credits on REAL traffic — the alternative is delivering a consented consumer's
    phone to a buyer whose balance cannot cover the price, which is an unbilled sale, not revenue.
    """
    return bool(marketplace.get("enforce_credits")) or bool(production)


def blocked_ids(con, buyers: list[dict], marketplace: dict, is_test: int, production: bool = False) -> frozenset:
    """Buyers that routing must not name in the consumer consent when enforcement is on:
    anyone whose balance is not strictly above min_balance_usd (default 0 = must hold positive credits)."""
    if not enforcement_on(marketplace, production):
        return frozenset()
    floor = float(marketplace.get("min_balance_usd", 0) or 0)
    return frozenset(b["id"] for b in buyers if balance(con, b["id"], is_test) <= floor)


CREDIT_REQUEST_NOTE_PREFIX = "no credit: "   # written by app._set_status when a return gets no automatic credit
CREDIT_DECISION = "CREDIT_DECISION"            # lead_outcomes outcome written by an operator decision (grant/deny)


def pending_credit_requests(con) -> list[dict]:
    """Return-credit requests a buyer has made that got NO automatic credit (outside the return window,
    contacted already, returns disabled...) and are still awaiting an operator decision.

    Detection is evidence-first, never invented:
      - the evidence is a lead_outcomes row outcome='RETURNED' whose note starts with the stable
        refusal prefix above (app._set_status records the refusal there — buyer-visible too),
      - the queue excludes a lead+buyer with a LEAD_CREDIT ledger row (already credited, e.g. by the
        desk's own heal), and one whose LAST credit decision is newer than its LAST refusal.
    Every row keeps its is_test flag so the operator can tell TEST rehearsals from real requests.
    """
    rows = con.execute("""
      SELECT l.lead_id, l.buyer_id, l.is_test, l.price, l.agent_status, l.delivered_at, l.tier,
             (SELECT MAX(id) FROM lead_outcomes x WHERE x.lead_id=l.lead_id AND x.buyer_id=l.buyer_id
                AND x.outcome='RETURNED' AND x.note LIKE ?) AS last_refusal_id,
             (SELECT MAX(id) FROM lead_outcomes x WHERE x.lead_id=l.lead_id AND x.buyer_id=l.buyer_id
                AND x.outcome=?) AS last_decision_id,
             (SELECT note FROM lead_outcomes x WHERE x.lead_id=l.lead_id AND x.buyer_id=l.buyer_id
                AND x.outcome='RETURNED' AND x.note LIKE ? ORDER BY x.id DESC LIMIT 1) AS refusal_note,
             (SELECT note FROM lead_outcomes x WHERE x.lead_id=l.lead_id AND x.buyer_id=l.buyer_id
                AND x.outcome='RETURNED' AND x.note NOT LIKE ? ORDER BY x.id DESC LIMIT 1) AS reason_note
      FROM leads l
      WHERE l.routing_status='DELIVERED'
        AND EXISTS (SELECT 1 FROM lead_outcomes x WHERE x.lead_id=l.lead_id AND x.buyer_id=l.buyer_id
                      AND x.outcome='RETURNED' AND x.note LIKE ?)
        AND NOT EXISTS (SELECT 1 FROM buyer_ledger b WHERE b.lead_id=l.lead_id AND b.buyer_id=l.buyer_id
                          AND b.kind='LEAD_CREDIT')
      ORDER BY l.delivered_at DESC
    """, (CREDIT_REQUEST_NOTE_PREFIX + "%", CREDIT_DECISION, CREDIT_REQUEST_NOTE_PREFIX + "%",
          CREDIT_REQUEST_NOTE_PREFIX + "%", CREDIT_REQUEST_NOTE_PREFIX + "%")).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        # a decision made AFTER the latest refusal clears the request; an older decision does not
        if d["last_decision_id"] is not None and d["last_refusal_id"] is not None \
                and d["last_decision_id"] > d["last_refusal_id"]:
            continue
        out.append(d)
    return out


def decided_credit_requests(con, limit: int = 25) -> list[dict]:
    """The operator's audit trail: recent credit decisions (grant/deny + note) and the ledger state
    that followed — enough to see WHAT was decided and FROM WHOSE ledger, never a re-adjudication."""
    return [dict(r) for r in con.execute("""
      SELECT o.ts, o.lead_id, o.buyer_id, o.outcome, o.note, l.price, l.is_test, l.tier,
             (SELECT COUNT(*) FROM buyer_ledger b WHERE b.lead_id=o.lead_id AND b.buyer_id=o.buyer_id
                AND b.kind='LEAD_CREDIT') AS credited
      FROM lead_outcomes o JOIN leads l ON l.lead_id=o.lead_id
      WHERE o.outcome=? ORDER BY o.id DESC LIMIT ?
    """, (CREDIT_DECISION, int(limit))).fetchall()]


def return_history(con, buyer_id: str, is_test: int, limit: int = 25) -> list[dict]:
    """The buyer's OWN return/credit story for the billing page — evidence joined from the existing
    records, never a parallel dispute store:
      - the RETURNED lead_outcomes rows ARE the buyer-submitted requests (note "returned: ..." when
        the policy auto-credited it, "no credit: ..." when the policy refused it — that prefix is the
        same evidence the operator queue runs on);
      - the CREDIT_DECISION rows ARE the operator decisions (always with a written why);
      - the buyer_ledger LEAD_CREDIT row is the ONLY proof money actually went back — a decision alone
        is never treated as a posted credit. The status below is derived from these records and nothing
        else; a grant decision without a ledger row is shown as an inconsistency, not as "credited".
    Scoped to the authenticated buyer + TEST scope; LIMIT caps the page like every other panel.
    """
    rows = con.execute("""
      SELECT l.lead_id, l.tier, l.price, l.delivered_at, l.agent_status, l.is_test,
        (SELECT MIN(x.ts) FROM lead_outcomes x WHERE x.lead_id=l.lead_id AND x.buyer_id=l.buyer_id AND x.outcome='RETURNED') AS request_ts,
        (SELECT note FROM lead_outcomes x WHERE x.lead_id=l.lead_id AND x.buyer_id=l.buyer_id AND x.outcome='RETURNED' ORDER BY x.id DESC LIMIT 1) AS return_note,
        (SELECT COUNT(*) FROM lead_outcomes x WHERE x.lead_id=l.lead_id AND x.buyer_id=l.buyer_id AND x.outcome='RETURNED') AS request_count,
        (SELECT ts FROM lead_outcomes x WHERE x.lead_id=l.lead_id AND x.buyer_id=l.buyer_id AND x.outcome='CREDIT_DECISION' ORDER BY x.id DESC LIMIT 1) AS decision_ts,
        (SELECT note FROM lead_outcomes x WHERE x.lead_id=l.lead_id AND x.buyer_id=l.buyer_id AND x.outcome='CREDIT_DECISION' ORDER BY x.id DESC LIMIT 1) AS decision_note,
        (SELECT amount FROM buyer_ledger b WHERE b.lead_id=l.lead_id AND b.buyer_id=l.buyer_id AND b.kind='LEAD_CREDIT' ORDER BY b.id DESC LIMIT 1) AS credit_amount
      FROM leads l
      WHERE l.buyer_id=? AND l.is_test=?
        AND (EXISTS (SELECT 1 FROM lead_outcomes x WHERE x.lead_id=l.lead_id AND x.buyer_id=l.buyer_id AND x.outcome='RETURNED')
          OR EXISTS (SELECT 1 FROM lead_outcomes x WHERE x.lead_id=l.lead_id AND x.buyer_id=l.buyer_id AND x.outcome=?)
          OR EXISTS (SELECT 1 FROM buyer_ledger b WHERE b.lead_id=l.lead_id AND b.buyer_id=l.buyer_id AND b.kind='LEAD_CREDIT'))
      ORDER BY COALESCE((SELECT MAX(x.id) FROM lead_outcomes x WHERE x.lead_id=l.lead_id),0) DESC, l.lead_id DESC
      LIMIT ?
    """, (buyer_id, is_test, CREDIT_DECISION, int(limit))).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        note = d["return_note"] or ""
        d["refused"] = note.startswith(CREDIT_REQUEST_NOTE_PREFIX)
        grant = bool(d["decision_note"]) and d["decision_note"].startswith("operator grant")
        if d["credit_amount"] is not None:
            d["status"] = "Credited back to your balance"          # ledger-confirmed money
        elif grant:
            d["status"] = "Operator decision recorded — no ledger credit: check with the operator"
        elif d["decision_note"]:
            d["status"] = "Operator denied — no credit"
        elif d["refused"]:
            d["status"] = "Awaiting operator review"
        else:
            d["status"] = "Return recorded — no credit posted"
        out.append(d)
    return out


def buyer_summary(con, buyer_id: str, is_test: int) -> dict:
    """Buyer account view: balance, package spend, charges, return credits, delivered count, recent ledger."""
    txns = [dict(r) for r in con.execute(
        "SELECT ts, kind, amount, balance_after, lead_id, package_id, note FROM buyer_ledger "
        "WHERE buyer_id=? AND is_test=? ORDER BY id DESC LIMIT 50", (buyer_id, is_test))]
    delivered = con.execute("SELECT COUNT(*) FROM leads WHERE buyer_id=? AND routing_status='DELIVERED' AND is_test=?",
                            (buyer_id, is_test)).fetchone()[0]
    rng = lambda kind: round(con.execute(
        "SELECT COALESCE(SUM(amount),0) FROM buyer_ledger WHERE buyer_id=? AND is_test=? AND kind=?",
        (buyer_id, is_test, kind)).fetchone()[0] or 0.0, 2)
    charged, credited, purchased = rng(LEAD_CHARGE), rng(LEAD_CREDIT), rng(PACKAGE_PURCHASE)
    return {"balance": balance(con, buyer_id, is_test), "purchased": purchased, "charged": round(-charged, 2),
            "credited": credited, "delivered": delivered,
            "avg_per_lead": round(-charged / delivered, 2) if delivered else None, "txns": txns}


def per_week(con, buyer_id: str, days: int, is_test: int) -> float:
    """Charges over the last `days` days, real ledger only — for buyer-facing spend reporting."""
    r = con.execute("SELECT COALESCE(SUM(-amount),0) FROM buyer_ledger WHERE buyer_id=? AND is_test=? "
                    "AND kind=? AND ts >= datetime('now', ?)", (buyer_id, is_test, LEAD_CHARGE, f"-{int(days)} days")).fetchone()
    return round(r[0] or 0.0, 2)
