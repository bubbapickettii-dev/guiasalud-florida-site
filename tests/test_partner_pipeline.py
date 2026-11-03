"""Partner follow-up pipeline (admin /admin/pipeline):

  - structured stage progression over the existing partners table (no new partner DB), with legacy
    free-text statuses backfilling until the operator sets a real stage
  - outreach is recorded AFTER the fact (dates, method, outcome, next action, note) — nothing contacts
    anyone; the full audit trail is append-only in partner_activity
  - follow-up scheduling by real ISO dates; overdue/due-today float to the top of the queue
  - input validation (stage/method enums, YYYY-MM-DD), auth on every route, and duplicate-retry
    protection via dedupe_key (an identical resubmit adds nothing)

Bookkeeping only: no emails, no external actions, synthetic partner rows.
"""
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_billing import make_app
from engine import partners as partners_mod


def pid(m):
    partners_mod.create(m.con, {"partner_id": "flow-school", "display_name": "Flow Real Estate School", "type": "re_school"})
    return "flow-school"


def act(c, partner, **kw):
    """POST one pipeline action form (what the admin panel submits)."""
    return c.post(f"/admin/pipeline/{partner}/action?token=t", data=kw, follow_redirects=True)


TODAY = date.today().isoformat()
YDAY = (date.today() - timedelta(days=1)).isoformat()
TMRW = (date.today() + timedelta(days=2)).isoformat()


def buckets_map(m):
    return {r["partner_id"]: (r["bucket"], r["stage"]) for r in partners_mod.pipeline(m.con)}


# ---------- stage progression ----------

def test_stage_change_updates_row_status_and_writes_audit_row(tmp_path):
    m = make_app(tmp_path)
    p = pid(m); c = m.app.test_client()
    r = act(c, p, stage="CONTACTED", method="EMAIL", note="introduced the program")
    assert r.status_code == 200
    row = dict(m.con.execute("SELECT * FROM partners WHERE partner_id=?", (p,)).fetchone())
    assert row["stage"] == "CONTACTED"
    assert row["status"] == "Contacted"           # legacy Partners tab stays in sync
    h = partners_mod.history(m.con, p)
    assert [x["kind"] for x in h] == ["STAGE", "OUTREACH"]          # append-only: both rows kept
    assert (h[0]["stage_from"], h[0]["stage_to"]) == ("IDENTIFIED", "CONTACTED")
    assert h[1]["method"] == "EMAIL" and h[1]["note"] == "introduced the program"
    assert row["last_outreach_at"]                # real recorded timestamp


def test_legacy_status_backfills_stage_without_writing(tmp_path):
    m = make_app(tmp_path)
    partners_mod.create(m.con, {"partner_id": "legacy-p", "display_name": "Legacy Co", "type": "cpa",
                                "status": "mod approval ready"})
    partners_mod.create(m.con, {"partner_id": "legacy-a", "display_name": "Active Co", "type": "cpa",
                                "status": "ACTIVE - packet delivered"})
    b = {k: v for k, v in buckets_map(m).items() if k in ("legacy-p", "legacy-a")}
    assert b["legacy-p"][1] == "READY_FOR_OUTREACH" and b["legacy-p"][0] == "AWAITING_FIRST_CONTACT"
    assert b["legacy-a"][1] == "ACTIVATED"
    assert m.con.execute("SELECT stage FROM partners WHERE partner_id='legacy-p'").fetchone()[0] is None  # DB not touched by display


# ---------- follow-up scheduling + queue buckets ----------

def test_overdue_due_and_upcoming_float_to_the_top(tmp_path):
    m = make_app(tmp_path)
    for i, d in enumerate((TMRW, YDAY, TODAY)):
        partners_mod.create(m.con, {"partner_id": f"p{i}", "display_name": f"P{i}", "type": "cpa"})
        m.con.execute("UPDATE partners SET last_outreach_at=?, next_followup=? WHERE partner_id=?", (YDAY, d, f"p{i}"))
    m.con.commit()
    mine = ["p0", "p1", "p2"]                     # the imported partners.json rows share the queue; scope to ours
    order = [r["partner_id"] for r in partners_mod.pipeline(m.con) if r["partner_id"] in mine]
    assert order[:2] == ["p1", "p2"]              # overdue, then due today, before upcoming
    assert buckets_map(m)["p1"][0] == "OVERDUE" and buckets_map(m)["p2"][0] == "DUE_TODAY" and buckets_map(m)["p0"][0] == "SCHEDULED"


def test_never_contacted_partner_is_a_real_awaiting_first_contact_zero(tmp_path):
    m = make_app(tmp_path)
    p = pid(m)
    b = buckets_map(m)
    assert b.get(p, ("?",))[0] == "AWAITING_FIRST_CONTACT"    # no outreach, no follow-up: honest zero state
    row = dict(m.con.execute("SELECT * FROM partners WHERE partner_id=?", (p,)).fetchone())
    assert row["last_outreach_at"] is None and row["next_followup"] is None    # nothing invented


def test_interested_without_next_step_awaits_next_action(tmp_path):
    m = make_app(tmp_path)
    partners_mod.create(m.con, {"partner_id": "intr", "display_name": "Intr", "type": "cpa"})
    m.con.execute("UPDATE partners SET stage='INTERESTED', last_outreach_at=? WHERE partner_id='intr'", (YDAY,))
    assert buckets_map(m)["intr"][0] == "INTERESTED_WAITING"


def test_activation_ready_partner_prioritized_without_followup(tmp_path):
    m = make_app(tmp_path)
    partners_mod.create(m.con, {"partner_id": "act-ready", "display_name": "ActReady", "type": "cpa"})
    m.con.execute("UPDATE partners SET stage='ACTIVATION_READY', last_outreach_at=? WHERE partner_id='act-ready'", (YDAY,))
    rows = [r for r in partners_mod.pipeline(m.con) if r["partner_id"] == "act-ready"]
    assert rows and rows[0]["bucket"] == "ACTIVATION_READY"


# ---------- activity history + duplicate protection ----------

def test_repeated_records_are_kept_no_overwrites(tmp_path):
    m = make_app(tmp_path)
    p = pid(m); c = m.app.test_client()
    act(c, p, method="PHONE", note="call 1: gatekeeper asked for email")
    act(c, p, method="EMAIL", note="follow-up 1: sent guide")
    act(c, p, stage="CONTACTED")
    assert len(partners_mod.history(m.con, p)) == 3           # nothing is overwritten
    notes = [h["note"] for h in partners_mod.history(m.con, p)]
    assert "call 1: gatekeeper asked for email" in notes and "follow-up 1: sent guide" in notes


def test_duplicate_resubmit_creates_no_second_record(tmp_path):
    m = make_app(tmp_path)
    p = pid(m); c = m.app.test_client()
    act(c, p, method="PHONE", note="same call", next_followup=TMRW)
    before = len(partners_mod.history(m.con, p))
    r = act(c, p, method="PHONE", note="same call", next_followup=TMRW)   # double-click / retry
    assert before == len(partners_mod.history(m.con, p)) == 2            # no second record
    assert "already recorded" in r.get_data(as_text=True)


def test_distinct_updates_same_minute_both_recorded(tmp_path):
    m = make_app(tmp_path)
    p = pid(m); c = m.app.test_client()
    act(c, p, method="PHONE", note="call 1")
    act(c, p, method="PHONE", note="call 2 was different")
    assert len(partners_mod.history(m.con, p)) == 2          # only EXACT duplicates are blocked


# ---------- rendering ----------

def test_multi_reason_bucket_lines_separated_not_glued(tmp_path):
    """A partner with no visits sits in INSUFFICIENT evidence with SEVERAL why-reasons; the queue
    rendered them glued together into one unreadable run-on ('...performance yetwhy: partner is...").
    Each reason must land on its own line."""
    m = make_app(tmp_path)
    p = pid(m); c = m.app.test_client()
    page = c.get("/admin/pipeline?token=t").get_data(as_text=True)
    assert "cannot evaluate performance yet</span><br>" in page   # separated, not 'yetwhy:'
    assert "yetwhy:" not in page


# ---------- validation + auth ----------

def test_invalid_inputs_are_rejected_and_write_nothing(tmp_path):
    m = make_app(tmp_path)
    p = pid(m); c = m.app.test_client()
    for bad in ({"stage": "CEO_BESTIE"}, {"method": "SMOKE_SIGNAL"}, {"next_followup": "10/later/maybe"}):
        r = act(c, p, **bad)
        assert r.status_code == 400
    assert len(partners_mod.history(m.con, p)) == 0          # nothing half-written
    row = dict(m.con.execute("SELECT * FROM partners WHERE partner_id=?", (p,)).fetchone())
    assert row["last_outreach_at"] is None


def test_unknown_partner_and_auth_guards(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    assert c.get("/admin/pipeline/ghost?token=t").status_code == 404
    assert c.post("/admin/pipeline/ghost/action?token=t", data={"stage": "CONTACTED"}).status_code == 404
    assert c.get("/admin/pipeline").status_code == 403                    # no token, nothing leaks
    assert c.get(f"/admin/pipeline/{pid(m)}").status_code == 403
    assert c.post(f"/admin/pipeline/{pid(m)}/action", data={"method": "EMAIL"}).status_code == 403


# ---------- integration with partner + acquisition records ----------

def test_detail_page_shows_history_assets_and_real_metrics(tmp_path):
    m = make_app(tmp_path)
    p = pid(m); c = m.app.test_client()
    m.con.execute("""INSERT INTO leads (lead_id, created_at, is_test, partner_id, phone, phone_source, phone_valid,
                     lead_type, qual_status, routing_status, agent_status, price, contact_scope)
                     VALUES ('LPL1', '2026-10-20T10:00:00', 0, 'flow-school', '3055550110', 'CONSUMER_SUBMITTED', 1,
                     'PHONE_LEAD', 'QUALIFIED', 'DELIVERED', 'SOLD', 55, 'PHONE_AND_EMAIL')""")
    m.con.commit()
    act(c, p, stage="INTERESTED", note="wants the guide printed")
    page = c.get(f"/admin/pipeline/{p}?token=t").get_data(as_text=True)
    assert "Activity history" in page and "wants the guide printed" in page
    assert "/qr.png?u=/p/flow-school" in page and "guide.pdf" in page     # existing assets, no regeneration
    assert "$55.00" in page and "Qualified" in page                      # acquisition metrics inline
    queue_page = c.get("/admin/pipeline?token=t").get_data(as_text=True)
    assert "Flow Real Estate School" in queue_page and "$55.00" in queue_page


def test_stage_endpoint_keeps_legacy_partners_page_working(tmp_path):
    m = make_app(tmp_path)
    p = pid(m); c = m.app.test_client()
    act(c, p, stage="CONTACTED")
    r = c.post(f"/admin/partners/{p}/status?token=t", data={"status": "custom note kept", "cobrand_approved": "1"},
               follow_redirects=True)
    assert r.status_code == 200                                          # old free-text editor still works
    row = dict(m.con.execute("SELECT status FROM partners WHERE partner_id=?", (p,)).fetchone())
    assert row["status"] == "custom note kept"
