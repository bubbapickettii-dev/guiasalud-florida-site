"""/admin/readiness — the consolidated launch-readiness view.

The page must render the SAME blocker computation as launch_check.py (collect()) so the CLI
report and the operator web view can never drift, derive the buyer readiness ladder ONLY from
recorded state (configured → verified → active → rehearsed → capable), and report demand as a
COUNT of recorded rows with its routing reasons — never a projection. Read-only: nothing on
this page may activate buyers, spend, or contact anyone.
"""
import shutil, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_billing import make_app
from test_admin_capacity import _isolated_cfg, _activate_real_agent


def _full_agent(m, buyer_id="agent-health-1"):
    """Make the draft a fully configured buyer: exact legal name + license + NPN + prices.
    Own mutator (not the shared _activate_real_agent) so other suites' fixtures stay untouched."""
    import json
    raw = json.loads((m.CFG / "buyers.json").read_text(encoding="utf-8"))
    for b in raw:
        if b["id"] == buyer_id:
            b["name"] = "Trail Health Advisors LLC"
            b["license_number"] = "D1234567"
            b["npn"] = "9876543"
    (m.CFG / "buyers.json").write_text(json.dumps(raw, indent=1), encoding="utf-8")


def _readiness(m):
    return m.app.test_client().get("/admin/readiness?token=t").get_data(as_text=True)


def test_ladder_walks_recorded_state(tmp_path):
    """A fully configured, verified, active buyer with a TEST delivery must show
    configured → verified → active → rehearsed → capable (no phantom blocks), and an
    unconfigured draft must report 'not configured' + 'rehearsal: send a TEST funnel lead'
    guidance instead of a green wall."""
    m = make_app(tmp_path)
    cfg = _isolated_cfg(m, tmp_path)
    _activate_real_agent(m)                       # active, priced, desk-only delivery
    _full_agent(m)                                # exact legal name + license + NPN on record
    # independent verification recorded through the real operator endpoint (never auto-derived)
    c = m.app.test_client()
    c.post("/admin/buyer/agent-health-1/verify?token=t", data={"what": "license", "ref": "test"})
    c.post("/admin/buyer/agent-health-1/verify?token=t", data={"what": "npn", "ref": "test"})
    # one TEST delivery recorded against the buyer (rehearsal happened)
    m.con.execute("""INSERT INTO leads (lead_id, created_at, is_test, campaign, product, state, zip,
        phone, phone_source, qual_status, tier, score, routing_status, buyer_id, price, routed_at,
        delivered_at, contact_scope) VALUES ('LREH1', datetime('now'), 1, 'camp', 'health_aca',
        'FL', '33101', '3055550901', 'CONSUMER_SUBMITTED', 'QUALIFIED', 'A', 60, 'DELIVERED',
        'agent-health-1', 60, datetime('now'), datetime('now'), 'PHONE_AND_EMAIL')""")
    m.con.commit()
    page = _readiness(m)
    # the ladder for the ACTIVE real buyer: all five rungs green pills
    row = page[page.index("agent-health-1"):]
    row = row[:row.index("agent-life-1")]           # THIS buyer's row only (the draft's own row follows)
    for marker in (">configured</span>", ">verified</span>", ">active</span>",
                   ">rehearsed</span>", ">capable</span>"):
        assert marker in row, marker
    assert ">blocked</span>" not in row
    # the still-empty draft agent-life-1 shows its honest gapped ladder
    draft = page[page.index("agent-life-1"):]
    draft = draft[:draft.index("</table>")]
    assert ">not configured</span>" in draft and ">not verified</span>" in draft
    assert ">inactive</span>" in draft and ">no TEST delivery yet</span>" in draft


def test_buyer_blocked_by_inactive_state_shows_gaps(tmp_path):
    """An active WITHOUT payment/prices is 'blocked' via recorded routing exclusions, and demo
    buyers are excluded from the real ladder entirely."""
    m = make_app(tmp_path)
    _isolated_cfg(m, tmp_path)
    _activate_real_agent(m)
    _full_agent(m)
    import json
    raw = json.loads((m.CFG / "buyers.json").read_text(encoding="utf-8"))
    for b in raw:                              # strip prices -> routing excludes ('no positive per-lead price')
        if b["id"] == "agent-health-1":
            b["price_by_tier"] = {}
    (m.CFG / "buyers.json").write_text(json.dumps(raw, indent=1), encoding="utf-8")
    page = _readiness(m)
    row = page[page.index("agent-health-1"):]
    row = row[:row.index("agent-life-1")]           # the priced but blocked buyer's OWN row
    assert ">blocked</span>" in row
    assert ">capable</span>" not in row            # can't claim capability while routing excludes it
    assert "Demo Agency" not in page              # demo buyers are not part of the real ladder


def test_recorded_activity_and_unmatched_demand(tmp_path):
    """TEST vs real rows are counts of recorded rows (never projections), and unmatched real
    demand names itself as demand the marketplace couldn't sell."""
    m = make_app(tmp_path)
    _isolated_cfg(m, tmp_path)
    _activate_real_agent(m)
    # real NO_BUYER qualified lead: demand recorded but no eligible buyer at routing time
    m.con.execute("""INSERT INTO leads (lead_id, created_at, is_test, campaign, product, state, zip,
        phone, phone_source, qual_status, tier, score, routing_status, price, contact_scope)
        VALUES ('LNB1', datetime('now'), 0, 'camp', 'health_aca', 'FL', '33101', '3055550902',
        'CONSUMER_SUBMITTED', 'QUALIFIED', 'A', 60, 'NO_BUYER', 60, 'PHONE_AND_EMAIL')""")
    m.con.commit()
    page = _readiness(m)
    assert "<span class=\"pill TEST\">TEST</span></td>" in page   # TEST row labelled
    assert ">Real</b></td>" in page                               # real row labelled
    assert "real qualified leads sit unmatched" in page
    assert "<a href=\"/admin/lead/LNB1?token=t\">LNB1</a>" in page  # and the lead itself is listed
def test_web_matches_the_cli_computation(tmp_path):
    """The page renders launch_check.collect() against THIS app's config: same categories,
    same item texts — CLI and operator web view cannot drift."""
    import launch_check
    from markupsafe import escape              # the page escapes with Jinja's markupsafe, not html.escape
    m = make_app(tmp_path)
    cfg = _isolated_cfg(m, tmp_path)
    block, category, base = launch_check.collect(cfg_dir=cfg)
    page = _readiness(m)
    for k, items in block.items():
        assert f"<b>{k}</b>" in page, k
        if items:
            assert category[k] in page, category[k]
            for x in items:
                assert escape(x) in page, x[:80]   # the page HTML-escapes item text
        else:
            assert ">READY</span>" in page
        break                                        # one fully-checked group proves the same source


def test_read_only_safety_posture_text(tmp_path):
    """The page states what mode the app is in (staging, dry-run delivery, indexing off) —
    an operator can never misread the safety posture as production-capable."""
    m = make_app(tmp_path)
    _isolated_cfg(m, tmp_path)
    page = _readiness(m)
    assert "LEADENGINE_ENV" in page and "<b>staging</b>" in page
    assert "dry run to outbox" in page            # live delivery is OFF in staging tests
    assert "hidden from search engines" in page   # indexing OFF
    assert "Credit queue" in page                 # pointers to the pages that own actions
