"""Opportunity clusters (engine/signal_clusters): OFFLINE tests only.

No Ollama call here — generate_json is faked; the real local-AI path is the
`clusters-build` CLI smoke (operator-triggered). What is proven offline:
  - facts/inference separation is structural (model text only in labeled fields)
  - prompt hygiene: no permalink, no @handle, no plaintext contact fields
  - dedupe: identical signal rows and repeat builds do not duplicate clusters
  - statuses gate on notes exactly like the signal queue (DISMISS requires note)
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from engine import signal_clusters as clus
from engine import signals as sig


@pytest.fixture()
def sdb(tmp_path, monkeypatch):
    monkeypatch.setattr(clus, "DEFAULT_DB", tmp_path / "signals.db")
    return sig.connect(tmp_path / "signals.db")


def _seed(con, url, title, body, topic="job_loss", lang="en", intent=6.0):
    con.execute(
        "INSERT OR IGNORE INTO signals (signal_key, source, community, title, url, snippet, "
        "observed_at, fetched_at, lang, topic, topic_label, intent, angle, opps, status, "
        "collection_method, access_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sig.signal_key(url), "reddit", "r/test", title, url, sig._snippet(body),
         "2026-10-27T00:00:00+00:00", "2026-10-27T00:00:00+00:00", lang, topic, "test label",
         intent, "health_check", '["OPP-02"]', "NEW", "public_api", "public_endpoint"),
    )
    con.commit()


FAKE_MODEL = {"title": "Warehouse layoffs: job-loss coverage scramble", "why": "Posts cluster "
              "around abrupt employer loss of coverage.", "category": "job_loss", "lens": "en"}


def _fake_generate_json(prompt, **kw):
    from engine import ollama_client as oai
    return {"json": dict(FAKE_MODEL), "model": "qwen2.5:test"}


def test_prompt_hygiene_no_urls_no_handles(sdb):
    _seed(sdb, "https://reddit.com/r/test/comments/abc/", "Help — job ended", "Should I take COBRA?")
    row = sdb.execute("SELECT * FROM signals LIMIT 1").fetchone()
    rec = dict(row)
    rec.update(topic_label="label", n_signals=1, mean_intent=6.0, languages="[\"en\"]",
               langs=json.dumps(["en"]), communities=json.dumps(["r/test"]),
               top_titles=json.dumps([rec["title"]]))
    prompt = clus.build_prompt(rec, 0)
    low = prompt.lower()
    assert "http" not in low                    # never a permalink in the model prompt
    assert "@" not in prompt                    # never a @handle
    assert "retrieved_at" not in low and "consent" not in low    # never consumer-record fields


def test_build_dedupes_clamps_and_facts(sdb):
    _seed(sdb, "https://reddit.com/r/test/comments/abc/", "Help — job ended", "Should I take COBRA?")
    _seed(sdb, "https://reddit.com/r/test/comments/def/", "Lost plan", "COBRA too expensive?")
    _seed(sdb, "https://reddit.com/r/test/comments/xyz/", "Same story", "COBRA again")   # same cluster
    res = clus.build_clusters(sdb, generate_json=_fake_generate_json, min_signals=2)
    assert res["skipped"] is None and len(res["clusters"]) == 1
    rows = clus.list_clusters(sdb)
    assert len(rows) == 1 and rows[0]["n_signals"] == 3
    r = rows[0]
    # facts stored as facts
    assert json.loads(r["top_titles"]) and r["mean_intent"] == pytest.approx(6.0, abs=0.2)
    # theses come from the static TOPICS map, never from the model
    assert json.loads(r["theses"]) == ["OPP-02", "HID-09", "HID-10"]
    assert r["model"] == "qwen2.5:test"
    # labeled inference lives only in the inference fields
    assert "Warehouse" in r["title"] or "Warehouse" in r["why"]


def test_repeat_build_does_not_duplicate(sdb):
    _seed(sdb, "https://reddit.com/r/test/comments/abc/", "t1", "COBRA or marketplace?")
    _seed(sdb, "https://reddit.com/r/test/comments/def/", "t2", "COBRA deadline soon?")
    clus.build_clusters(sdb, generate_json=_fake_generate_json, min_signals=2)
    clus.build_clusters(sdb, generate_json=_fake_generate_json, min_signals=2)
    assert len(clus.list_clusters(sdb)) == 1


def test_model_unavailable_still_builds_facts(sdb):
    def boom(prompt, **kw):
        raise RuntimeError("connection refused")
    _seed(sdb, "https://reddit.com/r/test/comments/abc/", "t1", "COBRA question?")
    _seed(sdb, "https://reddit.com/r/test/comments/def/", "t2", "COBRA cost?")
    res = clus.build_clusters(sdb, generate_json=boom, min_signals=2)
    assert res["skipped"] is None
    r = clus.list_clusters(sdb)[0]
    assert "local model unavailable" in r["why"] and r["model"] == ""
    assert r["category"] == "other"                       # clamped, never model-silently-trusted


def test_review_requires_note_for_dismiss(sdb):
    _seed(sdb, "https://reddit.com/r/test/comments/abc/", "t1", "COBRA?")
    _seed(sdb, "https://reddit.com/r/test/comments/def/", "t2", "COBRA!")
    clus.build_clusters(sdb, generate_json=_fake_generate_json, min_signals=2)
    cid = clus.list_clusters(sdb)[0]["id"]
    with pytest.raises(ValueError):
        clus.review(sdb, cid, "DISMISSED")                # no note on module call is allowed but...
    clus.review(sdb, cid, "REVIEWED", "cluster read")
    clus.review(sdb, cid, "DISMISSED", "pattern already covered by OPP-02")
    assert clus.counts(sdb).get("DISMISSED") == 1


def test_collect_cli_clusters_list_smoke(tmp_path, monkeypatch, capsys):
    """The CLI list path works against an EMPTY db without exploding."""
    import tools.collect as coll
    db = tmp_path / "signals.db"
    monkeypatch.setattr(coll, "DB", db, raising=False)
    monkeypatch.setattr(coll, "sig", sig)
    sig.connect(db)
    coll.cmd_clusters_list()
    out = capsys.readouterr().out
    assert "no clusters stored yet" in out


def test_admin_clusters_page_renders_and_review_gates(tmp_path, monkeypatch):
    """The /admin/intel/clusters console: renders empty + populated, shows the facts/inference
    split and the linked theses, and the route refuses an unexplained DISMISS."""
    import os
    os.environ["LEADENGINE_DB"] = str(tmp_path / "t.db")
    os.environ["PROSPECTS_DB"] = str(tmp_path / "p.db")
    os.environ["ADMIN_TOKEN"] = "t"
    os.environ["INTEL_DB"] = str(tmp_path / "none.db")
    os.environ["SIGNALS_DB"] = str(tmp_path / "signals.db")
    db = tmp_path / "signals.db"
    _seed(sig.connect(db), "https://reddit.com/r/test/comments/abc/", "t1", "COBRA or marketplace?")
    _seed(sig.connect(db), "https://reddit.com/r/test/comments/def/", "t2", "COBRA deadline soon?")
    clus.connect(db)
    import importlib
    import app as appmod
    importlib.reload(appmod)
    c = appmod.app.test_client()
    res = c.get("/admin/intel/clusters?token=t")
    page = res.get_data(as_text=True)
    assert res.status_code == 200 and "Signal clusters" in page
    # build via the same code path the CLI uses, then re-render
    con = sig.connect(db)
    clus.build_clusters(con, generate_json=_fake_generate_json, min_signals=2)
    con.close()
    page = c.get("/admin/intel/clusters?token=t").get_data(as_text=True)
    assert "inference" in page and "OPP-02" in page            # facts/inference split + thesis link
    assert "http" not in page.split("Cluster (facts)", 1)[-1]     # no permalinks next to the facts
    # unexplained dismiss refused → still NEW; the refusal message lands on the redirected page
    resp = c.post("/admin/intel/clusters/1/status?token=t", data={"status": "DISMISSED", "note": ""},
                  follow_redirects=True)
    con = sig.connect(db)
    row = clus.list_clusters(con)[0]
    con.close()
    assert row["status"] == "NEW" and "requires a note" in resp.get_data(as_text=True)
