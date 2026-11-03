"""Concurrency at the exact seams where a duplicate would cost money or trust.

The submission path is serialized by a BEGIN IMMEDIATE write-latch before the dedup read
(see app.py /api/lead). These tests reproduce the raced interleavings DETERMINISTICALLY
instead of relying on scheduler luck:

  1. several all-submitted consumers with the same phone whose dedup checks all sleep AFTER
     reading (the stale-read window that produced two sales) -> ONE lead, ONE sale, the rest DUPLICATE
  2. two DIFFERENT consumers submitted at the same moment -> BOTH succeed (the latch serializes
     writers without breaking normal concurrency, and never sheds a stale-snapshot 500)
  3. two RETURNED clicks racing the credit -> exactly ONE LEAD_CREDIT, balance back to zero
  4. two operator GRANT clicks racing -> exactly ONE LEAD_CREDIT (the SQL unique index is the
     last line of defense), and the loser is refused gracefully, never a 500

Pre-fix evidence (recorded before the latch existed): with the sleeps above the duplicate test
produced TWO DELIVERED leads plus a 'database is locked' 500 (WAL stale snapshot at write upgrade).

Bookkeeping only - TEST traffic, no live payments, no real buyers.
"""
import threading, time, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_billing import make_app, BASE, post


def race_n(n, fn):
    """Run fn() on n threads started together; return all results (exceptions included)."""
    out = []

    def run():
        try:
            out.append(fn())
        except Exception as exc:                      # noqa: BLE001 - surfaced by the caller
            out.append(exc)
    ts = [threading.Thread(target=run) for _ in range(n)]
    [t.start() for t in ts]; [t.join() for t in ts]
    return out


def racing_at_request(fn, barrier_n=2):
    """race_n, but the threads rendezvous at a Barrier just before issuing the request itself -
    for paths WITHOUT a write-latch, where the requests can safely run simultaneously."""
    bar = threading.Barrier(barrier_n)
    return race_n(barrier_n, lambda: (bar.wait(timeout=10), fn())[-1])


def test_simultaneous_duplicate_submissions_cannot_become_two_sales(tmp_path):
    """The read-then-write seam: every request's dedup read is followed by a sleep, so with no
    write-latch ALL of them finish checking before ANY inserts. The latch serializes them instead:
    the later request's dedup read happens after the winner commits -> DUPLICATE, never a 2nd sale."""
    import engine.qualify as ql_mod

    m = make_app(tmp_path); c = m.app.test_client()
    orig = ql_mod.is_duplicate

    def slow_dup(*a, **k):                            # sleep AFTER the check: widens the stale-read window
        r = orig(*a, **k)
        time.sleep(0.25)
        return r

    ql_mod.is_duplicate = slow_dup
    K = 3
    try:
        results = race_n(K, lambda: post(c, consent_checked=True).get_json())
    finally:
        ql_mod.is_duplicate = orig
    assert all(not isinstance(r, Exception) for r in results), results
    statuses = sorted(r["status"] for r in results)
    assert statuses == ["DUPLICATE"] * (K - 1) + ["QUALIFIED"], results   # one sold, the rest duplicates
    assert all(r["routed"] != "ROUTED" for r in results if r["status"] == "DUPLICATE"), results
    rows = [dict(r) for r in m.con.execute("SELECT lead_id, routing_status FROM leads").fetchall()]
    sold = [r for r in rows if r["routing_status"] in ("DELIVERED", "ROUTED")]
    assert len(sold) == 1 and sold[0]["routing_status"] == "DELIVERED", rows
    n_assign = m.con.execute("SELECT COUNT(*) FROM routing_decisions WHERE decision='ASSIGNED'").fetchone()[0]
    assert n_assign == 1, n_assign
    charges = m.con.execute("SELECT COUNT(*) FROM buyer_ledger WHERE kind='LEAD_CHARGE'").fetchone()[0]
    assert charges == 1, charges


def test_two_simultaneous_samphone_submissions_only_write_one_lead(tmp_path):
    """Stronger shape of the same seam: same-phone submissions racing with no sleeps at all.
    Every interleaving must end with exactly ONE DELIVERED sale - with no 500s."""
    m = make_app(tmp_path); c = m.app.test_client()
    results = race_n(3, lambda: post(c, consent_checked=True).get_json())
    assert all(not isinstance(r, Exception) for r in results), results
    delivered = m.con.execute("SELECT COUNT(*) FROM leads WHERE routing_status='DELIVERED'").fetchone()[0]
    charges = m.con.execute("SELECT COUNT(*) FROM buyer_ledger WHERE kind='LEAD_CHARGE'").fetchone()[0]
    assert delivered == 1 and charges == 1, (delivered, charges)


def test_concurrent_return_clicks_credit_exactly_once(tmp_path):
    """Two RETURNED requests land together: the SQL-guarded credit makes the second a no-op -
    one LEAD_CREDIT row, balance back to zero, never negative, never double."""
    import engine.billing as bl
    m = make_app(tmp_path); c = m.app.test_client()
    lid = post(c, consent_checked=True).get_json()["lead_id"]
    tok = next(x["portal_token"] for x in m.route.load_buyers(m.CFG) if x.get("is_demo"))

    def click_once():
        return c.post(f"/agent/{tok}", data={"lead_id": lid, "status": "RETURNED"})

    results = racing_at_request(click_once)
    assert all(r.status_code in (302, 303) for r in results), [r.status_code for r in results]
    credits = [dict(x) for x in m.con.execute("SELECT * FROM buyer_ledger WHERE kind='LEAD_CREDIT'")]
    assert len(credits) == 1 and credits[0]["amount"] > 0
    buyer = credits[0]["buyer_id"]
    assert bl.balance(m.con, buyer, 1) == 0.0


def test_concurrent_operator_grants_credit_exactly_once(tmp_path):
    """Two GRANT decisions race: the ledger's unique index is the last line of defense - exactly one
    LEAD_CREDIT, and the losing request is refused gracefully (redirect with a spoken reason, no 500)."""
    import engine.billing as bl
    m = make_app(tmp_path); c = m.app.test_client()
    # a lead refused an automatic credit (outside the 72h window) -> grant request pending
    r = post(c, consent_checked=True).get_json()
    lid = r["lead_id"]
    m.con.execute("UPDATE leads SET delivered_at=datetime('now','-4 days') WHERE lead_id=?", (lid,))
    m.con.commit()
    tok = next(x["portal_token"] for x in m.route.load_buyers(m.CFG) if x.get("is_demo"))
    assert c.post(f"/agent/{tok}", data={"lead_id": lid, "status": "RETURNED"}).status_code in (302, 303)
    buyer = m.con.execute("SELECT buyer_id, price FROM leads WHERE lead_id=?", (lid,)).fetchone()
    assert m.con.execute("SELECT COUNT(*) FROM buyer_ledger WHERE kind='LEAD_CREDIT'").fetchone()[0] == 0

    def grant_once():
        return c.post(f"/admin/lead/{lid}/credit_decision?token=t",
                      data={"decision": "grant", "note": "operator grant"}, follow_redirects=True)

    results = racing_at_request(grant_once)
    assert all(r.status_code == 200 for r in results), [r.status_code for r in results]
    credits = [dict(x) for x in m.con.execute("SELECT * FROM buyer_ledger WHERE lead_id=? AND kind='LEAD_CREDIT'", (lid,))]
    assert len(credits) == 1, credits
    assert bl.balance(m.con, buyer["buyer_id"], 1) == 0.0
