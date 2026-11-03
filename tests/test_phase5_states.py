"""Phase 5: the seven lifecycle states exist and cannot be confused.

Ground truth:  docs/PHASE5_STATES.md.
"""
import ast
import os
import pathlib
import shutil
import sqlite3
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import signals as sig           # noqa: E402
from engine import content_intel as cintel  # noqa: E402


# ---------------------------------------------------------------- state 1 vs 2
def test_ingest_never_writes_leads_db():
    """Ingest into signals.db touches exactly that file — no leads.db rows."""
    tmp = tempfile.mkdtemp()
    try:
        sdb = os.path.join(tmp, "signals.db")
        ldb = os.path.join(tmp, "leads.db")
        # create a leads.db that LOOKS ready to receive a lead
        lcon = sqlite3.connect(ldb)
        lcon.executescript("CREATE TABLE leads (id INTEGER PRIMARY KEY, phone TEXT);")
        lcon.commit(); lcon.close()

        con = sig.connect(sdb)
        res = sig.ingest(con, [{"source": "reddit",
                                "url": "https://reddit.com/r/HealthInsurance/x1",
                                "title": "lost my job, need cobra",
                                "snippet": "cobra runs out in 2 months, ugh",
                                "collection_method": "reddit_json",
                                "access_status": "public_endpoint"}], "reddit")
        assert res["added"] == 1, res
        row = con.execute("SELECT source, status FROM signals").fetchone()
        assert row[0] == "reddit" and row[1] == "NEW", tuple(row)

        con.close()
        # leads.db untouched and empty
        lcon2 = sqlite3.connect(ldb)
        n = lcon2.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        lcon2.close()
        assert n == 0, f"inventively created {n} leads rows from a signal!"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------- signals → content ideas only
def test_reviewed_signal_leads_to_content_ideas_not_contacts():
    """The reviewed-signal exit is a content brief: no contact/outreach fields, CTA is the Check."""
    tmp = tempfile.mkdtemp()
    try:
        con = sig.connect(os.path.join(tmp, "signals.db"))
        sig.ingest(con, [
            {"source": "reddit", "url": "https://reddit.com/r/HI/lost1",
             "title": "lost my job yesterday — how do I get health insurance?", "lang_hint": "en"},
            {"source": "reddit", "url": "https://reddit.com/r/HI/lost2",
             "title": "lost my job last week, no health insurance, what do I do?", "lang_hint": "en"},
        ], "reddit")
        for (sid,) in con.execute("SELECT id FROM signals"):
            sig.review(con, sid, "REVIEWED", "recurrent cobra affordability questions")

        briefs = cintel.ideas(con, min_score=2)
        assert briefs, "expected a brief from 2 reviewed cobra signals"
        b = briefs[0]
        assert b["topic"] == "job_loss", b["topic"]
        # drafts route consumers to OUR Check, never to contacting a signal author
        for lang in ("draft_en", "draft_es"):
            assert "/check?a=" in b[lang]["cta"]
        blob = str(b).lower()
        for w in ("call them", "dm them", "text them", "whatsapp", "reach out to the author"):
            assert w not in blob, f"brief leaks outreach pattern: {w!r}"
        # evidence chain intact
        assert b["evidence"]["count"] == 2
        assert b["evidence"]["sample"][0]["url"].startswith("https://reddit.com/")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_min_score_marks_low_evidence():
    tmp = tempfile.mkdtemp()
    try:
        con = sig.connect(os.path.join(tmp, "s.db"))
        sig.ingest(con, [{"source": "manual", "url": "https://example.com/one",
                          "title": "cobra too expensive", "lang_hint": "en"}], "manual")
        sid = con.execute("SELECT id FROM signals").fetchone()[0]
        sig.review(con, sid, "REVIEWED", "single data point")
        briefs = cintel.ideas(con, min_score=3)
        if briefs:
            assert all(b["state"] == "NEEDS_MORE_EVIDENCE" for b in briefs)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------- module separation (AST gate)
def _no_pipeline_imports(fs: pathlib.Path):
    """Assert a signals-layer module imports no routing / delivery machinery."""
    tree = ast.parse(fs.read_text(encoding="utf-8"))
    bad = {"engine.route", "engine.notify", "engine.outreach", "engine.billing", "engine.payments"}
    for node in ast.walk(tree):
        names = set()
        if isinstance(node, ast.Import):
            names = {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = {node.module}
        hit = names & bad
        assert not hit, f"{fs.name} imports pipeline machinery: {hit}"


def test_signals_layer_is_pipeline_free():
    for f in ("engine/signals.py", "engine/content_intel.py", "engine/sources.py"):
        p = ROOT / f
        if p.exists():
            _no_pipeline_imports(p)


# ------------------------------------------------- router has no signal pathway
def test_router_never_touches_signals_store():
    src = (ROOT / "engine" / "route.py").read_text(encoding="utf-8")
    assert "signals" not in src.lower(), "router imports the public-signals store"
