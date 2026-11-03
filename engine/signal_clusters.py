"""Opportunity discovery: public INTENT clusters -> prioritized decision theses.

Different input, same goal as `engine/opportunities.py`: that module ranks our own
acquisition theses from recorded experiments; this one builds INPUT for those theses from
what the public signal watch actually collected. Nothing here is a lead and nothing here is
a consumer record: clusters are computed from PUBLIC posts (already minimized by
`engine/signals.ingest`), and every AI-written sentence is stored as an explicit INFERENCE
separate from the computed FACTS.

Facts vs inference, structurally enforced:
  - facts   = counts, mean intent, languages, communities, cited permalink titles (recorded data)
  - analysis= local-Llama output: title/why/category/lens. Never shown without that label.
Rejection idea: the model only ever sees cluster titles + short snippets + community labels —
never permalinks, usernames, contact data, or lead/business records (one prompt-few test keeps
it honest offline: the prompt builder must emit no 'http' and no `@` handles).

Connection to the existing ladder: each cluster carries the topic->thesis grants that
`engine/signals.TOPICS[key]["opps"]` already defines, so reviewing a cluster links to the real
`/admin/opportunities` card (e.g. OPP-08, OPP-05) instead of inventing a parallel opportunity list.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .ollama_client import OllamaError, generate_json, list_models


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = Path(os.environ.get("SIGNALS_DB", str(ROOT / "data" / "signals.db")))

MODEL_DEFAULT = "qwen2.5:7b-instruct"

CLUSTER_SCHEMA = """
CREATE TABLE IF NOT EXISTS signal_clusters (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cluster_key TEXT UNIQUE,               -- topic | lang | signal-space suffix (stable per build inputs)
  topic TEXT, topic_label TEXT, lang TEXT,
  n_signals INTEGER, mean_intent REAL, max_intent REAL,
  communities TEXT,                      -- json array of community labels (no handles)
  langs TEXT,                            -- json array
  first_observed TEXT, last_observed TEXT,
  top_titles TEXT,                       -- json array of <=3 cited post titles (public, permalink-level)
  title TEXT, why TEXT, category TEXT, lens TEXT,
  theses TEXT,                           -- json array of existing /admin/opportunities ids
  model TEXT,
  status TEXT DEFAULT 'NEW',             -- NEW | REVIEWED | DISMISSED
  review_note TEXT,
  built_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_cluster_status ON signal_clusters(status, mean_intent DESC);
"""


def connect(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(CLUSTER_SCHEMA)
    con.commit()
    return con


# ------------------------------------------------------------------ prompt builder
def build_prompt(cluster: "sqlite3.Row", n_signals: int) -> str:
    """Build the extraction prompt from ONLY minimized facts: titles, short snippet
    fragments, community labels. Never an author handle (@...), never a permalink (http...),
    never lead/business record content."""
    titles = cluster["top_titles"]
    if isinstance(titles, str):
        try:
            titles = json.loads(titles)
        except ValueError:
            titles = [titles]
    lines = [f'- [{t}]' for t in titles[:3]]
    return (
        "You are analyzing recurring PUBLIC questions about health insurance coverage.\n"
        "Cluster facts (computed from public posts; no authors, no URLs):\n"
        f"topic_label: {cluster['topic_label']}\n"
        f"language(s): {cluster['langs']}\n"
        f"posts in cluster: {n_signals}\n"
        f"mean decision-point strength: {cluster['mean_intent']} / 10\n"
        f"communities: {cluster['communities']}\n"
        "example posts (titles only):\n" + "\n".join(lines) + "\n\n"
        "Return ONLY JSON with keys:\n"
        '- "title": short distillation (a coverage decision this cluster describes)\n'
        '- "why": 1-2 sentences on why it matters for a FREE bilingual coverage-check funnel; '
        "do not claim market demand beyond these posts\n"
        '- "category": one of job_loss | cobra | affordability | timing_deadline | family_change | '
        "enroll_help | other\n"
        '- "lens": "en" or "es" or "en+es" (predominant language of the need)\n'
        "No markdown, no extra keys.\n"
    )


def _categories_valid(cat) -> str:
    ok = {"job_loss", "cobra", "affordability", "timing_deadline", "family_change", "enroll_help", "other"}
    return cat if cat in ok else "other"


# ------------------------------------------------------------------ build from signals
def build_clusters(con: sqlite3.Connection,
                   generate_json=generate_json,
                   min_signals: int = 2,
                   max_clusters: int = 8,
                   model: str = MODEL_DEFAULT) -> dict:
    """Group non-irrelevant, topic-matched signals, then classify the recurring ones locally.
    Returns {'clusters': [...], 'skipped': reason|None} so the CLI/admin can show why nothing
    was built."""
    _ensure(con)
    rows = con.execute(  # noqa: E501 - one grouped SQL over the shared signals table
        "SELECT topic, topic_label, lang, COUNT(*) n, AVG(intent) avg_intent, MAX(intent) max_intent, "
        "       GROUP_CONCAT(DISTINCT community) comm, MIN(observed_at) first_obs, MAX(observed_at) last_obs, "
        "       GROUP_CONCAT(lang) langs_raw "
        "FROM signals WHERE status<>'IRRELEVANT' AND topic<>'none' GROUP BY topic, lang "
        "HAVING n>=? ORDER BY avg_intent DESC LIMIT ?",
        (min_signals, max_clusters),
    ).fetchall()
    if not rows:
        return {"clusters": [], "skipped": "no topic relevance clusters meet min_signals (collect or lower --min)"}

    built = []
    for row in rows:
        topic, lang, n = row["topic"], row["lang"], row["n"]
        lang_set = sorted({l for l in (row["langs_raw"] or "").split(",") if l})
        comm = sorted({c for c in (row["comm"] or "").split("," ) if c})
        titles = [t[0] for t in con.execute(
            "SELECT title FROM signals WHERE topic=? AND lang=? AND status<>'IRRELEVANT' "
            "ORDER BY intent DESC LIMIT 3", (topic, lang)) if t[0]]
        facts = {
            "cluster_key": f"{topic}|{lang}|{hashlib.md5(f'{topic}|{lang}'.encode()).hexdigest()[:6]}",
            "topic": topic, "topic_label": row["topic_label"], "lang": lang,
            "n_signals": n, "mean_intent": round(row["avg_intent"], 1), "max_intent": round(row["max_intent"], 1),
            "communities": json.dumps(comm), "langs": json.dumps(lang_set),
            "first_observed": row["first_obs"], "last_observed": row["last_obs"],
        }
        record = dict(facts)
        record["top_titles"] = json.dumps(titles)
        key = facts["cluster_key"]
        if con.execute("SELECT 1 FROM signal_clusters WHERE cluster_key=?", (key,)).fetchone():
            continue
        prompt = build_prompt(record, 0)
        analysis = {"title": "", "why": "", "category": "other", "lens": lang}
        model_used = ""
        if generate_json is not None:
            try:
                res = generate_json(prompt)
                analysis = res["json"]
                model_used = res.get("model", "")
            except Exception as exp:                 # noqa: BLE001 - surfacing beats crashing a build
                analysis["why"] = f"[local model unavailable: {type(exp).__name__}]"
        record["title"] = str(analysis.get("title") or "")[:200]
        record["why"] = str(analysis.get("why") or "")[:600]
        record["category"] = _categories_valid(analysis.get("category"))
        record["lens"] = str(analysis.get("lens") or lang)[:10]
        # theses come from the static topic map, never from the model: TOPICS[opps] = OPP ids
        from .signals import TOPICS  # local import: avoids circular import at module load
        record["theses"] = json.dumps(TOPICS.get(topic, {}).get("opps", []))
        record["langs"] = json.dumps(lang_set)
        record["communities"] = json.dumps(comm)
        record["model"] = model_used
        record["status"] = "NEW"
        record["review_note"] = ""
        record["built_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        con.execute(
            "INSERT OR IGNORE INTO signal_clusters (cluster_key, topic, topic_label, lang, n_signals, "
            "mean_intent, max_intent, communities, langs, first_observed, last_observed, top_titles, "
            "title, why, category, lens, theses, model, status, review_note, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(record[k] for k in (
                "cluster_key", "topic", "topic_label", "lang", "n_signals", "mean_intent", "max_intent",
                "communities", "langs", "first_observed", "last_observed", "top_titles", "title", "why",
                "category", "lens", "theses", "model", "status", "review_note", "built_at")),
        )
        built.append(key)
    con.commit()
    return {"clusters": built, "skipped": None}


def _ensure(con: sqlite3.Connection) -> sqlite3.Connection:
    """Idempotent schema application — build/list/review can run against the shared
    signals.db connection (same discovery store) without a separate connect step."""
    con.executescript(CLUSTER_SCHEMA)
    con.commit()
    return con


def list_clusters(con: sqlite3.Connection, status: str = ""):
    _ensure(con)
    if status:
        return con.execute("SELECT * FROM signal_clusters WHERE status=? ORDER BY mean_intent DESC",
                           (status,)).fetchall()
    return con.execute("SELECT * FROM signal_clusters ORDER BY mean_intent DESC").fetchall()


def counts(con: sqlite3.Connection) -> dict:
    _ensure(con)
    return {r["status"]: r["n"] for r in con.execute(
        "SELECT status, COUNT(*) n FROM signal_clusters GROUP BY status")}


def review(con: sqlite3.Connection, cluster_id: int, status: str, note: str = "") -> None:
    _ensure(con)
    if status not in ("NEW", "REVIEWED", "DISMISSED"):
        raise ValueError(f"unknown cluster status: {status}")
    if status == "DISMISSED" and not note.strip():
        raise ValueError("DISMISSED requires a note — why this pattern is not our need")
    con.execute("UPDATE signal_clusters SET status=?, review_note=? WHERE id=?", (status, note, cluster_id))
    con.commit()
