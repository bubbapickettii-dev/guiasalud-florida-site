"""A rehearsed TEST activity used to be invisible on a real buyer's billing page: the page is
scoped to the REAL ledger, so TEST packs and TEST lead charges rendered as "$0.00 / $-0.00" in
silence and the page DISAGREED with the authoritative ledger during the pilot rehearsal.
The fix adds a clearly labeled TEST-sandbox block; the real balance stays the authoritative one.
TEST-only bookkeeping in an isolated temp app — no live payments, no live delivery.
"""
import json, shutil, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_billing import make_app, post

BEHIND = '{"buyer_id": "portal-test", "action": "activate", "name": "Bay Health Advisors LLC", ' \
         '"license_number": "A112233", "npn": "12345678", "licensed_states": "FL", ' \
         '"products": "mortgage_protection", "tiers": "A,B", "price_A": "60", "price_B": "35", ' \
         '"daily_cap": "5", "age_min": "30", "age_max": "64"}'


def activate_buyer(m, c):
    c.post("/admin/buyers?token=t", data=json.loads(BEHIND))
    import engine.billing as bl
    tok = next(x["portal_token"] for x in json.loads((m.CFG / "buyers.json").read_text(encoding="utf-8"))
               if x["id"] == "portal-test")
    return tok


def test_test_activity_visible_and_labeled_for_real_buyer(tmp_path):
    m = make_app(tmp_path)
    cfg = tmp_path / "cfg"; shutil.copytree(m.CFG, cfg); m.CFG = cfg
    c = m.app.test_client()
    tok = activate_buyer(m, c)                                # NON-demo buyer
    import engine.billing as bl
    bl.record_package(m.con, "portal-test", {"id": "starter-250", "amount_usd": 250},
                      note="rehearsal recorded pack", is_test=1)
    m.con.commit()
    post(c, consent_checked=True, is_test=1)                  # TEST lead routed + charged
    page = c.get(f"/agent/{tok}/billing").get_data(as_text=True)
    assert "Balance: $0.00" in page                           # real scope: only TEST movement exists
    assert "TEST-mode activity" in page
    assert "TEST balance" in page
    assert "the real balance above keeps being the authoritative one" in page
    assert "$250.00" in page                                  # the TEST pack is shown in the sandbox block


def test_no_test_block_without_test_activity(tmp_path):
    m = make_app(tmp_path)
    cfg = tmp_path / "cfg"; shutil.copytree(m.CFG, cfg); m.CFG = cfg
    c = m.app.test_client()
    tok = activate_buyer(m, c)
    page = c.get(f"/agent/{tok}/billing").get_data(as_text=True)
    assert "TEST-mode activity" not in page
