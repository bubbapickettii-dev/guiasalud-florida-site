"""Partner acquisition analytics (admin /admin/acquisition):

  - the registry-wide partner view shows EVERY registered partner — including zero-activity ones as
    real ZEROS, with '-' (unknown) reserved for partner-level cost that no data can attribute
  - funnel math reuses the existing attribution (partner_id, utm_content) — no second attribution system
  - duplicate-flagged leads never enter Qualified (no double counting); dispositions stay honest:
    RETURNED / BAD_INFO / DNC separately; DNC is never a failure
  - cost exists ONLY where the operator recorded it in ops_log: an item with no recorded spend gets
    cost='-' and no cost/qualified, never an invented 0
  - untracked utm_content values with real leads surface as attribution leaks
  - test traffic never contaminates real numbers (and is viewable with ?test=1)

Bookkeeping only: synthetic visitor/event/lead rows, no live delivery, no payments.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_billing import make_app
from engine import partners as partners_mod, ops as ops_mod

TS = "2026-10-20T10:00:00"


def visit(con, vid, pid=None, test=0, tool_done=False):
    con.execute("INSERT INTO visitors (visitor_id, first_seen, last_seen, partner_id, stage, is_test) VALUES (?,?,?,?,?,?)",
                (vid, TS, TS, pid, "SUBMITTED" if tool_done else "VISITOR", test))
    con.execute("INSERT INTO events (ts, visitor_id, event, utm_content, partner_id, is_test) VALUES (?,?,?,?,?,?)",
                (TS, vid, "page_view", pid and f"pc-{pid}", pid, test))
    if tool_done:
        con.execute("INSERT INTO events (ts, visitor_id, event, utm_content, partner_id, is_test) VALUES (?,?,?,?,?,?)",
                    (TS, vid, "tool_complete", pid and f"pc-{pid}", pid, test))


def lead(con, lid, *, partner=None, content=None, phone="3055550110", valid=1, qual="QUALIFIED",
         routing="PENDING", agent=None, price=None, test=0):
    """One consented phone lead (or an invalid/unqualified variant) with the chosen attribution."""
    con.execute("""INSERT INTO leads (lead_id, created_at, is_test, partner_id, utm_content, phone, phone_source,
                   phone_valid, lead_type, qual_status, routing_status, agent_status, price, contact_scope)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (lid, TS, test, partner, content, phone if valid else None, "CONSUMER_SUBMITTED" if valid else None,
                 valid, "PHONE_LEAD" if valid else "EMAIL_ONLY_INQUIRY", qual, routing, agent, price, "PHONE_AND_EMAIL"))


def sold(con, lid, agent="buyer-x", price=60.0, routing="DELIVERED", agent_status="SOLD"):
    con.execute("UPDATE leads SET routing_status=?, buyer_id=?, agent_status=?, price=?, delivered_at=? WHERE lead_id=?",
                (routing, agent, agent_status, price, TS, lid))


# ---------- partner registry view ----------

def test_partner_registry_shows_zero_activity_as_real_zeros(tmp_path):
    m = make_app(tmp_path)
    partners_mod.create(m.con, {"partner_id": "quiet-partner", "display_name": "Quiet Partner Inc", "type": "re_school"})
    partners_mod.create(m.con, {"partner_id": "busy-partner", "display_name": "Busy Partner LLC", "type": "cpa"})
    visit(m.con, "v-busy-1", pid="busy-partner", tool_done=True)
    visit(m.con, "v-busy-2", pid="busy-partner")
    lead(m.con, "LBP1", partner="busy-partner", qual="QUALIFIED")
    sold(m.con, "LBP1")
    m.con.commit()

    rows, gap = partners_mod.performance(m.con, 0)
    byp = {r["partner_id"]: r for r in rows}
    q, b = byp["quiet-partner"], byp["busy-partner"]
    assert q["visits"] == 0 and q["qualified"] == 0          # real zeros, the partner exists and shows
    assert q["cost_usd"] is None                             # UNKNOWN, never an invented 0
    assert b["visits"] == 2 and b["check_done"] == 1         # unique visitors, not event counts
    assert b["asked_help"] == 1 and b["qualified"] == 1
    assert b["sold"] == 1 and b["delivered"] == 1 and b["revenue"] == 60.0


def test_duplicates_and_dispositions_counted_honestly(tmp_path):
    m = make_app(tmp_path)
    partners_mod.create(m.con, {"partner_id": "p1", "display_name": "P1", "type": "cpa"})
    lead(m.con, "LG1", partner="p1", qual="QUALIFIED")
    lead(m.con, "LD1", partner="p1", qual="DUPLICATE")       # the same consumer resubmitting
    lead(m.con, "LE1", partner="p1", qual="UNQUALIFIED")
    sold(m.con, "LG1", routing="DELIVERED", agent_status="SOLD", price=50.0)   # sold revenue
    sold(m.con, "LE1", routing="DELIVERED", agent_status="RETURNED", price=40.0)  # returned: price back
    sold(m.con, "LD1", routing="NO_BUYER")
    m.con.commit()
    r = partners_mod.performance(m.con, 0)[0][0]
    assert (r["consented"], r["qualified"], r["dup"]) == (3, 1, 1)   # dup excluded from qualified
    assert (r["delivered"], r["sold"], r["returned"]) == (2, 1, 1)
    assert r["revenue"] == 50.0                                     # the returned 40 is not revenue


def test_dnc_is_not_a_bad_outcome_and_test_traffic_is_excluded(tmp_path):
    m = make_app(tmp_path)
    partners_mod.create(m.con, {"partner_id": "p1", "display_name": "P1", "type": "cpa"})
    lead(m.con, "LR1", partner="p1", qual="QUALIFIED")
    sold(m.con, "LR1", agent_status="BAD_INFO", price=33.0)
    lead(m.con, "LD2", partner="p1", qual="QUALIFIED")
    sold(m.con, "LD2", agent_status="DNC", price=44.0)
    lead(m.con, "LT1", partner="p1", qual="QUALIFIED", test=1)
    sold(m.con, "LT1", agent_status="SOLD", price=99.0, routing="DELIVERED")
    m.con.commit()
    real = partners_mod.performance(m.con, 0)[0][0]
    assert (real["bad_info"], real["dnc"], real["sold"], real["revenue"]) == (1, 1, 0, 0.0)
    t = partners_mod.performance(m.con, 1)[0][0]
    assert (t["sold"], t["revenue"]) == (1, 99.0) and t["bad_info"] == 0   # TEST lives only in ?test=1


def test_attribution_gap_reports_unattributed_real_volume(tmp_path):
    m = make_app(tmp_path)
    visit(m.con, "v-direct", tool_done=True)                 # direct visitor, no partner id
    lead(m.con, "LDIR", partner=None, qual="QUALIFIED")      # direct lead, no partner id
    m.con.commit()
    _rows, gap = partners_mod.performance(m.con, 0)
    assert (gap["visits"], gap["consented"], gap["qualified"]) == (1, 1, 1)


# ---------- placement (utm_content) view ----------

def item_rows(m):
    """All configured placements keyed by id (no filter — tests pick what they need)."""
    rows, _untracked = ops_mod.performance(m.con, m.CFG, "")
    return {r["id"]: r for r in rows}


def test_placement_cost_per_qualified_only_when_cost_recorded(tmp_path):
    m = make_app(tmp_path)
    lead(m.con, "LA1", content="rd-florida-exit", qual="QUALIFIED")
    lead(m.con, "LA2", content="rd-florida-exit", qual="QUALIFIED")
    lead(m.con, "LB1", content="free-item", qual="QUALIFIED")   # same funnel, never any spend
    ops_mod.log(m.con, "rd-florida-exit", "POSTED", cost=48.0)          # operator-recorded real spend only
    m.con.commit()
    ad, free = item_rows(m)["rd-florida-exit"], item_rows(m)["seo-gsc"]      # seo-gsc: configured, never funded
    assert ad["cost_usd"] == 48.0 and ad["cost_per_qualified"] == 24.0
    assert free["cost_usd"] is None and free["cost_per_qualified"] is None   # unknown, NOT 0
    assert free["qualified"] == 0


def test_placement_cost_without_qualified_leads_is_unknown(tmp_path):
    m = make_app(tmp_path)
    lead(m.con, "LC1", content="rd-florida-exit", qual="UNQUALIFIED")
    ops_mod.log(m.con, "rd-florida-exit", "POSTED", cost=25.0)   # spend recorded, zero qualified leads
    m.con.commit()
    r = item_rows(m)["rd-florida-exit"]
    assert r["cost_usd"] == 25.0 and r["cost_per_qualified"] is None


def test_untracked_utm_content_surfaces_as_leak(tmp_path):
    m = make_app(tmp_path)
    lead(m.con, "LX1", content="mystery-campaign-post", qual="QUALIFIED")
    m.con.commit()
    _rows, untracked = ops_mod.performance(m.con, m.CFG, "")
    hit = next(u for u in untracked if u["k"] == "mystery-campaign-post")
    assert hit["leads_all"] == 1 and hit["qualified"] == 1


def test_placement_counts_unique_visitors_and_splits_dispositions(tmp_path):
    m = make_app(tmp_path)
    visit(m.con, "vA", tool_done=True)
    visit(m.con, "vB", tool_done=True)
    visit(m.con, "vC")
    # tie all three visitors to one placement content
    m.con.execute("UPDATE events SET utm_content='rd-florida-exit' WHERE visitor_id IN ('vA','vB','vC')")
    lead(m.con, "LD3", content="rd-florida-exit", qual="QUALIFIED")
    lead(m.con, "LD4", content="rd-florida-exit", qual="QUALIFIED")
    lead(m.con, "LD5", content="rd-florida-exit", qual="DUPLICATE")
    sold(m.con, "LD3", agent_status="SOLD", price=60.0)
    sold(m.con, "LD4", agent_status="RETURNED", price=60.0)
    m.con.commit()
    r = item_rows(m)["rd-florida-exit"]
    assert r["visits"] == 3 and r["check_done"] == 2
    assert (r["consented"], r["qualified"], r["dup"]) == (3, 2, 1)
    assert (r["sold"], r["returned"]) == (1, 1) and r["revenue"] == 60.0


# ---------- the admin page itself ----------

def test_admin_acquisition_page_and_csv_and_auth(tmp_path):
    m = make_app(tmp_path)
    partners_mod.create(m.con, {"partner_id": "p1", "display_name": "Visible Partner", "type": "cpa"})
    lead(m.con, "LV1", content="seen-item", qual="QUALIFIED")
    m.con.commit()
    c = m.app.test_client()
    assert c.get("/admin/acquisition").status_code == 403                          # no token, no data
    page = c.get("/admin/acquisition?token=t").get_data(as_text=True)
    assert "Visible Partner" in page and "Attribution gap" in page                 # registry + gap card
    assert "seen-item" in page and "unknown" in page                               # placements + costs
    csv_pl = c.get("/admin/acquisition.csv?token=t").get_data(as_text=True)
    assert csv_pl.splitlines()[0].startswith("id,") and "rd-florida-exit" in csv_pl  # placements CSV
    csv_pa = c.get("/admin/acquisition.csv?token=t&kind=partners").get_data(as_text=True)
    assert "partner_id,display_name" in csv_pa
    assert "Visible Partner" in csv_pa
    # a partner cost is never fabricated in the CSV either
    row = [r for r in csv_pa.splitlines() if "Visible Partner" in r][0]
    assert "None" not in row                                                        # blank means unknown
