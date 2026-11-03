"""Prepaid credit-pack payment rail (Stripe Checkout) — test mode only until approved.

Architecture (verified against Stripe docs, Oct 2026):
  1. The buyer picks a package on the billing page -> the SERVER creates a Checkout Session
     (POST /v1/checkout/sessions, mode=payment) from SERVER-side package data. The browser
     never chooses amount, price, or buyer.
  2. The buyer pays on Stripe's hosted page. The success page only redirects; IT NEVER
     GRANTS CREDITS.
  3. Credits are granted ONLY by the /api/webhooks/stripe endpoint after
     stripe.Webhook.construct_event(...) verifies the Stripe-Signature header against the
     endpoint's whsec_ secret (HMAC-SHA256 of "{t}.{raw_body}", 300s tolerance). Forged or
     replayed events are rejected before any DB write.
  4. Fulfillment is idempotent twice over: a unique webhook_event_id per processed event
     and a one-way PENDING -> PAID -> CREDITED status transition per checkout session.
     Stripe retries / duplicate events cannot double-credit.
  5. Handle BOTH checkout.session.completed and checkout.session.async_payment_succeeded
     but only when payment_status == "paid". Never grant credits on payment_status that is
     not "paid" or on async_payment_failed.

Mode discipline:
  - No STRIPE_SECRET_KEY in env -> rail OFF; billing page shows the operator top-up path.
  - sk_test_ keys -> test-mode sessions (no real money; allowed any time).
  - sk_live_ keys -> REFUSED unless marketplace.payments.live_payments is true AND
    LEADENGINE_ENV=production. Live payments are never switched on by default.

SANDBOX TEST rail (provider="sandbox"): with no Stripe keys the buyer can still rehearse a
package purchase against a LEADENGINE-made checkout page. It reuses the SAME buyer_payments
row lifecycle and the SAME charge-once guards as the Stripe rail, but:
  - it exists ONLY outside production (LEADENGINE_ENV=production refuses it entirely);
  - it ALWAYS books to the is_test=1 (TEST) ledger lane, on any buyer, so a real buyer's
    real-lead balance can never be moved by a simulated purchase;
  - no money moves and the browser *page* is the confirmation — there is no external
    webhook to trust, because there is no money to confirm.
"""
from __future__ import annotations

import os

from .db import now
from . import billing

PAID_EVENTS = ("checkout.session.completed", "checkout.session.async_payment_succeeded")
FAILED_EVENT = "checkout.session.async_payment_failed"
EXPIRED_EVENT = "checkout.session.expired"
PAYMENTS_SCHEMA = ("id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, buyer_id TEXT, package_id TEXT, amount_usd REAL, "
                   "provider TEXT, session_id TEXT UNIQUE, session_url TEXT, status TEXT, "
                   "webhook_event_id TEXT UNIQUE, is_test INTEGER DEFAULT 0, note TEXT")


def _keys() -> tuple[str, str]:
    return os.environ.get("STRIPE_SECRET_KEY", ""), os.environ.get("STRIPE_WEBHOOK_SECRET", "")


def mode() -> str:
    """off | test | live (live additionally requires operator approval, see module doc)."""
    sk, _ = _keys()
    if not sk:
        return "off"
    return "live" if sk.startswith("sk_live_") else ("test" if sk.startswith("sk_test_") else "unknown-key")


def rail_approved(marketplace: dict) -> bool:
    """Live mode may only run with BOTH the operator's explicit config flag AND the production env.
    An AND, not an OR: LEADENGINE_ENV=production alone must never auto-approve real charges —
    the operator turns live payments on by setting marketplace.payments.live_payments, deliberately.
    (sk_test_ is always approved: test-mode sessions can never move real money.)"""
    m = mode()
    if m == "test":
        return True
    if m == "live":
        return bool(marketplace.get("payments", {}).get("live_payments")) and os.environ.get("LEADENGINE_ENV") == "production"
    return False


def configured(marketplace: dict) -> bool:
    """A checkout can be created: secret key present (test, or approved live) AND webhook secret present."""
    sk, wh = _keys()
    m = mode()
    if not sk or not wh or m in ("off", "unknown-key"):
        return False
    return rail_approved(marketplace)


def _create_session(buyer: dict, package: dict, base_url: str) -> dict:
    """Thin wrapper around Stripe so tests can stub it. Returns the API response dict."""
    import stripe
    s = stripe.checkout.Session.create(
        mode="payment",
        client_reference_id=buyer["id"],
        line_items=[{
            "price_data": {"currency": "usd", "unit_amount": int(round(float(package["amount_usd"]) * 100)),
                           "product_data": {"name": f"{package.get('label', 'Credit pack')} — prepaid lead credits"}},
            "quantity": 1,
        }],
        metadata={"buyer_id": buyer["id"], "package_id": package.get("id", "")},
        # prefill Checkout with the buyer's delivery/reply-to email (declared on the application) so their
        # Stripe receipt matches the address we already correspond on; empty when the buyer has none.
        customer_email=(buyer.get("delivery") or {}).get("email") or None,
        success_url=f"{base_url}/agent/{buyer['portal_token']}/billing?payment=returning",
        cancel_url=f"{base_url}/agent/{buyer['portal_token']}/billing?payment=cancelled",
    )
    return {"id": s["id"], "url": s["url"]}


def create_checkout(con, buyer: dict, package: dict, base_url: str, marketplace: dict, is_test: int = 0) -> dict | None:
    """Create the session server-side, record the PENDING payment row, return {url, session_id}.
    Credits NOTHING here. Returns None when the rail is off / unapproved."""
    if not configured(marketplace):
        return None
    amount = float(package.get("amount_usd") or 0)
    if amount <= 0:
        raise ValueError("package amount_usd must be positive")
    s = _create_session(buyer, package, base_url)
    cur = con.execute(
        "INSERT OR IGNORE INTO buyer_payments (ts, buyer_id, package_id, amount_usd, provider, session_id, "
        "session_url, status, is_test, note) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (now(), buyer["id"], package.get("id", ""), round(amount, 2), "stripe", s["id"], s["url"], "PENDING",
         is_test, "awaiting webhook confirmation - no credits granted yet"[:300]))
    if cur.rowcount == 0:  # session already recorded (retry): reuse the existing row
        s["url"] = con.execute("SELECT session_url FROM buyer_payments WHERE session_id=?", (s["id"],)).fetchone()[0]
    con.commit()
    return {"url": s["url"], "session_id": s["id"], "amount_usd": round(amount, 2), "package_id": package.get("id", "")}


def _fulfill(con, event_id: str, session: dict, marketplace: dict) -> dict:
    """Grant the credit ONCE for a verified paid session. Never called with unverified input.
    The credited amount is the one the SERVER recorded at session creation (row.amount_usd).

    Returns {'state': credited|duplicate|refused|not_found, 'credited': bool, ...}."""
    row = con.execute("SELECT * FROM buyer_payments WHERE session_id=? AND provider='stripe'",
                      (session["id"],)).fetchone()
    if not row:
        return {"state": "not_found", "credited": False, "amount_usd": 0,
                "reason": f"no local PENDING payment row for session {session['id']} - the checkout must be "
                          "created server-side first; nothing is credited without one"}
    cur = con.execute("UPDATE buyer_payments SET status='PAID', webhook_event_id=? WHERE id=? AND status='PENDING'",
                      (event_id, row["id"]))
    con.commit()
    if cur.rowcount == 0:
        return {"state": "duplicate", "credited": False, "amount_usd": float(row["amount_usd"]),
                "reason": "payment already processed"}
    amount = float(row["amount_usd"])
    if amount <= 0:
        return {"state": "refused", "credited": False, "amount_usd": amount, "reason": "non-positive amount"}
    ledger = billing.record_package(con, row["buyer_id"], {"id": row["package_id"], "amount_usd": amount},
                                    note=f"stripe checkout {session['id']}", is_test=row["is_test"])
    con.execute("UPDATE buyer_payments SET status='CREDITED' WHERE id=?", (row["id"],))
    con.commit()
    return {"state": "credited", "credited": True, "amount_usd": amount,
            "ledger_id": ledger["id"], "buyer_id": row["buyer_id"], "package_id": row["package_id"],
            "is_test": row["is_test"]}


def handle_webhook(con, payload: bytes, sig_header: str, marketplace: dict) -> dict:
    """Verify the signature, then fulfil only verified paid-completion events. Never trusts the client."""
    import stripe
    _, wh = _keys()
    if not wh:
        return {"ok": False, "http": 503, "result": None, "reason": "webhook secret not configured"}
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, wh)
    except Exception as e:  # signature.SignatureVerificationError, json decode, tolerance -> always 400
        return {"ok": False, "http": 400, "result": None, "reason": f"signature/verification failed: {type(e).__name__}"}
    etype, obj = event["type"], (event["data"]["object"] or {})
    sid = (obj.get("id") or "") if isinstance(obj, dict) else ""
    # first-write-wins dedupe on the raw event id (stripe.Webhook.construct_event does not dedupe)
    try:
        con.execute("INSERT INTO webhook_events (event_id) VALUES (?)", (event["id"],))
        con.commit()
    except Exception:
        con.rollback()
        return {"ok": True, "http": 200, "result": {"state": "duplicate", "credited": False}, "reason": f"event {event['id']} already handled"}
    if etype in PAID_EVENTS:
        paid = obj.get("payment_status") == "paid"
        res = {"state": "ignored_unpaid", "credited": False, "amount_usd": 0,
               "reason": f"{etype} with payment_status={obj.get('payment_status')!r} -> no credits"}
        if paid:
            res = _fulfill(con, event["id"], obj, marketplace)
        return {"ok": True, "http": 200, "result": res, "reason": ""}
    if etype in (FAILED_EVENT, EXPIRED_EVENT):
        if sid:
            con.execute("UPDATE buyer_payments SET status='FAILED' WHERE session_id=? AND status='PENDING'", (sid,))
            con.commit()
        return {"ok": True, "http": 200, "result": {"state": "marked_failed", "credited": False}, "reason": etype}
    return {"ok": True, "http": 200, "result": {"state": "ignored", "credited": False,
            "amount_usd": 0, "reason": f"event type {etype} not handled"}, "reason": f"unhandled event type {etype}"}


# -----------------------------------------------------------------------------------------------
# SANDBOX TEST checkout — rehearsal purchases against the ledger, clearly labeled, TEST lane only.
# -----------------------------------------------------------------------------------------------
SANDBOX_NOTE = "SANDBOX TEST purchase: no money moves, credits land in the TEST lane only"


def sandbox_allowed() -> bool:
    """The sandbox rail exists ONLY outside production: a simulated checkout has no business
    sitting next to a real payment rail in production (and must never be reachable there)."""
    return os.environ.get("LEADENGINE_ENV") != "production"


def create_sandbox_checkout(con, buyer: dict, package: dict) -> int:
    """Record a PENDING sandbox purchase row and return its rowid for the confirmation page.
    Prices come from SERVER-side package config, never the browser — same rule as Stripe.
    is_test is hard-wired to 1: a sandbox purchase is TEST activity by definition."""
    amount = float(package.get("amount_usd") or 0)
    if amount <= 0:
        raise ValueError("package amount_usd must be positive")
    cur = con.execute(
        "INSERT INTO buyer_payments (ts, buyer_id, package_id, amount_usd, provider, session_id, "
        "session_url, status, is_test, note) VALUES (?,?,?,?,?,NULL,NULL,'PENDING',1,?)",
        (now(), buyer["id"], package.get("id", ""), round(amount, 2), "sandbox", SANDBOX_NOTE[:300]))
    con.commit()
    return cur.lastrowid


def sandbox_row(con, rowid: int, buyer_id: str) -> dict | None:
    """The buyer's OWN sandbox purchase row (provider-filtered, buyer-scoped) or None."""
    r = con.execute("SELECT * FROM buyer_payments WHERE id=? AND buyer_id=? AND provider='sandbox'",
                    (rowid, buyer_id)).fetchone()
    return dict(r) if r else None


def fulfill_sandbox(con, rowid: int, buyer_id: str) -> dict:
    """Confirm the sandbox purchase ONCE: one-way PENDING -> CREDITED, then ONE PACKAGE_PURCHASE
    ledger row on the TEST lane. Returns {'state': credited|duplicate|refused|not_found, ...}.
    A buyer can only ever reach their own row (sandbox_row filters on buyer_id)."""
    row = sandbox_row(con, rowid, buyer_id)
    if not row:
        return {"state": "not_found", "credited": False, "amount_usd": 0,
                "reason": "no such sandbox purchase for this buyer"}
    amount = float(row["amount_usd"])
    if row["status"] != "PENDING":
        return {"state": "duplicate", "credited": False, "amount_usd": amount,
                "reason": "sandbox purchase already resolved (credited or cancelled)"}
    if amount <= 0:
        con.execute("UPDATE buyer_payments SET status='FAILED', note=? WHERE id=?",
                    (SANDBOX_NOTE[:240] + " | refused: non-positive amount", rowid))
        con.commit()
        return {"state": "refused", "credited": False, "amount_usd": amount, "reason": "non-positive amount"}
    cur = con.execute("UPDATE buyer_payments SET status='CREDITED' WHERE id=? AND status='PENDING'", (rowid,))
    if cur.rowcount == 0:  # raced to PENDING -> resolved between the checks above
        con.commit()
        return {"state": "duplicate", "credited": False, "amount_usd": amount,
                "reason": "sandbox purchase already resolved"}
    ledger = billing.record_package(con, buyer_id, {"id": row["package_id"], "amount_usd": amount},
                                    note=SANDBOX_NOTE, is_test=1)
    con.commit()
    return {"state": "credited", "credited": True, "amount_usd": amount, "ledger_id": ledger["id"],
            "buyer_id": buyer_id, "package_id": row["package_id"], "is_test": 1,
            "reason": "TEST lane credited; no money moved"}


def cancel_sandbox(con, rowid: int, buyer_id: str) -> bool:
    """PENDING -> FAILED, nothing ledgered. Returns whether a pending row was cancelled."""
    cur = con.execute("UPDATE buyer_payments SET status='FAILED' WHERE id=? AND buyer_id=? "
                      "AND provider='sandbox' AND status='PENDING'", (rowid, buyer_id))
    con.commit()
    return cur.rowcount > 0


def pending(con, buyer_id: str, is_test: int) -> list:
    return [dict(r) for r in con.execute(
        "SELECT ts, package_id, amount_usd, status, session_id, session_url FROM buyer_payments "
        "WHERE buyer_id=? AND is_test=? ORDER BY id DESC LIMIT 25", (buyer_id, is_test))]


def deliveries(con, buyer_id: str, is_test: int) -> list:
    """Recent delivered leads with price + agent status: the buyer's delivery receipt list."""
    return [dict(r) for r in con.execute(
        "SELECT lead_id, delivered_at, tier, price, agent_status, lang FROM leads "
        "WHERE buyer_id=? AND is_test=? AND routing_status='DELIVERED' ORDER BY delivered_at DESC LIMIT 25",
        (buyer_id, is_test))]
