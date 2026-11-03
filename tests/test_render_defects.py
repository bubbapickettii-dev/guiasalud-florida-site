"""Regression tests for rendered-output defects found by the render audit
(tools/render_audit.py): the "${60.00}" class — malformed/misused template
expressions and python-into-markup leaks — plus raw None leaking into pages
when nullable lead fields (attribution, best_time, landing_url) are unset.

The audit harness itself is pinned here too, so it can never silently rot.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ROOT = Path(__file__).resolve().parents[1]

from tests.test_engine import make_app  # isolated app + temp DB, ADMIN_TOKEN "t"


def test_admin_organic_renders_marketing_copy_not_deleted_dict_repr(tmp_path):
    """r.copy resolved to dict.copy (a method) whenever a placement had no copy:
    the page printed '<built-in method copy of dict object at 0x...>'. The field
    is now copy_text, and the REAL configured copy renders."""
    m = make_app(tmp_path)
    c = m.app.test_client()
    r = c.get("/admin/organic?token=t")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "built-in method" not in body and "object at 0x" not in body
    # the configured Cigna end-of-2026 placement copy must render verbatim
    snippet = json.loads((ROOT / "config" / "distribution.json").read_text(encoding="utf-8"))
    one = next(p for p in snippet.get("placements", []) if isinstance(p, dict) and p.get("copy_en"))
    assert one["copy_en"][:40] in body


IDENTITY_PATHS = ("/", "/?lang=en", "/contact", "/terms", "/privacy", "/terminos", "/privacidad", "/optout",
                  "/for-agents", "/para-agencias")


def test_business_identity_placeholders_never_reach_a_public_page(tmp_path):
    """A bracketed/placeholder value must never be printed, whatever the shipped config says.

    The config now carries the real identity (Gia Salud, Miami, Florida) and no placeholder, so the
    guard is proven twice: first that the shipped values reach the page and nothing invented does,
    then by putting placeholders back into SITE and re-reading every public page."""
    m = make_app(tmp_path)
    c = m.app.test_client()
    for path in IDENTITY_PATHS:
        body = c.get(path).get_data(as_text=True)
        for leak in ("[YOUR LLC NAME]", "[BUSINESS MAILING ADDRESS]", "yourdomain.com", "CHANGE-ME"):
            assert leak not in body, f"{path} leaked {leak}"
        # the consumer pages print the real location; the buyer-facing pages carry their own footer
        # and link back to the consumer site instead (same operator, different audience)
        assert ("Miami, Florida" if path not in ("/for-agents", "/para-agencias") else "Gia Salud") in body, path
    ident = m.business_identity()
    assert ident["brand"] == "Gia Salud" and ident["location"] == "Miami, Florida"
    assert ident["legal_entity"] == "Gia Salud" and ident["mailing_address"] == ""
    assert ident["complete"] is False                     # the support inbox is still pending
    assert set(ident["missing"]) == {"support_email"}
    # ...and the placeholder filter still strips every form of "not supplied" from the same pages
    m.SITE.update({"legal_entity": "[YOUR LLC NAME]", "mailing_address": "[BUSINESS MAILING ADDRESS]",
                   "support_email": "[support@yourdomain.com]", "location": "CHANGE-ME"})
    for path in IDENTITY_PATHS:
        body = c.get(path).get_data(as_text=True)
        for leak in ("[YOUR LLC NAME]", "[BUSINESS MAILING ADDRESS]", "yourdomain.com", "CHANGE-ME"):
            assert leak not in body, f"{path} leaked {leak} with placeholders configured"
    assert set(m.business_identity()["missing"]) == {"legal_entity", "location", "support_email"}


def test_business_identity_renders_consistently_once_configured(tmp_path):
    """The four values are entered once (config or env) and the footer, terms, privacy (EN+ES) and contact
    page all show the SAME name and address — the drift this helper exists to prevent."""
    m = make_app(tmp_path)
    c = m.app.test_client()
    m.SITE.update({"legal_entity": "GS Allude LLC", "mailing_address": "1200 Brickell Ave, Miami, FL 33131",
                   "contact_email": "support@example.org", "support_phone": "(305) 555-0100"})
    for path in ("/", "/terms", "/privacy", "/privacidad", "/contact", "/for-agents"):
        body = c.get(path).get_data(as_text=True)
        assert "GS Allude LLC" in body, path
    brand = m.SITE["brand"]                                  # brand-agnostic: the copy reads site.brand
    terms = c.get("/terms").get_data(as_text=True)
    assert f"{brand} is operated by GS Allude LLC, 1200 Brickell Ave, Miami, FL 33131." in terms
    priv_es = c.get("/privacidad").get_data(as_text=True)
    assert f"es operado por GS Allude LLC, 1200 Brickell Ave, Miami, FL 33131." in priv_es
    contact = c.get("/contact").get_data(as_text=True)
    assert "mailto:support@example.org" in contact and "(305) 555-0100" in contact
    assert m.business_identity()["complete"] is True
    # the compliance sentence still sits beside the identity on terms, in both languages
    assert "not an insurance company" in terms and "no vendemos seguros" in c.get("/terminos").get_data(as_text=True)


def _seed_null_attributed_lead(m):
    m.con.execute(
        """INSERT INTO leads (lead_id, created_at, is_test, campaign, product, state, zip, phone, phone_source,
        qual_status, lead_type, tier, score, routing_status, contact_scope, consent_ts)
        VALUES ('LNULL1', datetime('now'), 1, 'camp-x', 'individual_marketplace', 'FL', '33130',
                '3055550141', 'CONSUMER_SUBMITTED', 'QUALIFIED', 'EXCLUSIVE', 'B', 44, 'UNROUTED', 'PHONE_SMS', NULL)""")
    m.con.commit()
    return "LNULL1"


def test_admin_lead_renders_dashes_for_unset_fields_not_bare_none(tmp_path):
    """Attribution, best_time and routing timestamps are nullable; the operator
    page must show dashes, never 'None / None / None'."""
    m = make_app(tmp_path)
    lid = _seed_null_attributed_lead(m)
    r = m.app.test_client().get(f"/admin/lead/{lid}?token=t")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    for leak in ["None /", "/ None", "prefers None", "prefers \u00a0None", "(None)", "link None", "partner None",
                 "routed None", "delivered None", "buyer None"]:
        assert leak not in body, f"raw null leaked into admin lead page: {leak!r}"


def test_agent_lead_renders_unset_landing_as_dash(tmp_path):
    """An API-submitted lead (no page context) has landing_url NULL; the buyer's
    lead page must not print 'landing None'."""
    m = make_app(tmp_path)
    c = m.app.test_client()
    r = c.post("/api/lead", json=dict(campaign="mortgage-protection", first_name="Ana", last_name="Landing",
                                     email="ana.landing@example.test", phone="3055550152", zip="33130", state="FL",
                                     age_range="40-49", coverage_for="me_spouse_kids", mortgage_balance="300-500k",
                                     tobacco="no", timeframe="asap", preferred_contact="phone", is_test=1,
                                     tool_result={"headline": "$300,000"}, consent_checked=True)).get_json()
    lid = (r.get("lead") or {}).get("lead_id") if isinstance(r.get("lead"), dict) else r.get("lead_id")
    assert lid, r
    tok = next(b["portal_token"] for b in m.route.load_buyers(m.CFG) if b.get("is_demo"))
    page = c.get(f"/agent/{tok}/lead/{lid}")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "landing None" not in body and "landing None" not in body
    assert "landing -" in body


def test_render_audit_harness_detects_artifacts_and_stays_clean(tmp_path):
    """The audit scanner must flag the bug classes it defends against — and pass
    clean markup — so it can never silently rot into a no-op."""
    import tools.render_audit as ra
    hits = ra.scan("<div class='kpi'><b>${60.00}</b></div>")
    assert any("currency artifact" in why for why, _ in hits)
    hits = ra.scan("<p>{{ item.copy }}</p>")
    assert any("Jinja" in why for why, _ in hits)
    hits = ra.scan("<p>{% if x %}hi{% endif %}</p>")
    assert any("Jinja statement" in why for why, _ in hits)
    hits = ra.scan("('credits','Credit queue','/admin/credits'),('readiness','Readiness','/admin/readiness')")
    assert any("tuple" in why for why, _ in hits)
    assert ra.scan("<tr><td>plain rendered html — fine</td></tr>") == []


def test_agent_home_avg_cost_renders_plain_dollars_not_brace_artifact(tmp_path):
    """The avg-cost card once rendered '${60.00}' (dollar followed by literal braces).
    Two $60 deliveries must render '$60.00' and the page must contain no '${' artifact
    anywhere (the bug class the render audit signs on)."""
    import test_buyer_dashboard as tbd
    m = make_app(tmp_path)
    tok, bid = tbd._tok(m, tmp_path)
    tbd._seed_delivered(m, bid, day_offset=0, price=60.0, cid="avg1", name="Ana Sixty", phone="4075551111")
    tbd._seed_delivered(m, bid, day_offset=1, price=60.0, cid="avg2", name="Bob Sixty", phone="4075552222")
    page = m.app.test_client().get(f"/agent/{tok}").get_data(as_text=True)
    assert "$60.00" in page
    assert "${" not in page


def test_seed_demo_credits_idempotent_test_only(tmp_path):
    """The demo funding tool must credit each demo buyer exactly once (idempotent),
    append TEST-mode rows only, and leave the display honest without deleting history."""
    import json
    import sqlite3
    import subprocess
    tmp_cfg = tmp_path / "cfg"
    tmp_cfg.mkdir()
    (tmp_cfg / "buyers.json").write_text(json.dumps([
        {"id": "demo-ct", "is_demo": True, "portal_token": "t-ct", "active": True}]), encoding="utf-8")
    db = tmp_path / "v.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE buyer_ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, buyer_id TEXT,"
                " lead_id TEXT, package_id TEXT, kind TEXT, amount REAL, balance_after REAL, note TEXT,"
                " is_test INTEGER DEFAULT 0)")
    con.commit(); con.close()
    env = {**os.environ, "LEADENGINE_DB": str(db), "LEADENGINE_CFG": str(tmp_cfg),
           "DEMO_SETUP_CREDITS_USD": "250"}
    r1 = subprocess.run([sys.executable, str(ROOT / "tools" / "seed_demo_credits.py")],
                        capture_output=True, text=True, env=env, cwd=str(ROOT))
    assert r1.returncode == 0, r1.stderr
    assert "demo-ct" in r1.stdout and "250" in r1.stdout
    r2 = subprocess.run([sys.executable, str(ROOT / "tools" / "seed_demo_credits.py")],
                        capture_output=True, text=True, env=env, cwd=str(ROOT))
    assert r2.returncode == 0 and "already funded" in r2.stdout
    con = sqlite3.connect(db)
    rows = con.execute("SELECT buyer_id, kind, amount, note, is_test FROM buyer_ledger").fetchall()
    con.close()
    assert len(rows) == 1  # exactly once, no matter how many runs
    buyer, kind, amt, note, is_test = rows[0]
    assert buyer == "demo-ct" and amt == 250.0 and is_test == 1
    assert note.startswith("demo setup credits") and "TEST" in note


def test_template_edits_reload_in_dev_not_only_after_restart():
    """A live demo must show template fixes on the next page load; production keeps
    caching for speed. This is the process fix behind the stale '${60.00}' page."""
    m = make_app(Path(__file__).parent / "_tmp_autoreload")  # fresh import, dev env
    assert m.app.config.get("TEMPLATES_AUTO_RELOAD") is True
