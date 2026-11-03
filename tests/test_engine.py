"""API + engine tests against a throwaway DB (no network, no real people)."""
import importlib, json, os, sqlite3, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def make_app(tmp, intel_db=None):
    os.environ["LEADENGINE_DB"] = str(tmp / "t.db")
    os.environ["ADMIN_TOKEN"] = "t"
    os.environ["INTEL_DB"] = str(intel_db or (tmp / "none.db"))
    import app as appmod
    importlib.reload(appmod)
    appmod.OUTBOX = tmp / "outbox"; appmod.SNAP = tmp / "snap"
    return appmod


BASE = dict(campaign="mortgage-protection", first_name="Ana", last_name="Lopez", email="ana@example.com",
            phone="3055550110", zip="33130", state="FL", age_range="40-49", coverage_for="me_spouse_kids",
            mortgage_balance="300-500k", tobacco="no", timeframe="asap", preferred_contact="phone", is_test=1,
            tool_result={"headline": "$300,000"})


def post(client, **kw):
    # consumers never send a buyer id — the platform assigns the buyer server-side
    d = dict(BASE); d.update(kw)
    return client.post("/api/lead", json=d)


def test_full_flow_routes_and_records_consent(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    r = post(c, consent_checked=True).get_json()
    assert r["status"] == "QUALIFIED" and r["routed"] == "ROUTED"
    row = m.con.execute("SELECT * FROM leads").fetchone()
    assert row["routing_status"] == "DELIVERED" and row["buyer_id"] == "demo-agency-a"
    cons = m.con.execute("SELECT * FROM consents").fetchone()
    # platform-controlled distribution: the on-screen consent names the platform, NEVER the buyer the
    # platform later assigns (a consumer cannot see or select a buyer)
    assert "licensed agent partner" in cons["consent_text"] and "[Demo Agency A" not in cons["consent_text"]
    assert "licensed agent partner" in cons["seller_named"] and cons["checkbox_checked"] == 1 and len(cons["form_snapshot_sha256"]) == 64
    assert list((tmp_path / "outbox").glob("*.json"))


def test_no_consent_is_rejected(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    assert post(c, consent_checked=False).status_code == 400


def test_email_only_is_nurture_not_sold(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    r = post(c, consent_checked=False, email_consent_checked=True).get_json()
    assert r["status"] == "NURTURE" and r["routed"] == "NOT_ROUTABLE"


def test_age_out_of_range_and_unserved_state(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    r = post(c, campaign="final-expense", age_range="30-39", fe_amount="5-10k", consent_checked=True).get_json()
    assert r["status"] == "UNQUALIFIED"
    # no licensed buyer in GA: the consumer's consent to the platform is still recorded (just nobody is
    # assigned); an unqualified lead is never routed or sold. The UI only offers the email path then.
    r2 = post(c, state="GA", consent_checked=True, email="x2@example.com").get_json()
    assert r2["ok"] is True and r2["routed"] != "ROUTED"
    r = post(c, state="GA", email_consent_checked=True, email="x3@example.com").get_json()
    assert r["status"] == "UNQUALIFIED"


def test_duplicate_and_honeypot(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    post(c, consent_checked=True)
    assert post(c, consent_checked=True).get_json()["status"] == "DUPLICATE"
    before = m.con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    post(c, consent_checked=True, website="spam", email="bot@example.com")
    assert m.con.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == before


def test_monthly_purchase_limit_blocks_routing(tmp_path):
    """Beyond the daily cap, a per-month purchase limit is a second buyer-side limit the platform
    enforces at assignment time — a buyer at their monthly cap is not offered (and never counted against them)."""
    m = make_app(tmp_path)
    from engine import route as rt
    b = dict(next(x for x in rt.load_buyers(m.CFG) if x["id"] == "demo-agency-a"))
    assert rt.preview(m.con, [b], b["products"][0], "FL", "40-49") is not None
    # one REAL delivered lead this month, then the monthly cap applies
    m.con.execute("""INSERT INTO leads (lead_id, created_at, is_test, campaign, product, state, zip, phone, phone_source,
        qual_status, tier, score, routing_status, buyer_id, price, routed_at, sale_type)
        VALUES ('LMTH1', datetime('now'), 0, 'camp', ?, 'FL', '33101', '3054440110', 'CONSUMER_SUBMITTED',
        'QUALIFIED', 'A', 60, 'DELIVERED', ?, 40, ?, 'EXCLUSIVE')""", (b["products"][0], b["id"], m.now()))
    b["monthly_cap"] = 1
    assert rt.preview(m.con, [b], b["products"][0], "FL", "40-49") is None
    reason, n = rt.explain(m.con, [b], {"state": "FL", "product": b["products"][0], "age_range": "40-49", "tier": "A"})
    assert "monthly purchase limit 1 reached" in reason
    # TEST traffic never counts toward a purchase limit
    m.con.execute("UPDATE leads SET is_test=1 WHERE lead_id='LMTH1'")
    assert rt.preview(m.con, [b], b["products"][0], "FL", "40-49") is not None


def test_reroute_unmatched_lead_conditionally_sells_once(tmp_path):
    """An unmatched (NO_BUYER) qualified phone lead can be re-offered later: it gets the SAME platform
    assignment, exactly one delivery + one charge, a RE_ASSIGNED audit row — and a delivered lead is
    never re-offered (400), so exclusivity cannot be broken from the admin."""
    m = make_app(tmp_path); c = m.app.test_client()
    from engine import route as rt
    b = dict(next(x for x in rt.load_buyers(m.CFG) if x["id"] == "demo-agency-a"))
    m.con.execute("""INSERT INTO leads (lead_id, created_at, is_test, campaign, product, state, zip, phone, phone_source,
        qual_status, lead_type, tier, score, routing_status, contact_scope, consent_ts)
        VALUES ('LUNM1', datetime('now'), 1, 'camp', ?, 'FL', '33101', '3055550110', 'CONSUMER_SUBMITTED',
        'QUALIFIED', 'PHONE_LEAD', 'A', 60, 'NO_BUYER', 'PHONE_AND_EMAIL', datetime('now'))""", (b["products"][0],))
    m.con.execute("""INSERT INTO consents (lead_id, ts, consent_version, consent_text, seller_named, checkbox_checked,
        scope, form_snapshot_sha256) VALUES ('LUNM1', datetime('now'), '1', 'consent text', 'platform', 1,
        'PHONE_AND_EMAIL', ?)""", ("a" * 64,))
    r = c.post(f"/admin/lead/LUNM1/reroute?token=t")
    assert r.status_code == 302
    row = m.con.execute("SELECT * FROM leads WHERE lead_id='LUNM1'").fetchone()
    assert row["routing_status"] == "DELIVERED" and row["buyer_id"] == b["id"]
    dec = [dict(x) for x in m.con.execute("SELECT decision, buyer_id FROM routing_decisions WHERE lead_id='LUNM1'")]
    assert dec and dec[-1]["decision"] == "RE_ASSIGNED"
    assert m.con.execute("SELECT COUNT(*) FROM buyer_ledger WHERE lead_id='LUNM1'").fetchone()[0] == 1   # one charge
    # a DELIVERED lead is never re-routed: the refusal SPEAKS (redirect + reason) and the guard still
    # holds — same buyer, same status, no new assignment audit row, no second charge.
    dec_count = m.con.execute("SELECT COUNT(*) FROM routing_decisions WHERE lead_id=?", (row["lead_id"],)).fetchone()[0]
    rr = c.post(f"/admin/lead/{row['lead_id']}/reroute?token=t", follow_redirects=True)
    assert rr.status_code == 200 and "already DELIVERED" in rr.get_data(as_text=True)
    row = m.con.execute("SELECT * FROM leads WHERE lead_id='LUNM1'").fetchone()
    assert row["routing_status"] == "DELIVERED" and row["buyer_id"] == b["id"]
    assert m.con.execute("SELECT COUNT(*) FROM routing_decisions WHERE lead_id=?", (row["lead_id"],)).fetchone()[0] == dec_count
    assert m.con.execute("SELECT COUNT(*) FROM buyer_ledger WHERE lead_id='LUNM1'").fetchone()[0] == 1
    m.con.execute("""INSERT INTO leads (lead_id, is_test, campaign, product, state, qual_status, lead_type, routing_status)
        VALUES ('LUNQ1', 1, 'camp', 'health_aca', 'FL', 'UNQUALIFIED', 'PHONE_LEAD', 'NO_BUYER')""")
    rn = c.post("/admin/lead/LUNQ1/reroute?token=t", follow_redirects=True)
    assert rn.status_code == 200 and "QUALIFIED PHONE lead" in rn.get_data(as_text=True)
    assert m.con.execute("SELECT routing_status FROM leads WHERE lead_id='LUNQ1'").fetchone()[0] == "NO_BUYER"


def test_auto_reoffer_sweep_recovers_aged_unmatched_once(tmp_path):
    """The timed sweep on the admin overview re-offers an aged NO_BUYER lead exactly like the operator
    button: same platform assignment, one delivery, one audit row, one charge — never a delivered sale,
    never an opted-out consumer — and it does NOT repeat while the last attempt is inside after_hours."""
    m = make_app(tmp_path); c = m.app.test_client()
    from engine import route as rt
    b = dict(next(x for x in rt.load_buyers(m.CFG) if x["id"] == "demo-agency-a"))
    prod = b["products"][0]
    m.con.execute("""INSERT INTO leads (lead_id, created_at, is_test, campaign, product, state, zip, phone, phone_source,
        qual_status, lead_type, tier, score, routing_status, contact_scope, consent_ts)
        VALUES ('LAUTO1', datetime('now','-72 hours'), 1, 'camp', ?, 'FL', '33101', '3055550110', 'CONSUMER_SUBMITTED',
        'QUALIFIED', 'PHONE_LEAD', 'A', 60, 'NO_BUYER', 'PHONE_AND_EMAIL', datetime('now'))""", (prod,))
    m.con.execute("""INSERT INTO consents (lead_id, ts, consent_version, consent_text, seller_named, checkbox_checked,
        scope, form_snapshot_sha256) VALUES ('LAUTO1', datetime('now'), '1', 'consent text', 'platform', 1,
        'PHONE_AND_EMAIL', ?)""", ("a" * 64,))
    # a lead already SOLD is never swept back into a second sale
    m.con.execute("""INSERT INTO leads (lead_id, created_at, is_test, campaign, product, state, zip, phone, phone_source,
        qual_status, lead_type, tier, score, routing_status, buyer_id, contact_scope, consent_ts)
        VALUES ('LSOLD1', datetime('now','-72 hours'), 1, 'camp', ?, 'FL', '33004', '9545550130', 'CONSUMER_SUBMITTED',
        'QUALIFIED', 'PHONE_LEAD', 'A', 62, 'DELIVERED', 'demo-agency-a', 'PHONE_AND_EMAIL', datetime('now'))""", (prod,))
    assert c.get("/admin?token=t").status_code == 200
    row = m.con.execute("SELECT * FROM leads WHERE lead_id='LAUTO1'").fetchone()
    assert row["routing_status"] == "DELIVERED" and row["buyer_id"] == b["id"]
    dec = m.con.execute("SELECT decision FROM routing_decisions WHERE lead_id='LAUTO1' ORDER BY id").fetchall()
    assert [d["decision"] for d in dec] == ["RE_ASSIGNED"]
    assert m.con.execute("SELECT COUNT(*) FROM buyer_ledger WHERE lead_id='LAUTO1'").fetchone()[0] == 1
    assert m.con.execute("SELECT COUNT(*) FROM routing_decisions WHERE lead_id='LSOLD1'").fetchone()[0] == 0
    # no eligible buyer at all (state nobody is licensed in): the sweep logs ONE audit row and does not
    # repeat it on the next two admin loads (inside the after_hours window)
    m.con.execute("""INSERT INTO leads (lead_id, created_at, is_test, campaign, product, state, zip, phone, phone_source,
        qual_status, lead_type, tier, score, routing_status, contact_scope, consent_ts)
        VALUES ('LBUSY1', datetime('now','-72 hours'), 1, 'camp', 'health_aca', 'ZZ', '00000', '3055550120', 'CONSUMER_SUBMITTED',
        'QUALIFIED', 'PHONE_LEAD', 'A', 60, 'NO_BUYER', 'PHONE_AND_EMAIL', datetime('now'))""")
    c.get("/admin?token=t"); c.get("/admin?token=t")
    d2 = m.con.execute("SELECT decision FROM routing_decisions WHERE lead_id='LBUSY1' ORDER BY id").fetchall()
    assert [d["decision"] for d in d2] == ["NO_BUYER"]
    assert m.con.execute("SELECT COUNT(*) FROM buyer_ledger WHERE lead_id='LBUSY1'").fetchone()[0] == 0


def test_admin_deliver_now_completes_stranded_sale_once(tmp_path):
    """A stranded ROUTED sale (crash between delivery and charge) — or one stranded because the buyer
    was DEACTIVATED before their desk could heal it — is completed by the operator action: same assigned
    buyer, exactly one delivery, exactly one charge, and routing is never re-decided."""
    m = make_app(tmp_path); c = m.app.test_client()
    import shutil
    cfg = tmp_path / "cfg"; shutil.copytree(m.CFG, cfg); m.CFG = cfg   # deactivations stay on a temp copy
    r = post(c, consent_checked=True).get_json()
    assert r["routed"] == "ROUTED"
    lead_id = r["lead_id"]
    buyer_id = m.con.execute("SELECT buyer_id FROM leads WHERE lead_id=?", (lead_id,)).fetchone()[0]
    # simulate the crash: drop the delivery and the charge (the sale stands only as ROUTED)
    m.con.execute("DELETE FROM deliveries WHERE lead_id=?", (lead_id,))
    m.con.execute("DELETE FROM buyer_ledger WHERE lead_id=?", (lead_id,))
    m.con.execute("UPDATE leads SET routing_status='ROUTED', delivered_at=NULL WHERE lead_id=?", (lead_id,))
    # deactivate the buyer: the desk self-heal can never run now
    buyers = json.loads((m.CFG / "buyers.json").read_text(encoding="utf-8"))
    for x in buyers:
        if x["id"] == buyer_id:
            x["active"] = False
    (m.CFG / "buyers.json").write_text(json.dumps(buyers), encoding="utf-8")
    assert c.post(f"/admin/lead/{lead_id}/deliver?token=t").status_code == 302
    row = m.con.execute("SELECT routing_status, delivered_at FROM leads WHERE lead_id=?", (lead_id,)).fetchone()
    assert row["routing_status"] == "DELIVERED" and row["delivered_at"]
    assert m.con.execute("SELECT COUNT(*) FROM deliveries WHERE lead_id=?", (lead_id,)).fetchone()[0] == 1
    charges = m.con.execute("SELECT COUNT(*) FROM buyer_ledger WHERE lead_id=? AND kind='LEAD_CHARGE'",
                            (lead_id,)).fetchone()[0]
    assert charges == 1
    # repeated admin attempts are no-ops: the refusal SPEAKS (redirect + reason) and never re-posts
    rd = c.post(f"/admin/lead/{lead_id}/deliver?token=t", follow_redirects=True)
    assert rd.status_code == 200 and "already DELIVERED" in rd.get_data(as_text=True)
    assert m.con.execute("SELECT COUNT(*) FROM deliveries WHERE lead_id=?", (lead_id,)).fetchone()[0] == 1
    assert m.con.execute("SELECT COUNT(*) FROM buyer_ledger WHERE lead_id=? AND kind='LEAD_CHARGE'",
                         (lead_id,)).fetchone()[0] == 1


def test_real_numbers_reject_fictional_phone(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    r = post(c, consent_checked=True, is_test=0).get_json()
    assert r["status"] == "UNQUALIFIED"


def test_optout_flags_lead(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    post(c, consent_checked=True)
    c.post("/optout", data={"email": "ana@example.com"})
    assert m.con.execute("SELECT opted_out FROM leads").fetchone()[0] == 1


def test_intel_match_boosts_score(tmp_path):
    idb = tmp_path / "intel.db"
    s = sqlite3.connect(idb)
    s.execute("CREATE TABLE signals (trigger TEXT, event_date TEXT, first_name TEXT, last_name TEXT, source TEXT, zip5 TEXT, details TEXT, addr1 TEXT, city TEXT, business_name TEXT)")
    s.execute("CREATE TABLE prospects (triggers TEXT, zip5 TEXT, city TEXT, ch_email INT, ch_phone_manual INT, latest_event TEXT)")
    s.execute("INSERT INTO signals VALUES ('NEW_REALTOR_LICENSE','2026-09-01','ANA','LOPEZ','dbpr','33130','{}','','','')")
    s.commit()
    m = make_app(tmp_path, idb); c = m.app.test_client()
    post(c, consent_checked=True)
    row = m.con.execute("SELECT intel_match, score_breakdown FROM leads").fetchone()
    assert "NEW_REALTOR_LICENSE" in row["intel_match"] and json.loads(row["score_breakdown"])["public_signal_match"] == 10


def test_tracking_link_redirect_and_admin(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    m.con.execute("INSERT INTO links (code,campaign,channel,utm_source,utm_medium,utm_campaign,created_at) VALUES ('x1','final-expense','direct_mail_postcard','mail','postcard','final-expense','now')")
    r = c.get("/go/x1")
    assert r.status_code == 302 and "/c/final-expense" in r.headers["Location"] and "lc=x1" in r.headers["Location"]
    assert c.get("/admin").status_code == 403 and c.get("/admin?token=t").status_code == 200
    for slug in [json.loads(l)["slug"] if False else x["slug"] for l in [0] for x in json.loads((Path(m.CFG) / "campaigns.json").read_text())]:
        assert c.get(f"/c/{slug}").status_code in (200, 302)   # health campaigns now redirect to /check


def test_health_es_consent_partner_and_feedback(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    m.partners_mod.create(m.con, {"partner_id": "sch-test", "display_name": "Test School", "type": "re_school", "language": "es", "token": "tok"})
    r = c.get("/p/sch-test")
    assert r.status_code == 302 and "/check?" in r.headers["Location"] and "a=es_self_employed" in r.headers["Location"] and "partner=sch-test" in r.headers["Location"]
    d = dict(BASE, campaign="oep-2027-health-es", health_timeframe="jan1", age_range="35-44", need_path="self_employed", household_size=1,
             income_band="150-250", enrollment_situation="OEP_UPCOMING", urgency="MEDIUM", product_category="ACA_INDIVIDUAL", lang="es",
             angle="es_self_employed", partner_id="sch-test", variant="es_self_employed", is_test=0, phone="3054461234", email="maria@example.com")
    d.pop("mortgage_balance")
    pv = c.get("/api/route-preview?campaign=oep-2027-health-es&state=FL&age_range=35-44").get_json()
    assert "seguro médico" in pv["consent_text"]
    d["consent_checked"] = True
    r = c.post("/api/lead", json=d).get_json()
    assert r["status"] == "QUALIFIED" and r["routed"] == "ROUTED"
    row = m.con.execute("SELECT * FROM leads").fetchone()
    assert row["buyer_id"] == "demo-health-agent" and row["need_path"] == "self_employed" and row["lang"] == "es"
    assert (tmp_path / "outbox" / "capi" / f"{row['lead_id']}.json").exists()          # real (non-test) lead -> CAPI payload (dry run)
    eml = (tmp_path / "outbox" / "notifications" / f"{row['lead_id']}.eml").read_text()
    assert "8am-8pm" in eml and "155.220" in eml                                       # corrected compliance reminders
    capi = json.loads((tmp_path / "outbox" / "capi" / f"{row['lead_id']}.json").read_text())
    assert "health" not in json.dumps(capi).lower()                                       # no health data sent to Meta
    stats = c.get("/partner/tok").get_data(as_text=True)
    assert "Asked for help" in stats and "maria" not in stats.lower() and "3054461234" not in stats   # counts only, no PII
    # agent desk: one-click statuses, other buyers can't touch the lead
    desk = c.get("/agent/demo-health-token").get_data(as_text=True)
    assert "Maria" in desk or "Ana" in desk
    c.get(f"/agent/demo-health-token/lead/{row['lead_id']}")
    assert m.con.execute("SELECT agent_status FROM leads").fetchone()[0] == "VIEWED"
    c.post("/agent/demo-health-token", data={"lead_id": row["lead_id"], "status": "APPOINTMENT"})
    assert m.con.execute("SELECT outcome FROM lead_outcomes").fetchone()[0] == "APPOINTMENT"
    c.post("/agent/demo-agency-a-token", data={"lead_id": row["lead_id"], "status": "SOLD"})
    assert m.con.execute("SELECT COUNT(*) FROM lead_outcomes").fetchone()[0] == 1
    assert m.con.execute("SELECT agent_status FROM leads").fetchone()[0] == "APPOINTMENT"
    q = {r_["k"]: r_ for r_ in m.quality.by(m.con, "partner", "real")}
    assert q["sch-test"]["appointments"] == 1 and q["sch-test"]["appointment_rate"] == 100.0


def test_dnc_propagates_system_wide(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    d = dict(BASE, campaign="oep-2027-health", age_range="35-44", health_timeframe="asap", urgency="HIGH", income_band="100-150",
             household_size=2, need_path="family", consent_checked=True)
    d.pop("mortgage_balance")
    assert c.post("/api/lead", json=d).get_json()["routed"] == "ROUTED"
    lid = m.con.execute("SELECT lead_id FROM leads").fetchone()[0]
    c.post("/agent/demo-health-token", data={"lead_id": lid, "status": "DNC"})
    assert m.con.execute("SELECT opted_out FROM leads").fetchone()[0] == 1
    assert m.con.execute("SELECT COUNT(*) FROM optouts").fetchone()[0] == 2


def test_lab_experiment_link_and_evidence_labels(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    assert m.con.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] >= 12        # seeded plan, all PLANNED
    eid = m.lab.create(m.con, {"name": "t", "angle": "es_family", "channel": "qr_counter_cards", "cost": 20})
    r = c.get(f"/go/{eid.lower()}")
    assert "/check?" in r.headers["Location"] and f"x={eid}" in r.headers["Location"] and "a=es_family" in r.headers["Location"]
    met = m.lab.metrics(m.con, eid)
    assert met["real"]["clicks"] == 1                                                   # a normal browser click counts
    assert met["evidence"] == "LIVE - NO LEADS YET" and met["real"]["cost_per_qualified_lead"] is None
    c.get(f"/go/{eid.lower()}?test=1")
    c.get(f"/go/{eid.lower()}", headers={"User-Agent": "facebookexternalhit/1.1"})
    assert m.lab.metrics(m.con, eid)["real"]["clicks"] == 1                             # test clicks and link-preview bots never count
    c.post("/api/event", json={"visitor_id": "v1", "event": "page_view", "experiment_id": eid, "is_test": 1, "campaign": "oep-2027-health-es"})
    assert m.lab.metrics(m.con, eid)["test"]["landing_page_visits"] == 1 and m.lab.metrics(m.con, eid)["evidence"] == "LIVE - NO LEADS YET"
    assert c.get("/admin/lab?token=t").status_code == 200 and b"UNTESTED" in c.get("/admin/lab.csv?token=t").data


def test_partner_create_guide_and_qr(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    c.post("/admin/partners?token=t", data={"display_name": "Hialeah Salon Suites", "type": "salon_suites", "language": "es"})
    p = m.con.execute("SELECT * FROM partners WHERE display_name='Hialeah Salon Suites'").fetchone()
    assert p["default_angle"] == "es_self_employed" and p["status"].startswith("PROSPECT")
    assert c.get("/qr.png?u=/p/" + p["partner_id"]).data[:4] == b"\x89PNG"
    assert c.get("/qr.png?u=https://evil.example").status_code == 400
    pdf = c.get(f"/admin/partners/{p['partner_id']}/guide.pdf?token=t")
    if pdf.status_code == 200:
        assert pdf.data[:4] == b"%PDF"          # healthy environment: a real PDF
    else:
        # optional-surface degradation (documented Windows long-path Pillow DLL issue):
        # the operator gets a clear message on the partners page, never a 500 crash.
        assert pdf.status_code == 302
        page = c.get(pdf.headers["Location"]).get_data(as_text=True)
        assert "PDF generation unavailable" in page


def test_check_pages_content_and_errors(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    for a in m.HEALTH_FLOW["angles"]:
        import html as h_; html = h_.unescape(c.get(f"/check?a={a}").get_data(as_text=True))
        assert m.HEALTH_FLOW["angles"][a]["headline"] in html
    assert 'lang="es"' in c.get("/check?a=es_check").get_data(as_text=True)
    for p in m.CONTENT["pages"]:
        r = c.get(("/aprende/" if p["lang"] == "es" else "/learn/") + p["slug"])
        cta = f"/c/{p['life_campaign']}?via=" if p.get("life_campaign") else f"a={p['angle']}"     # life pages -> life estimator
        assert r.status_code == 200 and "FAQPage" in r.get_data(as_text=True) and cta in r.get_data(as_text=True)
    assert c.get("/does-not-exist").status_code == 404 and c.get("/privacidad").status_code == 200
    assert c.get("/healthz").get_json()["ok"] is True
    for path in ["/admin/quality?token=t&dim=experiment&test=all", "/admin/partners?token=t", "/admin/content?token=t", "/admin/intel/briefs?token=t"]:
        assert c.get(path).status_code == 200, path


def test_health_scoring_urgency_and_gap_flag(tmp_path):
    m = make_app(tmp_path)
    hi = m.qualify.score_health({"urgency": "HIGH", "income_band": "150-250", "household_size": 3, "contact_scope": "PHONE_AND_EMAIL", "enrollment_situation": "SEP_LIKELY"}, [], None)
    lo = m.qualify.score_health({"urgency": "LOW", "income_band": "lt100", "household_size": 1, "contact_scope": "EMAIL_ONLY", "flags": "coverage_gap_risk"}, [], None)
    assert (hi[0] if isinstance(hi, tuple) else hi) > (lo[0] if isinstance(lo, tuple) else lo)


def test_production_hides_demo_buyers_from_real_traffic(tmp_path, monkeypatch):
    """In production NO forgeable client hint may surface demo buyers: a demo placeholder in a consumer's
    consent is a compliance failure. Staging keeps QA gadgetry (the le_qa cookie); production stays closed."""
    m = make_app(tmp_path)
    monkeypatch.setattr(m, "PRODUCTION", True)
    c = m.app.test_client()
    assert c.get("/api/route-preview?campaign=oep-2027-health&state=FL&age_range=35-44").get_json()["available"] is False
    assert c.get("/api/route-preview?test=1&campaign=oep-2027-health&state=FL&age_range=35-44").get_json()["available"] is False
    assert c.get("/api/route-preview?campaign=oep-2027-health&state=FL&age_range=35-44",
                 headers={"Cookie": "le_qa=1"}).get_json()["available"] is False   # even the QA cookie is staged-only
    # submissions in production are never marked TEST, whatever the payload claims
    r = m.route.route(m.con, [], {"qual_status": "QUALIFIED", "lead_type": "PHONE_LEAD", "tier": "A", "phone": "3054461234",
                                   "product": "health_aca", "state": "FL", "age_range": "35-44"})
    assert r["routing_status"] == "NO_BUYER"


def test_consent_names_the_real_button(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    en = c.get("/api/route-preview?flow=check&campaign=oep-2027-health&state=FL&age_range=35-44").get_json()["consent_text"]
    es = c.get("/api/route-preview?flow=check&campaign=oep-2027-health-es&state=FL&age_range=35-44").get_json()["consent_text"]
    assert '"Connect me with an agent"' in en and '"Conectarme con un agente"' in es
    legacy = c.get("/api/route-preview?campaign=mortgage-protection&state=FL&age_range=40-49").get_json()["consent_text"]
    assert '"Get my options"' in legacy
    js = (Path(m.BASE) / "static" / "health.js").read_text()
    assert 'tx("Connect me with an agent", "Conectarme con un agente")' in js   # button label == consent label


def test_consumer_summary_email_dry_run(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    d = dict(BASE, campaign="oep-2027-health-es", age_range="35-44", health_timeframe="jan1", urgency="MEDIUM", lang="es",                         email="eva@example.com", consent_checked=False, email_consent_checked=True,
             tool_result={"headline": "Podrías calificar para ayuda con la prima"})
    d.pop("mortgage_balance")
    r = c.post("/api/lead", json=d).get_json()
    assert r["status"] == "NURTURE"
    import email as em
    eml = em.message_from_bytes((tmp_path / "outbox" / "consumer" / f"{r['lead_id']}.eml").read_bytes()).get_payload(decode=True).decode()
    assert "Podrías calificar" in eml and "Nadie te va a llamar" in eml and "/optout?email=eva@example.com" in eml
    st = m.con.execute("SELECT status FROM deliveries WHERE method='consumer_summary'").fetchone()[0]
    assert st == "DRY_RUN"                                                             # test lead: never emailed
    assert 'value="eva@example.com"' in c.get("/optout?email=eva@example.com").get_data(as_text=True)


# ---------------------------------------------------------------- the product: consumer-submitted phone leads
def test_phone_validation_rules():
    from engine import phone as P
    assert P.validate("(305) 446-1234")["valid"] and P.validate("1-305-446-1234")["e164"] == "+13054461234"
    assert P.validate("305 446 1234")["area_region"] == "FL" and P.validate("212 446 1234")["area_region"] == "US_OTHER"
    for bad, why in [("305446123", "not_10_digits"), ("8004461234", "toll_free_not_personal"), ("1054461234", "invalid_nanp_format"),
                     ("3059111234", "n11_service_code"), ("3333333333", "junk_pattern"), ("3055550123", "fictional_555_01xx"), ("", "missing")]:
        r = P.validate(bad)
        assert not r["valid"] and r["reason"] == why, (bad, r)
    assert P.validate("3055550123", is_test=True)["valid"]                     # fictional numbers only on TEST traffic
    assert P.validate("3054461234")["line_type"] == "UNKNOWN"                  # no paid carrier lookup connected


def test_db_rejects_non_consumer_phone_and_edits(tmp_path):
    import sqlite3 as S
    m = make_app(tmp_path)
    try:
        m.con.execute("INSERT INTO leads (lead_id, phone, phone_source) VALUES ('X1','3054461234','PUBLIC_RECORD')")
        raise AssertionError("public-record phone was accepted")
    except S.IntegrityError as e:
        assert "CONSUMER_SUBMITTED" in str(e)
    m.con.execute("INSERT INTO leads (lead_id, phone, phone_e164, phone_source) VALUES ('X2','3054461234','+13054461234','CONSUMER_SUBMITTED')")
    try:
        m.con.execute("UPDATE leads SET phone='3054469999' WHERE lead_id='X2'")
        raise AssertionError("phone was editable")
    except S.IntegrityError as e:
        assert "immutable" in str(e)
    m.con.rollback()


def test_intel_layer_never_reads_public_contact_fields():
    import re as R
    src = (Path(__file__).resolve().parents[1] / "engine" / "intel.py").read_text()
    assert not R.search(r"phones_all|emails_all|ch_email|ch_phone|SELECT[^\"]*\bphone\b|SELECT[^\"]*\bemail\b", src)


def _health(c, **kw):
    d = dict(BASE, campaign="oep-2027-health", age_range="35-44", health_timeframe="asap", urgency="HIGH", income_band="150-250",
             household_size=1, need_path="self_employed", lang="en", experiment_id="X001", link_code="x001", utm_source="tiktok",
             page_url="http://localhost:8000/check?a=self_employed&x=X001", flow="check", tool_result={"headline": "You may qualify"})
    d.pop("mortgage_balance"); d.update(kw)
    return c.post("/api/lead", json=d).get_json()


def test_phone_lead_record_payload_and_provenance(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    r = _health(c, phone="(305) 446-1234", email="", consent_checked=True)        # email optional on a phone lead
    assert r["status"] == "QUALIFIED" and r["routed"] == "ROUTED", r
    l = m.con.execute("SELECT * FROM leads").fetchone()
    assert l["lead_type"] == "PHONE_LEAD" and l["phone_e164"] == "+13054461234" and l["phone_source"] == "CONSUMER_SUBMITTED"
    assert l["phone_valid"] == 1 and l["phone_area_region"] == "FL" and l["consent_version"] and l["consent_ts"] and l["tracking_id"] == "x001"
    pay = json.loads(next((tmp_path / "outbox").glob("*.json")).read_text())
    assert pay["phone"]["source"] == "CONSUMER_SUBMITTED" and pay["phone"]["number_e164"] == "+13054461234"
    assert pay["provenance"]["tracking_id"] == "x001" and pay["provenance"]["consent_version"] and "8am-8pm" in pay["contact_rules"]
    desk = c.get("/agent/demo-health-token").get_data(as_text=True)
    assert "(305) 446-1234" in desk and "consumer-submitted" in desk


def test_email_only_inquiry_keeps_no_phone_and_is_never_sold(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    r = _health(c, phone="3054461234", email="eva@example.com", consent_checked=False, email_consent_checked=True)
    assert r["status"] == "NURTURE" and r["routed"] == "NOT_ROUTABLE"
    l = m.con.execute("SELECT * FROM leads").fetchone()
    assert l["lead_type"] == "EMAIL_ONLY_INQUIRY" and not l["phone"] and not l["phone_e164"]   # typed without consent -> not kept
    assert m.route.route(m.con, m.route.load_buyers(m.CFG), dict(l, qual_status="QUALIFIED"))["routing_status"] == "NOT_ROUTABLE"


def test_phone_check_endpoint_stores_nothing_and_events_are_scrubbed(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    assert c.post("/api/phone-check", json={"phone": "8004461234"}).get_json()["reason"] == "toll_free_not_personal"
    ok = c.post("/api/phone-check", json={"phone": "3054461234"}).get_json()
    assert ok["valid"] and ok["pretty"] == "(305) 446-1234"
    assert m.con.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0
    c.post("/api/event", json={"visitor_id": "v9", "event": "contact_submit", "step": "phone 3054461234", "data": {"phone": "3054461234", "ok": 1}})
    e = m.con.execute("SELECT step, data FROM events WHERE visitor_id='v9'").fetchone()
    assert "3054461234" not in (e["step"] + e["data"]) and "phone" not in json.loads(e["data"])


def test_funnel_counts_each_stage(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    for vid, steps in [("a", ["page_view"]), ("b", ["page_view", "tool_start", "tool_complete", ("contact_submit", "phone_invalid:not_10_digits")]),
                       ("c", ["page_view", "tool_start", "tool_complete", ("contact_submit", "phone_valid")])]:
        for s in steps:
            ev, st = (s if isinstance(s, tuple) else (s, None))
            c.post("/api/event", json={"visitor_id": vid, "event": ev, "step": st, "is_test": 1, "utm_source": "tiktok", "campaign": "oep-2027-health"})
    _health(c, phone="3054461234", email="", consent_checked=True, visitor_id="c")
    lid = m.con.execute("SELECT lead_id FROM leads").fetchone()[0]
    c.post("/agent/demo-health-token", data={"lead_id": lid, "status": "SOLD"})
    row = m.funnel.by(m.con, "source", "only")
    t = next(r for r in row if r["k"] == "tiktok")
    assert (t["visits"], t["form_starts"], t["check_completions"], t["phones_submitted"], t["valid_phones"]) == (3, 2, 2, 2, 1)
    assert (t["consented_phone_leads"], t["qualified"], t["delivered"], t["contacted"], t["sales"]) == (1, 1, 1, 1, 1)
    assert t["revenue_per_lead"] and m.funnel.by(m.con, "source", "real") == []
    assert c.get("/admin/funnel?token=t&test=only&dim=source").status_code == 200


def test_concurrent_writes_are_not_lost(tmp_path):
    import threading
    m = make_app(tmp_path)
    errs = []

    def worker(n):
        c = m.app.test_client()
        for i in range(25):
            r = c.post("/api/event", json={"visitor_id": f"t{n}-{i}", "event": "page_view", "is_test": 1})
            if r.status_code != 200:
                errs.append(r.status_code)
    ts = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not errs and m.con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 200


# ---------------------------------------------------------------- acquisition experiments
def test_go_honors_landing_page_placement_tags_and_phone_variant(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    m.lab.create(m.con, {"experiment_id": "X900", "name": "t", "angle": "es_self_employed", "source": "instagram", "medium": "organic",
                         "landing_page": "/aprende/seguro-medico-trabajador-1099", "phone_variant": "B"})
    loc = c.get("/go/x900?c=reel").headers["Location"]
    assert loc.startswith("/aprende/seguro-medico-trabajador-1099?") and "x=X900" in loc and "pv=B" in loc and "utm_content=reel" in loc
    loc = c.get("/go/x015?a=es_check&c=es-adgroup").headers["Location"]
    assert "a=es_check" in loc and "x=X015" in loc and "utm_content=es-adgroup" in loc
    e = m.con.execute("SELECT * FROM experiments WHERE experiment_id='X013'").fetchone()
    assert e["tracking_id"] == "x013" and e["audience"] and e["demand_trigger"] and e["offer_cta"] and e["content_concept"]


def test_qa_cookie_marks_traffic_test_and_skips_clicks(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    c.get("/qa?on=1")
    before = m.con.execute("SELECT clicks FROM links WHERE code='x013'").fetchone()[0]
    c.get("/go/x013")
    assert m.con.execute("SELECT clicks FROM links WHERE code='x013'").fetchone()[0] == before
    c.post("/api/event", json={"visitor_id": "q1", "event": "page_view"})
    assert m.con.execute("SELECT is_test FROM events WHERE visitor_id='q1'").fetchone()[0] == 1
    c.post("/api/event", json={"visitor_id": "q2", "event": "page_view", "page_url": "http://x.com/admin/lab?token=t"})
    assert m.con.execute("SELECT COUNT(*) FROM events WHERE visitor_id='q2'").fetchone()[0] == 0   # admin screens aren't traffic


def test_returning_visitor_keeps_experiment_and_audience_status(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    c.post("/api/event", json={"visitor_id": "rv", "event": "page_view", "experiment_id": "X014", "utm_source": "tiktok", "link_code": "x014",
                               "entry_page": "/check", "phone_variant": "A", "is_test": 1})
    r = _health(c, phone="3054461234", email="", consent_checked=True, visitor_id="rv", experiment_id=None, link_code=None, utm_source=None)
    l = m.con.execute("SELECT experiment_id, utm_source, tracking_id, entry_page, phone_variant FROM leads").fetchone()
    assert tuple(l) == ("X014", "tiktok", "x014", "/check", "A"), tuple(l)
    st = {s["segment"]: s for s in m.lab.audience_status(m.con, m.PLAYBOOK, m.SIGNAL_COUNTS)}
    assert st["Spanish-speaking self-employed (Miami-Dade)"]["evidence"].startswith("HYPOTHESIS")   # test leads never count as evidence
    assert c.get("/admin/audiences?token=t").status_code == 200 and c.get("/admin/lab/X014?token=t&test=only").status_code == 200


# ---------------------------------------------------------------- wave 2: opportunities, layoffs, OEP status, page-owned experiments
def test_opportunities_rank_and_status_rules(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    rows = m.opp_mod.evaluate(m.con, m.CFG)
    assert len(rows) >= 15 and sum(1 for o in rows if o.get("hidden")) >= 5
    assert rows[0]["id"] == "OPP-01" and all(o["status_now"] != "PROVEN" for o in rows)        # research alone never proves anything
    for o in rows:
        assert all(k in o for k in ("who", "why_now", "trigger", "evidence", "distribution", "content", "cta", "check_angle", "competition", "different"))
    try:
        m.opp_mod.set_status(m.con, "OPP-01", "PROVEN"); raise AssertionError("manual PROVEN allowed")
    except ValueError:
        pass
    assert c.post("/admin/opportunities?token=t", data={"opp_id": "OPP-10", "status": "DISPROVEN"}).status_code == 400   # needs a result
    c.post("/admin/opportunities?token=t", data={"opp_id": "OPP-10", "status": "DISPROVEN", "result": "no real phone leads after read point"})
    assert {o["id"]: o for o in m.opp_mod.evaluate(m.con, m.CFG)}["OPP-10"]["status_now"] == "DISPROVEN"
    assert c.get("/admin/opportunities?token=t&f=hidden").status_code == 200


def test_layoff_pages_window_and_drafts(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    hub = c.get("/layoffs").get_data(as_text=True)
    assert "Magic Leap" in hub and "AssistRX" in hub
    page = c.get("/layoffs/assistrx-orlando-2026?test=1&today=2026-12-01").get_data(as_text=True)
    assert "window is likely open" in page and "January 29, 2027" in page and "not affiliated with AssistRX" in page
    old = c.get("/layoffs/spirit-airlines-florida-2026?test=1&today=2026-10-05").get_data(as_text=True)
    assert "has likely passed" in old and "Open Enrollment" in old
    import json as J
    d = J.loads((m.CFG / "layoffs.json").read_text())
    d["events"].append({"slug": "draft-co-2026", "company": "Draft Co", "city": "Tampa", "employees": 99, "effective_date": "2026-11-01",
                        "detail": "x", "source": "https://example.com", "source_label": "x", "status": "draft"})
    (tmp_path / "cfg").mkdir(); import shutil
    for f in m.CFG.iterdir():
        shutil.copy(f, tmp_path / "cfg" / f.name)
    (tmp_path / "cfg" / "layoffs.json").write_text(J.dumps(d)); m.CFG = tmp_path / "cfg"
    assert c.get("/layoffs/draft-co-2026").status_code == 404 and c.get("/layoffs/draft-co-2026?token=t").status_code == 200


def test_oep_status_page_changes_with_date(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    assert "It opens November 1" in c.get("/open-enrollment-2027?test=1&today=2026-10-05").get_data(as_text=True)
    assert "Enroll by December 15" in c.get("/open-enrollment-2027?test=1&today=2026-11-20").get_data(as_text=True)
    late = c.get("/inscripcion-abierta-2027?test=1&today=2026-12-20").get_data(as_text=True)
    assert "Sigue abierta hasta el 15 de enero" in late and "26</b>" in late
    assert "ended January 15" in c.get("/open-enrollment-2027?test=1&today=2027-02-01").get_data(as_text=True)
    assert c.get("/inscripcion-abierta-2027?today=2026-12-20").data == c.get("/inscripcion-abierta-2027").data   # real traffic cannot spoof the date


def test_untagged_arrival_on_owned_page_credits_experiment(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    c.post("/api/event", json={"visitor_id": "seo1", "event": "page_view", "entry_page": "/aprende/molina-se-va-de-florida-2027",
                               "utm_source": "google", "utm_medium": "organic", "is_test": 1})
    c.post("/api/event", json={"visitor_id": "seo2", "event": "page_view", "entry_page": "/layoffs/magic-leap-plantation-2026", "is_test": 1})
    ev = dict(m.con.execute("SELECT visitor_id, experiment_id FROM events WHERE visitor_id IN ('seo1','seo2')").fetchall())
    assert ev == {"seo1": "X016", "seo2": "X018"}
    for p in ["/aprende/molina-se-va-de-florida-2027", "/learn/florida-blue-2027-renewal-increase", "/learn/laid-off-florida-health-insurance"]:
        html = c.get(p).get_data(as_text=True)
        rv = next(p_["reviewed"] for p_ in m.CONTENT["pages"] if p_.get("kind") == "wave2")
        assert "dateModified" in html and (f"Reviewed {rv}" in html or f"Revisado {rv}" in html)


def test_warn_watcher_drafts_new_employers(tmp_path):
    import importlib.util, json as J, shutil
    from datetime import date as D
    spec = importlib.util.spec_from_file_location("ww", Path(__file__).resolve().parents[1] / "tools" / "warn_watch.py")
    ww = importlib.util.module_from_spec(spec); spec.loader.exec_module(ww)
    cfg = tmp_path / "layoffs.json"; shutil.copy(Path(__file__).resolve().parents[1] / "config" / "layoffs.json", cfg)
    csvp = tmp_path / "warn.csv"
    csvp.write_text("Company Name,City,County,Employees Affected,Notice Date,Layoff Date\nACME LOGISTICS,MIAMI,Miami-Dade,120,09/20/2026,11/15/2026\n"
                    "Tiny Shop,Tampa,Hillsborough,8,09/20/2026,11/15/2026\nOld Co,Orlando,Orange,300,01/01/2025,02/01/2025\n")
    added = ww.run(csvp, today=D(2026, 10, 5), cfg=cfg)
    assert [a["slug"] for a in added] == ["acme-logistics-miami-2026"] and added[0]["status"] == "draft"
    assert ww.run(csvp, today=D(2026, 10, 5), cfg=cfg) == []                                    # idempotent


# ---------------------------------------------------------------- wave 3: operator decisions + January window
def test_decisions_kill_redirect_merge_and_january_window(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    st = {r["experiment_id"]: dict(r) for r in m.con.execute("SELECT * FROM experiments")}
    assert st["X014"]["status"] == "KILLED" and st["X015"]["status"] == "KILLED" and st["X020"]["status"] == "KILLED"
    assert "MERGED into X021" in st["X020"]["learning"] and st["X021"]["start_date"] == "2026-12-16"
    assert st["X013"]["landing_page"] == "/learn/florida-health-plan-changes-2027" and st["X013"]["angle"] == "plan_change"
    assert c.get("/go/x013?c=story").headers["Location"].startswith("/learn/florida-health-plan-changes-2027?")   # redirected link
    assert c.get("/go/x014").status_code == 302                                    # killed links still resolve (old shares)
    assert "/learn/cobra-ending-18-months-florida" in st["X018"]["pages"]
    assert m.lab.apply_decision(m.con, {"experiment_id": "X014", "status": "KILLED"}) is False   # idempotent
    # page ownership: the OEP status page now credits X021 (X020 is killed), COBRA page credits X018
    for page, x in (("/inscripcion-abierta-2027", "X021"), ("/aprende/me-cambiaron-de-seguro-enero-2027", "X021"),
                    ("/learn/cobra-ending-18-months-florida", "X018")):
        c.post("/api/event", json={"visitor_id": "v" + x + page[-4:], "event": "page_view", "page_url": "http://x.com" + page, "entry_page": page, "is_test": 1})
        assert m.con.execute("SELECT experiment_id FROM events WHERE visitor_id=?", ("v" + x + page[-4:],)).fetchone()[0] == x, page
    for path in ("/learn/moved-to-new-health-plan-january-2027", "/aprende/me-cambiaron-de-seguro-enero-2027",
                 "/learn/cobra-ending-18-months-florida", "/aprende/cobra-se-termina-18-meses-florida"):
        r = c.get(path); assert r.status_code == 200, path
    assert "different insurance company" in c.get("/learn/molina-leaving-florida-marketplace-2027").get_data(as_text=True)


# ---------------------------------------------------------------- one central system: engines, channels, categories, acceptance, spend
def test_engines_channels_acceptance_spend_and_decisions(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    # visits from four engines (TEST traffic)
    for vid, src, med, partner in (("o1", "reddit", "community", None), ("o2", "google", "organic", None), ("p1", "facebook", "paid_social", None),
                                   ("q1", "flyer", "qr", "cs-south-florida"), ("a1", "chatgpt", "ai_answer", None)):
        c.post("/api/event", json={"visitor_id": vid, "event": "page_view", "utm_source": src, "utm_medium": med, "partner_id": partner,
                                   "entry_page": "/learn/florida-health-plan-changes-2027", "is_test": 1})
    rows = {r["k"]: r for r in m.funnel.by(m.con, "channel", "only")}
    assert {"social_community", "search", "paid_meta", "partner", "ai_answer"} <= set(rows), sorted(rows)
    eng = {r["k"]: r["visits"] for r in m.funnel.by(m.con, "engine", "only")}
    assert eng == {"ORGANIC": 3, "PAID": 1, "PARTNER": 1}, eng
    assert {r["k"] for r in m.funnel.by(m.con, "category", "only")} == {"health"}
    # leads: one health, one life; agent returns the health one -> not accepted, no revenue
    r = _health(c, phone="(305) 446-1234", email="", consent_checked=True, utm_source="reddit", utm_medium="community")
    lid = m.con.execute("SELECT lead_id FROM leads").fetchone()[0]
    assert m.con.execute("SELECT routing_status FROM leads").fetchone()[0] == "DELIVERED"
    c.post("/agent/demo-health-token", data={"lead_id": lid, "status": "RETURNED", "note": "wrong number"})
    row = {x["k"]: x for x in m.funnel.by(m.con, "channel", "only")}["social_community"]
    assert (row["delivered"], row["accepted"], row["returned"], row["revenue"]) == (1, 0, 1, 0), row
    _health(c, phone="(305) 446-1234", email="", consent_checked=True, last_name="Again")          # same phone again -> duplicate
    assert {x["k"]: x for x in m.funnel.by(m.con, "all", "only")}["all"]["duplicates"] == 1
    lf = c.post("/api/lead", json=dict(BASE, campaign="mortgage-protection", consent_checked=True, phone="3054467777"))
    assert lf.status_code == 200
    cats = {x["k"]: x["consented_phone_leads"] for x in m.funnel.by(m.con, "category", "only")}
    assert cats.get("life") == 1 and cats.get("health") == 2, cats
    # spend import (Meta export format) -> cost per lead on the REAL side, experiment cost updated
    csvp = tmp_path / "meta.csv"
    csvp.write_text("Day,Campaign name,Amount spent (USD)\n2026-10-20,X022 | ES carrier exit | Miami-Dade,40.50\n2026-10-21,X022 | ES carrier exit | Miami-Dade,35.00\n"
                    "2026-10-21,Life | mortgage protection test,20\n")
    from tools import import_spend, engine_report
    assert import_spend.run(str(csvp), "paid_meta", os.environ["LEADENGINE_DB"]) == {"rows": 3, "amount": 95.5}
    assert import_spend.run(str(csvp), "paid_meta", os.environ["LEADENGINE_DB"])["rows"] == 3            # re-import doesn't double count
    real = {x["k"]: x for x in m.funnel.by(m.con, "engine", "real")}
    assert real["PAID"]["spend"] == 95.5
    assert {x["k"]: x["spend"] for x in m.funnel.by(m.con, "category", "real")} == {"health": 75.5, "life": 20.0}
    assert engine_report.decide({"consented_phone_leads": 0, "accepted": 0, "delivered": 0, "spend": 80, "visits": 400}, True)[0] == "KILL"
    assert engine_report.decide({"consented_phone_leads": 5, "accepted": 4, "delivered": 5, "spend": 100, "visits": 400}, True)[0] == "SCALE"
    assert engine_report.decide({"consented_phone_leads": 0, "accepted": 0, "delivered": 0, "spend": 0, "visits": 350}, False)[0] == "FIX OFFER"
    assert engine_report.decide({"consented_phone_leads": 6, "accepted": 2, "delivered": 6, "spend": 0, "visits": 350}, False)[0] == "FIX QUALITY"
    assert "By engine" in engine_report.report(m.con)


def test_layoff_signal_feeds_life_category_too(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    h = c.get("/learn/life-insurance-after-layoff-florida").get_data(as_text=True)
    assert "31 days" in h and "/c/work-life-insurance-ending?via=" in h and "627.566" in h
    assert "/c/seguro-de-vida-del-trabajo?via=" in c.get("/aprende/seguro-de-vida-despues-de-despido-florida").get_data(as_text=True)
    assert c.get("/c/work-life-insurance-ending").status_code == 200 and c.get("/c/seguro-de-vida-del-trabajo").status_code == 200
    ev = next(e for e in json.loads((m.CFG / "layoffs.json").read_text())["events"] if e.get("status") == "ready")
    page = c.get(f"/layoffs/{ev['slug']}").get_data(as_text=True)
    assert "/c/work-life-insurance-ending?utm_content=" in page and "Check my 60-day options" in page
    pv = c.get("/api/route-preview?test=1&campaign=work-life-ends&state=FL&age_range=35-44").get_json()
    assert pv["available"] is True                                  # a life buyer is named in the consent
    assert m.con.execute("SELECT 1 FROM experiments WHERE experiment_id='X023' AND status='READY'").fetchone()


def test_engine1_placements_and_engine2_ads_are_tracked_and_compliant(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    D = json.loads((m.CFG / "distribution.json").read_text())["placements"]
    for p in D:
        if p.get("kind") == "setup":
            continue
        loc = c.get(f"/go/{p['code']}?test=1").headers["Location"]
        assert loc.startswith(p["landing_page"]) and f"utm_source={p['utm_source']}" in loc and f"utm_content={p['id']}" in loc, (p["id"], loc)
    # a Reddit placement and a Meta ad land in the right channel/engine buckets
    for vid, link in (("rv", "/go/rdhealthinsexit"), ("mv", None)):
        if link:
            q = dict(x.split("=") for x in c.get(link + "?test=1").headers["Location"].split("?")[1].split("&"))
            c.post("/api/event", json={"visitor_id": vid, "event": "page_view", "utm_source": q["utm_source"], "utm_medium": q["utm_medium"], "is_test": 1})
    meta = json.loads((m.CFG / "meta_campaigns.json").read_text())
    url = meta["campaigns"][0]["ads"][0]["url"]
    q = dict(x.split("=") for x in url.split("?")[1].split("&"))
    c.post("/api/event", json={"visitor_id": "mv", "event": "page_view", "utm_source": q["utm_source"], "utm_medium": q["utm_medium"], "experiment_id": q["x"], "is_test": 1})
    ch = {r["k"]: r["visits"] for r in m.funnel.by(m.con, "channel", "only")}
    assert ch.get("social_community") == 1 and ch.get("paid_meta") == 1, ch
    assert m.con.execute("SELECT status FROM experiments WHERE experiment_id='X022'").fetchone()[0] == "PLANNED"   # nothing runs without approval
    from tools import ad_lint
    assert not [r for r in ad_lint.lint() if r[0] == "ERROR"]
    assert any("laid off" in rx for rx in ad_lint.ATTR)


def test_organic_queue_tracks_effort_and_per_item_results(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    page = c.get("/admin/organic?token=t").get_data(as_text=True)
    assert "hr-saddle-creek-plant-city-2026" in page and "rd-healthins-exit" in page and "fbp-cuba-en-miami" in page
    rows = {r["id"]: r for r in m.ops_mod.board(m.con, m.CFG, "", 1)}
    assert rows["hr-nearly-natural-hialeah-2026"]["status"] == "VERIFY_FACTS_FIRST"      # single-source facts are gated
    assert rows["hr-saddle-creek-plant-city-2026"]["status"] == "NEEDS_APPROVAL_TO_SEND" and not rows["hr-saddle-creek-plant-city-2026"]["live"]
    c.post("/admin/organic?token=t", data={"item_id": "hr-saddle-creek-plant-city-2026", "action": "SENT", "url": "mailto:hr", "minutes": "12", "cost": "0"})
    link = rows["hr-saddle-creek-plant-city-2026"]["link"]
    q = dict(x.split("=") for x in link.split("?")[1].split("&"))
    c.post("/api/event", json={"visitor_id": "hrv", "event": "page_view", "utm_source": q["utm_source"], "utm_medium": q["utm_medium"],
                               "utm_content": q["utm_content"], "experiment_id": "X018", "is_test": 1})
    _health(c, phone="(305) 446-4321", email="", consent_checked=True, visitor_id="hrv", utm_source=q["utm_source"], utm_medium=q["utm_medium"],
            utm_content=q["utm_content"])
    r = {x["id"]: x for x in m.ops_mod.board(m.con, m.CFG, "", 1)}["hr-saddle-creek-plant-city-2026"]
    assert (r["status"], r["live"], r["minutes"], r["visits"], r["leads"]) == ("SENT", True, 12.0, 1, 1), r
    assert {x["k"]: x["consented_phone_leads"] for x in m.funnel.by(m.con, "engine", "only")}.get("PARTNER") == 1
    real = {x["id"]: x for x in m.ops_mod.board(m.con, m.CFG, "", 0)}["hr-saddle-creek-plant-city-2026"]
    assert real["leads"] == 0                                            # TEST submissions never count as real leads
    from tools import engine_report
    rep = engine_report.report(m.con)
    assert "Paid gate" in rep and "NOT YET" in rep and "hr-saddle-creek-plant-city-2026 | SENT | 12.0 min" in rep
    assert c.get("/admin/organic.csv?token=t").status_code == 200


def test_printables_public_and_outreach_links_tracked(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    r = c.get("/print/handout_job_es.pdf")
    assert r.status_code == 200 and r.mimetype == "application/pdf" and c.get("/print/secret.pdf").status_code == 404
    items = json.loads((m.CFG / "outreach.json").read_text())["items"]
    for i in items:
        loc = i["link"]
        assert "utm_content=" + i["id"] in loc and "utm_medium=partner" in loc, i["id"]
        path = loc.split("?")[0]
        assert c.get(path).status_code == 200, path


def test_military_audience_wired(tmp_path):
    """X026: separating service members -> guides (life + health), life campaign counted as LIFE, tracked partner links, printable."""
    m = make_app(tmp_path); c = m.app.test_client()
    for u in ("/learn/leaving-military-life-insurance-florida", "/learn/leaving-military-health-insurance-florida", "/c/leaving-military-life-insurance"):
        assert c.get(u).status_code == 200, u
    r = c.get("/go/ptmilitaryen")
    assert r.status_code == 302 and "x=X026" in r.headers["Location"] and "utm_source=military_transition" in r.headers["Location"]
    assert c.get("/print/handout_separating_en.pdf").data[:4] == b"%PDF"
    life = json.loads((m.CFG / "engines.json").read_text())["category"]["life_campaigns"]
    assert "military-life-ends" in life
    items = {i["id"]: i for i in json.loads((m.CFG / "outreach.json").read_text())["items"]}
    mil = [i for k, i in items.items() if k.startswith("mil-")]
    assert len(mil) == 7 and all(i["experiment_id"] == "X026" and i["status"] == "NEEDS_APPROVAL_TO_SEND" for i in mil)
    page = c.get("/learn/leaving-military-life-insurance-florida").get_data(as_text=True)
    assert "120 days" in page and "240 days" in page and "not affiliated" in page.lower()
    assert "$" not in "".join(next(p for p in m.CONTENT["pages"] if p["slug"] == "leaving-military-health-insurance-florida")["a"])  # guardrail: no prices


def test_operator_sweep_attempts_the_whole_queue_now(tmp_path):
    """POST /admin/sweep is the operator's explicit click: it attempts EVERY unmatched lead regardless of
    the after_hours window (the timed sweep would suppress a recent attempt). Same guards otherwise:
    one platform assignment, one delivery, one actual credit — audited either way."""
    m = make_app(tmp_path); c = m.app.test_client()
    from engine import route as rt
    b = dict(next(x for x in rt.load_buyers(m.CFG) if x["id"] == "demo-agency-a"))
    m.con.execute("""INSERT INTO leads (lead_id, created_at, is_test, campaign, product, state, zip, phone, phone_source,
        qual_status, lead_type, tier, score, routing_status, contact_scope, consent_ts)
        VALUES ('LSWP1', datetime('now'), 1, 'camp', ?, 'FL', '33101', '3055550110', 'CONSUMER_SUBMITTED',
        'QUALIFIED', 'PHONE_LEAD', 'A', 60, 'NO_BUYER', 'PHONE_AND_EMAIL', datetime('now'))""", (b["products"][0],))
    m.con.execute("""INSERT INTO consents (lead_id, ts, consent_version, consent_text, seller_named, checkbox_checked,
        scope, form_snapshot_sha256) VALUES ('LSWP1', datetime('now'), '1', 'consent text', 'platform', 1,
        'PHONE_AND_EMAIL', ?)""", ("b" * 64,))
    # a JUST-recorded attempt would suppress the timed sweep — the operator's click overrides it
    m.con.execute("INSERT INTO routing_decisions (ts, lead_id, decision, buyer_id, reason, is_test) VALUES (?,?,?,?,?,1)",
                  (m.now(), "LSWP1", "NO_BUYER", None, "operator re-offer: no buyer yet"))
    r = c.post("/admin/sweep?token=t", follow_redirects=True)
    assert r.status_code == 200 and "re-offered 1 unmatched lead(s), 0 still waiting" in r.get_data(as_text=True)
    row = m.con.execute("SELECT * FROM leads WHERE lead_id='LSWP1'").fetchone()
    assert row["routing_status"] == "DELIVERED" and row["buyer_id"] == b["id"]
    # a sweep with nothing to do still answers cleanly
    r = c.post("/admin/sweep?token=t", follow_redirects=True)
    assert "re-offered 0 unmatched lead(s), 0 still waiting" in r.get_data(as_text=True)
    assert c.post("/admin/sweep", follow_redirects=True).status_code == 403   # token required


def test_stuck_payment_resolution_never_credits(tmp_path):
    """An operator closes a >24h PENDING self-serve payment after checking with Stripe: the note is
    required and recorded, and the CLOSED row can never be turned into credits by a late webhook
     (fulfillment only moves PENDING rows). Nothing replaces the ledger — did-paid gets a top-up with a note."""
    m = make_app(tmp_path); c = m.app.test_client()
    m.con.execute("INSERT INTO buyer_payments (ts, buyer_id, package_id, amount_usd, provider, session_id, session_url, status, is_test, note) "
                  "VALUES ('2026-10-20T08:00:00','demo-agency-a','sm',100,'stripe','cs_test_1','https://checkout.example/stripe','PENDING',1,'awaiting webhook')")
    rid = m.con.execute("SELECT rowid FROM buyer_payments").fetchone()[0]
    page = c.get("/admin?token=t").get_data(as_text=True)
    assert "Payment stuck pending" in page and "Mark resolved" in page
    c.post(f"/admin/payments/{rid}/mark?token=t", data={})   # no note: refused, the row stays PENDING...
    r = c.post(f"/admin/payments/{rid}/mark?token=t", data={"note": "stripe dashboard: refunded"}, follow_redirects=True)
    assert "stuck payment closed" in r.get_data(as_text=True)
    row = m.con.execute("SELECT status, note FROM buyer_payments WHERE rowid=?", (rid,)).fetchone()
    assert row["status"] == "CLOSED" and "operator: stripe dashboard: refunded" in row["note"]
    # a late webhook for that session is a no-op, never credits
    from engine import payments as pmt
    res = pmt._fulfill(m.con, "evt_late", {"id": "cs_test_1"}, m.MARKETPLACE)
    assert res.get("state") == "duplicate", "a CLOSED row must late-credit nothing"
    assert m.con.execute("SELECT COUNT(*) FROM buyer_ledger WHERE kind='PACKAGE_PURCHASE'").fetchone()[0] == 0
    # closing again is refused
    r = c.post(f"/admin/payments/{rid}/mark?token=t", data={"note": "again"}, follow_redirects=True)
    assert "already CLOSED" in r.get_data(as_text=True)


def test_webhook_test_ping_staging_refusal(tmp_path):
    """The connectivity test ping contains no consumer data and, in staging, posts nothing — but the
    route itself is wired and validates the buyer's webhook configuration first."""
    m = make_app(tmp_path); c = m.app.test_client()
    r = c.post("/admin/buyer/demo-health-agent/test_webhook?token=t", follow_redirects=True)
    assert "is not on webhook delivery" in r.get_data(as_text=True)   # demo buyer: outbox method
    import shutil
    cfg = tmp_path / "cfg"; shutil.copytree(m.CFG, cfg); m.CFG = cfg   # stay on a temp copy, never the real config
    buyers = json.loads((m.CFG / "buyers.json").read_text(encoding="utf-8"))
    b = next(x for x in buyers if x["id"] == "demo-health-agent")
    b["delivery"] = {"method": "webhook", "webhook_url": "https://crm.example.test/hook", "webhook_secret": "s"}
    (m.CFG / "buyers.json").write_text(json.dumps(buyers), encoding="utf-8")
    r = c.post("/admin/buyer/demo-health-agent/test_webhook?token=t", follow_redirects=True)
    assert "nothing was posted" in r.get_data(as_text=True) and "LEADENGINE_LIVE_DELIVERY" in r.get_data(as_text=True)


def test_partner_admin_review_controls(tmp_path):
    """The admin partner-review page records the operator's review decision: status changes and the
    cobrand approval are stored on the partner row, and the review page renders the stored state."""
    m = make_app(tmp_path); c = m.app.test_client()
    c.post("/admin/partners?token=t", data={"display_name": "Coral Gables CPA Group", "type": "cpa", "language": "en"})
    pid = m.con.execute("SELECT partner_id FROM partners WHERE display_name='Coral Gables CPA Group'").fetchone()["partner_id"]
    r = c.post(f"/admin/partners/{pid}/status?token=t", data={"status": "CONTACTED - pitch sent", "cobrand_approved": "1"}, follow_redirects=True)
    assert r.status_code == 200
    row = m.con.execute("SELECT status, cobrand_approved FROM partners WHERE partner_id=?", (pid,)).fetchone()
    assert row["status"] == "CONTACTED - pitch sent" and row["cobrand_approved"] == 1
    page = c.get(f"/admin/partners?token=t").get_data(as_text=True)
    assert "CONTACTED - pitch sent" in page          # review state is visible on the admin page
    # a partner without a review decision still lists as a prospect
    assert page.count("PROSPECT") >= 1
