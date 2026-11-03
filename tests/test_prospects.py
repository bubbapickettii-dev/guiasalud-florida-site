"""Business-buyer prospect registry: separate DB, dedupe, decisions, verification,
and hard separation from consumer infrastructure. Offline tests only."""
import ast
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import prospects as pr                 # noqa: E402


def _tmp_db():
    return os.path.join(tempfile.mkdtemp(), "prospects.db")


# ------------------------------------------------------------------- registry behavior
def test_ingest_dedupes_website_then_namezip():
    con = pr.connect(_tmp_db())
    r1 = pr.ingest(con, [{"name": "Sunshine Health Group", "website": "sunshinehealth.example",
                          "city": "Miami", "state": "FL", "phone": "(305) 555-0100"}], "manual")
    assert r1["added"] == 1
    # same website, extra data → update not duplicate
    r2 = pr.ingest(con, [{"name": "Sunshine Health Group",
                          "website": "https://www.sunshinehealth.example", "category": "brokerage"}], "manual")
    assert r2["updated"] == 1 and r2["added"] == 0
    assert con.execute("SELECT COUNT(*) FROM prospects").fetchone()[0] == 1
    assert con.execute("SELECT category FROM prospects").fetchone()[0] == "brokerage"
    # same name + same ZIP but no website → SAME business, dedupe
    r3 = pr.ingest(con, [{"name": "Sunshine Health Group 2", "zip5": "33101"}], "manual")
    assert r3["added"] == 1
    r4 = pr.ingest(con, [{"name": "Sunshine Health Group 2", "zip5": "33101"}], "manual")
    assert r4["updated"] == 1 and r4["added"] == 0


def test_decisions_and_verification_are_operator_records():
    con = pr.connect(_tmp_db())
    pr.ingest(con, [{"name": "Gulf Coast Brokerage", "zip5": "33101"}], "manual")
    pid = con.execute("SELECT id FROM prospects").fetchone()[0]
    assert pr.decide(con, pid, "PRIORITIZED", "brokerage owners asked about exclusive leads at a conference") is True
    assert pr.verify(con, pid, "OPERATOR_VERIFIED", "phone + address checked against the state license board site") is True
    row = tuple(con.execute("SELECT decision, verify_state FROM prospects").fetchone())
    assert row == ("PRIORITIZED", "OPERATOR_VERIFIED")
    lg = pr.log(con, pid)
    assert any("decision:PRIORITIZED" in r["action"] for r in lg)
    assert any("verify:OPERATOR_VERIFIED" in r["action"] for r in lg)


def test_queue_filters():
    con = pr.connect(_tmp_db())
    pr.ingest(con, [{"name": "A Agency", "state": "FL", "category": "agency"}], "manual")
    pr.ingest(con, [{"name": "B Broker", "state": "TX"}], "csv")
    assert len(pr.queue(con, state="FL")) == 1
    assert len(pr.queue(con, source="csv")) == 1
    assert len(pr.queue(con, q="broker")) == 1
    assert len(pr.queue(con)) == 2


def test_csv_import_requires_name_column():
    con = pr.connect(_tmp_db())
    path = Path(tempfile.mkdtemp()) / "rows.csv"
    path.write_text("name,website,city\nCoral Gables Ins,coralgables.example,Coral Gables\n",
                    encoding="utf-8")
    r = pr.import_csv(con, path, "manual")
    assert r["added"] == 1
    path2 = Path(tempfile.mkdtemp()) / "bad.csv"
    path2.write_text("company,phone\nAcme,555\n", encoding="utf-8")
    r2 = pr.import_csv(con, path2, "manual")
    assert r2["added"] == 0 and r2["skipped"] == 1    # name missing → skipped, not crashed


# ------------------------------------------------------------------- separation
def test_places_adapter_refuses_without_key_and_approval():
    os.environ.pop("GOOGLE_PLACES_API_KEY", None)
    con = pr.connect(_tmp_db())
    with __import__("pytest").raises(NotImplementedError) as e:
        pr.fetch_places(con, [{"text_query": "insurance agencies in Miami"}],
                        search_text=lambda q: {})
    cfg = (ROOT / "config" / "sources.json").read_text(encoding="utf-8")
    assert 'approved' in cfg or "approval" in str(e.value)


# ------------------------------------------------------------------- lifecycle + handoff
def _seed_qualified(con, name="Test Agency LLC", **kw):
    key = f"{name.lower()}|"
    con.execute("INSERT INTO prospects (prospect_key, name, source, decision, verify_state, stage,"
                " state, service_territory, website, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (key, name, 'manual', kw.get("decision", "QUALIFIED"),
                 kw.get("verify_state", "OPERATOR_VERIFIED"), kw.get("stage", "QUALIFIED"),
                 kw.get("state", "FL"), kw.get("service_territory", "South FL"),
                 kw.get("website", "testagency.example"), "2026-10-27T00:00:00", "2026-10-27T00:00:00"))
    con.commit()
    return con.execute("SELECT id FROM prospects WHERE prospect_key=?", (key,)).fetchone()[0]


def test_stage_ladder_requires_notes_and_blocks_terminal_sets():
    con = pr.connect(_tmp_db())
    pid = _seed_qualified(con, stage="NEW", decision="NEW")
    # terminal stages can never be hand-set
    assert pr.set_stage_checked(con, pid, "ONBOARDED") is not None
    assert pr.set_stage_checked(con, pid, "ACTIVATED") is not None
    # qualifying without the why is refused
    assert "note" in (pr.set_stage_checked(con, pid, "QUALIFIED") or "")
    assert con.execute("SELECT stage FROM prospects WHERE id=?", (pid,)).fetchone()[0] == "NEW"
    # with the why: recorded + logged
    assert pr.set_stage_checked(con, pid, "QUALIFIED", "health-exclusive models matched our inventory") is None
    assert con.execute("SELECT stage FROM prospects WHERE id=?", (pid,)).fetchone()[0] == "QUALIFIED"
    assert any("stage:QUALIFIED" in r["action"] for r in pr.log(con, pid))
    # DISQUALIFIED also demands the why
    assert "note" in (pr.set_stage_checked(con, pid, "DISQUALIFIED") or "")


def test_update_fields_whitelist_and_noop_and_dates():
    con = pr.connect(_tmp_db())
    pid = _seed_qualified(con)
    changed = pr.update_fields(con, pid, {"stated_budget_usd": "up to $900/wk (stated)",
                                          "next_action": "send follow-up", "notes": "keeps"})
    assert set(changed) == {"stated_budget_usd", "next_action", "notes"} or changed
    # re-sending the same values is a no-op (no second log row)
    n_log = len(pr.log(con, pid))
    assert pr.update_fields(con, pid, {"stated_budget_usd": "up to $900/wk (stated)",
                                       "next_action": "send follow-up", "notes": "keeps"}) == []
    assert len(pr.log(con, pid)) == n_log
    # unknown keys are ignored entirely — never widened
    assert pr.update_fields(con, pid, {"decision": "ARCHIVED"}) == []
    assert con.execute("SELECT decision FROM prospects WHERE id=?", (pid,)).fetchone()[0] == "QUALIFIED"
    # bad date refused
    try:
        pr.update_fields(con, pid, {"next_action_date": "not-a-date"})
        raise SystemExit("bad date accepted")
    except ValueError:
        pass
    # clearing a value works and is logged
    assert pr.update_fields(con, pid, {"notes": ""}) == ["notes"]


def test_needs_attention_flags_gap_kinds():
    con = pr.connect(_tmp_db())
    pid0 = _seed_qualified(con, decision="NEW", verify_state="UNVERIFIED")
    row = pr.detail(con, pid0)
    gaps = pr.needs_attention(row)
    assert any("decision" in g for g in gaps)
    # stage ahead of verification is flagged even with a decision
    pid1 = _seed_qualified(con, name="Verified Gap LLC", decision="QUALIFIED", verify_state="UNVERIFIED",
                           stage="READY_FOR_ONBOARDING")
    assert any("ahead of verification" in g for g in pr.needs_attention(pr.detail(con, pid1)))


def test_handoff_gate_and_draft():
    con = pr.connect(_tmp_db())
    good = _seed_qualified(con)                  # QUALIFIED + OPERATOR_VERIFIED + territory
    buyers = [{"id": "other-agency", "name": "Other Agency", "active": True}]
    draft = pr.handoff_buyer_draft(pr.detail(con, good), buyers)
    assert draft["active"] is False and draft["is_demo"] is False
    assert not draft.get("portal_token")                               # portal-less draft
    assert draft["licensed_states"] == ["FL"] and draft["products"] == ["health_aca"]
    assert draft["converted_from_prospect"]["prospect_id"] == good

    # evidential gates: every refusal names exactly what is missing
    bad = _seed_qualified(con, name="Unverified LLC", decision="UNDER_REVIEW",
                          verify_state="UNVERIFIED", stage="RESEARCHED",
                          state="", service_territory="")
    try:
        pr.handoff_buyer_draft(pr.detail(con, bad), buyers)
        raise SystemExit("unverified handoff accepted")
    except ValueError as e:
        msg = str(e)
        assert "UNDER_REVIEW" in msg and "UNVERIFIED" in msg and "RESEARCHED" in msg
        assert "territory" in msg

    # duplicate buyer (same name) is refused instead of creating a second account
    dup = _seed_qualified(con, name="Other Agency")
    try:
        pr.handoff_buyer_draft(pr.detail(con, dup), buyers)
        raise SystemExit("duplicate handoff accepted")
    except ValueError as e:
        assert "other-agency" in str(e)


def test_set_buyer_link_recorded():
    con = pr.connect(_tmp_db())
    pid = _seed_qualified(con)
    pr.set_buyer_link(con, pid, "test-agency-llc")
    assert con.execute("SELECT buyer_id FROM prospects WHERE id=?", (pid,)).fetchone()[0] == "test-agency-llc"
    assert any("handoff" in r["action"] for r in pr.log(con, pid))


def test_gate_item_checklist_statuses():
    con = pr.connect(_tmp_db())
    # unrecorded: every prospect-side item is missing or explicitly unknown
    pid = _seed_qualified(con, name="Check LLC", decision="", verify_state="UNVERIFIED", stage="NEW",
                          state="", service_territory="")
    items = {i["label"]: i for i in pr.gate_item(pr.detail(con, pid))["items"]}
    assert all(i["status"] == pr.CHECK_MISSING for i in items.values())
    assert any(i["kind"] == "verified" for i in items.values())     # verification is labeled independent
    assert any(i["kind"] == "provided" for i in items.values())
    pil = [i for i in items.values() if "identity" in i["label"]][0]
    # pending review = checked but not operator-verified
    pr.verify(con, pid, "PUBLIC_DETAILS_CHECKED", "website + phone matched the listing")
    pil = [i for i in pr.gate_item(pr.detail(con, pid))["items"] if "identity" in i["label"]][0]
    assert pil["status"] == pr.CHECK_PENDING
    # completed + operator-verified
    pr.decide(con, pid, "QUALIFIED", "matched target profile")
    pr.verify(con, pid, "OPERATOR_VERIFIED", "FL DFS licensee search 2026-10-27, active")
    pr.set_stage_checked(con, pid, "QUALIFIED", "verified profile")
    pr.update_fields(con, pid, {"service_territory": "FL"})
    items = {i["label"]: i["status"] for i in pr.gate_item(pr.detail(con, pid))["items"]}
    assert all(s == pr.CHECK_OK for s in items.values())


def test_decision_reasons_returns_last_recorded_why():
    con = pr.connect(_tmp_db())
    pid = _seed_qualified(con)
    pr.decide(con, pid, "NOT_A_FIT", "out of our service area")
    reasons = pr.decision_reasons(con)
    assert reasons.get(pid) == "out of our service area"
    pr.decide(con, pid, "PRIORITIZED", "area revised — now covered")
    assert pr.decision_reasons(con).get(pid) == "area revised — now covered"   # last decision wins


def test_registry_never_touches_consumer_pipeline():
    src = (ROOT / "engine" / "prospects.py").read_text(encoding="utf-8")
    for bad in ("engine.route", "engine.notify", "engine.billing", "engine.payments",
                "leads.db", "LEADENGINE_DB"):
        assert bad not in src, f"prospect registry references consumer pipeline: {bad}"
    tree = ast.parse(src)
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
        elif isinstance(node, ast.Import):
            mods |= {a.name for a in node.names}
    assert not any(m in mods for m in ("engine.route", "engine.notify",
                                       "engine.billing", "engine.payments"))


def test_prospect_sources_rows_are_honest():
    rows = {r["id"]: r for r in pr.prospect_sources()}
    assert rows["manual_csv"]["status"] == "COLLECTS"
    assert rows["maps_scraper_kit"]["status"] == "TOS_BLOCKED"
    assert "ToS" in rows["maps_scraper_kit"]["how"] or "ToS" in rows["maps_scraper_kit"]["notes"]
    os.environ.pop("GOOGLE_PLACES_API_KEY", None)
    assert rows["google_places"]["status"] == "NEEDS_KEY"
    # the GeoLeadScraper export path must never look like a clean, unqualified source
    gls = rows["geoleadscraper_csv"]
    assert gls["status"] == "OPERATOR_FILE"
    assert "caveat" in gls["notes"]


# ------------------------------------------------- GeoLeadScraper export bridge
_GLS_ROW = {
    "place_id": "ChIJ77N9tvj92IgR5P8v1psOAFg", "title": "Casa Calabria",
    "address": "4040 Galt Ocean Dr L15, Fort Lauderdale, FL 33308",
    "city": "Fort Lauderdale", "state": "Florida", "postal_code": "33308",
    "phone": "+19549822191", "main_category": "Italian restaurant",
    "categories": "Italian restaurant, Fine dining restaurant",
    "website": "https://casacalabriaftl.com/menu/?utm_source=google",
    "maps_url": "https://www.google.com/maps?cid=6341084337813848036",
    "email": "info@casacalabriaftl.com", "socials": "https://facebook.com/x",
    "rating": 4.8, "opening_hours": "Monday: Closed",
}


def test_geoleads_rows_map_export_columns_onto_registry_fields():
    out = pr.geoleads_rows([_GLS_ROW])
    assert len(out) == 1
    r = out[0]
    assert r["name"] == "Casa Calabria"
    assert r["category"] == "Italian restaurant"          # main_category wins over the list
    assert r["phone"] == "+19549822191"
    assert r["website"] == "https://casacalabriaftl.com/menu"   # tracking params dropped
    assert (r["city"], r["state"], r["zip5"]) == ("Fort Lauderdale", "FL", "33308")
    assert r["evidence_url"].startswith("https://www.google.com/maps")
    # business identity ONLY: the export's email/socials/rating never reach the registry
    assert not {"email", "socials", "rating", "opening_hours"} & set(r)


def test_geoleads_state_zip_fall_back_to_the_address_and_skip_nameless_rows():
    bare = {"title": "Pier 66 Marine", "address": "2301 SE 17th St, Fort Lauderdale, FL 33316"}
    r = pr.geoleads_rows([bare, {"address": "no name here"}])[0]
    assert (r["state"], r["zip5"]) == ("FL", "33316")
    assert len(pr.geoleads_rows([{"address": "no name here"}])) == 0


def test_import_geoleads_csv_dedupes_deep_links_by_host(tmp_path):
    con = pr.connect(os.path.join(str(tmp_path), "prospects.db"))
    p1 = tmp_path / "gls1.csv"
    p1.write_text("title,address,city,state,postal_code,phone,website,maps_url\n"
                  "Casa Calabria,\"4040 Galt Ocean Dr L15, Fort Lauderdale, FL 33308\",Fort Lauderdale,"
                  "Florida,33308,+19549822191,https://casacalabriaftl.com/menu/,https://maps.example/a\n",
                  encoding="utf-8")
    r1 = pr.import_geoleads_csv(con, p1)
    assert (r1["added"], r1["mapped"]) == (1, 1)
    # a second export of the same business from a different deep link is an UPDATE, not a new row
    p2 = tmp_path / "gls2.csv"
    p2.write_text("title,website,maps_url\nCasa Calabria,https://casacalabriaftl.com/reservations,https://maps.example/b\n",
                  encoding="utf-8")
    r2 = pr.import_geoleads_csv(con, p2)
    assert (r2["added"], r2["updated"]) == (0, 1)
    assert con.execute("SELECT COUNT(*) FROM prospects").fetchone()[0] == 1
    row = con.execute("SELECT source, decision, verify_state FROM prospects").fetchone()
    assert tuple(row) == ("geoleadscraper", "NEW", "UNVERIFIED")
