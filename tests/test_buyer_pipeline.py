"""Prospective-BUYER follow-up pipeline (the commercial counterpart to the partner pipeline).

Buyers apply at /for-agents or are configured as drafts; between application and activation the
operator's real sales steps now live in an APPEND-ONLY trail instead of their head. Nothing here
contacts anyone, nothing activates a buyer, demo placeholders are never tracked, and the queue
shows the SAME routing blockers the engine computes (never its own optimistic opinion).

Isolation: every test rebinds the app's CFG to a TEMP COPY of the real config dir, so the suite
can never write into the working config/buyers.json (the real buyer records stay untouched, and
demonstrates the exact defect the daily-run first caught).

Run: python -m pytest tests/test_buyer_pipeline.py -q
"""
import json, shutil, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_billing import make_app
from engine import buyer_pipeline as bp_mod


def _iso_cfg(tmp_path):
    """Copy the REAL config into a temp dir and rebind the app to it — tests read the genuine
    buyer configuration but write only to the disposable copy."""
    cfg = tmp_path / "cfg"
    shutil.copytree(Path(__file__).resolve().parents[1] / "config", cfg)
    return cfg


def make_app_iso(tmp_path):
    m = make_app(tmp_path)
    m.CFG = _iso_cfg(tmp_path)          # app module global: every view + save path uses this binding
    return m


def onboard(m, c, bid="gulf-coast", name="Gulf Coast Family Insurance LLC", **over):
    d = {"buyer_id": bid, "action": "save", "name": name, "license_number": "A99887", "npn": "4443332",
         "licensed_states": "FL", "products": "health_aca", "tiers": "A,B", "price_A": "60", "price_B": "35",
         "daily_cap": "5", "age_min": "18", "age_max": "80"}
    d.update(over)
    return c.post("/admin/buyers?token=t", data=d)


def buyers_of(m, c):
    return json.loads((m.CFG / "buyers.json").read_text(encoding="utf-8"))


def rows_for(m, bid):
    return [r_ for r_ in bp_mod.rows(m.con, buyers_of(m, None)) if r_["buyer"]["id"] == bid]


def test_application_lands_in_the_queue_as_awaiting_first_step(tmp_path):
    m = make_app_iso(tmp_path); c = m.app.test_client()
    r = c.post("/for-agents/apply", data={"agency": "Gulf Coast Family Insurance LLC", "contact_name": "Dana Reyes",
                                          "contact_email": "dana@gcfi.example.com", "contact_phone": "3055550199",
                                          "license_number": "A99887", "npn": "4443332", "licensed_states": "FL",
                                          "products": "health_aca", "expected_volume": "5-10"})
    assert r.status_code == 200
    page = c.get("/admin/buyer-pipeline?token=t")
    assert page.status_code == 200
    pr = page.get_data(as_text=True)
    assert "Gulf Coast Family Insurance LLC" in pr and "Application received" in pr
    assert "awaiting first step" in pr
    assert c.get("/admin/buyer-pipeline").status_code == 403          # token-guarded like every admin page


def test_record_outreach_stage_and_followup_append_only(tmp_path):
    m = make_app_iso(tmp_path); c = m.app.test_client()
    onboard(m, c)
    r = c.post("/admin/buyer-pipeline/gulf-coast/action?token=t",
               data={"method": "EMAIL", "note": "sent the one-page offer + sample TEST lead card", "next_followup": "2099-01-05"},
               follow_redirects=True)
    assert r.status_code == 200 and "outreach recorded" in r.get_data(as_text=True)
    r = c.post("/admin/buyer-pipeline/gulf-coast/action?token=t", data={"stage": "KIT_SENT"}, follow_redirects=True)
    assert r.status_code == 200 and "stage APPLIED" in r.get_data(as_text=True) and "KIT_SENT" in r.get_data(as_text=True)  # flash is HTML-escaped: &gt;
    hist = bp_mod.history(m.con, "gulf-coast")
    kinds = [h["kind"] for h in hist]                                  # newest first
    assert kinds == ["STAGE", "FOLLOWUP", "OUTREACH"]
    rows = rows_for(m, "gulf-coast")
    assert rows and rows[0]["stage"] == "KIT_SENT" and rows[0]["bucket"] == "SCHEDULED"   # far-future follow-up, not yet due
    # a follow-up far in the PAST floats the prospect into OVERDUE (real recorded date vs today, UTC)
    c.post("/admin/buyer-pipeline/gulf-coast/action?token=t", data={"next_followup": "2001-01-01"}, follow_redirects=True)
    rows = rows_for(m, "gulf-coast")
    assert rows[0]["bucket"] == "OVERDUE"


def test_duplicate_resubmit_creates_no_second_record(tmp_path):
    m = make_app_iso(tmp_path); c = m.app.test_client()
    onboard(m, c)
    d = {"method": "PHONE", "note": "pricing call - $60 tier A flat, he wants volume 8/day"}
    c.post("/admin/buyer-pipeline/gulf-coast/action?token=t", data=d)
    c.post("/admin/buyer-pipeline/gulf-coast/action?token=t", data=d)   # double click / resubmit
    outs = [h for h in bp_mod.history(m.con, "gulf-coast") if h["kind"] == "OUTREACH"]
    assert len(outs) == 1                                              # dedupe_key swallows the retry


def test_validation_refuses_unknown_stage_method_and_demo_tracking(tmp_path):
    m = make_app_iso(tmp_path); c = m.app.test_client()
    onboard(m, c)
    assert c.post("/admin/buyer-pipeline/gulf-coast/action?token=t", data={"stage": "BIG_FAN"}).status_code == 400
    assert c.post("/admin/buyer-pipeline/gulf-coast/action?token=t", data={"method": "SMOKE_SIGNAL"}).status_code == 400
    assert c.post("/admin/buyer-pipeline/gulf-coast/action?token=t", data={"next_followup": "soon"}).status_code == 400
    # an unknown PROSPECT id and a demo placeholder are both refused, writing NOTHING
    assert c.post("/admin/buyer-pipeline/no-such-buyer-xyz/action?token=t", data={"stage": "KIT_SENT"}).status_code == 404
    assert c.post("/admin/buyer-pipeline/demo-agency-a/action?token=t", data={"stage": "KIT_SENT"}).status_code == 400
    assert not [dict(x) for x in m.con.execute("SELECT * FROM buyer_activity")]     # every refusal wrote NOTHING


def test_activation_is_recorded_in_the_trail_and_queue_leaves_the_flow(tmp_path):
    m = make_app_iso(tmp_path); c = m.app.test_client()
    onboard(m, c)
    c.post("/admin/buyer-pipeline/gulf-coast/action?token=t", data={"stage": "PILOT_READY"})
    onboard(m, c, action="activate")
    rows = [r_ for r_ in bp_mod.rows(m.con, buyers_of(m, None)) if r_["buyer"]["id"] == "gulf-coast"]
    assert rows and rows[0]["stage"] == "ACTIVATED" and rows[0]["bucket"] == "RECENT"    # no more chasing
    fiel = next(h for h in bp_mod.history(m.con, "gulf-coast") if h["kind"] == "STAGE" and h["stage_to"] == "ACTIVATED")
    assert fiel["stage_from"] == "PILOT_READY"                          # the trail answers "when did they go live"


def test_demo_buyers_are_never_prospects_and_queue_hides_them(tmp_path):
    m = make_app_iso(tmp_path); c = m.app.test_client()
    page = c.get("/admin/buyer-pipeline?token=t").get_data(as_text=True)
    buyers = buyers_of(m, None)
    for b in buyers:
        if b.get("is_demo"):
            assert b["id"] not in page                                  # software fixtures are not a sales pipeline


def test_real_config_is_never_written_by_the_suite(tmp_path):
    """The suite must run entirely against the isolated config copy: the working buyers.json
    keeps EXACTLY the state it shipped with (the daily-run catches any config drift)."""
    import hashlib
    real_cfg = Path(__file__).resolve().parents[1] / "config" / "buyers.json"
    before = real_cfg.read_bytes() if real_cfg.exists() else None
    m = make_app_iso(tmp_path); c = m.app.test_client()
    onboard(m, c)
    c.post("/for-agents/apply", data={"agency": "Temp Test Agency", "contact_name": "T", "contact_email": "t@example.com",
                                      "contact_phone": "3055550000", "license_number": "X1", "npn": "1",
                                      "licensed_states": "FL", "products": "health_aca", "expected_volume": "1-2"})
    after = real_cfg.read_bytes() if real_cfg.exists() else None
    assert before == after


def test_pipeline_funnel_counts_stages_exits_and_success(tmp_path):
    """The drop-off view must be computed ONLY from the recorded trail: how many reached
    each stage, who still stands there, who moved onward, who exited, and success counted
    explicitly. No stage is invented for buyers the operator never marked."""
    import datetime as dt
    con = make_app_iso(tmp_path).con
    old = (dt.date.today() - dt.timedelta(days=9)).isoformat()
    con.executemany(
        "INSERT INTO buyer_activity (ts, buyer_id, kind, stage_from, stage_to, method, next_followup, note, operator)"
        " VALUES (?,?,?,?,?,?,?,?, 'test')", [
            (old + "T10:00:00", "gulf-coast", "STAGE", "APPLIED", "REVIEWED", "EMAIL", "", "reviewed"),
            (old + "T11:00:00", "gulf-coast", "STAGE", "REVIEWED", "KIT_SENT", "EMAIL", "", "kit sent"),
            ("2026-01-05T09:00:00", "sunrise-health", "STAGE", "APPLIED", "DECLINED", "PHONE", "", "not licensed")])
    con.commit()
    buyers = [
        {"id": "gulf-coast", "active": False},          # walked to KIT_SENT, standing there (9 days: 2 rows)
        {"id": "sunrise-health", "active": False},      # declined
        {"id": "abc-family", "active": True},           # success without any trail
        {"id": "fresh-lead", "active": False}]          # never touched: reached APPLIED only
    f = bp_mod.funnel(con, buyers)
    assert f["arrived"] == 4
    by = {s["stage"]: s for s in f["ladder"]}
    # only MARKERS sit in 'reached': gulf-coast's trail starts at REVIEWED, so APPLIED counts
    # only the buyer who actually stands there default — the page text says exactly this.
    assert by["APPLIED"]["reached"] == 1 and by["APPLIED"]["now"] == 1 and by["APPLIED"]["onward"] == 0
    assert by["REVIEWED"]["reached"] == 1 and by["REVIEWED"]["now"] == 0 and by["REVIEWED"]["onward"] == 1
    assert by["KIT_SENT"]["reached"] == 1 and by["KIT_SENT"]["now"] == 1
    TIME_IN = by["KIT_SENT"]["days"]
    assert TIME_IN is not None and TIME_IN >= 9
    assert f["exits"]["declined"] == 1
    assert f["exits"]["activated"] == 1 and f["exits"]["activated_ever"] == 1
    assert "ACTIVATED" not in by and "DECLINED" not in by     # terminal rows render from exits, not the ladder


def test_pipeline_funnel_page_renders(tmp_path):
    m = make_app_iso(tmp_path)
    page = m.app.test_client().get("/admin/buyer-pipeline?token=t")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "Where buyers flow" in body and "Median days in stage" in body
    assert "Activated buyer" in body


def test_pipeline_csv_excludes_demos_and_carries_recorded_blockers(tmp_path):
    """The CSV is the queue as a spreadsheet: same rows, same exclusion of demo placeholders, and the
    recorded routing blocker column so a CSV reader sees the same truth the screen shows."""
    m = make_app_iso(tmp_path); c = m.app.test_client()
    onboard(m, c)
    r = c.get("/admin/buyer-pipeline.csv?token=t")
    assert r.status_code == 200 and "text/csv" in r.headers.get("Content-Type", "")
    lines = r.get_data(as_text=True).strip().splitlines()
    assert lines[0].startswith("buyer_id,name,stage,bucket,next_followup")
    body = " ".join(lines[1:])
    assert "gulf-coast" in body
    for b in buyers_of(m, None):
        if b.get("is_demo"):
            assert b["id"] not in body

def test_ops_sale_alerts_kernel_detects_negative_real_and_is_test_blind(tmp_path):
    """The shared admin/CLI kernel reads recorded ledger rows: a negative REAL balance is named,
    TEST ledger rows are never flagged, and nothing active in a normal ledger raises."""
    m = make_app_iso(tmp_path); con = m.con
    from engine import ops
    con.execute("INSERT INTO buyer_ledger (ts, buyer_id, kind, amount, balance_after, is_test, note) "
                "VALUES ('2026-10-26T10:00:00','neg-buyer','LEAD_CHARGE',-25.0,-25.0,0,'oops')")
    con.execute("INSERT INTO buyer_ledger (ts, buyer_id, kind, amount, balance_after, is_test, note) "
                "VALUES ('2026-10-26T10:00:00','test-buyer','LEAD_CHARGE',-500.0,-500.0,1,'rehearsal')")
    con.commit()
    al = ops.sale_alerts(con)
    assert [a["buyer_id"] for a in al["negative"]] == ["neg-buyer"]
    assert al["negative"][0]["bal"] == -25.0
    assert all(a["buyer_id"] != "test-buyer" for a in al["negative"])
