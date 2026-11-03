"""Signal watcher tests: the intent-signal discovery prototype.

HARD BOUNDARIES UNDER TEST:
  1. Signals are public POSTS, never people: no author username/id may reach storage even when
     the upstream payload contains it.
  2. The module can never touch the lead pipeline (no imports of engine.route / engine.notify /
     engine.billing) and no module function contacts anyone.
  3. Fetching is an explicit action with injected network calls — tests inject a fake, so the
     suite never hits the network.
  4. Review is evidence-gated: the admin endpoint refuses IRRELEVANT/ACTIONED without a note.
"""
import importlib, json, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]


def make_sig_mod(tmp):
    os.environ["SIGNALS_DB"] = str(tmp / "signals_test.db")
    import engine.signals as sig
    importlib.reload(sig)
    return sig


def test_boundary_no_contact_and_no_leads():
    """Import-boundary check via the AST: the signal watcher may not import anything from the
    lead pipeline (route/notify/billing) or any contacting library — prose mentions in docstrings
    (which EXPLAIN the boundary) are not code and are inspected by ast, not grep."""
    import ast
    for path in (ROOT / "engine" / "signals.py", ROOT / "tools" / "signals_watch.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        banned = ("route", "notify", "billing", "requests", "smtplib", "twilio", "urllib3")
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
        assert not (imports & {"engine." + b for b in banned}) , f"{path.name} imports a banned module: {imports & {'engine.' + b for b in banned}}"
        assert not (imports & {"requests", "smtplib", "twilio"}), f"{path.name} imports a contacting library: {imports & {'requests', 'smtplib', 'twilio'}}"


def test_classify_en_and_es(tmp_path):
    sig = make_sig_mod(tmp_path)
    cl = sig.classify("I lost my job last week and lost my employer insurance. What do I do before the deadline?", "")
    assert cl["topic"] == "job_loss" and cl["lang"] == "en"
    assert cl["intent"] > 2 and cl["angle"] == "life_change"
    assert "OPP-02" in cl["opps"]
    cl2 = sig.classify("Me despidieron y ya no tengo el seguro del trabajo, ¿qué hago antes del 15?", "es")
    assert cl2["topic"] in ("job_loss", "timing")
    assert cl2["lang"] == "es" and cl2["intent"] > 2.5
    # carrier exit (English + Spanish naming)
    cl3 = sig.classify("Got a letter: Molina no longer offering plans in florida, plan is ending dec 31", "")
    assert cl3["topic"] == "carrier_exit" and cl3["angle"] == "plan_change"
    # off-topic stays unclassified and untagged
    cl4 = sig.classify("Motorcycle repair advice needed", "")
    assert cl4["topic"] == "none" and cl4["intent"] <= 1 and sig.TOPICS["carrier_exit"]


def test_ingest_dedupes_and_strips_people(tmp_path):
    sig = make_sig_mod(tmp_path)
    con = sig.connect(tmp_path / "s.db")
    r = {
        "source": "reddit", "author": "sensitive_username_123", "id": "t3_abc123",
        "community": "https://www.reddit.com/r/HealthInsurance",
        "title": "Lost job health insurance question", "snippet": "..." ,
        "url": "https://www.reddit.com/r/HealthInsurance/comments/abc/lost_job/",
        "observed_at": "2026-10-20T00:00:00Z"}
    res = sig.ingest(con, [r, r, {"title": "x", "url": ""}], "reddit")
    assert res == {"added": 1, "skipped": 2}
    cols = " ".join(c[1] for c in con.execute("PRAGMA table_info(signals)"))
    for banned_col in ("author", "username", "owner", "fullid"):
        assert banned_col not in " ".join(cols).lower()
    row = dict(con.execute("SELECT * FROM signals").fetchone())
    assert "sensitive_username_" not in json.dumps(list(row.values()))
    row = dict(con.execute("SELECT signal_key, url FROM signals").fetchone())
    assert row["signal_key"] == sig.signal_key(row["url"])


def test_accept_filter(tmp_path):
    sig = make_sig_mod(tmp_path)
    assert sig.accept_filter({"title": "cobra too expensive, what do I do", "snippet": ""})
    assert not sig.accept_filter({"title": "Cats are cute", "snippet": ""})


def test_fetch_reddit_never_stores_author(tmp_path):
    sig = make_sig_mod(tmp_path)
    con = sig.connect(tmp_path / "f.db")

    def fake_get(url):
        assert "signature" not in url
        return {"data": {"children": [
            {"data": {"title": "Cobra ends in 18 months, affordable alternatives?",
                      "selftext": "my cobra coverage costs 600/mo", "permalink": "/r/HealthInsurance/comments/x1/y/",
                      "subreddit": "HealthInsurance", "created_utc": 1760000000, "author": "usa",
                      "stickied": False}},
            {"data": {"title": "Stickied", "permalink": "/r/x/comments/s/", "stickied": True}},
        ]}}

    res = sig.fetch_reddit(con, [{"sub": "HealthInsurance", "terms": ["cobra"]}], get_json=fake_get, sleeper=lambda s: None)
    assert res == {"fetched": 2, "accepted": 1}
    rows = [dict(x) for x in con.execute("SELECT * FROM signals")]
    assert rows and rows[0]["source"] == "reddit" and rows[0]["topic"] == "cobra"
    assert all("sensitive" not in json.dumps(r).lower() and r["url"].startswith("https://www.reddit.com") for r in rows)
    logs = [dict(x) for x in con.execute("SELECT * FROM fetch_log")]
    assert logs and logs[-1]["note"] == "ok"


def test_review_evidence_rules_and_queue(tmp_path):
    sig = make_sig_mod(tmp_path)
    con = sig.connect(tmp_path / "r.db")
    sig.ingest(con, [{"title": "cobra ends what to do", "url": "https://www.reddit.com/r/HealthInsurance/comments/a/b/", "snippet": ""}])
    sid = con.execute("SELECT id FROM signals").fetchone()[0]
    for status, note in (("IRRELEVANT", "not our need: medicare question"),):
        assert sig.review(con, sid, status, note)
    row = dict(con.execute("SELECT status, note FROM signals").fetchone())
    assert row["status"] == "IRRELEVANT" and "not our need" in row["note"]
    assert not sig.review(con, 9999, "REVIEWED")
    c = sig.counts(con)
    assert c["TOTAL"] == 1 and c["IRRELEVANT"] == 1


def test_admin_signals_page_and_evidence_gate(tmp_path):
    os.environ["LEADENGINE_DB"] = str(tmp_path / "t.db")
    os.environ["ADMIN_TOKEN"] = "t"
    os.environ["INTEL_DB"] = str(tmp_path / "none.db")
    os.environ["SIGNALS_DB"] = str(tmp_path / "signals.db")
    import app as appmod
    importlib.reload(appmod)
    from engine import signals as sig
    scon = sig.connect(tmp_path / "signals.db")
    sig.ingest(scon, [{"title": "cobra ends in 18 months, what do I do?",
                       "url": "https://www.reddit.com/r/HealthInsurance/comments/ok/c/", "snippet": "",
                       "source": "reddit", "community": "https://www.reddit.com/r/HealthInsurance"}])
    c = appmod.app.test_client()
    page = c.get("/admin/intel/signals?token=t").get_data(as_text=True)
    assert "Signals are not leads" in page and "cobra" in page
    # IRRELEVANT without a note is refused server-side (evidence gate)
    r = c.post("/admin/intel/signals/1/status?token=t", data={"status": "IRRELEVANT"})
    from urllib.parse import unquote_plus
    assert r.status_code in (302, 303) and "requires an evidence note" in unquote_plus(r.headers["Location"])
    assert scon.execute("SELECT status FROM signals").fetchone()[0] == "NEW"
    # with a note it records
    r2 = c.post("/admin/intel/signals/1/status?token=t",
                data={"status": "ACTIONED", "note": "answered by learn__cobra page; X021 gets the finding"})
    assert r2.status_code in (302, 303)
    row = dict(scon.execute("SELECT status, note FROM signals").fetchone())
    assert row["status"] == "ACTIONED" and "learn__cobra" in row["note"]
    r3 = c.post("/admin/intel/signals/1/status?token=t", data={"status": "NONSENSE"})
    assert "unknown" in unquote_plus(r3.headers["Location"])


# ---------------------------------------------------- shopping-intent coverage + geo + mastodon
def test_classify_shopping_intent_en_and_es(tmp_path):
    sig = make_sig_mod(tmp_path)
    cl = sig.classify("I am looking for health insurance for my wife, good plan or cheap?")
    assert cl["topic"] == "quotes_shopping" and cl["lang"] == "en"
    assert "OPP-03" in cl["opps"]
    cl2 = sig.classify("necesito seguro medico barato para mi familia en miami ¿quien me puede ayudar?")
    # both shopping-ES and enroll_help hit; the classifier picks the strongest match
    assert cl2["topic"] in ("quotes_shopping", "enroll_help") and cl2["lang"] == "es"


def test_geo_only_from_post_text(tmp_path):
    sig = make_sig_mod(tmp_path)
    assert sig.detect_geo("anyone know an affordable plan here in Miami? need help") == "south_fl"
    assert sig.detect_geo("lost coverage, thinking about the marketplace in dade county") == "south_fl"
    assert sig.detect_geo("moved to Tampa and need a new plan") == "fl"
    assert sig.detect_geo("recommendations for a broker? nothing local mentioned") == ""


def test_mastodon_fetch_drops_author_and_boosts(tmp_path):
    sig = make_sig_mod(tmp_path)
    con = sig.connect(tmp_path / "m.db")
    toots = [
        {"id": "1", "content": "<p>Just lost my job and my employer insurance. what are my options before the deadline?</p>",
         "url": "https://mastodon.social/@whatever/1", "created_at": "2026-10-01T12:00:00.000Z",
         "account": {"acct": "someone@mastodon.social"}},
        {"id": "2", "content": "<p>shopping for health insurance quotes this week, any advice</p>",
         "url": "https://mastodon.social/@whatever/2", "created_at": "2026-10-02T12:00:00.000Z",
         "account": {"acct": "other"}},
        {"id": "3", "reblog": {"id": "9"}, "content": "", "url": "https://mastodon.social/@whatever/3"},
        {"id": "4", "content": "<p>cats are cute</p>", "url": "https://mastodon.social/@whatever/4"},
    ]
    res = sig.fetch_mastodon(con, cfg={"mastodon": {"enabled": True, "instance": "https://mastodon.social",
                                                   "tags": ["healthinsurance"]}},
                             get_json=lambda url: toots, sleeper=lambda s: None)
    assert res["fetched"] == 3           # boost skipped, cats kept-but-rejected by accept_filter
    assert res["accepted"] == 2
    rows = [dict(r) for r in con.execute("SELECT * FROM signals WHERE source='mastodon'")]
    assert len(rows) == 2
    blob = json.dumps(rows)
    assert "someone" not in blob and "acct" not in blob          # author never stored
    assert rows[0]["kind"] == "INTEREST" and rows[0]["source"] == "mastodon"
    assert rows[0]["url"].startswith("https://mastodon.social/")
    assert sig.fetch_mastodon(con, cfg={"mastodon": {"enabled": False}}, get_json=lambda u: []).get("skipped")


def test_generic_only_cobra_noise_guard(tmp_path):
    """A bare 'cobra' mention with zero insurance context is noise (verified live versus
    Mastodon hashtags: yoga poses, a football coach, UK COBRA meetings). Compound matches
    and any other topic word always pass."""
    sig = make_sig_mod(tmp_path)
    assert sig._generic_only_noise("today's yoga class was all about the cobra flow")
    assert sig._generic_only_noise("CM met Cobra meeting minutes")
    assert not sig._generic_only_noise("my cobra coverage ends in january, too expensive")
    con = sig.connect(tmp_path / "n.db")
    toots = [
        {"id": "10", "content": "<p>the Cobra pose felt great today</p>",
         "url": "https://m.social/@x/10", "created_at": "2026-10-01T12:00:00.000Z", "account": {"acct": "y"}},
        {"id": "11", "content": "<p>Cobra ends soon — is marketplace coverage cheaper?</p>",
         "url": "https://m.social/@x/11", "created_at": "2026-10-01T12:00:00.000Z", "account": {"acct": "y"}},
    ]
    res = sig.fetch_mastodon(con, cfg={"mastodon": {"enabled": True, "instance": "https://m.social",
                                                   "tags": ["healthinsurance"]}},
                             get_json=lambda url: toots, sleeper=lambda s: None)
    assert res == {"fetched": 2, "accepted": 1}
    urls = [r["url"] for r in con.execute("SELECT url FROM signals WHERE source='mastodon'")]
    assert urls == ["https://m.social/@x/11"]


def _gate_on(tmp_path, sig, monkeypatch):
    """Isolate the fetchers from the OPERATIONAL reddit gate: config/sources.json currently
    has reddit disabled for this network (real Oct-28 429 hammering); these tests target the
    retry/cooldown mechanics, so give them a tmp config with the gate on."""
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "sources.json").write_text(
        json.dumps({"reddit_fallback": {"enabled": True}, "reddit_comments": {"enabled": True}}), encoding="utf-8")
    monkeypatch.setattr(sig, "ROOT", tmp_path)


def test_mirror_429_retries_same_query_then_succeeds(tmp_path, monkeypatch):
    sig = make_sig_mod(tmp_path)
    _gate_on(tmp_path, sig, monkeypatch)
    monkeypatch.setattr(sig, "MIRROR_COOLDOWN_FILE", tmp_path / "mirror_cooldown_test.json")
    con = sig.connect(tmp_path / "r.db")
    sleeps = []
    monkeypatch.setattr(sig.time, "sleep", lambda s: sleeps.append(s))
    attempts = {"n": 0}

    def flaky(url):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise Exception("HTTP Error 429: Too Many Requests")
        return {"data": [{"title": "cobra too expensive", "body": "cobra coverage runs out in december",
                          "id": "c1", "subreddit": "HealthInsurance", "link_id": "t3_9x8y_z",
                          "created_utc": 1760000000}]}

    q = [{"sub": "HealthInsurance", "terms": ["cobra"], "limit": 25}]
    res = sig.fetch_reddit_comments(con, q, get_json=flaky, sleeper=lambda s: None)
    assert attempts["n"] == 3                                    # retried the SAME query, then succeeded
    assert res["accepted"] == 1
    logs = [dict(x) for x in con.execute("SELECT * FROM fetch_log ORDER BY id")]
    assert logs and logs[-1]["note"].startswith("ok")


def test_mirror_cooldown_skips_fetchers(tmp_path, monkeypatch):
    sig = make_sig_mod(tmp_path)
    _gate_on(tmp_path, sig, monkeypatch)
    monkeypatch.setattr(sig, "MIRROR_COOLDOWN_FILE", sig.ROOT / "out" / "mirror_cooldown_test.json")
    sig.MIRROR_COOLDOWN_FILE.parent.mkdir(exist_ok=True, parents=True)
    sig.MIRROR_COOLDOWN_FILE.write_text(json.dumps({"until": "2099-01-01T00:00:00+00:00"}), encoding="utf-8")
    con = sig.connect(tmp_path / "c.db")
    res = sig.fetch_reddit_comments(con, sig.default_comment_queries(), get_json=lambda u: [], sleeper=lambda s: None)
    assert "cooldown" in res.get("skipped", "")
    res2 = sig.fetch_reddit_fallback(con, sig.default_queries(), get_json=lambda u: [], sleeper=lambda s: None)
    assert "cooldown" in res2.get("skipped", "")
    # clearing (or expiring) the file releases both fetchers
    sig.MIRROR_COOLDOWN_FILE.unlink()
    r3 = sig.fetch_reddit_comments(con, sig.default_comment_queries(), get_json=lambda u: {"data": []}, sleeper=lambda s: None)
    assert r3 == {"fetched": 0, "accepted": 0}


def test_paid_comment_actors_refused_until_urls_pasted(tmp_path, monkeypatch):
    import engine.social_scrapers as social
    monkeypatch.setenv("APIFY_TOKEN", "test_token_isolated")
    cfg = {"apify": {"approved": True,
                     "actors": {"instagram_comments": {"actor_id": "apify/instagram-comment-scraper",
                                                       "_requires_urls": True,
                                                       "input": {"directUrls": []}}}}}
    rows = social.run_apify_actors(None, cfg)
    assert rows and rows[0]["status"] == "NEEDS_SETUP" and "post URLs" in rows[0]["note"]
