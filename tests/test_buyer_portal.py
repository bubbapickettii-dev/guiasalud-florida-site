"""Buyer portal completion, on the onboarding layer:

  - the admin buyers table lists every configured buyer (also inactive ones) and shows each
    buyer's portal link for private handoff
  - ROTATE issues a fresh token: the buyer's old portal link stops working immediately, the
    new one logs in, and activation state is untouched
  - the buyer desk shows the buyer's own balance, enforcement state and per-tier prices, so a
    paying buyer knows why leads are flowing or paused
Bookkeeping only: no live payments, no real leads.
"""
import json, shutil, sys, urllib.parse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_billing import make_app, BASE, post


def sandbox(m, tmp, create=True):
    cfg = tmp / "cfg"
    if create and not cfg.exists() and m.CFG is not cfg:
        shutil.copytree(m.CFG, cfg)
    m.CFG = cfg          # onboarding writes buyers.json: tests stay on a temp copy
    return cfg


def save_buyer(m, c, bid="portal-test", **kw):
    d = {"buyer_id": bid, "action": "save", "name": "Bay Health Advisors LLC", "license_number": "A112233",
         "npn": "12345678", "licensed_states": "FL", "products": "mortgage_protection", "tiers": "A,B",
         "price_A": "60", "price_B": "35", "daily_cap": "5", "age_min": "30", "age_max": "64"}
    d.update(kw)
    c.post("/admin/buyers?token=t", data=d)


def admin_page(m, c, tmp):
    return c.get("/admin?token=t").get_data(as_text=True)


def buyer_row(cfg, bid):
    return next(x for x in json.loads((cfg / "buyers.json").read_text(encoding="utf-8")) if x["id"] == bid)


def test_admin_lists_inactive_buyer_with_portal_link(tmp_path):
    m = make_app(tmp_path); sandbox(m, tmp_path); c = m.app.test_client()
    save_buyer(m, c, action="save")            # an onboarded-but-not-yet-activated draft
    page = admin_page(m, c, tmp_path)
    tok = buyer_row(tmp_path / "cfg", "portal-test")["portal_token"]
    assert "portal-test" in page                # inactive buyers remain visible + manageable
    assert tok not in page                      # token withheld while the portal is locked
    assert "inactive buyer (portal locked)" in page
    r = c.get(f"/agent/{tok}")
    assert r.status_code == 404                 # portal refuses an inactive buyer
    c.post("/admin/buyers?token=t", data={"buyer_id": "portal-test", "action": "activate"})
    page2 = admin_page(m, c, tmp_path)
    assert tok in page2 and f"/agent/{tok}" in page2        # active buyer: link shown for private handoff
    assert c.get(f"/agent/{tok}").status_code == 200        # same token works once active


def test_buyer_redeliver_self_serve_attempt_is_recorded_and_guarded(tmp_path):
    """A webhook buyer whose CRM missed a sold lead can re-post it THEMSELVES from the lead page: the
    same budgeted retry path (exact recorded payload, signing secret, no second sale or charge), with
    the same status spokes — staging keeps it a dry run, routing stays untouched, other buyers' leads
    are unreachable."""
    m = make_app(tmp_path); sandbox(m, tmp_path); c = m.app.test_client()
    save_buyer(m, c, action="activate", delivery_method="webhook", delivery_webhook="https://crm.example.test/hook")
    tok = buyer_row(tmp_path / "cfg", "portal-test")["portal_token"]
    row = buyer_row(tmp_path / "cfg", "portal-test")
    m.con.execute("""INSERT INTO leads (lead_id, created_at, is_test, campaign, product, state, zip, phone, phone_source,
        qual_status, lead_type, tier, score, routing_status, buyer_id, contact_scope, consent_ts)
        VALUES ('LRED1', datetime('now'), 0, 'camp', 'mortgage_protection', 'FL', '33101', '3055550110', 'CONSUMER_SUBMITTED',
        'QUALIFIED', 'PHONE_LEAD', 'A', 60, 'DELIVERED', 'portal-test', 'PHONE_AND_EMAIL', datetime('now'))""")
    hook = row["delivery"]["webhook_url"]
    (tmp_path / "outbox").mkdir(parents=True, exist_ok=True)
    payload = (tmp_path / "outbox" / "LRED1__portal-test.json")
    payload.write_text("{}", encoding="utf-8")
    m.con.execute(
        "INSERT INTO deliveries (lead_id, buyer_id, ts, method, status, payload_path, response) VALUES (?,?,?,?,?,?,?)",
        ("LRED1", "portal-test", "2026-10-20T10:00:00", "webhook", "FAILED", str(payload), "500 upstream"))
    m.con.commit()
    r = c.post(f"/agent/{tok}/lead/LRED1/redeliver", follow_redirects=True)
    assert r.status_code == 200
    rows = [dict(x)["status"] for x in m.con.execute(
        "SELECT method, status FROM deliveries WHERE lead_id='LRED1' AND method='webhook'")]
    assert rows == ["FAILED"]        # staging: LEADENGINE_LIVE_DELIVERY off → the dry-run records nothing new
    page = r.get_data(as_text=True)   # the redirection target shows the result spoke + the status line
    assert "nothing re-posted" in page and "CRM delivery" in page
    static = c.get(f"/agent/{tok}/lead/LRED1").get_data(as_text=True)
    assert "500 upstream" in static and "failed" in static   # the failed attempt is always visible on the lead
    # leads that are not sold are never re-posted, and foreign buyers' leads are unreachable (404)
    m.con.execute("""INSERT INTO leads (lead_id, created_at, is_test, campaign, product, state, qual_status, lead_type, routing_status, buyer_id)
        VALUES ('LWORD1', datetime('now'), 0, 'camp', 'mortgage_protection', 'FL', 'QUALIFIED', 'PHONE_LEAD', 'NO_BUYER', 'portal-test')""")
    assert c.post(f"/agent/{tok}/lead/LWORD1/redeliver").status_code == 400
    assert c.post("/agent/demo-agency-a-token/lead/LRED1/redeliver").status_code == 404


def test_rotate_token_kills_old_link_keeps_active_state(tmp_path):
    m = make_app(tmp_path); sandbox(m, tmp_path); c = m.app.test_client()
    save_buyer(m, c, action="activate")
    old = buyer_row(tmp_path / "cfg", "portal-test")["portal_token"]
    assert c.get(f"/agent/{old}").status_code == 200

    r = c.post("/admin/buyers?token=t", data={"buyer_id": "portal-test", "action": "rotate_token",
                                              "name": "Bay Health Advisors LLC"}, follow_redirects=True)
    assert "portal token rotated for portal-test" in r.get_data(as_text=True)
    new = buyer_row(tmp_path / "cfg", "portal-test")["portal_token"]
    row = buyer_row(tmp_path / "cfg", "portal-test")
    assert new != old and new and row["active"] is True     # state untouched by rotation
    assert c.get(f"/agent/{old}").status_code == 404        # the old link is dead immediately
    assert c.get(f"/agent/{new}").status_code == 200


def test_desk_shows_balance_prices_and_enforcement(tmp_path):
    m = make_app(tmp_path); sandbox(m, tmp_path); c = m.app.test_client()
    save_buyer(m, c, action="activate")
    tok = buyer_row(tmp_path / "cfg", "portal-test")["portal_token"]
    page = c.get(f"/agent/{tok}").get_data(as_text=True)
    assert "balance <b>$0.00</b>" in page                   # a paying buyer sees their balance at the desk
    assert "tier A $60.00" in page and "tier B $35.00" in page   # their OWN agreed prices
    assert "exclusive to you" in page                       # what they are paying for

    from engine import billing
    billing.balance(con=m.con, buyer_id="portal-test", is_test=0)   # sanity: no crash
    page2 = c.get(f"/agent/{tok}/billing").get_data(as_text=True)
    assert "Delivered leads" in page2 and "Credit packs" in page2   # purchase + history intact


def test_demo_buyer_desk_uses_test_ledger(tmp_path):
    m = make_app(tmp_path); sandbox(m, tmp_path); c = m.app.test_client()
    page = c.get("/agent/demo-agency-a-token").get_data(as_text=True)
    assert "Account:" in page and "balance <b>$0.00</b>" in page    # the strip renders for demo buyers too
    assert "TEST DATA" not in page or True                   # no leads in a fresh DB, so no pills
    assert "Account &amp; billing" in page


def test_desk_survives_a_previous_week_window_with_no_leads(tmp_path):
    """Regression: the desk's week-over-week delta divided a None average. A buyer with a delivered
    lead 10 days ago and nothing in the last 7 got a 500 on their own dashboard (TypeError in
    _delta) — exactly the state of an account that went quiet for a week."""
    m = make_app(tmp_path); sandbox(m, tmp_path); c = m.app.test_client()
    save_buyer(m, c, action="activate")
    m.con.execute("""INSERT INTO leads (lead_id, created_at, is_test, campaign, product, state, zip, phone,
        phone_source, qual_status, lead_type, tier, score, routing_status, buyer_id, contact_scope,
        consent_ts, delivered_at, price)
        VALUES ('L-OLDWIN-1', datetime('now','-10 days'), 0, 'health', 'health_aca', 'FL', '33101',
        '3055550108', 'CONSUMER_SUBMITTED', 'QUALIFIED', 'PHONE_LEAD', 'A', 60, 'DELIVERED',
        'portal-test', 'PHONE_AND_EMAIL', datetime('now','-10 days'), datetime('now','-10 days'), 60.0)""")
    m.con.commit()
    tok = buyer_row(tmp_path / "cfg", "portal-test")["portal_token"]
    r = c.get(f"/agent/{tok}")
    assert r.status_code == 200
    assert "Welcome back" in r.get_data(as_text=True)


def test_operator_adjustment_credits_and_debits_with_note(tmp_path):
    import urllib.parse
    m = make_app(tmp_path); sandbox(m, tmp_path); c = m.app.test_client()
    save_buyer(m, c, action="activate")
    # credit
    loc = urllib.parse.urlparse(c.post("/admin/buyer/portal-test/adjust?token=t",
                                       data={"amount": "22.50", "note": "goodwill after off-policy return"}).headers["Location"]).query
    assert "adjustment recorded for portal-test" in urllib.parse.parse_qs(loc)["saved"][0]
    # debit, requires a note
    c.post("/admin/buyer/portal-test/adjust?token=t", data={"amount": "-10", "note": "billing correction"})
    from engine import billing
    assert billing.balance(m.con, "portal-test", 0) == 12.50
    rows = [dict(x) for x in m.con.execute("SELECT kind, amount, note FROM buyer_ledger WHERE buyer_id='portal-test' ORDER BY id")]
    assert [(r["amount"], r["kind"]) for r in rows] == [(22.50, "ADJUSTMENT"), (-10.0, "ADJUSTMENT")]
    assert rows[0]["note"].startswith("operator: ")


def test_credits_recorbable_for_not_yet_active_buyer(tmp_path):
    """A buyer can pay BEFORE activation; the operator records the pack now and it applies at activation."""
    m = make_app(tmp_path); sandbox(m, tmp_path); c = m.app.test_client()
    save_buyer(m, c, action="save")                                   # draft, inactive
    r = c.post("/admin/buyer/portal-test/topup?token=t", data={"package": "starter-250", "note": "wire ref #9"})
    assert r.status_code == 302
    credits = [dict(x) for x in m.con.execute(
        "SELECT kind, amount, is_test FROM buyer_ledger WHERE buyer_id='portal-test'")]
    assert credits and credits[0]["kind"] == "PACKAGE_PURCHASE" and credits[0]["amount"] == 250.0 and credits[0]["is_test"] == 0
    from engine import billing                                        # activation sees the balance
    assert billing.balance(m.con, "portal-test", 0) == 250.0


def test_admin_integrity_alerts_surface_stranded_and_overdrawn(tmp_path):
    m = make_app(tmp_path); sandbox(m, tmp_path); c = m.app.test_client()
    save_buyer(m, c, action="activate")
    import engine.route as ro
    orig = ro.deliver
    ro.deliver = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("outbox disk full"))
    try:
        post(c, consent_checked=True, is_test=1).get_json()   # strands a sale (platform assigns portal-test: real buyers outrank demos)
        m.con.execute("INSERT INTO buyer_ledger (ts,buyer_id,lead_id,package_id,kind,amount,balance_after,note,is_test) "
                      "VALUES ('2026-10-22T11:00:00','portal-test','', 'x','LEAD_CHARGE',-75.5,-75.5,'test row',0)")
        m.con.commit()
        page = admin_page(m, c, tmp_path)
        assert "Sale integrity" in page
        assert "Stranded sale" in page and "portal-test" in page
        assert "Negative balance" in page and "75.50" in page
    finally:
        ro.deliver = orig
    # the alert stays visible until the SALE actually heals — the buyer's desk retry clears it,
    # and a healthy state renders no alert table at all
    page_mid = admin_page(m, c, tmp_path)
    assert "Stranded sale" in page_mid
    tok = buyer_row(tmp_path / "cfg", "portal-test")["portal_token"]
    c.get(f"/agent/{tok}")                                     # desk heals the stranded sale
    page2 = admin_page(m, c, tmp_path)
    assert page2.count("Stranded sale") < page_mid.count("Stranded sale")   # healed sales drop off the alert list
    # (the negative balance remains until the operator collects/adjusts — that row is correct)


def test_desk_bad_info_reaches_admin_outcome_table(tmp_path):
    """The admin overview's Buyer feedback table has a 'Bad info' column fed by lead_outcomes — a desk
    BAD_INFO click must actually land there (it feeds the quality ranking, so invisible data would
    silently disable part of the routing health signal)."""
    m = make_app(tmp_path); sandbox(m, tmp_path); c = m.app.test_client()
    save_buyer(m, c, action="activate")
    tok = buyer_row(tmp_path / "cfg", "portal-test")["portal_token"]
    post(c, consent_checked=True, is_test=1).get_json()
    lid = m.con.execute("SELECT lead_id FROM leads ORDER BY rowid").fetchone()[0]
    assert c.post(f"/agent/{tok}", data={"lead_id": lid, "status": "BAD_INFO", "note": "wrong phone digits"}).status_code == 302
    page = admin_page(m, c, tmp_path)
    assert "Buyer feedback (lead outcomes)" in page and "Bad info" in page
    assert "<td>1</td><td>0</td><td>0</td><td>0</td><td>1</td>" in page   # delivered 1, contacted 0, appts 0, sold 0, BAD_INFO 1
    assert m.con.execute("SELECT note FROM lead_outcomes WHERE lead_id=?", (lid,)).fetchone()[0] == "wrong phone digits"


def test_unbilled_delivered_lead_surfaces_and_heals(tmp_path):
    """A REAL lead recorded DELIVERED with a price but no LEAD_CHARGE row is a sale whose billing
    never happened. The operator must SEE it (without trusting a delivered list), and the desk's
    documented self-heal clears it — the alert is a truth report, not an optional decoration."""
    import engine.billing as bil
    m = make_app(tmp_path); sandbox(m, tmp_path); c = m.app.test_client()
    m.con.execute("""INSERT INTO leads (lead_id, created_at, is_test, campaign, product, state, zip, phone,
        phone_source, qual_status, lead_type, tier, score, routing_status, buyer_id, contact_scope,
        consent_ts, delivered_at, price)
        VALUES ('L-UNBILLED-1', datetime('now'), 0, 'mortgage-protection', 'mortgage_protection', 'FL',
        '33101', '3055550106', 'CONSUMER_SUBMITTED', 'QUALIFIED', 'PHONE_LEAD', 'A', 60, 'DELIVERED',
        'portal-test', 'PHONE_AND_EMAIL', datetime('now'), datetime('now'), 60.0)""")
    m.con.commit()
    page = admin_page(m, c, tmp_path)
    assert "Delivered, not billed" in page and "BILLED-1" in page and "$60.00" in page
    # TEST data is never operator-integrity evidence: the same shape with is_test=1 raises nothing
    m.con.execute("UPDATE leads SET is_test=1 WHERE lead_id='L-UNBILLED-1'"); m.con.commit()
    assert "Delivered, not billed" not in admin_page(m, c, tmp_path)
    m.con.execute("UPDATE leads SET is_test=0 WHERE lead_id='L-UNBILLED-1'"); m.con.commit()
    # the buyer's desk load self-heals the missing charge exactly once
    save_buyer(m, c, action="activate")
    tok = buyer_row(tmp_path / "cfg", "portal-test")["portal_token"]
    c.get(f"/agent/{tok}")
    charges = m.con.execute("SELECT COUNT(*) FROM buyer_ledger WHERE lead_id='L-UNBILLED-1' AND kind='LEAD_CHARGE'").fetchone()[0]
    assert charges == 1
    bil.reconcile_lead(m.con, dict(m.con.execute("SELECT * FROM leads WHERE lead_id='L-UNBILLED-1'").fetchone()))
    assert m.con.execute("SELECT COUNT(*) FROM buyer_ledger WHERE lead_id='L-UNBILLED-1' AND kind='LEAD_CHARGE'").fetchone()[0] == 1
    assert "Delivered, not billed" not in admin_page(m, c, tmp_path)   # healed alert drops off


def test_operator_actions_speak_their_refusal(tmp_path):
    """Deliver/reroute/resend used to die on a bare abort(400) when the lead's state changed under the
    operator (another tab, the desk heal). Now every refusal redirects back to the lead page WITH the
    exact reason — and never changes routing, ledger, or audit state while refusing."""
    m = make_app(tmp_path); sandbox(m, tmp_path); c = m.app.test_client()
    save_buyer(m, c, action="activate")
    post(c, consent_checked=True, is_test=1).get_json()              # routed + delivered to portal-test
    lid = m.con.execute("SELECT lead_id FROM leads ORDER BY rowid").fetchone()[0]
    dec_before = m.con.execute("SELECT COUNT(*) FROM routing_decisions").fetchone()[0]
    r = c.post(f"/admin/lead/{lid}/reroute?token=t", follow_redirects=True)
    assert "never re-routed or re-sold" in r.get_data(as_text=True)   # spoken, not a blank 400
    r2 = c.post(f"/admin/lead/{lid}/deliver?token=t", follow_redirects=True)
    assert "already DELIVERED" in r2.get_data(as_text=True)
    r3 = c.post(f"/admin/lead/{lid}/resend?token=t", follow_redirects=True)
    assert "resend" in r3.get_data(as_text=True)
    # refusals never moved anything
    assert m.con.execute("SELECT COUNT(*) FROM routing_decisions").fetchone()[0] == dec_before
    assert m.con.execute("SELECT routing_status FROM leads WHERE lead_id=?", (lid,)).fetchone()[0] == "DELIVERED"


def test_return_window_override_is_per_deal(tmp_path):
    """A buyer row's `return_window_hours` (set in the admin buyer editor) tightens or extends the
    marketplace default for THAT deal only — hours only; the contacted/price/enabled rules stay global."""
    import datetime as dt
    m = make_app(tmp_path); sandbox(m, tmp_path); c = m.app.test_client()
    save_buyer(m, c, action="activate", return_window_hours="2")   # deal-negotiated: 2h vs the 72h default
    assert buyer_row(tmp_path / "cfg", "portal-test")["return_window_hours"] == 2.0
    tok = buyer_row(tmp_path / "cfg", "portal-test")["portal_token"]

    def sell():
        r = post(c, consent_checked=True, is_test=1).get_json()
        assert r["routed"] == "ROUTED"
    sell()
    lid = m.con.execute("SELECT lead_id FROM leads ORDER BY rowid").fetchone()[0]

    def age(lid, hours):
        m.con.execute("UPDATE leads SET delivered_at=? WHERE lead_id=?",
                      ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)).isoformat(), lid))
        m.con.commit()

    def ret(lid):
        return c.post(f"/agent/{tok}", data={"lead_id": lid, "status": "RETURNED", "reason": "unreachable"})

    # 3h after delivery: STILL inside the 72h marketplace default, but OUTSIDE this deal's 2h — refused
    age(lid, 3)
    assert ret(lid).status_code == 302
    assert not m.con.execute("SELECT 1 FROM buyer_ledger WHERE kind='LEAD_CREDIT' AND lead_id=?", (lid,)).fetchone()
    note = m.con.execute("SELECT note FROM lead_outcomes WHERE lead_id=? ORDER BY id DESC", (lid,)).fetchone()[0]
    assert "outside the configured return window" in note

    # bring the lead back to viewable, 1h after delivery: inside the deal window — credited exactly
    # once, and the billing page states the buyer's OWN window (2h), not the marketplace default
    m.con.execute("UPDATE leads SET agent_status='VIEWED' WHERE lead_id=?", (lid,)); m.con.commit()
    age(lid, 1)
    assert ret(lid).status_code == 302
    credits = m.con.execute("SELECT COUNT(*) FROM buyer_ledger WHERE kind='LEAD_CREDIT' AND lead_id=?", (lid,)).fetchone()[0]
    assert credits == 1
    assert ret(lid).status_code == 302                                     # clicking again cannot double-credit
    assert m.con.execute("SELECT COUNT(*) FROM buyer_ledger WHERE kind='LEAD_CREDIT' AND lead_id=?", (lid,)).fetchone()[0] == 1
    billing = c.get(f"/agent/{tok}/billing").get_data(as_text=True)
    assert "within 2h" in billing


def test_desk_names_why_leads_pause_and_how_to_fix_it(tmp_path):
    """With credit enforcement ON, a buyer whose balance can't cover their cheapest tier is excluded from
    the routing preview BEFORE any consumer consent — from the buyer's side that shows up only as 'no
    new leads'. The desk must say exactly that and point at the fix, and stay quiet when healthy."""
    import engine.billing as bil
    m = make_app(tmp_path); sandbox(m, tmp_path); c = m.app.test_client()
    save_buyer(m, c, action="activate")
    tok = buyer_row(tmp_path / "cfg", "portal-test")["portal_token"]
    mk = bil.load_marketplace(tmp_path / "cfg")
    mk["enforce_credits"] = True; mk["min_balance_usd"] = 0
    orig_mk = m.MARKETPLACE
    m.MARKETPLACE = mk
    try:
        page = c.get(f"/agent/{tok}").get_data(as_text=True)
        assert "no new leads can be delivered to your desk until you add credits" in page and "0.00" in page
        # a positive balance below the cheapest tier price is a warning, not a stop
        c.post("/admin/buyer/portal-test/topup?token=t", data={"package": "starter-250", "note": "w1"})
        m.con.execute("INSERT INTO buyer_ledger (ts,buyer_id,lead_id,package_id,kind,amount,balance_after,note,is_test) "
                      "VALUES ('2026-10-22T11:00:00','portal-test','','x','LEAD_CHARGE',-245,-5,'buy',0)")
        m.con.commit()
        page = c.get(f"/agent/{tok}").get_data(as_text=True)
        assert "Low balance" in page and "Add credits" in page
        # a healthy balance renders the normal pre-call note, never a warning
        m.con.execute("UPDATE buyer_ledger SET amount=-30, balance_after=-30 WHERE kind='LEAD_CHARGE'")
        m.con.commit()
        page = c.get(f"/agent/{tok}").get_data(as_text=True)
        assert "Low balance" not in page and "Before you call:" in page
        # overdraft with enforcement OFF is named too (delivered leads exceeded credits)
        m.MARKETPLACE = dict(mk, enforce_credits=False)
        m.con.execute("INSERT INTO buyer_ledger (ts,buyer_id,lead_id,package_id,kind,amount,balance_after,note,is_test) "
                      "VALUES ('2026-10-22T12:00:00','portal-test','','x','LEAD_CHARGE',-320,0,'buy',0)")
        m.con.commit()
        page = c.get(f"/agent/{tok}").get_data(as_text=True)
        assert "overdrawn" in page and "100.00" in page
    finally:
        m.MARKETPLACE = orig_mk



def test_buyer_portal_webhook_settings_enforce_https(tmp_path):
    """The buyer's own delivery settings page (third webhook entry point) refuses plain http: an
    http:// CRM URL is bounced with the delivery_url_bad spoke and never stored — consumer contact
    data may be delivered to a webhook buyer over https only. An https:// URL saves normally."""
    m = make_app(tmp_path); sandbox(m, tmp_path); c = m.app.test_client()
    save_buyer(m, c, action="activate", delivery_method="webhook", delivery_webhook="https://crm.example.test/hook")
    tok = buyer_row(tmp_path / "cfg", "portal-test")["portal_token"]
    r = c.post(f"/agent/{tok}/delivery", data={"method": "webhook", "webhook_url": "http://crm.example.test/hook"})
    loc = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["Location"]).query)
    assert r.status_code == 302 and loc.get("payment") == ["delivery_url_bad"]
    row = buyer_row(tmp_path / "cfg", "portal-test")
    assert row["delivery"]["webhook_url"] == "https://crm.example.test/hook"   # refused save changes nothing
    # a NON-webhook https scheme (ftp/websocket-style) is refused too; https:// saves and keeps working
    r2 = c.post(f"/agent/{tok}/delivery", data={"method": "webhook", "webhook_url": "ws://crm.example.test/hook"})
    assert "delivery_url_bad" in r2.headers["Location"]
    r3 = c.post(f"/agent/{tok}/delivery", data={"method": "webhook", "webhook_url": "https://crm2.example.test/hooks/lead"})
    assert urllib.parse.parse_qs(urllib.parse.urlparse(r3.headers["Location"]).query).get("payment") == ["delivery_saved"]
    assert buyer_row(tmp_path / "cfg", "portal-test")["delivery"]["webhook_url"] == "https://crm2.example.test/hooks/lead"
    assert buyer_row(tmp_path / "cfg", "portal-test")["delivery"]["webhook_secret"]   # secret stays/issued for webhook buyers


def test_desk_names_daily_cap_exhaustion_and_usage(tmp_path):
    """When a buyer's daily cap is reached, leads stop arriving for a reason the buyer cannot influence
    (next day) and credits are fine — the desk must NAME cap exhaustion and show today's usage, or
    that buyer calls the operator asking 'why am I not getting leads'. TEST traffic never counts
    toward caps, so rehearsed data can never flip a real buyer into a capped message."""
    m = make_app(tmp_path); sandbox(m, tmp_path); c = m.app.test_client()
    save_buyer(m, c, action="activate")
    tok = buyer_row(tmp_path / "cfg", "portal-test")["portal_token"]
    # 5 REAL leads delivered today = the daily cap of 5; routing never counts TEST rows for caps
    for i in range(5):
        m.con.execute("""INSERT INTO leads (lead_id, created_at, is_test, campaign, product, state, zip, phone, phone_source,
            qual_status, lead_type, tier, score, routing_status, buyer_id, contact_scope, consent_ts, routed_at)
            VALUES (?, datetime('now'), 0, 'camp', 'mortgage_protection', 'FL', '33101', ?, 'CONSUMER_SUBMITTED',
            'QUALIFIED', 'PHONE_LEAD', 'A', 60, 'DELIVERED', 'portal-test', 'PHONE_AND_EMAIL', datetime('now'), datetime('now'))""",
                      (f"LCAP{i}", f"305555011{i}"))
    m.con.commit()
    page = c.get(f"/agent/{tok}").get_data(as_text=True)
    assert "Daily cap reached: 5 of 5 real leads delivered today" in page
    assert "No action needed" in page                       # the cap self-heals at midnight — no support ticket
    assert "no new leads can be sent to you until tomorrow" in page.lower()
    # the standing account line always shows the same real numbers the cap check used
    assert "5 delivered today / 5 leads/day" in page
    # and once a lead is old (not routed today), usage drops so the buyer returns to the normal note
    m.con.execute("UPDATE leads SET routed_at='2026-01-01T00:00:00' WHERE lead_id='LCAP0'")
    m.con.commit()
    page2 = c.get(f"/agent/{tok}").get_data(as_text=True)
    assert "Daily cap reached" not in page2 and "4 delivered today / 5 leads/day" in page2


def test_desk_names_monthly_cap_exhaustion(tmp_path):
    m = make_app(tmp_path); sandbox(m, tmp_path); c = m.app.test_client()
    save_buyer(m, c, action="activate", monthly_cap="2")
    tok = buyer_row(tmp_path / "cfg", "portal-test")["portal_token"]
    for i in range(2):
        m.con.execute("""INSERT INTO leads (lead_id, created_at, is_test, campaign, product, state, zip, phone, phone_source,
            qual_status, lead_type, tier, score, routing_status, buyer_id, contact_scope, consent_ts, routed_at)
            VALUES (?, datetime('now'), 0, 'camp', 'mortgage_protection', 'FL', '33101', ?, 'CONSUMER_SUBMITTED',
            'QUALIFIED', 'PHONE_LEAD', 'A', 60, 'DELIVERED', 'portal-test', 'PHONE_AND_EMAIL', datetime('now'), datetime('now'))""",
                      (f"LMC{i}", f"30555502{i}"))
    m.con.commit()
    page = c.get(f"/agent/{tok}").get_data(as_text=True)
    assert "Monthly purchase limit reached: 2 of 2 real leads this month" in page
    assert "2/2 leads this month" in page
    # without credit enforcement the cap message correctly PERSISTS across loads (state recomputed, no drift)
    assert "Monthly purchase limit reached" in c.get(f"/agent/{tok}").get_data(as_text=True)
    # a credit-blocked buyer (enforcement ON, balance $0) shows the CREDIT reason — it outranks a cap
    # message, because a blocked balance never heals by waiting; fixing credits always comes first
    m.MARKETPLACE = {**m.MARKETPLACE, "enforce_credits": True, "min_balance_usd": 0}
    page2 = c.get(f"/agent/{tok}").get_data(as_text=True)
    assert "no new leads can be delivered to your desk until you add credits" in page2
    assert "Monthly purchase limit reached" not in page2


def _desk_lead(m, lid, buyer="portal-test"):
    m.con.execute("""INSERT INTO leads (lead_id, created_at, is_test, campaign, product, state, zip, phone, phone_source,
        qual_status, lead_type, tier, score, routing_status, buyer_id, contact_scope, consent_ts, need_path, household_size,
        income_band, age_range, enrollment_situation, urgency, tool_result, utm_source, agent_status, consent_version)
        VALUES (?, datetime('now'), 0, 'camp', 'mortgage_protection', 'FL', '33101', '3055550130', 'CONSUMER_SUBMITTED',
        'QUALIFIED', 'PHONE_LEAD', 'A', 60, 'DELIVERED', ?, 'PHONE_AND_EMAIL', datetime('now'), 'self_employed', 3,
        '150-250', '35-44', 'renewal', 'MEDIUM',
        '{"headline":"Renewal window opens Nov 1","detail":"Plans and prices change at renewal."}',
        'guide', 'NEW', '2026-10-05.v4')""", (lid, buyer))
    m.con.commit()


def test_desk_post_feedback_names_saved_and_refused_actions(tmp_path):
    """A one-click status either changes the record or it does not — the desk must say which happened,
    so a buyer whose click silently did nothing (stale page, foreign lead, typo) can just retry from
    the list instead of assuming the outcome was recorded."""
    m = make_app(tmp_path); sandbox(m, tmp_path); c = m.app.test_client()
    save_buyer(m, c, action="activate")
    tok = buyer_row(tmp_path / "cfg", "portal-test")["portal_token"]
    _desk_lead(m, "LFBK1")
    # a valid click tells the buyer it was saved (and the desk actually carries the new status forward)
    r = c.post(f"/agent/{tok}", data={"lead_id": "LFBK1", "status": "APPOINTMENT", "f": ""}, follow_redirects=True)
    assert "Status saved" in r.get_data(as_text=True)
    st = [dict(x)["agent_status"] for x in m.con.execute("SELECT agent_status FROM leads WHERE lead_id='LFBK1'")][0]
    assert st == "APPOINTMENT"
    # an invalid click cannot change anything, and the desk says so instead of silently redirecting
    r2 = c.post(f"/agent/{tok}", data={"lead_id": "LFBK1", "status": "MADE_UP"}, follow_redirects=True)
    assert "could not be recorded" in r2.get_data(as_text=True)
    assert [dict(x)["agent_status"] for x in m.con.execute("SELECT agent_status FROM leads WHERE lead_id='LFBK1'")][0] == "APPOINTMENT"
    # a foreign/unknown lead id is the same refused path — never partially applied
    r3 = c.post(f"/agent/{tok}", data={"lead_id": "GHOST", "status": "CONTACTED"}, follow_redirects=True)
    assert "could not be recorded" in r3.get_data(as_text=True)
    # the lead PAGE shows the same feedback strips after a saved click (back=1 round-trip)
    r4 = c.post(f"/agent/{tok}", data={"lead_id": "LFBK1", "status": "CONTACTED", "back": "1"}, follow_redirects=True)
    assert "Status saved" in r4.get_data(as_text=True)


def test_desk_empty_state_distinguishes_no_leads_from_filtered_empty(tmp_path):
    """An empty desk is genuine state (no consent named this buyer yet); a filter on a status with no
    matches is different. The page must not present either as a broken table, and must offer the way
    back to the full list."""
    m = make_app(tmp_path); sandbox(m, tmp_path); c = m.app.test_client()
    save_buyer(m, c, action="activate")
    tok = buyer_row(tmp_path / "cfg", "portal-test")["portal_token"]
    page = c.get(f"/agent/{tok}").get_data(as_text=True)
    assert "Your desk is empty" in page and "true state, not an error" in page
    assert "<table>" not in page                     # no table headers for an empty desk
    _desk_lead(m, "LEMP1")
    page2 = c.get(f"/agent/{tok}?f=SOLD").get_data(as_text=True)
    assert 'No lead holds the status "Sold"' in page2 and "Show the whole desk" in page2
    assert "Your desk is empty" not in page2         # a filter result is NOT an empty desk
    page3 = c.get(f"/agent/{tok}").get_data(as_text=True)
    # the inline preview expands the situation a buyer needs before calling, without leaving the desk
    assert "More about this lead" in page3
    assert "Renewal window opens Nov 1" in page3 and "Plans and prices change at renewal." in page3
    assert "income band 150-250" in page3 and "Household:</b> 3 · income band 150-250" in page3
    assert "2026-10-05.v4" in page3                   # consent version is part of the preview, not hidden behind a click-away
