"""Acquisition collection plumbing: the run-all sweep + the Google Places adapter path.

All tests are OFFLINE (injected network fetchers; no real HTTP). Guardrails exercised:
  - the sweep only collects what the runtime registry marks COLLECTS, reports every skipped
    source honestly (no channel config → youtube skipped; no credentials → adapters refuse);
  - dedupe: a second identical sweep must NOT duplicate stored signals;
  - every fetch outcome lands in fetch_log (visible later on /admin/integrations);
  - fetch_places: per-query error isolation, counters, return dict, collect_log rows,
    the double gate (API key + places.approved) still in force.
"""
import os
import sys
import tempfile
import shutil
from pathlib import Path

import pytest
import time as _t

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import signals as sig                 # noqa: E402
from engine import prospects as pr                # noqa: E402
import tools.collect as coll                      # noqa: E402

ROBOTS = "User-agent: *\nAllow: /\n"

RSS_NEWS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>News</title>
<item><title>Health insurance plan discontinued for 2027 before open enrollment</title>
<link>https://news.example.com/a1</link><pubDate>Tue, 20 Oct 2026 10:00:00 +0000</pubDate>
<source>Example Web</source></item>
<item><title>Sports team wins championship</title>
<link>https://news.example.com/a2</link><pubDate>Tue, 20 Oct 2026 11:00:00 +0000</pubDate></item>
</channel></rss>"""

YT_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/">
 <title>Health Coverage Help</title>
 <entry><title>Cobra too expensive? Your options before it ends</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v=zz901"/>
  <published>2026-10-20T15:00:00+00:00</published><yt:videoId>zz901</yt:videoId>
  <media:group><media:description>cobra coverage costs 600 a month, what do I do</media:description></media:group>
 </entry>
</feed>"""

WEB_PAGE = """<html><head><title>Open enrollment deadline approaching</title></head>
<body><p>Enrollment deadline January 15. Lost my job? You may still qualify for help paying.</p></body></html>"""


def _fake_network_factory():
    """Fake network: keys the fetch by path hint so each collector gets plausible data."""
    def fetch_url_or_txt(arg):
        if isinstance(arg, str) and "/r/HealthInsurance/search.json" in arg:   # subreddit query
            return {"data": {"children": [
                {"data": {"title": "COBRA ends in 18 months, affordable alternatives?",
                          "selftext": "my cobra coverage is way too expensive",
                          "permalink": "/r/HealthInsurance/comments/kc1/", "subreddit": "HealthInsurance",
                          "created_utc": 1760000000, "author": "sensitive_handle_9", "stickied": False}}]}}
        if isinstance(arg, str) and "search.json" in arg:      # all-reddit ES sweep: no hits
            return {"data": {"children": []}}
        raise AssertionError(f"unexpected network arg: {arg[:120]}")
    return fetch_url_or_txt


def test_run_all_sweep_offline_end_to_end():
    """Full sweep with injected network: rows persist, dedupe holds, every source reported."""
    tmp = tempfile.mkdtemp()
    try:
        con = sig.connect(Path(tmp) / "sweep.db")
        cfg = {"youtube_channels": ["UCfix"], "google_news_queries": ["carrier exits"],
               "webreader": {"max_pages_per_seed": 2, "seeds": [
                   {"url": "https://health.example.com/blog", "instructions": "find coverage facts"}]}}

        def get_json(url):
            return _fake_network_factory()(url)

        def fetch_text(url):
            if url.endswith("robots.txt"):
                return ROBOTS
            if "news.google" in url:
                return RSS_NEWS
            if "youtube.com/feeds" in url:
                return YT_FEED
            return WEB_PAGE

        rows = coll.run_all(con, cfg, get_json=get_json, fetch_text=fetch_text, sleeper=lambda s: None)
        got = {r["source"].split(":")[0]: r for r in rows}
        assert got["reddit"]["accepted"] == 1
        assert got["hackernews"]["fetched"] == 0            # fake get_json only answers reddit search
        assert got["youtube"]["accepted"] == 1
        assert got["google_news"]["accepted"] == 1          # sports headline filtered by taxonomy
        ig = [r for r in rows if r["source"] == "platform:instagram"][0]
        assert ig["status"] == "NEEDS_CREDENTIALS"
        got_wr = [r for r in rows if r["source"].startswith("webreader:")]
        assert len(got_wr) == 1 and got_wr[0]["added"] == 1
        assert any(r["source"] == "webreader" and r["effective_mode"] for r in rows)

        srcs = {row[0] for row in con.execute("SELECT source FROM signals")}
        assert {"reddit", "youtube", "google_news", "webreader"} <= srcs

        # second identical sweep: dedupe holds (no new rows), fetches still logged
        n_before = con.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        coll.run_all(con, cfg, get_json=get_json, fetch_text=fetch_text, sleeper=lambda s: None)
        n_after = con.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        assert n_after == n_before

        # every attempted fetch is logged with a note (success or honest skip)
        logged = {r[0] for r in con.execute("SELECT source FROM fetch_log")}
        assert {"reddit", "hackernews", "youtube", "google_news"} <= logged

        # author usernames were stripped BEFORE storage, across everything
        blob = (Path(tmp) / "sweep.db").read_bytes().decode("utf-8", "ignore")
        assert "sensitive_handle_9" not in blob
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_run_all_youtube_skip_is_honest_without_config():
    tmp = tempfile.mkdtemp()
    try:
        con = sig.connect(Path(tmp) / "s.db")

        def boom(*a, **k):
            raise AssertionError("youtube collector must not run without channel ids")

        rows = coll.run_all(con, {"youtube_channels": []}, get_json=boom, fetch_text=boom,
                            sleeper=lambda s: None)
        yt = [r for r in rows if r["source"] == "youtube"][0]
        assert yt["fetched"] == 0 and "skipped" in yt["note"] and "youtube_channels" in yt["note"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_collect_status_and_report_offline():
    tmp = tempfile.mkdtemp()
    try:
        con = sig.connect(Path(tmp) / "s.db")
        sig.ingest(con, [{"title": "cobra too expensive, what do I do",
                          "url": "https://reddit.com/r/HealthInsurance/comments/z9/"}])
        sig.log_fetch(con, "reddit", "url", 5, 1, "ok")
        coll.cmd_status(con)                                   # prints without error
        coll.cmd_report(con)
        rep = (ROOT / "out" / "collect_report.md").read_text(encoding="utf-8")
        assert "SIGNALS ARE NOT LEADS" in rep and "cobra" in rep.lower() and "reddit" in rep
        text = coll.cmd_schedule()                             # prints scheduler lines, registers nothing
        assert "schtasks" in text and "--network" in text
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        (ROOT / "out" / "collect_report.md").unlink(missing_ok=True)


# ------------------------------------------------------------------------ Google Places
def test_places_adapter_pushes_businesses_not_signals():
    tmp = tempfile.mkdtemp()
    try:
        os.environ["GOOGLE_PLACES_API_KEY"] = "TEST_ONLY_KEY"
        import importlib
        importlib.reload(pr)
        pr._places_approved = lambda: True                     # offline stand-in for the config record
        con = pr.connect(Path(tmp) / "p.db")
        calls = []

        def fake_search(text_query):
            calls.append(text_query)
            if text_query == "FAIL ME":
                raise RuntimeError("API hiccup")
            return {"places": [
                {"displayName": {"text": "Sunrise Health Agency"},
                 "primaryTypeDisplayName": {"text": "Insurance agency"},
                 "nationalPhoneNumber": "(305) 555-0111",
                 "websiteUri": "https://sunrise.example",
                 "formattedAddress": "100 Main St, Miami, FL 33101, USA",
                 "addressComponents": [
                     {"longText": "Miami", "types": ["locality"]},
                     {"longText": "Florida", "types": ["administrative_area_level_1"]},
                     {"longText": "33101", "types": ["postal_code"]}],
                 "googleMapsUri": "https://maps.google.com/?cid=123"}]}
        res = pr.fetch_places(con, [
            {"text_query": "insurance agencies in Miami"},
            {"text_query": "FAIL ME"},
            {"text_query": "insurance agencies in Miami"},      # same → update/dedupe path
            {"text_query": "  "}],                              # blank query skipped silently
            search_text=fake_search)
        assert res == {"fetched": 2, "added": 1, "updated": 1, "skipped": 0, "errors": 1}
        row = dict(con.execute("SELECT name, public_phone, city, state, zip5, source, website, "
                               "evidence_url FROM prospects").fetchone())
        assert row["name"] == "Sunrise Health Agency" and row["source"] == "google_places"
        assert row["city"] == "Miami" and row["state"] == "Florida" and row["zip5"] == "33101"
        assert row["public_phone"] == "(305) 555-0111" and row["website"] == "https://sunrise.example"
        # decision/verify states must be UNTOUCHED by collection (facts ≠ operator confirmations)
        prrow = dict(con.execute("SELECT decision, verify_state FROM prospects").fetchone())
        assert prrow["decision"] == "NEW" and prrow["verify_state"] == "UNVERIFIED"
        # collect_log: per-query rows with ok/error outcomes — the admin console's evidence
        lg = con.execute("SELECT source, query, fetched, added, note FROM collect_log "
                         "ORDER BY id").fetchall()
        assert len(lg) == 3 and lg[0][0] == "google_places"
        assert lg[0][4] == "ok" and "error" in lg[1][4] and "with 1 failed" in lg[2][4]
    finally:
        os.environ.pop("GOOGLE_PLACES_API_KEY", None)
        shutil.rmtree(tmp, ignore_errors=True)


def test_places_gate_still_refuses_without_key_and_console_page_shows_activity(tmp_path):
    os.environ["LEADENGINE_DB"] = str(tmp_path / "t.db")
    os.environ["PROSPECTS_DB"] = str(tmp_path / "p.db")
    os.environ["ADMIN_TOKEN"] = "t"
    os.environ["INTEL_DB"] = str(tmp_path / "none.db")
    os.environ["SIGNALS_DB"] = str(tmp_path / "signals.db")
    import importlib
    importlib.reload(pr)
    importlib.reload(sig)
    con = pr.connect(tmp_path / "p.db")
    with pytest.raises(NotImplementedError):
        pr.fetch_places(con, [{"text_query": "x"}], search_text=lambda q: {})
    assert con.execute("SELECT COUNT(*) FROM collect_log").fetchone()[0] == 0   # nothing logged: NOTHING happened

    scon = sig.connect(tmp_path / "signals.db")
    sig.ingest(scon, [{"title": "cobra ends soon", "url": "https://reddit.com/r/HealthInsurance/comments/v1/"}])
    sig.log_fetch(scon, "reddit", "search url", 4, 1, "ok")
    pr.collect_log(con, "google_places", "agencies Miami", 3, 2, "ok")
    import app as appmod
    importlib.reload(appmod)
    c = appmod.app.test_client()
    page = c.get("/admin/integrations?token=t").get_data(as_text=True)
    assert "Collection activity" in page
    assert "agencies Miami" in page                # prospect collect_log surfaced
    assert "search url" in page                    # signal fetch_log surfaced
    assert "No fetch has run yet" not in page
    assert "google_places" in page


def test_run_lock_refuses_fresh_owner_and_allows_stale(tmp_path, monkeypatch, capsys):
    """Overlap guard for SCHEDULED sweeps: a fresh lock refuses a second run; a stale lock
    (crashed run) is replaceable; a successful run releases its own lock. No network at all."""
    import tools.collect as coll
    lock = tmp_path / "collect.lock"
    monkeypatch.setattr(coll, "LOCK_PATH", lock)

    # fresh foreign lock → refusal before any DB/network work
    lock.write_text("pid=99999", encoding="utf-8")
    with pytest.raises(SystemExit):
        coll.cmd_run()

    # stale lock (>LOCK_MINUTES old) → replaceable, no refusal
    old = _t.time() - (coll.LOCK_MINUTES + 5) * 60
    os.utime(lock, (old, old))
    monkeypatch.setattr(coll, "OUT", tmp_path)
    monkeypatch.setattr(coll, "DB", tmp_path / "signals.db")
    scon = sig.connect(tmp_path / "signals.db")
    scon.close()
    monkeypatch.setattr(coll, "run_all", lambda con: [])
    rows = coll.cmd_run()
    assert rows == []
    assert not lock.exists()                      # released after a finished run
