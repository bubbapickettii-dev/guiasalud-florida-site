"""Quality-aware routing: buyer_health feeds the platform's buyer-choice.

Proves that the marketplace's feedback loop (agent desk outcomes) actually changes WHO gets the next
fresh consumer — with hard safety bounds, auditable explanations, and no penalty from TEST traffic,
small histories, old volume, or honest buyer work (contacted/DNC/NOT_QUALIFIED).

Bookkeeping/routing only — no real money, no live delivery, consumer data only as synthetic seed rows.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.db import connect, now, SCHEMA
import engine.route as ro

CFG = dict(enabled=True, window_days=14, grace_hours=48, min_sample=4, demote_rate=0.34, block_rate=0.66)

GOOD = dict(id="good-agency", name="Good Agency", licensed_states=["FL"], products=["health_aca"],
            tiers=["A", "B"], price_by_tier={"A": 60.0, "B": 45.0}, daily_cap=10, priority=2, age_min=18, age_max=64)
BAD = dict(id="stall-agency", name="Stall Agency", licensed_states=["FL"], products=["health_aca"],
           tiers=["A", "B"], price_by_tier={"A": 60.0, "B": 45.0}, daily_cap=10, priority=1, age_min=18, age_max=64)

LEAD = dict(lead_id="LHEALTH1", qual_status="QUALIFIED", lead_type="PHONE_LEAD", phone="3055550110",
            product="health_aca", state="FL", age_range="40-49", tier="A")

_seq = [0]


def seed_delivered(con, buyer_id, *, agent_status=None, days_ago=1, hours_ago=None, is_test=0):
    """One DELIVERED real-sale-shaped lead row (routing DELIVERED + delivered_at + a price)."""
    ts = now()
    if hours_ago is not None:
        ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    elif days_ago is not None:
        ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")
    _seq[0] += 1
    con.execute("""INSERT INTO leads (lead_id, created_at, is_test, campaign, product, lead_type, phone, phone_source,
                   qual_status, tier, routing_status, buyer_id, price, sale_type, delivered_at, agent_status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"LBH{_seq[0]:03d}", ts, is_test, "mortgage-protection", "health_aca", "PHONE_LEAD", "3055550110", "CONSUMER_SUBMITTED",
                 "QUALIFIED", "A", "DELIVERED", buyer_id, 60.0, "EXCLUSIVE", ts, agent_status))
    con.commit()


def fresh_con(tmp_path):
    con = connect(tmp_path / "h.db")
    con.executescript(SCHEMA)
    return con


def test_counts_real_windowed_rejections_only(tmp_path):
    con = fresh_con(tmp_path)
    for st, days in (("RETURNED", 2), ("RETURNED", 3), ("BAD_INFO", 5), ("CONTACTED", 1)):
        seed_delivered(con, "stall-agency", agent_status=st, days_ago=days)
    seed_delivered(con, "stall-agency", agent_status="RETURNED", days_ago=40)   # outside the window
    seed_delivered(con, "stall-agency", agent_status="RETURNED", is_test=1)     # TEST traffic never counts
    h = ro.buyer_health(con, CFG)["stall-agency"]
    assert (h["n"], h["penalty"], h["rate"]) == (4, 3, 0.75)      # 3/4 penalties inside the window
    assert h["rank"] == 2                                        # 0.75 >= block_rate 0.66 -> excluded


def test_grace_excludes_fresh_uncontacted_outcomes(tmp_path):
    con = fresh_con(tmp_path)
    seed_delivered(con, "stall-agency", agent_status="RETURNED", days_ago=2)
    seed_delivered(con, "stall-agency", agent_status="RETURNED", days_ago=3)
    seed_delivered(con, "stall-agency", agent_status=None, hours_ago=1)         # just delivered, no status yet
    seed_delivered(con, "stall-agency", agent_status=None, days_ago=1)          # inside grace (48h)
    h = ro.buyer_health(con, CFG)["stall-agency"]
    assert (h["n"], h["penalty"]) == (4, 2)                                     # stale is counted only past grace


def test_small_history_never_penalized_and_block_at_ceiling(tmp_path):
    con = fresh_con(tmp_path)
    seed_delivered(con, "stall-agency", agent_status="RETURNED", days_ago=1)    # n=1 < min_sample
    assert ro.buyer_health(con, CFG)["stall-agency"]["rank"] == 0                # penalty, but sample too small
    for st in ("RETURNED", "RETURNED", "RETURNED", "RETURNED", "BAD_INFO", "CONTACTED"):
        seed_delivered(con, "stall-agency", agent_status=st, days_ago=2)         # n=6 total, 5/6 = 0.83
    # n=7 total (the early row counts too — it is inside the window), 6/7 = 0.86
    h = ro.buyer_health(con, CFG)["stall-agency"]
    assert (h["penalty"], h["rate"], h["rank"]) == (6, 0.86, 2)                  # >= block -> excluded


def test_honest_work_is_not_a_penalty(tmp_path):
    con = fresh_con(tmp_path)
    for st in ("RETURNED", "BAD_INFO", "DNC", "NOT_QUALIFIED", "CONTACTED", "CONTACTED"):
        seed_delivered(con, "stall-agency", agent_status=st, days_ago=2)
    assert ro.buyer_health(con, CFG)["stall-agency"]["rate"] == 0.33            # 1/6 < demote_rate 0.34


def test_disabled_or_demo_cfg_short_circuits(tmp_path):
    con = fresh_con(tmp_path)
    seed_delivered(con, "stall-agency", agent_status="RETURNED", days_ago=1)
    assert ro.buyer_health(con, dict(CFG, enabled=False)) == {}
    assert ro.buyer_health(con, {}) == {}


def blocked_history(con):
    seed_delivered(con, "stall-agency", agent_status="RETURNED", days_ago=2)
    for st in ("RETURNED", "RETURNED", "RETURNED", "BAD_INFO"):
        seed_delivered(con, "stall-agency", agent_status=st, days_ago=1)        # 5/5 penalties -> blocked


def test_healthy_buyer_outranks_even_higher_priority_staller(tmp_path):
    con = fresh_con(tmp_path)
    blocked_history(con)
    h = ro.buyer_health(con, CFG)
    assert h["stall-agency"]["rank"] == 2
    b = ro.preview(con, [GOOD, BAD], "health_aca", "FL", "40-49", health=h)     # BAD has the higher priority
    assert b and b["id"] == "good-agency"


def test_blocked_buyer_is_excluded_and_explained(tmp_path):
    con = fresh_con(tmp_path)
    blocked_history(con)
    h = ro.buyer_health(con, CFG)
    assert ro.preview(con, [BAD], "health_aca", "FL", "40-49", tier="A", health=h) is None
    reason, n = ro.explain(con, [BAD], LEAD, health=h)
    assert n == 1
    assert "BLOCKED by recent buyer history" in reason and "of 5 delivered leads" in reason


def test_route_never_sells_when_only_the_blocked_buyer_fits(tmp_path):
    con = fresh_con(tmp_path)
    blocked_history(con)
    h = ro.buyer_health(con, CFG)
    r = ro.route(con, [BAD], dict(LEAD), health=h)
    assert r["routing_status"] == "NO_BUYER" and r["buyer_id"] is None


def test_no_demotion_below_threshold_priority_wins(tmp_path):
    con = fresh_con(tmp_path)
    seed_delivered(con, "stall-agency", agent_status="RETURNED", days_ago=2)    # 1 of 4 = 0.25 < 0.34
    for st in ("CONTACTED", "CONTACTED", "CONTACTED"):
        seed_delivered(con, "stall-agency", agent_status=st, days_ago=3)
    assert ro.buyer_health(con, CFG)["stall-agency"]["rank"] == 0
    assert ro.preview(con, [GOOD, BAD], "health_aca", "FL", "40-49")["id"] == "stall-agency"


def test_demoted_buyer_loses_the_next_lead_to_an_equal_cleaner_buyer(tmp_path):
    con = fresh_con(tmp_path)
    seed_delivered(con, "stall-agency", agent_status="RETURNED", days_ago=2)
    seed_delivered(con, "stall-agency", agent_status="RETURNED", days_ago=3)
    seed_delivered(con, "stall-agency", agent_status="CONTACTED", days_ago=5)   # 2/4 = 0.5 -> demoted, NOT blocked
    seed_delivered(con, "stall-agency", agent_status="CONTACTED", days_ago=6)
    h = ro.buyer_health(con, CFG)
    assert h["stall-agency"]["rank"] == 1
    tied = dict(GOOD, priority=1)                                              # same priority as BAD now
    assert ro.preview(con, [tied, BAD], "health_aca", "FL", "40-49", health=h)["id"] == "good-agency"


def test_explain_says_demoted_not_eligible_but_unassigned(tmp_path):
    """A health-DEMOTED buyer that passes every hard check but still lost the tie must not be
    recorded as 'eligible but unassigned': the routing_decisions row is what the operator audits,
    and it has to carry the real ordering reason. A hard exclusion (cap/licence/price) still
    outranks the demotion note — that IS the reason the buyer was refused."""
    con = fresh_con(tmp_path)
    seed_delivered(con, "stall-agency", agent_status="RETURNED", days_ago=2)
    seed_delivered(con, "stall-agency", agent_status="RETURNED", days_ago=3)
    seed_delivered(con, "stall-agency", agent_status="CONTACTED", days_ago=5)
    seed_delivered(con, "stall-agency", agent_status="CONTACTED", days_ago=6)
    h = ro.buyer_health(con, CFG)
    assert h["stall-agency"]["rank"] == 1
    reason, _ = ro.explain(con, [BAD], LEAD, health=h)
    assert "DEMOTED by recent buyer history" in reason and "eligible but unassigned" not in reason
    assert "of 4 delivered leads" in reason                                                   # the health detail rides along
    # hard reasons still surface first: the demote note must never mask a real refusal
    reason2, _ = ro.explain(con, [BAD], dict(LEAD, tier="C"), health=h)
    assert "does not buy tier C" in reason2
