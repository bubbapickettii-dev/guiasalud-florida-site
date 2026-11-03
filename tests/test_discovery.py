"""Automated public-prospect discovery tests (engine/prospector.py + engine/discovery.py).

Boundaries under test:
  1. Classification is precision-first: a real first-person coverage ask passes; a guide thread, an
     agent pitch, an "already covered" statement and a policy op-ed do not.
  2. Discoveries land in the lead DATABASE (`public_prospects`), never in `leads` — a public post can
     never become a sellable lead by itself.
  3. Status assignment follows evidence: no contact -> NEW_PROSPECT; contact without invitation ->
     CONTACT_INFORMATION_FOUND; contact + invitation -> CONTACT_PERMISSION_REQUIRED; nothing above
     that is reachable by an adapter.
  4. Operator transitions demand evidence, and ROUTING_ELIGIBLE needs a real consented lead row.
  5. Adapters are offline-testable: board/thread/JSON parsing with an injected fetcher, robots denial
     recorded as a restriction (not a crash), and both counters measured per run.
  6. Ingestion is idempotent and deduplicates on the published contact.
No network is used here; fixtures simulate the pages.
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import discovery, prospector          # noqa: E402

TS = "2026-10-30T12:00:00"


def store(tmp) -> sqlite3.Connection:
    return prospector.connect(tmp / "leads.db")


def rows(*items) -> list[dict]:
    return list(items)


NEED = ("I lost my job last month and my COBRA ends in January. Does anyone know how to get "
        "health insurance in Florida? My income is about $34k.")


# ------------------------------------------------------------------ 1. classification
def test_real_need_is_a_prospect():
    r = prospector.classify(NEED)
    assert r["need"] and r["person"]
    assert "health insurance" in r["sentence"].lower()
    assert r["topics"]


def test_guide_agent_covered_and_op_ed_are_not_prospects():
    assert not prospector.classify("New to Medicare? Start here. This post explains plan, "
                                   "supplement and coverage basics for anyone who is new.")["need"]
    assert not prospector.classify("I am a licensed agent and I can get you the best rates "
                                   "guaranteed. Call our office for a free quote today.")["need"]
    assert not prospector.classify("I have health insurance through my employer so I don't need "
                                   "coverage. Thanks everyone for the advice.")["need"]
    assert not prospector.classify("How Medicare Advantage Scams the Government. I'd encourage "
                                   "retirees to read the article about the coverage policy.")["need"]


def test_location_is_only_what_the_person_named(tmp_path):
    assert prospector.extract_geo("I need coverage in Florida for my kids") == "Florida"
    assert prospector.extract_geo("I am in FL and lost my job") == "FL"
    assert prospector.extract_geo("no location mentioned here") == ""
    con = store(tmp_path)
    prospector.ingest(con, rows({"url": "https://f.example/geo", "title": "need",
                                 "text": NEED + " I am in Florida."}), "s")
    assert con.execute("SELECT geo FROM public_prospects").fetchone()[0] == "Florida"


def test_spanish_need_is_recognised():
    r = prospector.classify("Perdi mi trabajo el mes pasado. Necesito seguro medico para mi "
                            "familia y no tengo cobertura.")
    assert r["need"] and r["sentence"]


# ------------------------------------------------------------------ 2/3. ingest + statuses
def test_need_is_stored_in_the_lead_db_but_never_in_leads(tmp_path):
    con = store(tmp_path)
    stats = prospector.ingest(con, rows({"url": "https://f.example/t/1", "title": "COBRA ending",
                                         "text": NEED}), "test_source")
    assert stats["accepted"] == 1 and stats["new"] == 1
    p = con.execute("SELECT * FROM public_prospects").fetchone()
    assert p["status"] == "NEW_PROSPECT" and p["source"] == "test_source"
    assert p["contact_status"] == "CONTACT_NOT_AVAILABLE"
    # the sellable table is untouched: no lead, no consent, no visitor
    assert con.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM consents").fetchone()[0] == 0


def test_published_contact_sets_permission_status_but_never_consent(tmp_path):
    con = store(tmp_path)
    with_contact = ("I need health insurance for my family and I lost my job. Email me at "
                    "ana.lopez@example.com and I can send the details.")
    prospector.ingest(con, rows({"url": "https://f.example/t/2", "title": "need coverage",
                                 "text": with_contact}), "test_source")
    p = dict(con.execute("SELECT * FROM public_prospects").fetchone())
    assert p["contact_status"] == "CONTACT_AVAILABLE"
    assert p["status"] == "CONTACT_PERMISSION_REQUIRED"      # invite wording present
    assert p["permission_status"] == "INVITED_INQUIRIES"     # a claim by the author, not consent
    assert p["status"] not in ("CONSENTED", "QUALIFIED", "ROUTING_ELIGIBLE")

    bare = ("My COBRA is ending and I need to find affordable coverage. Reach me at 305-555-0142.")
    prospector.ingest(con, rows({"url": "https://f.example/t/3", "title": "cobra",
                                 "text": bare}), "test_source")
    p2 = dict(con.execute("SELECT * FROM public_prospects WHERE source_url LIKE '%/t/3'").fetchone())
    assert p2["contact_status"] == "CONTACT_AVAILABLE"
    assert p2["status"] in ("CONTACT_INFORMATION_FOUND", "CONTACT_PERMISSION_REQUIRED")


def test_contact_first_keeps_a_published_contact_next_to_coverage_talk(tmp_path):
    """A published address beside coverage vocabulary is held even when the ask is too vague to
    classify as a need - parked at CONTACT_INFORMATION_FOUND with weak_intent, never promoted."""
    con = store(tmp_path)
    vague = "Health insurance premiums went up again here in Tampa. You can text me at 813-555-0177."
    stats = prospector.ingest(con, rows({"url": "https://f.example/t/cf", "title": "premiums",
                                         "text": vague}), "test_source")
    assert stats["accepted"] == 1 and stats["new"] == 1
    p = dict(con.execute("SELECT * FROM public_prospects").fetchone())
    assert p["contact_status"] == "CONTACT_AVAILABLE"
    assert p["status"] == "CONTACT_INFORMATION_FOUND"
    assert json.loads(p["evidence"])["weak_intent"] is True
    assert p["status"] not in ("CONSENTED", "QUALIFIED", "ROUTING_ELIGIBLE")
    # a vague post with no contact is still discarded: no contact, no reason to keep it
    stats2 = prospector.ingest(con, rows({"url": "https://f.example/t/cf2", "title": "premiums",
                                          "text": "Health insurance premiums went up again."}), "test_source")
    assert stats2["new"] == 0 and stats2["skipped"] == 1


def test_ingest_is_idempotent_and_dedupes_on_contact(tmp_path):
    con = store(tmp_path)
    row = {"url": "https://f.example/t/4", "title": "need", "text": NEED}
    assert prospector.ingest(con, rows(row), "test_source")["new"] == 1
    again = prospector.ingest(con, rows(row), "test_source")
    assert again["new"] == 0 and again["duplicates"] == 1
    first = ("I lost my job and need health insurance. Write me at ana.lopez@example.com please.")
    prospector.ingest(con, rows({"url": "https://f.example/t/4b", "title": "first",
                                 "text": first}), "test_source")
    same_person = ("I lost my job and need health insurance for my wife. Write me at "
                   "ana.lopez@example.com please.")
    prospector.ingest(con, rows({"url": "https://f.example/t/5", "title": "again",
                                 "text": same_person}), "test_source")
    dup = con.execute("SELECT status, dup_status FROM public_prospects WHERE source_url LIKE '%/t/5'").fetchone()
    assert dup["status"] == "DUPLICATE" and dup["dup_status"] == "DUPLICATE"


# ------------------------------------------------------------------ 4. operator transitions
def test_operator_transitions_require_evidence(tmp_path):
    con = store(tmp_path)
    prospector.ingest(con, rows({"url": "https://f.example/t/6", "title": "need", "text": NEED}), "s")
    pid = con.execute("SELECT id FROM public_prospects").fetchone()[0]
    assert prospector.verify(con, pid, "") is False               # no note, no verification
    assert prospector.verify(con, pid, "opened the thread; still current, Florida resident") is True
    assert prospector.qualify(con, pid, "relevant") is False      # no contact published
    assert prospector.record_consent(con, pid, "ok") is False     # too short to be evidence
    assert prospector.reject(con, pid, "not a fit") is True
    row = dict(con.execute("SELECT status, operator_note FROM public_prospects WHERE id=?", (pid,)).fetchone())
    assert row["status"] == "REJECTED" and "not a fit" in row["operator_note"]


def test_routing_eligible_requires_that_persons_own_consented_lead(tmp_path):
    con = store(tmp_path)
    text = ("I need health insurance and lost my job. Email me at ana.lopez@example.com and I can "
            "share my income details.")
    prospector.ingest(con, rows({"url": "https://f.example/t/7", "title": "need", "text": text}), "s")
    pid = con.execute("SELECT id FROM public_prospects").fetchone()[0]
    assert prospector.link_lead(con, pid, "L-NOT-REAL") is False  # no such lead, no upgrade
    con.execute("""INSERT INTO leads (lead_id, created_at, is_test, product, state, lead_type, phone,
                   phone_e164, phone_valid, phone_source, consent_ts, qual_status, routing_status)
                   VALUES ('L1', ?, 0, 'health_aca', 'FL', 'PHONE_LEAD', '3055550111', '+13055550111',
                   1, 'CONSUMER_SUBMITTED', ?, 'QUALIFIED', 'PENDING')""", (TS, TS))
    con.commit()
    assert prospector.link_lead(con, pid, "L1") is False          # lead exists but has no consent row
    con.execute("""INSERT INTO consents (lead_id, ts, consent_version, consent_text, seller_named,
                   checkbox_checked, scope) VALUES ('L1', ?, 'v4', 'I agree to be contacted.',
                   'Agency LLC', 1, 'PHONE_AND_EMAIL')""", (TS,))
    con.commit()
    assert prospector.link_lead(con, pid, "L1") is True
    p = dict(con.execute("SELECT status, lead_id FROM public_prospects WHERE id=?", (pid,)).fetchone())
    assert p["status"] == "ROUTING_ELIGIBLE" and p["lead_id"] == "L1"


# ------------------------------------------------------------------ 5. adapters, offline
BOARD = """<html><body>
<a href="/forum/health-insurance/index2.html">2</a>
<a href="/forum/health-insurance/111-hello.html">New to Medicare? Start here.</a>
<a href="/forum/health-insurance/222-need-coverage.html">Lost my job, need health insurance?</a>
<a href="/forum/health-insurance/333-cobra.html">COBRA premium question</a>
</body></html>"""

THREAD = """<html><body><div class="nav">User Name Remember Me Password</div>
<div id="post_message_222"><!-- google_ad_section_start -->I lost my job last month and my COBRA
ends in January. Does anyone know how to get health insurance in Florida? My income is $34k.<br />
</div><!-- message --><div id="post_message_999">Buy gold now.</div></body></html>"""


def fake_fetcher(pages: dict):
    def _f(url, robots=True, timeout=15):
        for k, v in pages.items():
            if k in url:
                return v
        raise discovery.AccessRestricted("HTTP 404")
    return _f


def test_citydata_adapter_parses_board_and_opening_post_only(tmp_path):
    con = store(tmp_path)
    f = fake_fetcher({"/forum/health-insurance/index2.html": "<html></html>",
                      "/forum/health-insurance/222-need-coverage.html": THREAD,
                      "/forum/health-insurance/333-cobra.html": "<html><body><div id=\"post_message_1\">"
                      "My COBRA premium went up again this year, any advice on options?</div></body></html>",
                      "/forum/health-insurance/": BOARD})
    res = discovery.citydata(max_threads=2, pages=1, fetcher=f, delay=0, skip=0,
                             boards={"health-insurance": "https://www.city-data.com/forum/health-insurance/"})
    assert res["fetched"] == 2
    assert len(res["rows"]) == 2
    need = next(r for r in res["rows"] if "222" in r["url"])
    assert "User Name" not in need["text"]          # chrome dropped
    assert "Buy gold" not in need["text"]           # later posts excluded
    stats = prospector.ingest(con, res["rows"], "citydata_forum")
    assert stats["new"] == 2                        # both opening posts are real asks


def test_robots_denial_is_recorded_not_crashed(tmp_path):
    con = store(tmp_path)

    def deny(url, robots=True, timeout=15):
        raise discovery.AccessRestricted("robots.txt disallows this URL for our agent")

    res = discovery.citydata(max_threads=2, fetcher=deny, delay=0,
                             boards={"health-insurance": "https://www.city-data.com/forum/health-insurance/"})
    assert res["errors"] == 1 and res["rows"] == []
    out = discovery.run(con, sources=("citydata",), fetcher=deny, delay=0)
    assert out[0]["errors"] >= 1
    run = con.execute("SELECT * FROM discovery_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert run["source"] == "citydata_forum" and "restricted" in (run["note"] or "").lower()


def test_discourse_adapter_reads_public_search_json(tmp_path):
    con = store(tmp_path)
    body = json.dumps({"topics": [{"id": 9, "title": "Need coverage after job loss",
                                   "slug": "need-coverage", "created_at": "2026-10-29T10:00:00"}],
                       "posts": [{"topic_id": 9, "username": "ana_l",
                                  "blurb": "I lost my job and need health insurance for my family. "
                                           "My COBRA ends soon, what are my options?",
                                  "created_at": "2026-10-29T10:00:00"}]})
    res = discovery.discourse("https://forum.example", ["health insurance"],
                              fetcher=fake_fetcher({"search.json": body}), delay=0)
    assert res["fetched"] == 1
    assert res["rows"][0]["url"] == "https://forum.example/t/need-coverage/9"
    assert res["rows"][0]["author"] == "ana_l"      # public username only
    stats = prospector.ingest(con, res["rows"], "discourse")
    assert stats["new"] == 1


# ------------------------------------------------------------------ 6. admin surface
def test_admin_people_page_lists_public_prospects_and_records_a_rejection(tmp_path):
    from test_billing import make_app
    m = make_app(tmp_path)
    prospector.ingest(m.con, rows({"url": "https://f.example/t/8", "title": "need", "text": NEED}), "s")
    m.con.commit()
    c = m.app.test_client()
    assert c.get("/admin/people").status_code == 403
    page = c.get("/admin/people?token=t").get_data(as_text=True)
    assert "Public prospects" in page and "new prospect" in page.replace("_", " ").lower()
    pid = m.con.execute("SELECT id FROM public_prospects").fetchone()[0]
    c.post(f"/admin/people/{pid}/status?token=t", data={"status": "REJECTED", "note": "no"})
    assert m.con.execute("SELECT status FROM public_prospects WHERE id=?", (pid,)).fetchone()[0] == "NEW_PROSPECT"
    c.post(f"/admin/people/{pid}/status?token=t",
           data={"status": "REJECTED", "note": "not a consumer, it is an agent"})
    assert m.con.execute("SELECT status FROM public_prospects WHERE id=?", (pid,)).fetchone()[0] == "REJECTED"


# ------------------------------------------------------------------ 7. legacy migration
def test_legacy_rows_are_migrated_without_upgrading_them(tmp_path):
    con = store(tmp_path)
    legacy = sqlite3.connect(":memory:")
    legacy.row_factory = sqlite3.Row
    legacy.execute("""CREATE TABLE prospect_people (id INTEGER PRIMARY KEY, person_key TEXT,
        source TEXT, community TEXT, source_url TEXT, stated_need TEXT, intent_cat TEXT, lang TEXT,
        observed_at TEXT, discovered_at TEXT, geo TEXT, contact_methods TEXT, contact_status TEXT,
        contact_evidence TEXT, permission_status TEXT, permission_evidence TEXT, confidence REAL,
        dup_status TEXT, dup_of TEXT, profile_url TEXT, contact_origin TEXT, stage TEXT,
        operator_note TEXT, created_at TEXT)""")
    legacy.execute("""INSERT INTO prospect_people (person_key, source, source_url, stated_need,
        lang, contact_methods, contact_status, permission_status, stage, dup_status)
        VALUES ('k1','reddit','https://r.example/1','I need coverage','en','[]',
                'CONTACT_NOT_AVAILABLE','NONE','DISCOVERED','UNIQUE')""")
    legacy.commit()
    assert prospector.migrate_legacy(con, legacy) == 1
    p = dict(con.execute("SELECT * FROM public_prospects").fetchone())
    assert p["status"] == "NEW_PROSPECT" and p["source"].startswith("legacy:")
    assert prospector.migrate_legacy(con, legacy) == 0        # idempotent


# ------------------------------------------------------------------ 7. provider-API search
def test_websearch_reads_result_pages_and_counts_real_contacts(tmp_path):
    """The search adapter only calls the provider API, then reads the result pages it points at.
    A page that publishes an address beside the ask is stored as a prospect with a contact; the
    sweep counters must report the actual email, not the page count."""
    import json as _json
    page = ("<html><body><nav>menu</nav><div class=\"messageBody\">I lost my job in March and "
            "my COBRA runs out soon. Can anyone help me find health insurance in Florida? Email me "
            "at ana.lopez@example.com and I will send the details.</div></body></html>")
    api = _json.dumps({"webPages": {"value": [
        {"url": "https://f.example/post/1", "name": "COBRA ending", "snippet": "need coverage"}]}})

    def api_fetcher(url, headers):
        assert "api.bing.microsoft.com" in url
        return api

    def page_fetcher(url, robots=True):
        if url.endswith("/robots.txt"):
            return "User-agent: *\nAllow: /\n"
        return page

    con = store(tmp_path)
    cfg = {"websearch": {"provider": "bing", "queries": ['"need health insurance" "email me"'],
                         "results_per_query": 5},
           "_api_fetcher": api_fetcher, "max_threads_per_run": 5}
    import os
    os.environ["BING_SEARCH_KEY"] = "test-key"
    try:
        out = discovery.run(con, sources=("websearch",), fetcher=page_fetcher, delay=0, cfg=cfg)
    finally:
        os.environ.pop("BING_SEARCH_KEY", None)
    assert out[0]["fetched"] == 1 and out[0]["new"] == 1
    assert out[0]["emails"] == 1 and out[0]["contact_rows"] == 1
    p = dict(con.execute("SELECT * FROM public_prospects").fetchone())
    assert p["contact_status"] == "CONTACT_AVAILABLE"
    assert p["source"] == "websearch" and p["source_url"] == "https://f.example/post/1"
    assert "ana.lopez@example.com" in p["contact_methods"]
    assert p["status"] != "QUALIFIED"
    run = dict(con.execute("SELECT * FROM discovery_runs").fetchone())
    assert run["emails"] == 1 and run["phones"] == 0 and run["contact_rows"] == 1
    assert con.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0


# ------------------------------------------------------------------ 8. Reddit public archive
def test_reddit_archive_reads_recency_listing_and_keeps_the_permalink(tmp_path):
    """The archive adapter asks for a plain recency listing (its full-text query times out), turns
    each post into a row with the PUBLIC permalink, and never fabricates an author."""
    body = json.dumps({"data": [
        {"id": "abc123", "title": "Lost my job, COBRA ends", "selftext": NEED,
         "author": "throwaway_f", "subreddit": "HealthInsurance", "created_utc": 1793000000},
        {"id": "gone", "title": "deleted", "selftext": "[removed]",
         "author": "x", "subreddit": "HealthInsurance", "created_utc": 1793000000},
    ]})
    res = discovery.reddit_archive(["HealthInsurance"], fetcher=fake_fetcher({"api/posts/search": body}),
                                   delay=0)
    assert res["fetched"] == 1                      # the removed post carries no text
    row = res["rows"][0]
    assert row["url"] == "https://www.reddit.com/comments/abc123"
    assert row["author"] == "throwaway_f" and row["platform"] == "reddit_archive"
    assert row["observed_at"].startswith("20") and row["community"] == "reddit:r/HealthInsurance"
    con = store(tmp_path)
    stats = prospector.ingest(con, res["rows"], "reddit_archive")
    assert stats["new"] == 1 and stats["emails"] == 0
    assert con.execute("SELECT COUNT(*) FROM public_prospects WHERE status='NEW_PROSPECT'").fetchone()[0] == 1


def test_agent_pitch_phrased_as_help_is_not_a_prospect():
    """Live sweep, 2026-10-30: a comment reading 'I help people find affordable health coverage
    through the ACA Marketplace, including $0/month plans for those who qualify.' was stored as a
    contact prospect. It is a licensed-sounding pitch, not a consumer."""
    pitch = ("I help people find affordable health coverage through the ACA Marketplace, including "
             "$0/month plans for those who qualify. Text me at (470) 737-3233.")
    r = prospector.classify(pitch)
    assert not r["need"] and "seller" in r["reason"]


def test_contact_first_capture_does_not_readmit_a_refused_seller(tmp_path):
    """Contact-first capture exists for the vague human ask, not for a seller. Without this guard
    the broker pitch below was stored twice, on two separate live sweeps."""
    con = store(tmp_path)
    pitch = ("Access Affordable Health Insurance. Georgia-licensed health insurance broker here! "
             "I help people find affordable health coverage through the ACA Marketplace, including "
             "$0/month plans. Text me at (470) 737-3233.")
    stats = prospector.ingest(con, rows({"url": "https://f.example/broker", "title": "", "text": pitch}), "s")
    assert stats["new"] == 0 and stats["contact_rows"] == 0
    assert con.execute("SELECT COUNT(*) FROM public_prospects").fetchone()[0] == 0


def test_a_quoted_third_party_number_is_not_the_author_contact():
    """Live sweep, 2026-10-30: a complaint post ('Received a call from 773 389 5697 and MEDICARE
    HELPLINE appeared on Caller ID') was stored with the spammer's number as the author's contact.
    Direction matters: a number somebody was CALLED FROM is not a published contact."""
    from engine import prospect_contacts as pc
    text = ("Medicare Helpline scam. Received a call from 773 389 5697 and MEDICARE HELPLINE appeared "
            "on Caller ID. I called back and they wanted my Medicare number. Be careful everyone.")
    ev = pc.extract_contacts(text)
    assert ev["phones"] == [] and ev["contact_status"] == "CONTACT_NOT_AVAILABLE"
    mine = "My COBRA ends next month and I need coverage. Text me at (305) 555-0142 please."
    assert pc.extract_contacts(mine)["phones"] == ["(305) 555-0142"]


def test_websearch_without_a_key_reports_the_gap_instead_of_scraping(tmp_path):
    src = Path(__file__).resolve().parents[1] / "engine" / "discovery.py"
    assert "captcha" in src.read_text(encoding="utf-8").lower()
    res = discovery.websearch(['"need health insurance"'], provider="bing", keys={})
    assert res["rows"] == [] and res["fetched"] == 0
    assert "no credential" in res["note"] and "BING_SEARCH_KEY" in res["note"]
