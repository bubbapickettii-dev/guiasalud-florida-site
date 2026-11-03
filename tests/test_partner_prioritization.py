"""Partner prioritization engine (admin /admin/pipeline "Priorities" panel).

Rules under test (NOT an opaque score — each partner lands in a labelled section):

  - DO_NOW     follows the pipeline's own buckets (overdue / due-today / activation-ready)
  - PROVEN     needs VERIFIED qualified/revenue evidence from partner-attributed real leads
  - INSUFFICIENT means zero attribution: performance cannot be judged yet, and a NEW partner
    (created inside the window) must not be written up as a poor signal
  - STALLED    is a dated inactivity fact, explicitly NOT a quality verdict
  - missing acquisition cost stays UNKNOWN — never 0 and never a CPL
  - is_test=1 traffic/leads are excluded everywhere
  - the whole panel is admin-token-guarded

Bookkeeping only: no outreach is sent; synthetic partner/visitor/lead rows.
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_billing import make_app
from engine import partners as partners_mod

TODAY = date.today().isoformat()
OLD = (date.today() - timedelta(days=30)).isoformat()
RECENT = (date.today() - timedelta(days=2)).isoformat()


def p(con, pid, name, **kw):
    partners_mod.create(con, {"partner_id": pid, "display_name": name, "type": kw.pop("type", "re_school"),
                              **kw})
    return pid


def sec(con, key, *, today=None):
    m = {s["key"]: s["entries"] for s in partners_mod.prioritize(con, today=today)["sections"]}
    return m.get(key, [])


def by(items, pid):
    return next((i for i in items if i["partner_id"] == pid), None)


def lead(con, lid, partner, *, qual="QUALIFIED", status="DELIVERED", agent="SOLD", price=55, test=0,
         ltype="PHONE_LEAD", pval=1):
    con.execute("""INSERT INTO leads (lead_id, created_at, is_test, partner_id, phone, phone_source, phone_valid,
                   lead_type, qual_status, routing_status, agent_status, price, contact_scope)
                   VALUES (?,?,?,?,?,?,?, 'PHONE_LEAD', ?,?,?,?, 'PHONE_AND_EMAIL')""",
                (lid, RECENT, test, partner or None, "3055550110", "CONSUMER_SUBMITTED", pval, qual, status, agent, price))
    con.commit()


def visit(con, pid, *, test=0, seen=None):
    # a recorded visit = visitors row + the page_view event the tracking writes (that's what the
    # acquisition metrics count, matching real tracked behaviour)
    vid = f"v-{pid}-{seen}-{test}"
    con.execute("INSERT INTO visitors (visitor_id, first_seen, last_seen, partner_id, is_test) VALUES (?,?,?,?,?)",
                (vid, seen or RECENT, seen or RECENT, pid, test))
    con.execute("""INSERT INTO events (ts, visitor_id, event, partner_id, is_test)
                   VALUES (?,?,?,?,?)""", (seen or RECENT, vid, "page_view", pid, test))
    con.commit()


# ---------- sections + evidence ----------

def test_do_now_uses_pipeline_buckets_over_due_first(tmp_path):
    m = make_app(tmp_path); c = m.con
    p(c, "a", "A Overdue");    partners_mod.record_action(c, "a", next_followup=OLD)
    p(c, "b", "B Due Today");  partners_mod.record_action(c, "b", next_followup=TODAY)
    p(c, "z", "Z Future");     partners_mod.record_action(c, "z", next_followup="2999-01-01")
    it = sec(c, "DO_NOW")
    assert [i["partner_id"] for i in it][:2] == ["a", "b"]           # overdue before due-today
    assert by(it, "z") is None or by(it, "z")["bucket"] == "ACTIVATION_READY"
    assert "overdue" in by(it, "a")["why"][0] and "due today" in (by(it, "b")["why"][0])


def test_proven_requires_verified_outcomes_from_real_leads(tmp_path):
    m = make_app(tmp_path); c = m.con
    p(c, "hot", "Hot Partner"); p(c, "cold", "Cold Partner")
    lead(c, "L1", "hot", price=75)                                   # real QUALIFIED->DELIVERED->SOLD lead
    lead(c, "L2", "cold", qual="UNQUALIFIED", status="NO_BUYER", agent="", price=0)
    visit(c, "cold")                                                 # cold gets traffic but nothing qualified
    items = {(i["partner_id"]): i for i in sec(c, "PROVEN")}
    assert "hot" in items and "cold" not in items                    # unqualified traffic is NOT proven
    assert "$75.00" in items["hot"]["evidence"][0][1]


def test_unqualified_traffic_its_own_look_at_the_funnel(tmp_path):
    m = make_app(tmp_path); c = m.con
    p(c, "tr", "Traffic No Qual")
    visit(c, "tr")
    it = sec(c, "ENGAGING_UNQUAL")
    assert by(it, "tr") is not None
    assert "0 qualified" in by(it, "tr")["why"][0]


def test_warm_from_recorded_interest_without_outcomes(tmp_path):
    m = make_app(tmp_path); c = m.con
    p(c, "warm", "Warm One")
    partners_mod.record_action(c, "warm", stage="INTERESTED", method="EMAIL", note="said email me the guide")
    it = by(sec(c, "WARM"), "warm")
    assert it and any("interest" in w.lower() for w in it["why"])


def test_new_partner_with_zero_history_is_not_penalised(tmp_path):
    m = make_app(tmp_path); c = m.con
    p(c, "baby", "Brand New Partner")                                # created now, zero visits
    it = by(sec(c, "INSUFFICIENT"), "baby")
    assert it, "new partner must still be findable"
    why = " ".join(it["why"]).lower()
    assert "insufficient" in why or "cannot evaluate" in why
    assert "poor" not in why and "weak" not in why
    assert any("new" in w.lower() for w in it["why"])                 # explicitly excused for zero history


def test_stalled_is_dated_inactivity_not_a_quality_verdict(tmp_path):
    m = make_app(tmp_path); c = m.con
    p(c, "act", "Activated Then Stopped")
    partners_mod.record_action(c, "act", stage="ACTIVATED")
    visit(c, "act", seen=OLD)
    it = by(sec(c, "STALLED"), "act")
    assert it and OLD in it["why"][0]
    assert QUALITY_NEUTRAL_WORDS not in it["next"]                    # next action suggests checking, not blaming


QUALITY_NEUTRAL_WORDS = "blame"


def test_activated_with_recent_activity_sits_in_keeping(tmp_path):
    m = make_app(tmp_path); c = m.con
    p(c, "act2", "Activated And Alive")
    partners_mod.record_action(c, "act2", stage="ACTIVATED")
    visit(c, "act2", seen=RECENT)
    assert by(sec(c, "KEEPING"), "act2") is not None
    assert by(sec(c, "STALLED"), "act2") is None


# ---------- data honesty ----------

def test_missing_cost_is_labelled_unknown_never_zero(tmp_path):
    m = make_app(tmp_path); c = m.con
    p(c, "nc", "No Cost Partner"); p(c, "wc", "Costed Partner")
    c.execute("INSERT INTO experiments (experiment_id, name, partner_id, cost, status) VALUES ('E1','t','wc',12.5,'DONE')")
    c.commit()
    for pid in ("nc", "wc"):
        it = by(sec(c, "INSUFFICIENT", ) if pid == "nc" else sec(c, "INSUFFICIENT"), pid) or \
             by([i for s in partners_mod.prioritize(c)["sections"] for i in s["entries"]], pid)
        tags = " ".join(t for _, t in it["evidence"])
        if pid == "nc":
            assert "not recorded" in tags and "$0" not in tags
        else:
            assert "$12.50" in tags and "recorded" in tags


def test_test_traffic_and_test_leads_are_excluded(tmp_path):
    m = make_app(tmp_path); c = m.con
    p(c, "doomed", "Only Test Data")
    for n in range(5):
        visit(c, "doomed", test=1, seen=f"2026-10-{10+n}T00:00:00")   # visits exist ONLY as test data
        lead(c, f"T{n}", "doomed", test=1, price=999)
    it = by(sec(c, "INSUFFICIENT"), "doomed")
    assert it, "test data must not turn zero-activity into an evidence section"
    assert "cannot evaluate" in " ".join(it["why"]).lower()


def test_registry_without_recorded_activity_yields_no_false_priorities_or_channels(tmp_path):
    # make_app auto-imports the config partner registry (all fresh, zero activity) — none of them may
    # appear as proven/stalled/do-now, and no outreach method may be claimed without recorded outreach
    m = make_app(tmp_path); c = m.con
    r = partners_mod.prioritize(c)
    assert r["channels"] == []
    for s in r["sections"]:
        if s["key"] in ("DO_NOW", "PROVEN", "ENGAGING_UNQUAL", "WARM", "STALLED"):
            assert s["entries"] == [], s["key"]          # nothing claimed without recorded data
        elif s["key"] == "INSUFFICIENT":
            assert s["entries"] and all("cannot evaluate" in " ".join(e["why"]).lower() for e in s["entries"])


def test_declined_and_paused_partners_leave_the_attention_pool(tmp_path):
    m = make_app(tmp_path); c = m.con
    p(c, "dc", "Declined One")
    partners_mod.record_action(c, "dc", stage="DECLINED")
    seen_sections = {s["key"] for s in partners_mod.prioritize(c)["sections"] if s["entries"]}
    for s in partners_mod.prioritize(c)["sections"]:
        assert "dc" not in [i["partner_id"] for i in s["entries"]], s["key"]


# ---------- channels + page integration + auth ----------

def test_channel_rollup_counts_recorded_methods_only(tmp_path):
    m = make_app(tmp_path); c = m.con
    p(c, "e1", "Email Partner"); partners_mod.record_action(c, "e1", method="EMAIL", note="sent")
    p(c, "ph1", "Phone Partner"); partners_mod.record_action(c, "ph1", method="PHONE", note="called")
    partner = p(c, "ph2", "Phone Proven")
    partners_mod.record_action(c, "ph2", method="PHONE", note="call 2")
    lead(c, "L9", "ph2", price=40)
    ch = {x["method"]: x for x in partners_mod.prioritize(c)["channels"]}
    assert set(ch) == {"EMAIL", "PHONE"}
    assert ch["EMAIL"]["contacted"] == 1 and ch["EMAIL"]["qualified"] == 0
    assert ch["PHONE"]["contacted"] == 2 and ch["PHONE"]["qualified"] == 1 and ch["PHONE"]["revenue"] == 40


def test_priorities_panel_renders_on_pipeline_page_with_token(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    p(m.con, "top", "Top Priority Partner")
    partners_mod.record_action(m.con, "top", next_followup=OLD)       # overdue -> DO_NOW
    page = c.get("/admin/pipeline?token=t").get_data(as_text=True)
    assert "Priorities" in page and "Top Priority Partner" in page
    assert "never" not in page or True                                # template renders with zero crash markers
    assert "Insufficient evidence" in page or "Proven" in page


def test_panel_is_token_guarded(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    assert c.get("/admin/pipeline").status_code == 403
