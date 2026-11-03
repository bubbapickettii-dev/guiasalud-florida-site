"""Prospect → buyer-onboarding handoff through the RUNNING APP (app-level, offline).

An operator handoff only ever creates an INACTIVE, portal-less buyer draft from a
QUALIFIED + OPERATOR_VERIFIED prospect. Never auto-activated; refuses duplicates;
prospect and buyer stay linked; buyer_activity records the APPLIED trail.
Bookkeeping only: no contact, no routing, no delivery, no live buyer.
"""
import importlib
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import prospects as pr                 # noqa: E402

import pytest                                      # noqa: E402
_GKEPT = {k: os.environ.get(k) for k in
          ("LEADENGINE_DB", "ADMIN_TOKEN", "INTEL_DB", "PROSPECTS_DB", "LEADENGINE_CFG")}


@pytest.fixture(autouse=True)
def _restore_env():
    """These tests configure the whole app (cfg dir, prospects db); restore the environment
    after EACH test so later modules (readiness, buyer tests) reload against defaults."""
    yield
    for k, v in _GKEPT.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _app(tmp_path):
    os.environ["LEADENGINE_DB"] = str(tmp_path / "t.db")
    os.environ["ADMIN_TOKEN"] = "t"
    os.environ["INTEL_DB"] = str(tmp_path / "none.db")
    os.environ["PROSPECTS_DB"] = str(tmp_path / "prospects.db")
    cfg = tmp_path / "cfg"
    if not cfg.exists():
        shutil.copytree(ROOT / "config", cfg)
        b = json.loads((cfg / "buyers.json").read_text(encoding="utf-8"))
        b = [x for x in b if x.get("is_demo")]      # the app must create ITS OWN draft
        (cfg / "buyers.json").write_text(json.dumps(b, indent=2), encoding="utf-8")
    os.environ["LEADENGINE_CFG"] = str(cfg)
    import app as appmod
    importlib.reload(appmod)
    appmod.OUTBOX = tmp_path / "outbox"
    appmod.SNAP = tmp_path / "snap"
    return appmod, cfg


def _qlty_prospect(tmp_path, con, name="Pilot Agency LLC"):
    con.execute("INSERT INTO prospects (prospect_key, name, source, decision, verify_state,"
                " stage, state, service_territory, website, biz_email, contact_person,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"{name.lower()}|", name, "manual", "QUALIFIED", "OPERATOR_VERIFIED",
                 "READY_FOR_ONBOARDING", "FL", "FL", f"{name.split()[0].lower()}.example",
                 "owner@pilotagency.example", "Dana Operator",
                 "2026-10-27T00:00:00", "2026-10-27T00:00:00"))
    con.commit()
    return con.execute("SELECT id FROM prospects WHERE name=?", (name,)).fetchone()[0]


def _seed_unqualified(tmp_path, con):
    con.execute("INSERT INTO prospects (prospect_key, name, decision, verify_state, stage,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                ("under-review-llc|", "Under Review LLC", "UNDER_REVIEW", "UNVERIFIED",
                 "RESEARCHED", "2026-10-27T00:00:00", "2026-10-27T00:00:00"))
    con.commit()
    return con.execute("SELECT id FROM prospects WHERE name='Under Review LLC'").fetchone()[0]


def test_full_handoff_creates_inactive_linked_draft(tmp_path):
    m, cfg = _app(tmp_path)
    pcon = pr.connect(m.PROSPECTS_DB)
    pid = _qlty_prospect(tmp_path, pcon)
    pcon.close()
    c = m.app.test_client()
    # detail page renders the record, the ladder and the handoff confirm
    page = c.get(f"/admin/prospects/{pid}?token=t").get_data(as_text=True)
    assert "Pilot Agency LLC" in page and "CONFIRM unreachable" not in page
    assert "confirm" in page          # the explicit handoff confirmation control is present
    # update route records commercial fields the operator actually obtained
    c.post(f"/admin/prospects/{pid}/update?token=t", data={
        "stated_budget_usd": "~$1000/wk (stated only)", "interested": "yes",
        "proposed_price": "A=$45 B=$28"})
    pcon = pr.connect(m.PROSPECTS_DB)
    row = pr.detail(pcon, pid)
    assert row["interested"] == "yes" and "~$1000/wk" in row["stated_budget_usd"]
    assert any("field:stated_budget_usd" in h["action"] for h in pr.log(pcon, pid))
    pcon.close()
    # handoff without the explicit confirmation is refused
    r = c.post(f"/admin/prospects/{pid}/handoff?token=t", data={}, follow_redirects=True)
    assert "explicit confirmation" in r.get_data(as_text=True)
    # WITH confirmation: inactive draft in config + linked prospect + buyer trail
    r = c.post(f"/admin/prospects/{pid}/handoff?token=t", data={"confirm": "convert"}, follow_redirects=True)
    body = r.get_data(as_text=True)
    assert "INACTIVE" in body or "inactive" in body
    buyers = json.loads((cfg / "buyers.json").read_text(encoding="utf-8"))
    mine = [b for b in buyers if b.get("converted_from_prospect", {}).get("prospect_id") == pid]
    assert len(mine) == 1 and mine[0]["active"] is False and mine[0]["is_demo"] is False
    assert "portal_token" not in mine[0]
    assert mine[0]["name"] == "Pilot Agency LLC" and mine[0]["products"] == ["health_aca"]
    assert mine[0]["_application"]["source"].startswith("prospect:#")
    pcon = pr.connect(m.PROSPECTS_DB)
    row = pr.detail(pcon, pid)
    assert row["stage"] == "ONBOARDING_STARTED" and row["buyer_id"] == mine[0]["id"]
    pcon.close()
    act = m.con.execute("SELECT kind, note FROM buyer_activity WHERE buyer_id=?", (mine[0]["id"],)).fetchall()
    assert act   # the buyer-pipeline trail recorded the creation (APPLIED is the trail's default state)
    assert any("prospect #1" in n for _k, n in act)


def test_activation_only_through_real_gates_records_prospect_activated(tmp_path):
    """The whole ladder: QUALIFIED+verified prospect -> handoff -> inactive draft -> the
    operator completes onboarding -> activation THROUGH the real gate -> prospect records
    ACTIVATED. An incomplete draft is refused by _onboard_errors at every attempt."""
    m, cfg = _app(tmp_path)
    pcon = pr.connect(m.PROSPECTS_DB)
    pid = _qlty_prospect(tmp_path, pcon)
    pcon.close()
    c = m.app.test_client()
    c.post(f"/admin/prospects/{pid}/handoff?token=t", data={"confirm": "convert"})
    bid = json.loads((cfg / "buyers.json").read_text(encoding="utf-8"))[-1]["id"]
    # incomplete activate attempt: refused by the REAL onboarding errors, draft stays inactive
    c.post(f"/admin/buyers?token=t", data={"buyer_id": bid, "action": "activate"})
    buyers = json.loads((cfg / "buyers.json").read_text(encoding="utf-8"))
    assert next(b for b in buyers if b["id"] == bid)["active"] is False
    # a portal-less draft cannot sell either
    assert not next(b for b in buyers if b["id"] == bid).get("portal_token")
    # complete onboarding: token, license, NPN, prices, cap, delivery — then activate
    full = {"buyer_id": bid, "action": "save", "name": "Pilot Agency LLC",
            "license_number": "FL-L-999", "npn": "9999991", "licensed_states": "FL",
            "products": "health_aca", "tiers": "A,B", "price_A": "45", "price_B": "28",
            "daily_cap": "10", "age_min": "18", "age_max": "80",
            "delivery_method": "email", "delivery_email": "owner@pilotagency.example"}
    c.post(f"/admin/buyers?token=t", data=dict(full, action="rotate_token"))
    c.post(f"/admin/buyers?token=t", data=full)
    r = c.post(f"/admin/buyers?token=t", data={"buyer_id": bid, "action": "activate"}, follow_redirects=True)
    buyers = json.loads((cfg / "buyers.json").read_text(encoding="utf-8"))
    b = next(x for x in buyers if x["id"] == bid)
    assert b["active"] is True and b.get("portal_token")
    # the originating prospect now records ACTIVATED (mirror written by the real flow)
    pcon = pr.connect(m.PROSPECTS_DB)
    row = pr.detail(pcon, pid)
    pcon.close()
    assert row["stage"] == "ACTIVATED" and row["buyer_id"] == bid


def test_handoff_resumes_existing_draft_without_duplicates(tmp_path):
    m, cfg = _app(tmp_path)
    pcon = pr.connect(m.PROSPECTS_DB)
    pid = _qlty_prospect(tmp_path, pcon)
    pcon.close()
    c = m.app.test_client()
    c.post(f"/admin/prospects/{pid}/handoff?token=t", data={"confirm": "convert"})
    buyers = json.loads((cfg / "buyers.json").read_text(encoding="utf-8"))
    bid = next(b["id"] for b in buyers if b.get("converted_from_prospect", {}).get("prospect_id") == pid)
    n_before = len(buyers)
    # second handoff (or after activation) RESUMES the same draft: same account count
    r = c.post(f"/admin/prospects/{pid}/handoff?token=t", data={"confirm": "convert"}, follow_redirects=True)
    body = r.get_data(as_text=True)
    assert "no new buyer account was created" in body
    buyers2 = json.loads((cfg / "buyers.json").read_text(encoding="utf-8"))
    assert len(buyers2) == n_before and next(b["id"] for b in buyers2 if b.get("converted_from_prospect", {}).get("prospect_id") == pid) == bid
    # the detail page's checklist shows draft-side items derived from the REAL onboarding rules
    page = c.get(f"/admin/prospects/{pid}?token=t").get_data(as_text=True)
    assert "Onboarding-readiness checklist" in page
    assert "independently" in page.lower() and "provided" in page
    assert "_onboard" not in page      # internal names never leak onto the page


def test_listing_workspace_buckets_and_filters(tmp_path):
    m, _cfg = _app(tmp_path)
    pcon = pr.connect(m.PROSPECTS_DB)
    pid = _qlty_prospect(tmp_path, pcon)
    # a prospect whose next action is overdue (yesterday)
    con = pcon
    con.execute("INSERT INTO prospects (prospect_key, name, decision, verify_state, stage, state,"
                " service_territory, next_action, next_action_date, created_at, updated_at)"
                " VALUES ('late-llc|','Late LLC','QUALIFIED','OPERATOR_VERIFIED','QUALIFIED','FL','FL',"
                "'call back','2026-10-26','2026-10-26T00:00:00','2026-10-27T00:00:00')")
    con.commit()
    late = con.execute("SELECT id FROM prospects WHERE name='Late LLC'").fetchone()[0]
    pcon.close()
    c = m.app.test_client()
    page = c.get(f"/admin/prospects?token=t&due=1").get_data(as_text=True)
    assert "Late LLC" in page and "Pilot Agency LLC" not in page    # only the follow-up-due row
    page = c.get(f"/admin/prospects?token=t&sort=next").get_data(as_text=True)
    # due row floats above the otherwise-newer pilot row
    assert page.find("Late LLC") < page.find("Pilot Agency LLC")
    page = c.get(f"/admin/prospects?token=t&needs_verify=1").get_data(as_text=True)
    assert "Pilot Agency LLC" not in page    # both seeds are verified
    import datetime as _dt
    row_page = c.get(f"/admin/prospects/{pid}?token=t").get_data(as_text=True)
    assert "Onboarding-readiness checklist" in row_page     # complete rule set on the record


def test_unverified_prospect_never_becomes_a_buyer(tmp_path):
    m, _cfg = _app(tmp_path)
    pcon = pr.connect(m.PROSPECTS_DB)
    pid = _seed_unqualified(tmp_path, pcon)
    pcon.close()
    c = m.app.test_client()
    r = c.post(f"/admin/prospects/{pid}/handoff?token=t", data={"confirm": "convert"}, follow_redirects=True)
    body = r.get_data(as_text=True)
    assert "handoff refused" in body and "UNDER_REVIEW" in body
    buyers = json.loads((Path(os.environ["LEADENGINE_CFG"]) / "buyers.json").read_text(encoding="utf-8"))
    assert not [b for b in buyers if b.get("converted_from_prospect", {}).get("prospect_id") == pid]


def test_admin_shows_prospect_provenance_on_draft(tmp_path):
    """The buyer side keeps the link visible too: /admin (with ?f=<draft>) names the
    originating prospect; buyers without one stay clean, no invented lineage."""
    m, _cfg = _app(tmp_path)
    pcon = pr.connect(m.PROSPECTS_DB)
    pid = _qlty_prospect(tmp_path, pcon)
    pcon.close()
    c = m.app.test_client()
    c.post(f"/admin/prospects/{pid}/handoff?token=t", data={"confirm": "convert"})
    buyers = json.loads((Path(os.environ["LEADENGINE_CFG"]) / "buyers.json").read_text(encoding="utf-8"))
    bid = next(b["id"] for b in buyers if b.get("converted_from_prospect", {}).get("prospect_id") == pid)
    page = c.get(f"/admin?token=t&f={bid}").get_data(as_text=True)
    assert f"/admin/prospects/{pid}" in page and "its full commercial record" in page
    # a buyer with no prospect lineage shows no provenance banner
    plain = c.get("/admin?token=t").get_data(as_text=True)
    assert "its full commercial record" not in plain
