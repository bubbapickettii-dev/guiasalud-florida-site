"""Tests for the person-shaped discovery expansion (Oct 2026):
comment/Q&A collectors + the double-gated paid service adapters.
Boundaries under test:
  1. New sources classify as INTEREST exactly like reddit rows (comments are person-shaped).
  2. Author fields NEVER reach storage from comment payloads (mirror/Algolia/Stack return them).
  3. 429 pauses + error rows instead of crashes.
  4. Paid adapters do ZERO network without operator approval AND a credential — fakes raise
     if contacted (proves the gate), and ingest only when both gates + results allow.
"""
import importlib, json, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def make_sig_mod(tmp):
    os.environ["SIGNALS_DB"] = str(tmp / "signals_test.db")
    import engine.signals as sig
    importlib.reload(sig)
    return sig


def _con(tmp, sig):
    return sig.connect(tmp / "s.db")


def test_kind_rule_covers_comments_and_gates(tmp_path):
    """demand_kind: comment/Q&A sources with a topic -> INTEREST; without topic -> MARKET.
    The paid-service rows follow the same taxonomy filter so they're INTEREST too."""
    sig = make_sig_mod(tmp_path)
    for src in ("reddit_comments", "stackexchange", "hn_comments", "apify", "brightdata"):
        assert sig.demand_kind(src, "cobra") == "INTEREST", src
        assert sig.demand_kind(src, "none") == "MARKET", src
    assert sig.demand_kind("google_news", "carrier_exit") == "MARKET"


def _ingest_one(con, sig, payload, source):
    res = sig.ingest(con, [payload], source)
    assert res["added"] == 1, res
    row = dict(con.execute("SELECT * FROM signals").fetchone())
    return row


def test_reddit_comment_clean_and_ingest(tmp_path):
    sig = make_sig_mod(tmp_path)
    con = _con(tmp_path, sig)
    comment = {"id": "abc123", "link_id": "t3_xyz456", "subreddit": "HealthInsurance",
               "body": "I lost my job last month, Cobra ends in January, what are the affordable options?",
               "author": "personal_handle", "score": 3, "created_utc": 1761000000}
    cleaned = sig._clean_reddit_comment(comment)
    assert cleaned["source"] == "reddit_comments"
    assert cleaned["url"].startswith("https://www.reddit.com/r/HealthInsurance/comments/xyz456/_/abc123")
    assert cleaned["collection_method"] == "public_mirror_comment"
    assert "personal_handle" not in json.dumps(cleaned)
    row = _ingest_one(con, sig, cleaned, "reddit_comments")
    assert row["kind"] == "INTEREST" and row["topic"] in ("job_loss", "cobra")
    deleted = sig._clean_reddit_comment({"id": "d", "link_id": "t3_x", "subreddit": "r",
                                         "body": "[deleted]"})
    assert deleted is None
    fac = sig.facets(con)
    assert "reddit_comments" in fac["sources"] and "INTEREST" in fac["kinds"]


def test_fetch_reddit_comments_429_pause_and_error_rows(tmp_path, monkeypatch):   # noqa: E302 - patched sleep below
    sig = make_sig_mod(tmp_path)
    # isolate from the LIVE mirror cooldown: a real network 429 earlier today wrote out/mirror_cooldown.json
    monkeypatch.setattr(sig, "MIRROR_COOLDOWN_FILE", tmp_path / "mirror_cooldown_test.json")
    con = _con(tmp_path, sig)
    class Boom(Exception):
        pass
    calls = []
    def fake_get(url):
        calls.append(url)
        if len(calls) == 1:
            raise Boom("HTTP Error 429: Too Many Requests")
        return {"data": [{"id": "c2", "link_id": "t3_x2", "subreddit": "money",
                          "body": "can't afford marketplace plan after subsidy? what do i do",
                          "created_utc": 1761000000, "author": "handle2"}]}
    sleeps = []
    retry_sleeps = []
    monkeypatch_sleep = __import__("unittest.mock", fromlist=["_x"]).patch.object(sig.time, "sleep", retry_sleeps.append)
    queries = [{"sub": "money", "terms": ["marketplace plan"]},
               {"sub": "HealthInsurance", "terms": ["cobra"]}]
    with monkeypatch_sleep:
        res = sig.fetch_reddit_comments(con, queries,
                                        get_json=fake_get, sleeper=sleeps.append)
    assert res == {"fetched": 2, "accepted": 2}
    assert len(calls) == 3                       # first query retried ONCE after its 429, then succeeded
    assert sleeps[0] == sig.MIRROR_PAUSE_S       # paced BEFORE the request (mirror burst discipline)
    assert sig.MIRROR_RETRY_BASE_S in retry_sleeps   # the 429 backoff pause happened between retries
    assert sleeps[-1] == sig.REQUEST_PAUSE_S     # normal polite pause between queries
    logs = [dict(x) for x in con.execute("SELECT source, note FROM fetch_log ORDER BY id")]
    assert logs[-1]["source"] == "reddit_comments"
    assert con.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 1   # both queries matched the SAME fake comment URL: dedupe keeps one
    assert "handle2" not in json.dumps([dict(r) for r in con.execute("SELECT * FROM signals")])


import time as _time

def test_stackexchange_and_hn_comments(tmp_path):
    sig = make_sig_mod(tmp_path)
    con = _con(tmp_path, sig)
    def fake_stack(url):
            assert "api.stackexchange.com" in url and "site=health.stackexchange" in url
            return {"items": [                 {"item_type": "question", "title": "How to navigate &lt;b&gt;COBRA&lt;/b&gt; after layoff?",
                  "excerpt": "cobra too expensive; which marketplace plan the subsidies make worth it?",
                  "question_id": 9, "creation_date": int(_time.time()) - 3600},
                {"item_type": "answer", "title": "random answer text without keywords",
                 "excerpt": "banana pancakes on a saturday", "question_id": 7, "answer_id": 2}],
                "quota_remaining": 277}
    sleeps = []
    res = sig.fetch_stackexchange(con, sites_queries={"health": ["cobra"]},
                                  get_json=fake_stack, sleeper=sleeps.append)
    assert res["accepted"] == 1 and res["fetched"] == 2
    rows = [dict(r) for r in con.execute("SELECT * FROM signals")]
    assert rows[0]["source"] == "stackexchange" and rows[0]["kind"] == "INTEREST"
    assert rows[0]["url"] == "https://health.stackexchange.com/questions/9"
    assert rows[0]["observed_at"].startswith("20")          # fresh by the recency filter
    assert "<b>" not in rows[0]["title"] and "COBRA" in rows[0]["title"]
    served_rows = [json.dumps(dict(r)) for r in con.execute("SELECT * FROM signals")]
    assert all("getitem" not in json.dumps(j) for j in served_rows)
    # HN comments: serve a story comment with a topic + an off-topic one
    def fake_hn(url):
        assert "tags=comment" in url
        return {"hits": [
            {"objectID": "33110010", "story_title": "Show HN: Health insurance marketplace",
             "comment_text": "I lost my job, looking at cobra plans now; the subsidies matter a lot",
             "created_at": "2026-10-20T00:00:00Z", "author": "hn_user"},
            {"objectID": "33110011", "comment_text": "TypeScript compiler perf is great",
             "story_title": "", "created_at": "2026-10-21T00:00:00Z", "author": "dev"}]}
    hc = sig.fetch_hn_comments(con, "health insurance", get_json=fake_hn)
    assert hc == {"fetched": 2, "accepted": 1}
    hnrows = [dict(r) for r in con.execute("SELECT * FROM signals WHERE source='hn_comments'")]
    assert (len(hnrows) == 1 and hnrows[0]["kind"] == "INTEREST"
            and hnrows[0]["topic"] in ("job_loss", "cobra"))
    assert all("hn_user" not in json.dumps(dict(r)) for r in con.execute("SELECT * FROM signals WHERE source='hn_comments'"))
    assert len(sleeps) >= 1    # one polite pause per query, no hammering


def test_paid_gates_zero_network_until_approved_and_credentialed(tmp_path, monkeypatch):
    sig = make_sig_mod(tmp_path)
    from engine import social_scrapers as social
    con = _con(tmp_path, sig)
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    monkeypatch.delenv("BD_DATA_API_TOKEN", raising=False)
    cfg = {"apify": {"approved": False, "actors": {}},
           "brightdata": {"approved": False, "datasets": {}}}
    # a contacted network means the test FAILED: raise loudly if so
    def refuse(url, *a, **k):
        raise AssertionError("network contacted while gates closed: " + str(url)[:120])
    rows = social.run_apify_actors(con, cfg, fetch_json=refuse)
    assert rows and rows[0]["status"] == "NEEDS_APPROVAL"
    rows = social.run_brightdata_datasets(con, cfg, trigger=refuse, poll=refuse)
    assert rows and rows[0]["status"] == "NEEDS_APPROVAL"
    # approval without credential: same zero network
    cfg2 = {"apify": {"approved": True, "actors": {"instagram_hashtag": {"actor_id": "a/b", "input": {}}}},
            "brightdata": {"approved": True, "datasets": {"instagram": {"dataset_id": "x", "inputs": []}}}}
    rows = social.run_apify_actors(con, cfg2, fetch_json=refuse)
    assert rows[0]["status"] == "NEEDS_CREDENTIALS" and "APIFY_TOKEN" in rows[0]["note"]
    rows = social.run_brightdata_datasets(con, cfg2, trigger=refuse, poll=refuse)
    assert rows[0]["status"] == "NEEDS_CREDENTIALS" and "BD_DATA_API_TOKEN" in rows[0]["note"]


def test_paid_apify_run_ingests_person_shaped_rows(tmp_path, monkeypatch):
    sig = make_sig_mod(tmp_path)
    from engine import social_scrapers as social
    con = _con(tmp_path, sig)
    monkeypatch.setenv("APIFY_TOKEN", "tok-test")
    def fake_run(url, method=None, body=None, bearer=None):
        assert "apify.com" in url and method == "POST" and bearer == "tok-test"
        return [
            {"url": "https://www.instagram.com/p/abc123/", "caption": "just lost my job… where do I get seguro medico in miami? help",
             "owner_username": "should_not_be_stored", "createdAt": "2026-10-21T06:00:00.000Z",
             "commentsCount": 4},
            {"url": "https://www.instagram.com/p/def456/", "caption": "new makeup launch!!! #sponsored",
             "createdAt": "2026-10-21T07:00:00Z"},
            {"caption": "no permalink, dropped"},
        ]
    cfg = {"apify": {"approved": True,
                     "actors": {"instagram_hashtag": {"actor_id": "apify/instagram-hashtag-scraper",
                                                      "input": {"hashtags": ["seguro medico"]},
                                                      "label": "#seguro medico"}}}}
    rows = social.run_apify_actors(con, cfg, fetch_json=fake_run)
    assert rows and rows[0]["status"] == "ok" and rows[0]["added"] == 1 and rows[0]["fetched"] == 3
    stored = [dict(r) for r in con.execute("SELECT * FROM signals")]
    assert len(stored) == 1
    assert stored[0]["source"] == "apify"
    assert stored[0]["kind"] == "INTEREST"      # taxonomy topic ran => person-shaped
    assert "should_not_be_stored" not in json.dumps(stored)
    assert stored[0]["collection_method"] == "paid_service_api"
    assert stored[0]["access_status"] == "licensed_service"


def test_paid_brightdata_run(tmp_path, monkeypatch):
    sig = make_sig_mod(tmp_path)
    from engine import social_scrapers as social
    con = _con(tmp_path, sig)
    monkeypatch.setenv("BD_DATA_API_TOKEN", "bd-tok")
    triggered = {}
    def fake_trigger(dataset_id, inputs, token):
        assert dataset_id == "gd_dsid" and token == "bd-tok"
        triggered["ok"] = True
        return "snap1"
    def fake_poll(snapshot_id, token, sleeper):
        assert snapshot_id == "snap1"
        return [{"url": "https://www.tiktok.com/@user/video/777", "description": "cumplo 26 y ya no tengo el seguro de mis papas, ¿cómo me inscribo? #obamacare",
                 "timestamp": 1760000000, "author_unique_id": "leave_this_out"}]
    cfg = {"brightdata": {"approved": True,
                          "datasets": {"tiktok": {"dataset_id": "gd_dsid", "label": "tiktok",
                                                  "inputs": [{"hashtag": "seguro medico"}]}}}}
    sleeps = []
    rows = social.run_brightdata_datasets(con, cfg, trigger=fake_trigger, poll=fake_poll,
                                          sleeper=sleeps.append)
    assert triggered.get("ok") and rows and rows[0]["status"] == "ok" and rows[0]["added"] == 1
    row = dict(con.execute("SELECT * FROM signals").fetchone())
    assert row["kind"] == "INTEREST" and "cumplo" in row["title"]  # ES timing topic matched


def _recent_iso(hours=1):
    import datetime as _dt
    return (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=hours)).isoformat(timespec="seconds").replace("+00:00", "Z")


def test_bluesky_clean_ingest_author_free(tmp_path):
    """Bluesky anonymous AppView search: text + durable DID permalink only; poster
    handle/counts never stored; taxonomy topic => kind=INTEREST; recency via since."""
    sig = make_sig_mod(tmp_path)
    con = _con(tmp_path, sig)
    calls = []
    def fake_get(url):
        calls.append(url)
        assert "app.bsky.feed.searchPosts" in url and "sort=latest" in url
        assert "since=" in url                                  # recency at the SOURCE
        return {"posts": [
            {"uri": "at://did:plc:abc123/app.bsky.feed.post/3rkk99",
             "record": {"text": "just got laid off — lost my benefits, need health insurance this month, any recommendations?",
                        "created_at": _recent_iso()},
             "author": {"handle": "drop.me.bsky"}, "likeCount": 9},
            {"uri": "at://did:plc:abc123/app.bsky.feed.post/3rkk98",
             "record": {"text": "shopping for health insurance quotes this week"},
             "author": {"handle": "drop.me2.bsky"}},
            {"uri": "https://not-an-at-uri.example/nope", "record": {"text": "bad shape, dropped"}},
        ]}
    res = sig.fetch_bluesky(con, cfg={"bluesky": {"enabled": True,
                                                  "queries": ['"health insurance"']}},
                            get_json=fake_get, sleeper=lambda s: None)
    assert res == {"fetched": 2, "accepted": 2}    # malformed URI dropped pre-count
    rows = [dict(r) for r in con.execute("SELECT * FROM signals WHERE source='bluesky'")]
    assert len(rows) == 2 and all(r["kind"] == "INTEREST" for r in rows)
    assert rows[0]["url"].startswith("https://bsky.app/profile/did:plc:abc123/post/")
    blob = json.dumps(rows)
    assert "drop.me" not in blob and "likeCount" not in blob and "handle" not in blob
    assert sig.demand_kind("bluesky", "cobra") == "INTEREST"   # INTEREST_SOURCES includes bluesky
    assert sig.fetch_bluesky(con, cfg={"bluesky": {"enabled": False}},
                             get_json=lambda u: []).get("skipped")


def test_bluesky_generic_cobra_noise_guarded(tmp_path):
    sig = make_sig_mod(tmp_path)
    con = _con(tmp_path, sig)
    res = sig.fetch_bluesky(con, cfg={"bluesky": {"enabled": True, "queries": [{"q": "test"}]}},
                            get_json=lambda u: {"posts": [
                                {"uri": "at://did:x/app.bsky.feed.post/r1",
                                 "record": {"text": "the cobra pose in yoga class today", "created_at": _recent_iso()}}]},
                            sleeper=lambda s: None)
    assert res == {"fetched": 1, "accepted": 0}                 # guarded like mastodon


def test_lemmy_clean_ingest_author_free(tmp_path):
    sig = make_sig_mod(tmp_path)
    con = _con(tmp_path, sig)
    calls = []
    def fake_get(url):
        calls.append(url)
        assert "api/v3/search" in url and "type_=Comments" in url and "sort=New" in url
        return [{"comment": {"content": "&lt;p&gt;can't afford marketplace plan after subsidy, what do i do?&lt;/p&gt;",
                             "ap_id": "https://lemmy.world/comment/12345",
                             "published": _recent_iso() + "+00:00"},
                 "community": {"name": "personal_finance"},
                 "creator": {"actor_id": "https://lemmy.world/u/dropme"}},
                {"comment": {"content": "banana pancakes", "ap_id": "https://lemmy.world/comment/12346"}},
                {"comment": {"content": "no permalink case", "ap_id": "https://lemmy.world/post/7"}}]
    res = sig.fetch_lemmy(con, cfg={"lemmy": {"enabled": True, "instances": ["https://lemmy.world"],
                                              "queries": [{"q": "test"}]}},
                          get_json=fake_get, sleeper=lambda s: None)
    assert res == {"fetched": 2, "accepted": 1}
    rows = [dict(r) for r in con.execute("SELECT * FROM signals WHERE source='lemmy'")]
    assert len(rows) == 1 and rows[0]["kind"] == "INTEREST"
    assert rows[0]["url"] == "https://lemmy.world/comment/12345"
    assert rows[0]["community"] == "personal_finance"
    blob = json.dumps(rows)
    assert "dropme" not in blob and "actor_id" not in blob
    assert sig.demand_kind("lemmy", "cobra") == "INTEREST"


def test_recency_filters_hn_pullpush_stackexchange(tmp_path, monkeypatch):
    """Recency is enforced at the SOURCE (Oct audit: 92 of 104 INTEREST rows were archive
    pages, 2014-2024): HN numericFilters, PullPush after epoch, StackExchange stale skip."""
    sig = make_sig_mod(tmp_path)
    # isolate from the LIVE mirror cooldown — patch AFTER the reload a second reset applied
    # (this morning's real 429 wrote out/mirror_cooldown.json; pullpush is a mirror fetcher)
    monkeypatch.setattr(sig, "MIRROR_COOLDOWN_FILE", tmp_path / "mirror_cooldown_test.json")
    con = _con(tmp_path, sig)
    min_ep = int(sig._min_created_epoch())
    def _near(url, marker):
        """epoch cutoff in `url` within 60s of the test's own snapshot (the source computes
        its own cutoff at request time; exact int equality would be a clock race)."""
        unq = __import__("urllib.parse", fromlist=["unquote"]).unquote(url)
        assert marker in unq
        return abs(int(unq.split(marker)[1]) - min_ep) < 60
    # HN comments: numericFilters in the exact request
    def hn_get(url):
        assert _near(url, "created_utc_i>")
        return {"hits": [{"comment_text": "can't afford marketplace plan after subsidy, what do i do",
                          "story_title": "st", "objectID": "1",
                          "created_at": _recent_iso()}]}
    assert sig.fetch_hn_comments(con, "health", get_json=hn_get)["accepted"] == 1
    # pullpush comment search: after=<epoch> in the request params
    def pp_get(url):
        assert _near(url, "after=")
        return {"data": [{"id": "c1", "link_id": "t3_x", "subreddit": "r",
                          "body": "looking for health insurance quotes for my family",
                          "created_utc": min_ep + 10, "author": "a1"}]}
    sleeps = []
    assert sig.fetch_reddit_comments(con, [{"terms": ["health insurance quotes"]}],
                                     get_json=pp_get, sleeper=sleeps.append)["accepted"] == 1
    # stackexchange: stale creation rows are SKIPPED entirely (not even fetched-counted)
    old = int(_time.time()) - 400 * 86400
    def se_get(url):
        return {"items": [
            {"item_type": "question", "title": "cobra after layoff?",
             "excerpt": "cobra too expensive", "question_id": 5, "creation_date": old},
            {"item_type": "answer", "title": "banana pancakes",
             "excerpt": "banana pancakes on saturday", "question_id": 6, "creation_date": old},
        ]}
    res = sig.fetch_stackexchange(con, sites_queries={"money": ["cobra"]},
                                  get_json=se_get, sleeper=lambda s: None)
    assert res == {"fetched": 0, "accepted": 0}
    # and _fresh() guards non-source rows too
    assert sig._fresh(None) and sig._fresh("")
    assert not sig._fresh("2014-04-04T00:00:00Z")


# ---------------------------------------------------------------- intent CATEGORY (new)
def test_intent_category_shapes_from_live_review():
    """The category split was tuned against the actual 73-sample operator review: completed
    enrollment outranks shopping phrasing, announcements with a link taper into news, a bare
    '?' is a rhetorical aside (not a question), and an insurance-free post is irrelevant."""
    import engine.signals as sig
    cases = [
        ("I did my open enrollment for insurance I've been putting off", "enrolled"),
        ("Due to a miscommunication I missed my opportunity to signup for insurance through my job. Why is that?", "shopping"),
        ("Health Insurance open enrollment period is HERE & my head is spinning; lotta options", "shopping"),
        ("Open Enrollment season is here! Residents can find affordable coverage https://ma.gov", "news"),
        ("Need health insurance? Here's how 2027 ACA options may affect immigrants", "news"),
        ("Can HealthCare.gov force-change my plan variant mid-year due to an income increase?", "question"),
        ("How do you determine the SLCSP if there are no Silver plans in your area?", "question"),
        ("Charging $50/hr as a freelancer? After taxes, health insurance, that's not $104k", "generic"),
        ("Won't someone PLEASE think of the billion dollar health insurance companies!", "generic"),
        ("Trainingszentrum Maritime Schiffssicherheit: neues Schneidloeschsystem", "irrelevant"),
        ("busco un seguro medico para mi familia, donde puedo encontrar planes asequibles", "shopping"),
        ("ya tengo mi seguro medico nuevo, gracias", "enrolled"),
    ]
    for text, want in cases:
        assert sig.intent_category(text) == want, text[:60]


def test_intent_cat_news_curly_apostrophe():
    """Real Bluesky row #260 (Oct 29 sweep): a publisher link with a CURLY apostrophe in
    "Here’s how" slipped past the news shapes into shopping. Curly quotes must normalize."""
    sig = make_sig_mod(Path("/tmp"))
    body = "Need health insurance? Here\u2019s how 2027 ACA options may affect immigrants and more - The Haitian Times"
    assert sig.intent_category(body, "en") == "news"
    assert sig.intent_category(body.replace("\u2019", "'"), "en") == "news"


def test_ingest_stores_intent_cat_and_filters_by_it(tmp_path):
    import engine.signals as sig
    con = sig.connect(tmp_path / "sig.db")
    try:
        con.execute("DELETE FROM signals")
        sig.ingest(con, [
            {"source": "bluesky", "title": "I missed my signup deadline for open enrollment at work, why?",
             "snippet": "can I still get covered this year", "url": "https://x.example/1"},
            {"source": "bluesky", "title": "Open Enrollment season is here! affordable coverage https://x.example",
             "snippet": "state residents can check options", "url": "https://x.example/2"},
            {"source": "bluesky", "title": "banana pancakes recipe for saturday",
             "snippet": "T sweated my chubbies all week for this", "url": "https://x.example/3"},
        ])
        rows = {r["intent_cat"]: r for r in sig.queue_filtered(con, "NEW", kind="INTEREST")}
        assert "shopping" in rows and "news" in rows
        only_shop = sig.queue_filtered(con, "NEW", kind="INTEREST", intent_cat="shopping", limit=50)
        assert [r["intent_cat"] for r in only_shop] == ["shopping"]
    finally:
        con.close()
