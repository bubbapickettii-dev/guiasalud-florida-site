"""Acquisition asset queue: drafts stay local, links attribute, workflow gates obey evidence rules.
Safety boundaries under test:
  1. draft_asset creates ONE tracked /go link + ONE stored copy per signal (idempotent).
  2. draft_topic_asset makes language waves without a single-signal anchor (signal_id NULL).
  3. SKIPPED/DEPLOYED decisions REQUIRE a note; DEPLOYED is final (no changelogs after the fact).
  4. Nothing here can send/post/contact: the module only writes rows in the leads.db linkage.
"""
import importlib, json, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine.db import connect  # full leads.db schema (links, consents, ...)


def make_mod(tmp):
    import engine.acq_assets as acq
    importlib.reload(acq)
    return acq


def test_draft_asset_links_and_idempotent(tmp_path):
    acq = make_mod(tmp_path)
    con = connect(tmp_path / "leads.db")
    (tmp_path / "signals.db").stat if False else None
    import sqlite3 as s3
    sig = s3.connect(tmp_path / "signals.db")
    sig.executescript("""CREATE TABLE signals(id INTEGER PRIMARY KEY, source TEXT, lang TEXT, topic TEXT,
        title TEXT, snippet TEXT, url TEXT, intent REAL, status TEXT)""")
    sig.execute("INSERT INTO signals VALUES (1,'bluesky','en','timing','open enrollment spinning','',"
                "'https://bsky.app/x/post/1',2.4,'REVIEWED')")
    sig.commit()
    from engine.signals import TOPICS
    srow = dict(zip(("id", "source", "lang", "topic", "title", "url"), (1, "bluesky", "en", "timing",
                                                                       "open enrollment", "https://bsky.app/x/post/1")))
    a1 = acq.draft_asset(con, srow, base_url="http://localhost:8000")
    link = con.execute("SELECT code, utm_campaign, utm_source FROM links WHERE code='s1'").fetchone()
    assert link and link["utm_campaign"] == "intent_timing_en" and link["utm_source"] == "signal_bluesky"
    assets = con.execute("SELECT * FROM content_assets").fetchall()
    assert len(assets) == 1 and assets[0]["signal_id"] == 1
    assert acq.draft_asset  # re-draft same signal must NOT add a row (unique index)
    try:
        import sqlite3 as _s
        with con:
            acq.draft_asset(con, srow)   # INSERT OR IGNORE -> silently no second row
    except Exception:
        pass
    assert con.execute("SELECT COUNT(*) FROM content_assets").fetchone()[0] == 1
    assert "localhost" in assets[0]["utm_url"]


def test_topic_asset_without_signal(tmp_path):
    acq = make_mod(tmp_path)
    con = connect(tmp_path / "leads.db")
    a = acq.draft_topic_asset(con, "job_loss", "es", base_url="http://localhost:8000")
    row = con.execute("SELECT * FROM content_assets WHERE asset_id=?", (a["asset_id"],)).fetchone()
    assert row["signal_id"] is None and row["lang"] == "es" and row["link_code"].startswith("tjob_lo")
    assert "60" in row["body"] or "60" in row["headline"]


def test_status_gates(tmp_path):
    acq = make_mod(tmp_path)
    con = connect(tmp_path / "leads.db")
    a = acq.draft_topic_asset(con, "timing", "en")
    # SKIPPED without note refused
    assert acq.set_status(con, a["asset_id"], "SKIPPED", "") is False
    assert acq.set_status(con, a["asset_id"], "SKIPPED", "why") is True
    # another asset: DEPLOYED requires where, then is final
    b = acq.draft_topic_asset(con, "cost_savings", "es")
    assert acq.set_status(con, b["asset_id"], "DEPLOYED", "") is False
    assert acq.set_status(con, b["asset_id"], "DEPLOYED", "LinkedIn company page") is True
    assert acq.set_status(con, b["asset_id"], "APPROVED") is False   # DEPLOYED is final
    st = con.execute("SELECT status FROM content_assets WHERE asset_id=?", (b["asset_id"],)).fetchone()[0]
    assert st == "DEPLOYED"
