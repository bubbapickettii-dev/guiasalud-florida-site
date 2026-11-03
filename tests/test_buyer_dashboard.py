"""Buyer dashboard (agency portal shell) regression tests.

The new portal surface modeled on the reference agent dashboard:
  /agent/<token>            home overview (recorded stat cards, financial overview, transactions)
  /agent/<token>/leads      searchable/sortable/paged lead list + pin toggle
  /agent/<token>/outcomes   inline record fields (contact date, est. submitted AV, follow-up)

Guarantees under test:
  - every figure comes from the buyer's OWN delivered leads / ledger (scope by portal token)
  - pin/fields actions touch ONLY the token's own lead and nothing in routing/pricing
  - field validation refuses bad dates/values WITHOUT changing the lead
  - the returned (redirect) page re-enters the SAME filtered list view
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_billing import make_app  # isolated app on a temp DB (matches existing suites)
from test_buyer_portal import sandbox, save_buyer, buyer_row as buyer_row_in  # test-buyer provisioning


def _seed_delivered(m, buyer_id, day_offset=0, price=25.0, cid="seed", name="Ana Torres",
                    phone="4075551234", email="ana@test.example", campaign="oep-2027-health", state="FL",
                    status="NEW", pinned=0):
    """Insert a DELIVERED lead row directly (TEST traffic) against the email/phone immutability triggers."""
    from datetime import datetime, timedelta, timezone
    ts = (datetime.now(timezone.utc) - timedelta(days=day_offset)).isoformat(timespec="seconds")
    lid = f"L{cid}"
    m.con.execute(
        "INSERT INTO leads (lead_id, created_at, is_test, campaign, product, first_name, last_name, email, phone,"
        " zip, state, qual_status, tier, routing_status, buyer_id, price, delivered_at, agent_status, pinned,"
        " contact_at, est_av, followup_at, consent_ts, consent_version, lead_type, phone_e164, phone_valid,"
        " phone_source, preferred_contact)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (lid, ts, 1, campaign, "health_aca", name.split()[0], name.split()[-1], email, phone,
         "32801", state, "QUALIFIED", "A", "DELIVERED", buyer_id, price, ts, status, pinned,
         None, None, None, ts, "v5-consent-DRAFT", "PHONE_LEAD", phone, 1, "CONSUMER_SUBMITTED", "phone"))
    m.con.commit()
    return lid


def _tok(m, tmp, bid="dash-buyer-1"):
    """Provision an ACTIVATED non-demo buyer on a sandboxed config copy (same pattern as the
    buyer-portal suite) and return (portal_token, buyer_id). The project's real buyers stay
    inactive/untouched."""
    cfg = sandbox(m, tmp)
    save_buyer(m, m.app.test_client(), bid=bid, action="activate")
    return buyer_row_in(cfg, bid)["portal_token"], bid


def test_home_overview_renders_recorded_figures(tmp_path):
    m = make_app(tmp_path); tok, bid = _tok(m, tmp_path)
    lid = _seed_delivered(m, bid)
    c = m.app.test_client()
    m.con.execute(
        "INSERT INTO buyer_ledger (ts,buyer_id,lead_id,kind,amount,balance_after,note,is_test) VALUES"
        " (datetime('now'),?,?, 'LEAD_CHARGE', -25.0, 475.0, ?,1)", (bid, lid, "charge for " + lid))
    m.con.commit()
    page = c.get(f"/agent/{tok}").get_data(as_text=True)
    for needle in ("<h1", "Leads received today", "Leads this week", "Avg cost per lead", "$25.00",
                   "Financial overview", "Available balance", "Lead delivered", "-$25.00",
                   "Markets you stand in right now", "Recent matches", "TEST",
                   "delivered so far today"):
        assert needle in page, needle


def test_leads_list_search_filter_sort_paging(tmp_path):
    m = make_app(tmp_path); tok, bid = _tok(m, tmp_path)
    _seed_delivered(m, bid, day_offset=1, cid="l1", name="Ana Torres", phone="4075551234")
    b_ = _seed_delivered(m, bid, day_offset=2, cid="l2", name="Bob Vance", phone="4075559999",
                         email="bob@test.example", campaign="medicare-fl")
    _seed_delivered(m, bid, day_offset=40, cid="l3", name="Old Caro", phone="4075550000")
    c = m.app.test_client()
    # search narrows to the matching name only
    page = c.get(f"/agent/{tok}/leads?q=bob").get_data(as_text=True)
    assert "Bob Vance" in page and "Ana Torres" not in page
    # received-date filter starts at the whole desk (day_offset=1/2 are "today" for the week filter)
    page = c.get(f"/agent/{tok}/leads?days=0").get_data(as_text=True)
    assert "Old Caro" in page
    # name sort puts Ana before Old Caro
    page = c.get(f"/agent/{tok}/leads?days=0&sort=name&dir=asc").get_data(as_text=True)
    assert page.index("Ana Torres") < page.index("Old Caro")
    # pin then the pinned-only filter selects Bob
    c.post(f"/agent/{tok}/lead/{b_}/pin", data={"ret": f"/agent/{tok}/leads?days=0"})
    con_row = m.con.execute("SELECT pinned FROM leads WHERE lead_id=?", (b_,)).fetchone()
    assert con_row["pinned"] == 1
    page = c.get(f"/agent/{tok}/leads?days=0&pinned=1").get_data(as_text=True)
    assert "Bob Vance" in page and "Ana Torres" not in page


def test_pin_and_fields_cannot_reach_other_buyers_leads(tmp_path):
    m = make_app(tmp_path); tok, bid = _tok(m, tmp_path)
    other_lid = _seed_delivered(m, "other-buyer-9", cid="x9", name="Not Mine", phone="4075557777")
    c = m.app.test_client()
    r = c.post(f"/agent/{tok}/lead/{other_lid}/pin", data={"ret": f"/agent/{tok}/leads"})
    assert r.status_code == 404
    assert m.con.execute("SELECT pinned FROM leads WHERE lead_id=?", (other_lid,)).fetchone()["pinned"] == 0
    r = c.post(f"/agent/{tok}/lead/{other_lid}/fields", data={"contact_at": "2026-10-01"})
    assert r.status_code == 404
    assert m.con.execute("SELECT contact_at FROM leads WHERE lead_id=?", (other_lid,)).fetchone()[0] is None


def test_fields_validation_changes_nothing_on_refusal(tmp_path):
    m = make_app(tmp_path); tok, bid = _tok(m, tmp_path)
    lid = _seed_delivered(m, bid, cid="f1")
    c = m.app.test_client()
    # garbage date format is refused and NOTHING is written
    r = c.post(f"/agent/{tok}/lead/{lid}/fields",
               data={"contact_at": "10/01/2026", "est_av": "", "followup_at": "",
                     "ret": f"/agent/{tok}/outcomes"})
    assert "field:contact_at" in r.headers["Location"]
    row = m.con.execute("SELECT contact_at, est_av, followup_at FROM leads WHERE lead_id=?", (lid,)).fetchone()
    assert row["contact_at"] is None and row["est_av"] is None and row["followup_at"] is None
    # nonsensical AV is refused too
    r = c.post(f"/agent/{tok}/lead/{lid}/fields", data={"est_av": "-12", "ret": f"/agent/{tok}/leads"})
    assert "field:est_av" in r.headers["Location"]
    # a valid submission saves all three fields and returns to the SAME filtered view
    r = c.post(f"/agent/{tok}/lead/{lid}/fields",
               data={"contact_at": "2026-10-20", "est_av": "4200.50", "followup_at": "2026-10-25",
                     "ret": f"/agent/{tok}/outcomes?week=4"})
    assert r.headers["Location"].startswith(f"/agent/{tok}/outcomes?week=4")
    assert "msg=fields" in r.headers["Location"]
    row = m.con.execute("SELECT contact_at, est_av, followup_at FROM leads WHERE lead_id=?", (lid,)).fetchone()
    assert row["contact_at"] == "2026-10-20" and row["est_av"] == 4200.5 and row["followup_at"] == "2026-10-25"
    page = c.get(r.headers["Location"]).get_data(as_text=True)
    assert row["contact_at"] in page and "4200.5" in page


def test_outcomes_tracker_renders_inline_record_fields(tmp_path):
    m = make_app(tmp_path); tok, bid = _tok(m, tmp_path)
    _seed_delivered(m, bid, cid="o1", name="Paula Ruiz", phone="4075554321")
    c = m.app.test_client()
    page = c.get(f"/agent/{tok}/outcomes").get_data(as_text=True)
    for needle in ("My outcomes", "Est. submitted AV", "Next follow-up", "(407) 555-4321"):
        assert needle in page, needle
    page = c.get(f"/agent/{tok}/outcomes?q=zzz-nobody").get_data(as_text=True)
    assert "No submitted apps in this window" in page


def test_desk_status_flow_still_works_through_the_new_home(tmp_path):
    """The legacy one-click outcome POST math must survive the new shell (existing buyer workflow)."""
    m = make_app(tmp_path); tok, bid = _tok(m, tmp_path)
    lid = _seed_delivered(m, bid, cid="s1")
    c = m.app.test_client()
    r = c.post(f"/agent/{tok}", data={"lead_id": lid, "status": "CONTACTED", "note": "called"})
    assert r.status_code in (200, 302)
    row = m.con.execute("SELECT agent_status FROM leads WHERE lead_id=?", (lid,)).fetchone()
    assert row["agent_status"] == "CONTACTED"
    hist = m.con.execute("SELECT outcome, note FROM lead_outcomes WHERE lead_id=?", (lid,)).fetchall()
    assert any(h["outcome"] == "CONTACTED" and "called" in h["note"] for h in hist)


def test_schema_added_desk_columns_exist_with_defaults(tmp_path):
    m = make_app(tmp_path)
    cols = [r[1] for r in m.con.execute("PRAGMA table_info(leads)")]
    assert {"pinned", "contact_at", "est_av", "followup_at"} <= set(cols)


def test_followup_workflow_due_view_and_csv_columns(tmp_path):
    """A follow-up date in the past surfaces on the desk and in a dedicated due view;
    a foreign buyer's due lead never appears through another token."""
    import csv
    import io
    m = make_app(tmp_path); tok, _bid_unused = _tok(m, tmp_path)
    bid = _bid_unused
    _ = _seed_delivered
    due = _seed_delivered(m, bid, day_offset=15, cid="due", name="Dana Overdue")
    _seed_delivered(m, bid, day_offset=1, cid="future", name="Felix Future")
    m.con.execute("UPDATE leads SET followup_at=date('now','-2 days') WHERE lead_id=?", ("Ldue",))
    m.con.execute("UPDATE leads SET followup_at=date('now','+5 days') WHERE lead_id=?", ("Lfuture",))
    _seed_delivered(m, "ghost-buyer-z", cid="ghost", name="Ghost Lead", phone="4075558888")
    m.con.commit()
    c = m.app.test_client()

    # dashboard names the due count and links the filtered view
    home = c.get(f"/agent/{tok}").get_data(as_text=True)
    assert "Follow-ups due" in home and "due=1" in home
    page = c.get(f"/agent/{tok}/leads?due=1&days=0").get_data(as_text=True)
    assert "Follow-up view" in page and "Ldue" in page
    assert "Lfuture" not in page                                    # a future date is NOT due

    page_uid = c.get(f"/agent/{tok}/leads?due=1").get_data(as_text=True)   # default days=7 must NOT hide older due leads
    assert "Ldue" in page_uid
    empty = c.get(f"/agent/{tok}/leads?due=1&q=zzz-nobody&days=0").get_data(as_text=True)
    assert "No leads match this filter" in empty

    # CSV carries the tracker columns and the same due filter
    r = c.get(f"/agent/{tok}/leads.csv?days=0&due=1")
    lines = list(csv.DictReader(io.StringIO(r.get_data(as_text=True))))
    assert lines and {"contact_at", "est_av", "followup_at"} <= set(lines[0])
    assert [l["lead_id"] for l in lines] == ["Ldue"]

    # the buyer's full desk shows both follow-up states but never another buyer's lead
    own_all = c.get(f"/agent/{tok}/leads?days=0").get_data(as_text=True)
    assert "Ldue" in own_all and "Lfuture" in own_all and "Not Mine" not in own_all
    # another buyer's token never shows this desk's due rows (each separate buyer sees only its own desk)
    other_tok = _tok(m, tmp_path, bid="dash-buyer-2")[0]
    allrows = c.get(f"/agent/{other_tok}/leads?days=0").get_data(as_text=True)
    assert "Dana Overdue" not in allrows and "Ghost Lead" not in allrows and "Felix Future" not in allrows


def test_home_shows_crm_delivery_health_from_recorded_rows(tmp_path):
    """New desk card: CRM delivery counts the LATEST attempt per record, never double-counts retries."""
    m = make_app(tmp_path); tok, bid = _tok(m, tmp_path)
    lid = _seed_delivered(m, bid, day_offset=0, cid="dl1")
    c = m.app.test_client()
    m.con.executemany(
        "INSERT INTO deliveries (ts, lead_id, buyer_id, method, status, payload_path, response)"
        " VALUES (?,?,?,?,?,'out/dl1.json',?)", [
            ("2026-10-25T10:00:00", lid, bid, "webhook", "FAILED", "retry: 500 boom"),
            ("2026-10-25T10:05:00", lid, bid, "webhook", "RESENT", "retry: 200 ok")])
    other = _seed_delivered(m, bid, day_offset=0, cid="dl2", phone="4075551235", name="Bob Lane")
    m.con.execute("INSERT INTO deliveries (ts, lead_id, buyer_id, method, status, payload_path, response)"
                  " VALUES (?,?,?,?,?,'out/dl2.json','retry: 503 still down')",
                  ("2026-10-26T11:00:00", other, bid, "webhook", "FAILED"))
    m.con.commit()
    page = c.get(f"/agent/{tok}").get_data(as_text=True)
    assert "CRM delivery" in page
    assert "1 failed" in page                       # latest attempt of dl2 (dl1's latest is RESENT)
    for n in ("CRM delivery (latest attempt per lead)", "retries are budgeted"):
        assert n in page
    # failed latest attempt links the lead page where the recorded-payload Retry lives
    assert f"/agent/{tok}/lead/{other}" in page


def test_outcomes_csv_mirrors_the_tracker_filters_and_scope(tmp_path):
    """outcomes.csv exports the SAME rows the tracker shows (same filters, same lane): only this
    token's delivered leads, status NEW when nothing recorded, and the buyer's own working fields."""
    m = make_app(tmp_path); tok, bid = _tok(m, tmp_path)
    _seed_delivered(m, bid, day_offset=0, cid="csv1", email="ana@test.example", phone="4075551234")
    _seed_delivered(m, bid, day_offset=0, cid="csv2", email="bob@test.example", phone="4075552233",
                    name="Bob Lane", status="CONTACTED")
    _seed_delivered(m, bid, day_offset=0, cid="csv3", email="carl@test.example", phone="4075553233",
                    name="Carl Ny")
    m.con.execute("UPDATE leads SET contact_at='2026-10-26', est_av=4200.50, followup_at='2026-10-28' "
                  "WHERE lead_id='Lcsv1'"); m.con.commit()
    c = m.app.test_client()
    r = c.get(f"/agent/{tok}/outcomes.csv?week=0")
    assert r.status_code == 200 and "text/csv" in r.headers["Content-Type"]
    head = r.get_data(as_text=True).strip().splitlines()
    assert head[0].startswith("lead_id,first_name,last_name,phone")
    body = "\n".join(head[1:])
    for lid in ("Lcsv1", "Lcsv2", "Lcsv3"):
        assert lid in body
    assert "4200.5" in body and "CONTACTED" in body
    # search filter matches the tracker: the q= filter drops other names from BOTH surfaces
    csvq = c.get(f"/agent/{tok}/outcomes.csv?week=0&q=Bob")
    assert "Lcsv2" in csvq.get_data(as_text=True) and "Lcsv1" not in csvq.get_data(as_text=True)
    # isolation: another buyer's token exports NOTHING of this desk
    save_buyer(m, c, bid="dash-buyer-2")    # second real buyer on the same sandbox config
    c.post("/admin/buyers?token=t", data={"buyer_id": "dash-buyer-2", "action": "activate"})
    other_tok = buyer_row_in(m.CFG, "dash-buyer-2")["portal_token"]
    other = c.get(f"/agent/{other_tok}/outcomes.csv?week=0").get_data(as_text=True)
    assert "Lcsv1" not in other and "Lcsv2" not in other
