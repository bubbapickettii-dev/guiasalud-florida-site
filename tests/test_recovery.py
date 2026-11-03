"""Failure handling in the sold-lead pipeline: a sale must never strand silently.

  - a crash between routing and delivery leaves the lead committed as ROUTED with NO charge:
    the consumer's consent named this buyer, so it is never re-sold - the buyer's desk heals
    it (deliver + charge exactly once) and records why it was stranded
  - recovery is idempotent: repeated desk loads cannot double-deliver, double-charge or re-sell
  - a buyer-notification failure AFTER the sale commits costs only the notification roster-row,
    never the sale itself
Bookkeeping only - no live payments, no live delivery, TEST-flagged leads.
"""
import json, sys, urllib.parse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_billing import make_app, post


def buyer_token(m, buyer_id):
    return next(x["portal_token"] for x in json.loads((m.CFG / "buyers.json").read_text(encoding="utf-8"))
                if x["id"] == buyer_id)


def test_stranded_routed_lead_heals_on_buyer_desk_exactly_once(tmp_path):
    import engine.route as ro
    m = make_app(tmp_path); c = m.app.test_client()
    orig = ro.deliver

    def boom(*a, **k):
        raise RuntimeError("webhook timeout")
    ro.deliver = boom
    try:
        r = post(c, consent_checked=True).get_json()
    finally:
        ro.deliver = orig
    assert r["routed"] == "ROUTED"                      # the sale committed; delivery crashed
    lead = m.con.execute("SELECT * FROM leads").fetchone()
    assert lead["routing_status"] == "ROUTED" and lead["buyer_id"] and lead["price"]
    # stranded, never silently charged
    assert m.con.execute("SELECT COUNT(*) FROM buyer_ledger WHERE kind='LEAD_CHARGE'").fetchone()[0] == 0
    assert m.con.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0
    tok = buyer_token(m, lead["buyer_id"])
    # the buyer's own desk heals it
    c.get(f"/agent/{tok}")
    healed = m.con.execute("SELECT * FROM leads WHERE lead_id=?", (lead["lead_id"],)).fetchone()
    assert healed["routing_status"] == "DELIVERED"
    assert m.con.execute("SELECT COUNT(*) FROM deliveries WHERE lead_id=? AND method!='email_notification'",
                         (lead["lead_id"],)).fetchone()[0] == 1
    charges = [dict(x) for x in m.con.execute("SELECT * FROM buyer_ledger WHERE lead_id=? AND kind='LEAD_CHARGE'", (lead["lead_id"],))]
    assert len(charges) == 1 and charges[0]["amount"] == round(-float(lead["price"]), 2)
    # the stranding and the healing are both on record
    notes = [dict(x)["outcome"] for x in m.con.execute("SELECT outcome FROM lead_outcomes WHERE lead_id=?", (lead["lead_id"],))]
    assert "DELIVERY_RETRY" in notes
    # idempotence: repeated concurrent desk loads heal nothing twice (the race already passed
    # test_concurrent_writes_are_not_lost in test_engine.py — here we prove the SOLD state itself is protected)
    c.get(f"/agent/{tok}")
    assert m.con.execute("SELECT COUNT(*) FROM deliveries WHERE lead_id=? AND method!='email_notification'",
                         (lead["lead_id"],)).fetchone()[0] == 1
    assert len(m.con.execute("SELECT * FROM buyer_ledger WHERE lead_id=? AND kind='LEAD_CHARGE'",
                             (lead["lead_id"],)).fetchall()) == 1


def test_buyer_with_unpriced_tier_never_receives_a_free_sale(tmp_path):
    """A buyer whose price_by_tier lacks a positive price for the lead's tier must never be selected:
    charge() and reconcile_lead() both skip price<=0, so selecting them would deliver the sale FREE
    in silence — a config typo (empty price cell) can never convert into a permanent no-pay delivery."""
    import engine.route as ro
    m = make_app(tmp_path)
    lead = {"lead_id": "L-FREE", "qual_status": "QUALIFIED", "lead_type": "PHONE_LEAD", "phone": "3055550110",
            "product": "term_life", "state": "FL", "age_range": "40-49", "tier": "B"}
    # two otherwise-identical buyers; B's tier price cell is empty (typo), A's is priced
    priced = {"id": "priced-buyer", "active": True, "is_demo": False, "priority": 2, "licensed_states": ["FL"],
              "products": ["term_life"], "tiers": ["A", "B"], "price_by_tier": {"A": 40, "B": 25},
              "daily_cap": 10, "age_min": 18, "age_max": 80}
    typo = {**priced, "id": "typo-buyer", "priority": 1, "price_by_tier": {"A": 40}}   # NO 'B' entry
    buyers = [typo, priced]
    pick = ro.preview(m.con, buyers, "term_life", "FL", "40-49", tier="B")
    assert pick and pick["id"] == "priced-buyer"          # the unpriced buyer is invisible to preview
    res = ro.route(m.con, buyers, lead)
    assert res["routing_status"] == "ROUTED" and res["buyer_id"] == "priced-buyer" and res["price"] == 25
    # with ONLY the typo buyer configured, the lead is held as NO_BUYER — never delivered free;
    # the routing-decision reason names the missing price so the operator can fix the config
    only_typo = ro.route(m.con, [typo], lead)
    assert only_typo == {"routing_status": "NO_BUYER", "buyer_id": None, "price": None, "sale_type": ""}
    reason, _n = ro.explain(m.con, [typo], lead)
    assert "no positive price for tier B" in reason
    # and the direct call cannot be tricked past the preview (defense in depth)
    no_pal = ro.route(m.con, [typo], lead)
    assert no_pal["buyer_id"] is None


def test_notification_failure_never_undoes_a_committed_sale(tmp_path):
    import engine.notify as nt
    m = make_app(tmp_path); c = m.app.test_client()
    orig = nt.buyer_new_lead

    def boom(*a, **k):
        raise RuntimeError("smtp down")
    nt.buyer_new_lead = boom
    try:
        r = post(c, consent_checked=True).get_json()
    finally:
        nt.buyer_new_lead = orig
    assert r["routed"] == "ROUTED"
    lead = m.con.execute("SELECT * FROM leads").fetchone()
    assert lead["routing_status"] == "DELIVERED"        # the sale stands on its own
    assert len(m.con.execute("SELECT * FROM buyer_ledger WHERE lead_id=? AND kind='LEAD_CHARGE'",
                             (lead["lead_id"],)).fetchall()) == 1
    # the sold-lead delivery row exists even though the notification failed
    assert m.con.execute("SELECT COUNT(*) FROM deliveries WHERE lead_id=?",
                         (lead["lead_id"],)).fetchone()[0] >= 1
    # and the buyer's desk still shows the lead (despite the notification failing)
    tok = buyer_token(m, lead["buyer_id"])
    assert lead["lead_id"] in c.get(f"/agent/{tok}").get_data(as_text=True)
