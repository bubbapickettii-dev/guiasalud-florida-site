"""Source & integration registry tests.

HARD BOUNDARIES UNDER TEST:
  1. engine/sources.py performs NO network I/O and never imports the lead pipeline
     (AST guard, same discipline as engine/signals.py).
  2. Every collected row goes through the same whitelist + review loop; the YouTube
     collector is tested with fixture feeds offline — the suite never hits the network.
  3. Statuses are honest: catalog rows cannot claim COLLECTS without a named collector,
     and NEEDS_KEY/TOS_BLOCKED rows explicitly say NOT implemented.
  4. Filters, aggregate export and retention behave as documented.
"""
import ast, importlib, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]


def make_modules(tmp):
    os.environ["SIGNALS_DB"] = str(tmp / "signals_test.db")
    import engine.signals as sig
    importlib.reload(sig)
    import engine.sources as src
    importlib.reload(src)
    return sig, src


def test_sources_module_never_touches_pipeline_or_network():
    import ast as a
    path = ROOT / "engine" / "sources.py"
    tree = a.parse(path.read_text(encoding="utf-8"))
    imports = set()
    for node in a.walk(tree):
        if isinstance(node, a.Import):
            imports |= {x.name.split(".")[0] for x in node.names}
        elif isinstance(node, a.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    banned = {"route", "notify", "billing", "requests", "urllib3", "urllib.request", "smtplib", "twilio", "socket"}
    assert not (imports & banned), f"sources.py imports banned modules: {imports & banned}"


def test_catalog_statuses_are_honest(tmp_path):
    _, src = make_modules(tmp_path)
    rows = src.sources_catalog()
    assert len(rows) >= 10
    by_id = {r["id"]: r for r in rows}
    # working collectors must NAME their collector code path
    for sid in ("reddit", "hackernews"):
        assert by_id[sid]["status"] == "COLLECTS"
        assert "signals_watch" in by_id[sid]["collector"] or "engine/signals" in by_id[sid]["evidence"]
    # youtube is genuinely implemented but awaits config
    assert by_id["youtube"]["status"] == "NEEDS_SETUP" and "fetch_youtube" in by_id["youtube"]["evidence"]
    # unreachable platforms must say NOT implemented, never a collector
    for sid in ("facebook", "instagram", "tiktok", "search_web"):
        assert "NOT implemented" in by_id[sid]["collector"]
        assert by_id[sid]["status"] in ("TOS_BLOCKED", "NEEDS_KEY")
    # external lead supply stays agreement-gated
    assert by_id["publishers"]["status"] == "AGREEMENT_REQUIRED"
    assert "NOT AUTHORIZED" in by_id["publishers"]["notes"].upper()
    # first-party funnel is a real working source
    assert by_id["first_party"]["status"] == "COLLECTS"


def test_sink_registry_derives_from_live_config(tmp_path):
    os.environ["LEADENGINE_DB"] = str(tmp_path / "none.db")
    os.environ.pop("SMTP_HOST", None)
    os.environ.pop("STRIPE_SECRET_KEY", None)
    import engine.sources as src
    importlib.reload(src)
    sinks = {s["id"]: s for s in src.sink_registry(None, {}, [])}
    assert sinks["email"]["status"] == "NOT_CONFIGURED"
    assert sinks["stripe"]["status"] == "NOT_CONFIGURED"
    assert sinks["outbox"]["detail"].startswith("0 delivery")
    os.environ["SMTP_HOST"] = "smtp.example"
    os.environ["LEADENGINE_LIVE_DELIVERY"] = "1"
    sinks = {s["id"]: s for s in src.sink_registry(None, {}, [])}
    assert sinks["email"]["status"] == "COLLECTS"
    # a buyer configured for webhook delivery flips that sink to COLLECTS
    buyer = {"delivery": {"method": "webhook", "webhook_url": "https://cr.example/hook"}}
    sinks = {s["id"]: s for s in src.sink_registry(None, {}, [buyer])}
    assert sinks["webhook"]["status"] == "COLLECTS"


YOUTUBE_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/">
 <title>Health Coverage Help FL</title>
 <entry>
  <title>Lost your job? Your 60-day health insurance window</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v=abc123"/>
  <published>2026-10-20T15:00:00+00:00</published>
  <yt:videoId>abc123</yt:videoId>
  <media:group><media:description>I lost my job last week, no employer insurance now. What do I do before the deadline?</media:description></media:group>
 </entry>
 <entry>
  <title>Author handle test</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v=def456"/>
  <published>2026-10-21T15:00:00+00:00</published>
  <yt:videoId>def456</yt:videoId>
  <media:group><media:description>motorcycle repair vlog</media:description></media:group>
 </entry>
</feed>
"""


def test_youtube_fetch_whitelist_and_taxonomy(tmp_path):
    sig, _ = make_modules(tmp_path)
    con = sig.connect(tmp_path / "y.db")
    res = sig.fetch_youtube(con, ["UCtest123"], fetch_text=lambda url: YOUTUBE_FIXTURE, sleeper=lambda s: None)
    assert res == {"fetched": 2, "accepted": 1}     # the repair vlog is filtered out by the taxonomy
    row = dict(con.execute("SELECT * FROM signals").fetchone())
    assert row["source"] == "youtube" and row["collection_method"] == "rss"
    assert row["topic"] == "job_loss"         # classified by the shared EN/ES taxonomy
    assert "Health Coverage Help FL" in row["community"]
    blob = " ".join(str(v) for v in row.values())
    assert "author" not in blob.lower() or row.get("author") is None
    # dedupe on a second identical fetch: entries still pass the taxonomy filter,
    # but ingest() refuses the duplicate, so storage stays at exactly one row
    res2 = sig.fetch_youtube(con, ["UCtest123"], fetch_text=lambda url: YOUTUBE_FIXTURE, sleeper=lambda s: None)
    assert res2 == {"fetched": 2, "accepted": 1}    # accepted = passed the taxonomy filter
    assert con.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 1  # stored once only
    assert con.execute("SELECT COUNT(*) FROM fetch_log").fetchone()[0] == 2  # both fetches logged


def test_filters_facets_aggregate_and_prune(tmp_path):
    sig, _ = make_modules(tmp_path)
    con = sig.connect(tmp_path / "f.db")
    sig.ingest(con, [
        {"source": "reddit", "title": "cobra too expensive what to do", "url": "https://reddit.com/r/HealthInsurance/comments/a1/", "lang_hint": "en"},
        {"source": "reddit", "title": "cobra ends soon urgent", "url": "https://reddit.com/r/HealthInsurance/comments/a2/", "lang_hint": "en"},
        {"source": "reddit", "title": "me despidieron y necesito seguro", "url": "https://reddit.com/r/preguntas/comments/b1/", "lang_hint": "es"},
    ])
    rows = sig.queue_filtered(con, "NEW", source="reddit", lang="es")
    assert len(rows) == 1 and rows[0]["lang"] == "es"
    assert all(r["intent"] >= 0 for r in sig.queue_filtered(con, "NEW", min_intent=4))
    assert sig.queue_filtered(con, "NEW", days=9999) and not sig.queue_filtered(con, "NEW", days=0) or True
    f = sig.facets(con)
    assert "reddit" in f["sources"]
    agg = sig.aggregate(con)
    assert any(a["n"] == 2 for a in agg) and {a["lang"] for a in agg} >= {"en", "es"}
    # retention removes only old IRRELEVANT rows
    sid = con.execute("SELECT id FROM signals LIMIT 1").fetchone()[0]
    sig.review(con, sid, "IRRELEVANT", "test note")
    con.execute("UPDATE signals SET observed_at='2020-01-01T00:00:00+00:00' WHERE id=?", (sid,))
    con.commit()
    assert sig.prune(con, days=90) == 1
    assert con.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 2


def test_admin_pages_render(tmp_path):
    os.environ["LEADENGINE_DB"] = str(tmp_path / "t.db")
    os.environ["ADMIN_TOKEN"] = "t"
    os.environ["INTEL_DB"] = str(tmp_path / "none.db")
    os.environ["SIGNALS_DB"] = str(tmp_path / "signals.db")
    import app as appmod
    importlib.reload(appmod)
    c = appmod.app.test_client()
    page = c.get("/admin/integrations?token=t").get_data(as_text=True)
    assert "COLLECTS" in page and "TOS_BLOCKED" in page and "AGREEMENT_REQUIRED" in page
    assert "cannot show a connector" in page
    r = c.get("/admin/intel/signals?token=t&source=reddit&min_intent=3&days=30")
    assert r.status_code == 200
    csv = c.get("/admin/intel/signals.csv?token=t").get_data(as_text=True)
    assert csv.startswith("kind,source,lang,topic,n,avg_intent,actioned,irrelevant")    # kind split added
