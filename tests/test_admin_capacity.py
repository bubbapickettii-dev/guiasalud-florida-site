"""Admin overview “buyer capacity vs incoming demand” strip (workstream: commercial readiness).

The strip must report ONLY recorded state — active real buyers, their remaining daily caps,
their prepaid real-lead credit value, and how they receive deliveries — never demand or
revenue that isn't in the ledger. TEST/demo buyers must not inflate the numbers, and a
cap-exhausted active buyer is counted as blocked, never as phantom capacity.
"""
import shutil, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_billing import make_app


def _isolated_cfg(m, tmp_path):
    cfg = tmp_path / "cfg"
    shutil.copytree(m.CFG, cfg)
    m.CFG = cfg
    return cfg


def _activate_real_agent(m, buyer_id="agent-health-1", daily_cap=25):
    import json
    raw = json.loads((m.CFG / "buyers.json").read_text(encoding="utf-8"))
    for b in raw:
        if b["id"] == buyer_id:
            b["active"] = True
            b["price_by_tier"] = {"A": 60.0, "B": 35.0}
            b["daily_cap"] = daily_cap
            b["delivery"] = {"method": "outbox"}          # desk-only for deterministic counts
    (m.CFG / "buyers.json").write_text(json.dumps(raw, indent=1), encoding="utf-8")
    return raw


def _deliver_real_leads(m, buyer_id, n):
    for i in range(n):
        m.con.execute("""INSERT INTO leads (lead_id, created_at, is_test, campaign, product, state, zip,
            phone, phone_source, qual_status, tier, score, routing_status, buyer_id, price, routed_at,
            delivered_at, contact_scope) VALUES (?, datetime('now'), 0, 'camp', 'health_aca', 'FL', '33101',
            ?, 'CONSUMER_SUBMITTED', 'QUALIFIED', 'A', 60, 'DELIVERED', ?, 60, datetime('now'),
            datetime('now'), 'PHONE_AND_EMAIL')""", (f"LCAP{i}", f"3055550{10+i}", buyer_id))
    m.con.commit()


def _strip(page):
    s = page[page.index("Buyer capacity vs. incoming demand"):]
    return s[:s.index("</p>")]


def test_strip_counts_recorded_state_only(tmp_path):
    m = make_app(tmp_path)
    _isolated_cfg(m, tmp_path)
    _activate_real_agent(m)                          # open, cap 25, desk-only delivery
    _deliver_real_leads(m, "agent-health-1", 2)      # 2 delivered today -> 23 remaining
    m.con.execute("""INSERT INTO buyer_ledger (ts, buyer_id, lead_id, kind, amount, balance_after,
                     note, is_test) VALUES (datetime('now'), 'agent-health-1', NULL,
                     'PACKAGE_PURCHASE', 250, 250, 'recorded top-up', 0)""")
    m.con.commit()
    page = m.app.test_client().get("/admin?token=t&test=real").get_data(as_text=True)
    strip = _strip(page)
    assert "1 active real buyer(s) open" in strip
    assert "23 more delivered lead(s) today" in strip
    assert "$250.00 prepaid credit" in strip
    assert "0 on webhook/email" in strip and ", 1 desk-only" in strip
    assert "no eligible buyer" not in strip          # the strip never claims guarantees


def test_no_active_buyer_shows_zero_not_fabricated_demand(tmp_path):
    m = make_app(tmp_path)
    _isolated_cfg(m, tmp_path)
    page = m.app.test_client().get("/admin?token=t").get_data(as_text=True)
    strip = _strip(page)
    assert "0 active real buyer(s) open" in strip
    assert "absorb 0 more delivered lead(s)" in strip
    assert "$0.00 prepaid credit" in strip


def test_cap_exhausted_buyer_is_blocked_and_adds_no_capacity(tmp_path):
    m = make_app(tmp_path)
    _isolated_cfg(m, tmp_path)
    _activate_real_agent(m, daily_cap=1)
    _deliver_real_leads(m, "agent-health-1", 2)      # 2 > cap 1 -> daily-cap blocker
    page = m.app.test_client().get("/admin?token=t").get_data(as_text=True)
    strip = _strip(page)
    assert "1 active real buyer(s) open" in strip
    assert "1 currently excluded by blockers" in strip
    assert "absorb 0 more delivered lead(s)" in strip
    assert "$0.00 prepaid credit" in strip
