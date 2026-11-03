"""Public web extraction (ScrapeGraphAI-integrated reader): capability honesty +
fallback collection path. All tests run OFFLINE via injected fetch_text fixtures."""
import os
import sys
import tempfile
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import web_extract as webx            # noqa: E402
from engine import signals as sig                 # noqa: E402

ROBOTS = "User-agent: *\nAllow: /\n"

PAGE = """<!doctype html><html><head><title>Enrollment deadlines changed for 2027</title>
<script>var secret = "should-not-leak";</script>
</head><body><p>Marketplace open enrollment for 2027 health insurance coverage
starts November 1. If your plan is discontinued, you get a Special Enrollment Period.</p>
<p>Sobre la inscripcion: si pierde su plan, tiene 60 dias.</p>
<a href="/blog/second-post">next</a><a href="/static/logo.png">img</a>
<a href="https://outsider.example.com/no">out</a></body></html>"""

PAGE2 = """<html><head><title>Second post about subsidies</title></head>
<body><p>Premium tax credits changed. Cobra bridge options are limited in 2027.</p></body></html>"""


def _fetcher_factory():
    calls = []

    def fetch(url):
        calls.append(url)
        if url.endswith("robots.txt"):
            return ROBOTS
        if url.endswith("blog"):
            return PAGE
        return PAGE2
    fetch.calls = calls
    return fetch


# ------------------------------------------------------------------- capability honesty
def test_sga_status_refuses_to_lie():
    st = webx.sga_status(probe_ollama=False)
    assert st["sga_lib_installed"] is False
    assert st["effective_mode"] == "fallback_reader"
    assert st["provider"] in ("none", "openai_available_not_approved")
    # no lib -> mode can NEVER be sga, whatever env says
    os.environ["OPENAI_API_KEY"] = "sk-test"
    try:
        st2 = webx.sga_status(probe_ollama=False)
        assert st2["provider"] == "openai_available_not_approved"
        assert st2["effective_mode"] == "fallback_reader"
    finally:
        os.environ.pop("OPENAI_API_KEY", None)


def test_extract_sga_guards_installed_and_provider():
    with pytest.raises(NotImplementedError) as e:
        webx.extract_sga("https://example.com/x")
    assert "not installed" in str(e.value)


# ------------------------------------------------------------------- fallback collection
def test_collect_page_reads_classifies_dedupes():
    tmp = tempfile.mkdtemp()
    try:
        con = sig.connect(os.path.join(tmp, "s.db"))
        fetch = _fetcher_factory()
        r = webx.collect_page(con, "https://www.health.example.com/blog", fetch_text=fetch)
        assert r["status"] == "ingested" and r["added"] == 1
        row = con.execute("SELECT source, community, title FROM signals").fetchone()
        assert row["source"] == "webreader" and row["community"] == "health.example.com"
        # script block content never lands in stored text
        blob = con.execute("SELECT snippet || title FROM signals").fetchone()[0]
        assert "should-not-leak" not in blob
        # second identical run: dedupe by URL key
        r2 = webx.collect_page(con, "https://www.health.example.com/blog", fetch_text=fetch)
        assert r2["status"] == "ingested" and r2.get("added", 0) == 0 and r2.get("skipped", 0) >= 0
        assert con.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_collect_site_same_domain_bounded():
    tmp = tempfile.mkdtemp()
    try:
        con = sig.connect(os.path.join(tmp, "s.db"))
        fetch = _fetcher_factory()
        r = webx.collect_site(con, "https://www.health.example.com/blog",
                              max_pages=5, fetch_text=fetch)
        assert r["status"] == "ok" and r["fetched"] == 2 and r["added"] == 2
        # only ONE host's pages: outsider link dropped
        urls = {row[0] for row in con.execute("SELECT url FROM signals")}
        assert all("health.example.com" in u for u in urls)
        assert any("second" in u for u in urls)
        # static asset + robot-served pages never over-fetch
        assert len(fetch.calls) <= 2 + 1   # pages + robots
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_robots_denied_is_reported_not_fetched():
    tmp = tempfile.mkdtemp()
    try:
        con = sig.connect(os.path.join(tmp, "s.db"))

        def fetch(url):
            if url.endswith("robots.txt"):
                return "User-agent: *\nDisallow: /\n"
            raise AssertionError("page fetch must not happen when robots denies")

        r = webx.collect_page(con, "https://quiet.example.com/x", fetch_text=fetch)
        assert r["status"] == "robots_denied"
        assert con.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------- isolation
def test_web_extract_module_is_pipeline_free():
    import ast
    tree = ast.parse((ROOT / "engine" / "web_extract.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("engine.route")
            assert node.module not in ("engine.notify", "engine.billing", "engine.payments")
