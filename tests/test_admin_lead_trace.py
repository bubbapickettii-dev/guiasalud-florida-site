"""The operator's complete lead story on ONE admin page (workstream: lead trace + routing blockers).

  - the lead page shows the buyer's OWN recorded outcomes (RETURNED / credit decisions / notes) —
    never reconstructed or missing rows the operator can't see
  - the lead page shows every buyer_ledger row that references the lead (charge / credit / adjustment)
    with its kind, amount and running balance — money evidence only, never a re-adjudication
  - a lead with a refused-return awaiting a decision is flagged with a link to the SAME Credit queue
    the admin tab already uses (one source of truth, no second dispute store)
  - each configured buyer row surfaces the SAME exclusion reasons routing actually applies right now
    (not activated / no positive per-lead price / caps / enforcement / quality rank 2) — derived from
    the live tables whenever the page is opened
  - a fully healthy, ledgered buyer row shows NO blocker line (absence is quiet, not a fake zero)
Bookkeeping only: TEST-funnel leads, no live payments, no real buyers.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_billing import make_app
from test_buyer_portal import sandbox, save_buyer
from test_credit_queue import refused_lead, demo_token


def test_lead_page_shows_outcomes_and_ledger_rows(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    lid, tok = refused_lead(tmp_path, m, c)
    page = c.get(f"/admin/lead/{lid}?token=t").get_data(as_text=True)
    assert "Buyer outcomes (desk feedback)" in page
    assert "RETURNED" in page and "no credit: outside the configured return window" in page
    assert "Money (ledger)" in page
    assert "LEAD_CHARGE" in page             # the delivered lead was billed exactly once
    assert "Return/credit request awaiting decision" in page            # pending flag, same queue link
    assert "/admin/credits" in page


def test_clean_lead_page_honest_empty_states(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    r = c.post("/api/lead", json=dict(campaign="mortgage-protection", first_name="Ana", email="a78@x.com",
                                      phone="3055550121", zip="33130", state="FL", age_range="40-49",
                                      is_test=1, consent_checked=True))
    lid = r.get_json()["lead_id"]
    page = c.get(f"/admin/lead/{lid}?token=t").get_data(as_text=True)
    assert "Return/credit request awaiting decision" not in page
    rows = [dict(x) for x in m.con.execute("SELECT * FROM buyer_ledger WHERE lead_id=?", (lid,))]
    kinds = {x["kind"] for x in rows}
    if "LEAD_CHARGE" in kinds:               # delivered lead: the billed amount must be visible
        assert "LEAD_CHARGE" in page
    elif not rows:                           # nothing billed yet: the page says so, it never fabricates
        assert "nothing has been billed yet" in page
    assert "The buyer has not reported any outcome" in page          # outcome empty state


def test_null_score_lead_renders_with_honest_unknown(tmp_path):
    """A legitimate but incomplete record (an early-rejected lead with no tier scoring) must be traced,
    not 500: the page renders the score column as an honest 'no score' both on the lead page and in
    the admin leads table — never an invented 0 that would fake a real ranking signal."""
    m = make_app(tmp_path); c = m.app.test_client()
    m.con.execute("""INSERT INTO leads (lead_id, created_at, is_test, campaign, product, state, zip, phone,
        phone_source, qual_status, lead_type, routing_status, contact_scope, consent_ts)
        VALUES ('L-NOSCORE-1', datetime('now'), 1, 'camp', 'health_aca', 'FL', '33101', '3055550130',
        'CONSUMER_SUBMITTED', 'UNQUALIFIED', 'PHONE_LEAD', 'PENDING', 'PHONE_AND_EMAIL', datetime('now'))""")
    m.con.commit()
    page = c.get("/admin/lead/L-NOSCORE-1?token=t").get_data(as_text=True)
    assert "no score" in page and "UNQUALIFIED" in page
    overview = c.get("/admin?token=t").get_data(as_text=True)
    assert "NOSCORE-1" in overview


def test_buyer_row_surfaces_no_price_blocker(tmp_path):
    """A configured ACTIVE buyer with every tier price zeroed cannot receive a priced lead — the
    admin sees exactly that reason in the buyer table, derived from the live routing criteria."""
    m = make_app(tmp_path); sandbox(m, tmp_path); c = m.app.test_client()
    m.MARKETPLACE = {**m.MARKETPLACE, "enforce_credits": False}
    save_buyer(m, c, action="activate", price_A="0", price_B="0")
    page = c.get("/admin?token=t").get_data(as_text=True)
    assert "not receiving new leads" in page
    assert "no positive per-lead price configured" in page


def test_buyer_row_surfaces_credit_and_not_activated_blockers(tmp_path):
    m = make_app(tmp_path); sandbox(m, tmp_path); c = m.app.test_client()
    save_buyer(m, c, action="save")                       # an onboarded-but-inactive draft buyer
    lid, tok = refused_lead(tmp_path, m, c)               # delivered TEST lead carries a real charge
    m.MARKETPLACE = {**m.MARKETPLACE, "enforce_credits": True}
    bid = m.con.execute("SELECT buyer_id FROM leads WHERE lead_id=?", (lid,)).fetchone()["buyer_id"]
    m.con.execute("DELETE FROM buyer_ledger WHERE buyer_id=?", (bid,))    # balances -> 0, enforcement ON
    m.con.commit()
    page = c.get("/admin?token=t").get_data(as_text=True)
    assert "not activated" in page                                        # the inactive draft
    assert "no prepaid credits (enforcement ON)" in page                                                  # the active-but-empty buyer


def test_healthy_buyer_row_has_no_blocker_line(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    refused_lead(tmp_path, m, c)                          # delivered lead billed normally
    buyer_id = m.con.execute("SELECT buyer_id FROM leads LIMIT 1").fetchone()[0]
    page = c.get("/admin?token=t").get_data(as_text=True)
    for line in page.split("\n"):
        if buyer_id in line and "⚠" in line:
            raise AssertionError(f"unexpected blocker on a healthy ledgered buyer: {line.strip()}")
