"""SIGNAL WATCHER: local, sandboxed discovery of PUBLIC health-insurance intent signals.

WHAT THIS IS
  A discovery layer that reads PUBLIC question-and-discussion sources (Reddit's public JSON
  endpoints, Hacker News' public Algolia search API) looking for posts where someone publicly
  describes SHOPPING for health coverage: losing job-based insurance, a plan being discontinued,
  a carrier leaving the state, COBRA ending, enrollment deadlines, self-employed coverage
  questions. Each hit becomes a SIGNAL row: title + permalink + community + short public excerpt,
  classified against the same need taxonomy the campaigns already answer (plan_change /
  life_change / self_employed / savings / timing) and the opportunity rows that pool belongs to,
  so an operator can review them and decide whether a legitimate, platform-compliant PUBLIC
  response or content page exists for that moment.

WHAT THIS IS NOT - AND HOW THE CODE ENFORCES IT
  * NOT a lead source. A public post is an INTENT SIGNAL, never a lead: the author gave us no
    contact details, no consent, and no permission to be contacted. Nothing here can feed the
    leads table, routing, billing or delivery (this module never imports engine.route /
    engine.notify / engine.billing, and writes only to data/signals.db).
  * NOT a contact list. We deliberately store FEWER fields than the public API returns: the
    post title, permalink, community name and a short excerpt are kept; the AUTHOR's username
    and id are never extracted, stored, or returned - being publicly visible was not
    permission, and we don't need the identifier, just the place and the moment.
  * NOT an automatic publisher. Every fetch is an explicit operator command-line action
    (tools/signals_watch.py). Nothing is sent, posted or replied to by this module - ever.

DESIGN NOTES
  * Signals live in data/signals.db, SEPARATE from the guarded lead database, so the two
    populations can never be confused: PUBLIC DISCUSSION (reviewed here) vs CONSUMER
    SUBMISSIONS (leads.db: consented, qualified, routed).
  * Sources are credential-free public endpoints chosen from the Oct 2026 research
    (docs/LEADGEN_RESEARCH.md). Public-API etiquette is enforced in code: explicit, entrenched
    User-Agent, small limits, and a pause between requests.
  * Offline ingestion (`tools/signals_watch.py --ingest file.json`) lets an operator record
    durable public URLs found by hand from platforms with no open API.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UA = "GS-Allude-market-research/0.1 (local, read-only; contact via repository)"
REQUEST_PAUSE_S = 2.0          # polite, one-thread-at-a-time pace; public API etiquette
HTTP_TIMEOUT_S = 15

# ----------------------------------------------------------------------------- the taxonomy
# Need paths mirror the Health Coverage Check angles (config/health_flow.json) so a signal points
# at the SAME content the campaigns already answer that need with, plus the opportunity IDs
# (config/opportunities.json) this pool belongs to.
TOPICS: dict[str, dict] = {
    "carrier_exit": {
        "label": "carrier exit / plan discontinued (Dec 15 + Jan window)",
        "angle": "plan_change",
        "opps": ["OPP-01", "OPP-01B", "HID-12", "HID-15", "HID-03"],
        "kw_en": ["cigna", "molina", "sunshine health", "florida blue", "ambetter",
                  "discontinued", "plan is ending", "no longer offering", "leaving the marketplace",
                  "not renewed", "cancelled my plan"],
        "kw_es": ["se va de florida", "deja de ofrecer", "dejan de ofrecer", "plan termina",
                  "carta de cancelación", "cancelaron el plan", "ya no venden seguro"],
    },
    "auto_moved": {
        "label": "auto-placed in a plan they didn't choose (Jan 1-15 window)",
        "angle": "plan_change",
        "opps": ["OPP-01B"],
        "kw_en": ["they switched my plan", "re-enrolled me in", "new plan i never", "changed my plan without",
                  "auto enrolled", "autoenrolled", "moved to a different plan", "got placed in"],
        "kw_es": ["me cambiaron de", "me inscribieron en", "sin mi permiso", "ya no tengo mi plan"],
    },
    "job_loss": {
        "label": "lost employer coverage (60-day SEP)",
        "angle": "life_change",
        "opps": ["OPP-02", "HID-09", "HID-10"],
        "kw_en": ["lost my job", "laid off", "got fired", "laid-off", "lost my benefits",
                  "no longer have employer"],
        "kw_es": ["me despidieron", "perdi mi trabajo", "perdí mi trabajo", "quede sin trabajo",
                  "quedé sin trabajo", "perdi el seguro del trabajo", "ya no tengo el seguro del trabajo"],
    },
    "medicaid_loss": {
        "label": "lost / losing Medicaid (redeterminations, income change)",
        "angle": "life_change",
        "opps": ["OPP-02", "HID-03", "OPP-09"],
        "kw_en": ["lost my medicaid", "losing medicaid", "medicaid ended", "dropped from medicaid",
                  "kicked off medicaid", "medicaid unwinding", "medicaid redetermination",
                  "medicaid expired", "ended my medicaid", "lost medicaid",
                  "no more medicaid", "medicaid stops", "off my medicaid", "medicaid benefit ended",
                  "now that medicaid", "denied medicaid", "medicaid denied"],
        "kw_es": ["me quitaron el medicaid", "me quitaron medicaid", "se me acabo el medicaid",
                  "se me acabó el medicaid", "se me acaba el medicaid", "perdi medicaid",
                  "perdí medicaid", "ya no tengo medicaid", "me quitaron el seguro del gobierno",
                  "ya no califico para medicaid", "ya no califico", "medicaid se acaba",
                  "quitaron medicaid", "revisaron mi medicaid"],
    },
    "cobra": {
        "label": "COBRA (ending / too expensive / 18-month exhaustion)",
        "angle": "life_change",
        "opps": ["HID-08", "OPP-02"],
        "kw_en": ["cobra ending", "cobra ends", "cobra too expensive", "on cobra", "cobra coverage",
                  "cobra"],   # plain-name catch: ANY active COBRA mention is a coverage decision
        "kw_es": ["cobra se acaba", "se me acaba el cobra", "cobra caro"],
    },
    "self_employed": {
        "label": "1099 / self-employed / freelancer coverage",
        "angle": "self_employed",
        "opps": ["OPP-06", "OPP-04", "OPP-03"],
        "kw_en": ["self employed", "self-employed", "freelance", "freelancer", "1099", "gig worker", "sole proprietor"],
        "kw_es": ["por cuenta propia", "trabajador independiente", "independiente", "autonomo", "autónomo"],
    },
    "timing": {
        "label": "open-enrollment windows and deadlines",
        "angle": "timing",
        "opps": ["HID-01", "OPP-01"],
        "kw_en": ["open enrollment", "enrollment deadline", "dec 15", "december 15", "jan 15", "last day to enroll",
                  "still open", "did i miss"],
        "kw_es": ["inscripción abierta", "inscripcion abierta", "fecha limite", "fecha límite", "puedo inscribirme"],
    },
    "cost_savings": {
        "label": "premium / subsidy / affordability",
        "angle": "savings",
        "opps": ["HID-03", "OPP-09", "OPP-10", "HID-04"],
        "kw_en": ["subsidy", "tax credit", "too expensive", "can't afford", "cannot afford", "premium went up",
                  "unaffordable"],
        "kw_es": ["no puedo pagar", "muy caro", "ayuda para pagar", "subsidio", "credito fiscal", "crédito fiscal"],
    },
    "family_aging": {
        "label": "family: turning 26, plan-for-family needs",
        "angle": "life_change",
        "opps": ["OPP-07", "OPP-01"],
        "kw_en": ["turned 26", "turning 26", "aging off", "insurance for my family", "plan for my family"],
        "kw_es": ["cumplo 26", "seguro para mi familia"],
    },
    "enroll_help": {
        "label": "how/where to enroll, human help wanted",
        "angle": "health_check",
        "opps": ["OPP-08", "OPP-05"],
        "kw_en": ["how do i enroll", "how to sign up", "help signing up", "who can help me", "navigators", "where do i apply"],
        "kw_es": ["como inscribirme", "cómo inscribirme", "ayuda para inscribirse", "donde puedo inscribirme",
                  "quien me puede ayudar", "quién me puede ayudar"],
    },
    "quotes_shopping": {
        "label": "actively shopping / asking for coverage or a quote",
        "angle": "health_check",
        "opps": ["OPP-03", "OPP-04", "OPP-05", "OPP-08"],
        "kw_en": ["looking for health insurance", "need health insurance", "health insurance quotes",
                  "health insurance quote", "cheap health insurance", "affordable health coverage",
                  "help me find health insurance", "recommend a health insurance", "get me health insurance",
                  "health insurance for me", "need a health plan", "find me a health plan",
                  "insurance to cover", "coverage for a new job", "starting a new job",
                  "starting a new position", "insurance through my new job", "insurance outside my job",
                  "new job health insurance", "just got hired", "quit my job", "resigned", "going independent"],
        "kw_es": ["necesito seguro medico", "necesito seguro de salud", "busco aseguranza", "busco seguro",
                  "seguro medico barato", "aseguranza barata", "aseguranza barato", "cotizar seguro",
                  "donde consigo aseguranza", "donde puedo conseguir seguro", "recomienden un seguro",
                  "seguro de salud para", "nesecito seguro medico"],
    },
}

# words that make a post MORE likely to be an active decision point (weighted in scoring)
BOOSTERS_EN = ["deadline", "what do i do", "options", "recommend", "help", "urgent", "by the",
               "first time", "which plan", "compare", "should i", "end of", "before"]
BOOSTERS_ES = ["que hago", "qué hago", "urgente", "opciones", "ayuda", "antes del", "recomiendan",
               "cual plan", "cuál plan", "debo", "plazo", "estoy"]

# "obamacare" is NOT an ES marker: English posters use it constantly (verified vs Stack rows)
ES_MARKERS = ("¿", "ñ", "á", "é", "í", "ó", "ú", "¡", "aseguranza", "seguro medico",
              "cobertura medica", "inscripcion abierta", "despidieron", "cumplo", "no puedo pagar",
              "inscribirme", "me cambiaron de")

# Geographic relevance ONLY where the post itself names the place (never inferred from a
# source's metadata): the South-FL focus of the campaigns means a Miami/Coral Springs ask is
# worth more review time than an otherwise-identical ask from elsewhere. The marker list is a
# small, hand-checkable whitelist — '305'/'786' are South-FL area codes used as location slang.
GEO_MARKERS = [
    ("south_fl", ["miami", "hialeah", "south fl", "south florida", "broward", "ft lauderdale",
                  "fort lauderdale", "palm beach", "homestead", "kendall", "dade", "miami lakes",
                  "miramar", "coral springs", "weston", "pembroke", "aventura", "305", "786"]),
    ("fl", ["florida", "orlando", "tampa", "jacksonville", "naples", "fl ", "gainesville"]),
]


def detect_geo(text: str) -> str:
    """Pure, offline, evidence-based: return the finest location the post's own text names,
    or '' when the post says nothing geographic (stricter than counting any hit: 'fl '
    requires the abbreviation to stand like a word in its surroundings)."""
    low = " " + re.sub(r"\s+", " ", text.lower()) + " "
    for key, markers in GEO_MARKERS:
        for m in markers:
            if m in low:
                return key
    return ""

DEFAULT_DB = Path(os.environ.get("SIGNALS_DB", ROOT / "data" / "signals.db"))

# -------------------------------------------------------------------- kind: why a row is in the DB
# MARKET    = market intelligence: news, official datasets, published pages. Shapes content
#             and timing; never points at a person.
# INTEREST  = a public expression of potential interest: a person-shaped post someone made
#             asking for coverage help. Still NOT a lead — no contact details, no consent —
#             but it identifies WHERE the demand conversation lives and what to build for it.
# Rule (history-consistent): community sources (reddit / reddit_fallback / hackernews) with a
# taxonomy topic classify as INTEREST; everything else (google_news, official data, webreader
# extracts of brand pages, platform adapters) classifies as MARKET.
INTEREST_SOURCES = {"reddit", "reddit_fallback", "reddit_comments", "stackexchange", "hn_comments",
                    "mastodon", "bluesky", "lemmy", "apify", "brightdata"}   # person-shaped community posts + comments
def kind_sources(edit_sets: bool = False) -> dict:
    """Runtime-visible copies of the classifier sets (used by tools + admin pages so the
    rule is never restated somewhere else)."""
    return {"interest": set(INTEREST_SOURCES) if edit_sets else INTEREST_SOURCES,
            "news_market": set(NEWS_DATA_SOURCES) if edit_sets else NEWS_DATA_SOURCES}

# INSIGHT: comment sources (reddit_comments / hn_comments / stackexchange) are ALSO
# person-shaped: they sit INSIDE someone's own question thread, so a taxonomy topic on one of
# them means an individual speaking about their own coverage need — INTEREST by the same rule.
# apify/brightdata are the PAID third-party scraper adapters; their rows hit ingest only when
# the items already passed accept_filter(=a taxonomy topic), so they follow the same rule.
# Hacker News rows stay MARKET: the collector reads STORIES (news links + occasional "Ask HN"
# advice threads about the industry), which is market context — not an individual asking for
# help with their own coverage. Conservative by design: no kind inflation.
NEWS_DATA_SOURCES = {"google_news", "official_cms", "hcgov", "youtube", "instagram", "facebook",
                     "tiktok", "hackernews"}


def demand_kind(source: str, topic: str) -> str:
    """Pure classifier for the stored kind. topic != 'none' means the taxonomy keyword ran
    on this row already, so a community row with a topic is a person-shaped need expression."""
    if topic != "none" and topic and source in INTEREST_SOURCES:
        return "INTEREST"
    return "MARKET"


SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  signal_key TEXT UNIQUE,                 -- stable dedupe hash of the public permalink
  source TEXT,                            -- reddit | hackernews | manual
  community TEXT,                         -- r/... or site label (NEVER an author handle)
  title TEXT, url TEXT, snippet TEXT,
  observed_at TEXT,                       -- post date where the source gives it; else fetch time
  fetched_at TEXT,
  lang TEXT,                              -- en | es | ?
  topic TEXT,                             -- a TOPICS key or 'none'
  topic_label TEXT, intent REAL, angle TEXT, opps TEXT,
  status TEXT DEFAULT 'NEW',              -- NEW | REVIEWED | ACTIONED | IRRELEVANT
  kind TEXT DEFAULT 'MARKET',             -- MARKET (market intel) | INTEREST (person-shaped public need)
  note TEXT, actioned_note TEXT,
  collection_method TEXT DEFAULT 'manual',    -- public_api | rss | manual
  access_status TEXT DEFAULT 'public_endpoint', -- public_endpoint | rate_limited | error
  geo TEXT DEFAULT ''                    -- south_fl | fl | '' - ONLY what the post text itself names
);   -- intent_cat: shopping|question|enrolled|news|generic|irrelevant (added by migration)
CREATE INDEX IF NOT EXISTS ix_sig_status ON signals(status, observed_at);
CREATE TABLE IF NOT EXISTS fetch_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, source TEXT, query TEXT,
  fetched INTEGER, accepted INTEGER, note TEXT
);
"""


def connect(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.commit()
    # migration for pre-existing databases (new ones get both columns from SCHEMA)
    for _col, _ddl in (("collection_method", "TEXT DEFAULT 'manual'"),
                       ("access_status", "TEXT DEFAULT 'public_endpoint'"),
                       ("kind", "TEXT DEFAULT 'MARKET'"),
                       ("geo", "TEXT DEFAULT ''"),
                       ("intent_cat", "TEXT DEFAULT ''")):
        try:
            con.execute(f"ALTER TABLE signals ADD COLUMN {_col} {_ddl}")
            con.commit()
        except sqlite3.OperationalError:
            pass                                # column already exists
    # one-time backfill so historical rows carry the same distinction
    if con.execute("SELECT 1 FROM signals WHERE kind IS NULL LIMIT 1").fetchone():
        for _r in con.execute("SELECT id, source, topic FROM signals WHERE kind IS NULL").fetchall():
            con.execute("UPDATE signals SET kind=? WHERE id=?", (demand_kind(_r[1], _r[2] or "none"), _r[0]))
        con.commit()
    # intent_cat backfill: pre-classifier rows get the shape split too (pure offline pass)
    if con.execute("SELECT 1 FROM signals WHERE intent_cat = '' LIMIT 1").fetchone():
        for _r in con.execute("SELECT id, title, snippet, lang FROM signals WHERE intent_cat = ''").fetchall():
            body = " ".join(x for x in (_r[1], _r[2]) if x)
            cat = intent_category(body, _r[3] or "")
            con.execute("UPDATE signals SET intent_cat=? WHERE id=?", (cat, _r[0]))
        con.commit()
    return con


# -------------------------------------------------------------------- classification
def _hits(text: str, kws: list[str]) -> int:
    return sum(1 for k in kws if k in text)


def classify(text: str, lang_hint: str = "") -> dict:
    """Classify one public post into the need taxonomy. Pure function, fully testable offline."""
    # social platforms love curly quotes ("Here's how" is stored as "Here’s how"); normalize
    # once so every downstream keyword match sees the same apostrophes
    text = text.replace(chr(0x2019), "'").replace(chr(0x2018), "'") \
               .replace(chr(0x201C), '"').replace(chr(0x201D), '"')
    low = " " + re.sub(r"\s+", " ", text.lower()) + " "
    is_es = bool(lang_hint == "es" or any(m in low for m in ES_MARKERS))
    scored = []
    for key, t in TOPICS.items():
        n = _hits(low, t["kw_en"]) + _hits(low, t["kw_es"])
        if n:
            scored.append((n, key))
    scored.sort(key=lambda x: -x[0])
    topic, topic_label, angle = "none", "", ""
    intent = 0.0
    if scored:
        n, topic = scored[0]
        t = TOPICS[topic]
        topic_label, angle = t["label"], t["angle"]
        intent = min(float(n), 6.0)
    boost = _hits(low, BOOSTERS_ES if is_es else BOOSTERS_EN)
    qmark = 1.0 if ("?" in text or "¿" in text) else 0.0    # an actual question, not commentary
    intent = min(intent + boost * 0.7 + qmark, 10.0)
    lang = "es" if is_es else ("en" if topic != "none" else "?")
    return {"lang": lang, "topic": topic, "topic_label": topic_label, "intent": round(intent, 1),
            "angle": angle, "opps": json.dumps(TOPICS[topic]["opps"]) if topic != "none" else "[]",
            "snippet": _snippet(text)}


def _snippet(text: str, limit=240) -> str:
    s = re.sub(r"\s+", " ", text).strip()
    return s[:limit].rsplit(" ", 1)[0] if len(s) > limit else s


# ---------------------------------------------------------------- intent CATEGORY
# A second dimension over the topic+intent score: WHY this row deserves review attention.
# Tuned Oct 2026 against a review of 73 stored Obamacare-era INTEREST rows — the rules below
# are the observed shapes, not a keyword wish-list:
#   enrolled   "I did my open enrollment for insurance I've been putting off"   (done, not shopping)
#   shopping   "I missed my signup for insurance through my job... why"         (still needs coverage NOW)
#   shopping   "health insurance open enrollment is here & my head is spinning; lotta options"
#   question   "Can HealthCare.gov force-change my plan variant mid-year?"      (informational ask)
#   generic    "Charging $50/hr as a freelancer? After taxes, health insurance..."  (talks ABOUT it)
#   news       "Open Enrollment season is here! Residents can find affordable..."   (announcement)
#   news       "Need health insurance? Here's how 2027 ACA options may affect..."   (publisher link)
# A public post is NEVER a prospect by itself: this category only orders the review queue.
INTENT_CATS = ("shopping", "question", "enrolled", "news", "generic", "irrelevant")

_CAT_ENROLLED = ("did my open enrollment", "finished my open enrollment", "completed my open enrollment",
                 "signed up for", "i signed up", "got covered", "i got insurance", "got my insurance",
                 "got health insurance", "already on my parents", "on my parents'",
                 "ya tengo", "me inscribí", "me inscribi", "ya me aseguré", "contraté")
_CAT_SHOPPING = ("need health insurance", "need coverage", "need insurance", "looking for health insurance",
                 "looking for coverage", "shopping for plans", "comparing plans", "which plan",
                 "where can i find", "can anyone recommend", "recommend an agent", "affordable plans",
                 "how much will", "how much would", "quote", "missed my sign", "missed my opportunity",
                 "missed open enrollment", "missed the deadline", "head is spinning",
                 "did my open enrollment yet", "haven't enrolled", "no tengo", "necesito seguro",
                 "necesito un seguro", "busco seguro", "busco un seguro", "cotización", "cotizacion",
                 "me lo dijeron que", "recomienden")
_CAT_NEWS = ("here's how", "here's what", "here is how", "here is what", "season is here", "period is here",
             "deadline is", "enrollment period ends", "enrollment window", "open enrollment begins",
             "announcing", "check out our", "deadlin earli", "deadline earlier")


def intent_category(text: str, lang: str = "en") -> str:
    """Group a signal into the split the review queue needs: which rows are actively SHOPPING,
    which are information QUESTIONS, which already ENROLLED, which are NEWS/marketing, which
    only mention insurance GENERALLY, and which are IRRELEVANT chatter. Pure + offline.
    Order matters: completed-enrollment statements outrank shopping phrasing, announcements
    outrank generic talk, and the news shape needs its tail (linked pages)."""
    text = text.replace(chr(0x2019), "'").replace(chr(0x2018), "'") \
               .replace(chr(0x201C), '"').replace(chr(0x201D), '"')
    # normalize curly apostrophes/quotes once so keyword shapes match real platform text
    low = re.sub(r"\s+", " ", text.lower())
    has_ins = any(k in low for k in (
        "insurance", "coverage", "enrollment", "marketplace", "cobra", "plan", "obamacare",
        "subsidy", "premium", "deductible", "broker", "agent", "aseguranza", "seguro",
        "cobertura", "inscri", "planes",
    ))
    if not has_ins:
        return "irrelevant"
    if any(k in low for k in _CAT_ENROLLED):
        return "enrolled"
    if any(k in low for k in _CAT_NEWS) and ("http" in low or "#" in text or "www" in low
                                             or re.search(r"\b(news|plan deadline|residents|options may)", low)):
        return "news"
    if any(k in low for k in _CAT_SHOPPING):
        return "shopping"
    # a bare "?" is NOT a question ask ("Charging $50/hr... ?" is a rhetorical aside); an
    # actual question has an ask-VERB plus the question mark, or the Spanish inverted marks
    if "¿" in text or ("?" in text and re.search(r"\b(how|what|are|can|does|will|should|why|is)\b", low)):
        return "question"
    if "http" in low or "www" in low or "#" in text or re.search(r"\b(news|study|report|announc)", low):
        return "news"
    return "generic"


def signal_key(url: str) -> str:
    u = urllib.parse.urlsplit(url.strip().lower())
    return hashlib.sha1((u.netloc.replace("www.", "") + u.path.rstrip("/") + u.query).encode()).hexdigest()[:24]


# -------------------------------------------------------------------- ingestion
def ingest(con: sqlite3.Connection, rows: list[dict], source_label: str = "manual") -> dict:
    """Insert cleaned signal rows. CLEANER THAN THE SOURCE, ON PURPOSE: every row is rebuilt from
    an explicit whitelist of fields, so an author username/id can never reach storage, no matter
    what the upstream payload contained."""
    now_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    added, skipped = 0, 0
    seen_text: set[str] = set()   # exact-text dupes WITHIN one batch: same quoted comment
    for r in rows:                # re-quoted into three sibling replies gets three rows
        url = (r.get("url") or "").strip()
        if not url or "://" not in url:                 # only durable public permalinks
            skipped += 1
            continue
        body = " ".join(x for x in (r.get("title"), r.get("snippet"), r.get("body")) if x)
        text_key = re.sub(r"\s+", " ", body.strip().lower())
        if text_key in seen_text:
            skipped += 1
            continue
        seen_text.add(text_key)
        cl = classify(body, r.get("lang_hint", ""))
        key = signal_key(url)
        if con.execute("SELECT 1 FROM signals WHERE signal_key=?", (key,)).fetchone():
            skipped += 1
            continue
        # stored excerpt: the classifier already read the FULL body (title+snippet+body),
        # so for sources that carry one (comments, mirrors, platform payloads) store a
        # wider excerpt than the 240 display cap — otherwise a contact a person published
        # only in the tail of their own comment would be invisible to the contact miner.
        stored_snippet = cl["snippet"]
        body_full = re.sub(r"\s+", " ", (r.get("body") or "").strip())
        if body_full and len(body_full) > len(stored_snippet):
            stored_snippet = body_full[:600].strip()
        geo_val = r.get("geo") or detect_geo(body)
        cat_val = r.get("intent_cat") or intent_category(body, cl["lang"])
        con.execute(
            "INSERT OR IGNORE INTO signals (signal_key, source, community, title, url, snippet, "
            "observed_at, fetched_at, lang, topic, topic_label, intent, angle, opps, "
            "collection_method, access_status, kind, geo, intent_cat) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, r.get("source") or source_label, (r.get("community") or "")[:80],
             (r.get("title") or "")[:300], url[:400], stored_snippet, r.get("observed_at") or now_ts,
             now_ts, cl["lang"], cl["topic"], cl["topic_label"], cl["intent"], cl["angle"], cl["opps"],
             (r.get("collection_method") or "manual"),
             (r.get("access_status") or "public_endpoint"),
             (r.get("kind") or demand_kind(r.get("source") or source_label, cl["topic"])),
             geo_val, cat_val))
        added += 1
    con.commit()
    return {"added": added, "skipped": skipped}


def review(con: sqlite3.Connection, signal_id: int, status: str, note: str = "") -> bool:
    assert status in ("REVIEWED", "ACTIONED", "IRRELEVANT")
    cur = con.execute("UPDATE signals SET status=?, note=COALESCE(nullif(?,''),note) WHERE id=?",
                      (status, note, signal_id))
    con.commit()
    return cur.rowcount == 1


def kind_counts(con) -> dict:
    """The acquisition split: market intelligence rows vs person-shaped interest rows."""
    k = {r[0]: r[1] for r in con.execute("SELECT kind, COUNT(*) FROM signals GROUP BY 1")}
    return {"MARKET": k.get("MARKET", 0), "INTEREST": k.get("INTEREST", 0),
            "UNSET": k.get("UNSET", k.get(None, 0))}


def counts(con: sqlite3.Connection) -> dict:
    st = {r[0]: r[1] for r in con.execute("SELECT status, COUNT(*) FROM signals GROUP BY 1")}
    return {"NEW": st.get("NEW", 0), "REVIEWED": st.get("REVIEWED", 0), "ACTIONED": st.get("ACTIONED", 0),
            "IRRELEVANT": st.get("IRRELEVANT", 0), "TOTAL": sum(st.values())}


def queue(con: sqlite3.Connection, status: str = "NEW", limit: int = 60) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM signals WHERE status=? ORDER BY intent DESC, observed_at DESC LIMIT ?", (status, limit))]


def queue_filtered(con: sqlite3.Connection, status: str = "NEW",
                   source: str = "", lang: str = "", topic: str = "",
                   min_intent: float = 0.0, days: int = 0, kind: str = "",
                   intent_cat: str = "", limit: int = 100) -> list[dict]:
    """The admin console's filterable queue: platform (source), language, topic taxonomy,
    relevance floor, an age window (days), the kind split (MARKET vs INTEREST), and the
    intent CATEGORY (shopping/question/enrolled/news/generic/irrelevant). Every parameter is
    optional; status is required so a hidden population is never silently rendered."""
    sql = "SELECT * FROM signals WHERE status=?"
    args: list = [status]
    if source:
        sql += " AND source=?"
        args.append(source)
    if kind in ("MARKET", "INTEREST"):
        sql += " AND kind=?"
        args.append(kind)
    if intent_cat in INTENT_CATS:
        sql += " AND intent_cat=?"
        args.append(intent_cat)
    if lang in ("en", "es", "?"):
        sql += " AND lang=?"
        args.append(lang)
    if topic:
        sql += " AND topic=?"
        args.append(topic)
    if min_intent > 0:
        sql += " AND intent>=?"
        args.append(float(min_intent))
    if days and days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        sql += " AND COALESCE(observed_at,'')>=?"
        args.append(cutoff)
    sql += " ORDER BY intent DESC, observed_at DESC LIMIT ?"
    args.append(int(limit))
    return [dict(r) for r in con.execute(sql, args)]


def facets(con: sqlite3.Connection) -> dict:
    """Distinct values for the console's filter dropdowns (cheap, single queries)."""
    return {
        "sources": [r[0] for r in con.execute("SELECT DISTINCT source FROM signals ORDER BY 1")],
        "topics": [r[0] for r in con.execute("SELECT DISTINCT topic FROM signals WHERE topic != '' ORDER BY 1")],
        "langs": [r[0] for r in con.execute("SELECT DISTINCT lang FROM signals WHERE lang != '' ORDER BY 1")],
        "kinds": [r[0] for r in con.execute("SELECT DISTINCT kind FROM signals WHERE kind IS NOT NULL ORDER BY 1")],
        "cats": [r[0] for r in con.execute("SELECT DISTINCT intent_cat FROM signals WHERE intent_cat != '' ORDER BY 1")],
    }


def aggregate(con: sqlite3.Connection) -> list[dict]:
    """Aggregate insight rows (source x topic x lang): counts + avg relevance + actioned.
    This is what the CSV export serves - aggregates, never a contact list."""
    return [dict(r) for r in con.execute(
        "SELECT kind, source, lang, topic, COUNT(*) n, ROUND(AVG(intent),2) avg_intent, "
        "SUM(status='ACTIONED') actioned, SUM(status='IRRELEVANT') irrelevant "
        "FROM signals GROUP BY kind, source, lang, topic ORDER BY n DESC")]


def prune(con: sqlite3.Connection, days: int = 90) -> int:
    """Retention: reviewed-but-irrelevant findings older than `days` are deleted.
    REVIEWED and ACTIONED stay (they carry evidence); NEW is never auto-deleted.
    Returns the number of rows removed so the operator sees the effect."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    cur = con.execute("DELETE FROM signals WHERE status='IRRELEVANT' AND COALESCE(observed_at,'') < ?", (cutoff,))
    con.commit()
    return cur.rowcount


def per_source_counts(con: sqlite3.Connection) -> dict:
    st = {r[0]: {"signals": r[1], "new": r[2], "actioned": r[3]} for r in con.execute(
        "SELECT source, COUNT(*), SUM(status='NEW'), SUM(status='ACTIONED') FROM signals GROUP BY 1")}
    return st


# ------------------------------------------------------------------ recency discipline
# The Oct 2026 audit of 104 stored INTEREST rows found only ~7 expressing a CURRENT need —
# the rest were relevance-ranked archive pages from 2014-2024 (and tax-accounting "COBRA
# premiums" questions). Recency is therefore part of DISCOVERY, not report-time filtering:
# every collector sorts/limits at the SOURCE (newest-first, since=<cutoff>), so stale rows
# never enter the review queue in the first place.
MAX_AGE_DAYS = max(1, int(os.environ.get("SIGNALS_MAX_AGE_DAYS", "30")))

def _min_created_epoch(days: int = MAX_AGE_DAYS) -> int:
    return time.time() - days * 86400


def _fresh(observed, days: int = MAX_AGE_DAYS) -> bool:
    """True when a row's own timestamp is within the recency window (missing ts -> keep,
    dedupe + review gates still apply)."""
    if not observed:
        return True
    try:
        ts = datetime.fromisoformat(str(observed).replace("Z", "+00:00"))
    except ValueError:
        return True
    return ts.timestamp() >= _min_created_epoch(days)


def _iso_date(days: int = MAX_AGE_DAYS) -> str:
    """YYYY-MM-DD cutoff (Bluesky since/recency guards)."""
    return time.strftime("%Y-%m-%d", time.gmtime(_min_created_epoch(days)))


def accept_filter(cleaned: dict) -> bool:
    """Keep only posts that plausibly belong to the taxonomy (at least one keyword hit overall).
    Keeps lifestyle/off-topic chatter out of the review queue; classification still happens."""
    body = " ".join(x for x in (cleaned.get("title"), cleaned.get("snippet")) if x).lower()
    for t in TOPICS.values():
        if _hits(body, t["kw_en"]) or _hits(body, t["kw_es"]):
            return True
    return False


def _generic_only_noise(text: str, generic: tuple = ("cobra",)) -> bool:
    """NOISE GUARD: a post whose ONLY taxonomy match is a bare generic word (verified live
    versus Mastodon hashtags on Oct 28: yoga poses, a football coach named Cobra, and UK
    COBRA meetings all matched the plain 'cobra' keyword) is not a coverage decision.
    Posts that ALSO match any compound keyword ('cobra coverage', 'cobra ends', ...) or any
    other topic word pass through unchanged."""
    low = " " + re.sub(r"\s+", " ", text.lower()) + " "
    hits = [k for t in TOPICS.values() for k in (t["kw_en"] + t["kw_es"]) if k in low]
    return bool(hits) and all(h in generic for h in hits)


# -------------------------------------------------------------------- fetchers (explicit, offline-safe)
def _http_json(url: str) -> dict | list:
    """One polite GET of a fixed public JSON endpoint. Called ONLY from the explicit fetch path."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:   # noqa: S310 - fixed public endpoints
        return json.loads(resp.read().decode("utf-8", "replace"))


def _clean_reddit_post(p: dict) -> dict | None:
    """Rebuild a reddit post into our MINIMAL whitelist. Intentionally discards author and ids."""
    if not p or p.get("stickied") or p.get("over_18"):
        return None
    title = (p.get("title") or "").strip()
    if not title or not p.get("permalink"):
        return None
    body = _snippet((p.get("selftext") or "")[:600])
    ts = datetime.fromtimestamp(int(p.get("created_utc") or 0), tz=timezone.utc) if p.get("created_utc") else None
    return {"source": "reddit",
            "community": "https://www.reddit.com/" + (p.get("subreddit") or ""),
            "title": title, "snippet": body,
            "url": "https://www.reddit.com" + p["permalink"],
            "observed_at": ts.isoformat(timespec="seconds") if ts else None,
            "collection_method": "public_api", "access_status": "public_endpoint"}


def log_fetch(con, source, query, fetched, accepted, note):
    con.execute("INSERT INTO fetch_log (ts, source, query, fetched, accepted, note) VALUES (datetime('now'),?,?,?,?,?)",
                (source, (query or "")[:200], fetched, accepted, (note or "")[:300]))
    con.commit()


def fetch_reddit(con, queries: list[dict], get_json=_http_json, sleeper=time.sleep) -> dict:
    """Pull recent public posts from subreddits (or all of Reddit) via Reddit's public search JSON.

    queries: [{"sub": "HealthInsurance", "terms": [...]}] - terms come from the taxonomy, or
    {"sub": "", "terms": [...]} for an all-Reddit search (used for Spanish-language questions,
    whose communities we may not know). Author fields are dropped in _clean_reddit_post BEFORE
    ingest, so they never reach storage even transiently.
    """
    fetched = accepted = 0
    for q in queries:
        params = {"q": _or_search(q.get("terms") or []), "restrict_sr": int(bool(q.get("sub"))),
                  "sort": "new", "t": q.get("t", "month"), "limit": min(int(q.get("limit", 25)), 100)}
        base = (f"https://www.reddit.com/r/{q['sub']}/" if q.get("sub") else "https://www.reddit.com/") + "search.json"
        url = base + "?" + urllib.parse.urlencode(params)
        try:
            res = get_json(url)
        except Exception as e:            # noqa: BLE001 - network trouble is a reportable condition, not a crash
            log_fetch(con, "reddit", url, fetched, accepted, f"error: {type(e).__name__}: {e}")
            continue
        for child in (res.get("data", {}).get("children") or []):
            cleaned = _clean_reddit_post(child.get("data") or {})
            fetched += 1
            if cleaned and accept_filter(cleaned):
                accepted += 1
                ingest(con, [cleaned], "reddit")
        log_fetch(con, "reddit", url, fetched, accepted, "ok")
        sleeper(REQUEST_PAUSE_S)
    return {"fetched": fetched, "accepted": accepted}


def fetch_reddit_fallback(con, queries: list[dict], get_json=_http_json, sleeper=time.sleep) -> dict:
    """Reddit FALLBACK provider via the PUBLIC PullPush mirror API (api.pullpush.io —
    a free, community-run public search mirror). Used when reddit.com's own search is
    unreachable from this network (its datacenter-IP block, logged as a retryable error).

    NOT a circumvention of reddit.com: we call a DIFFERENT public service's documented
    endpoint, it returns whatever it has, and everything is treated as unverified-copy
    data (community label + title + text only). Same discipline as fetch_reddit:
    _clean_reddit_post drops the author BEFORE storage, accept_filter + URL-dedupe
    still apply, one request per query, polite pause. Config gate:
    config/sources.json > reddit_fallback > {"enabled": true, "endpoint": "..."}.
    """
    cfg = {}
    try:
        cfg = (json.loads((ROOT / "config" / "sources.json").read_text(encoding="utf-8"))
               or {}).get("reddit_fallback") or {}
    except (OSError, ValueError):
        pass
    if cfg.get("enabled") is False:          # default is ON (free public mirror)
        return {"fetched": 0, "accepted": 0, "skipped": "disabled by config (reddit_fallback.enabled=false)"}
    cool = _mirror_cooldown_iso()
    if cool and cool > datetime.now(timezone.utc).isoformat(timespec="seconds"):
        return {"fetched": 0, "accepted": 0,
                "skipped": f"mirror cooldown active until {cool} (a full retry cycle just 429'd; "
                           f"delete out/mirror_cooldown.json to force a retry)"}
    base = (cfg.get("endpoint") or "https://api.pullpush.io/reddit/search/submission/").rstrip("/") + "/"
    fetched = accepted = 0
    for q in queries:
        params = {"q": _or_search(q.get("terms") or []), "size": min(int(q.get("limit", 25)), 100),
                  "sort": "desc", "sort_type": "created",
                  "after": str(int(_min_created_epoch()))}
        if q.get("sub"):
            params["subreddit"] = q["sub"]
        url = base + "?" + urllib.parse.urlencode(params)
        try:
            # the mirror 429s a query BURST: pace BEFORE the request, and if it still 429s,
            # RETRY THE SAME QUERY for up to RETRIES attempts with a growing pause —
            # verified live Oct 28: when the mirror answers, real comment rows come back.
            sleeper(MIRROR_PAUSE_S)
            res = _mirror_get(url, get_json)
        except Exception as e:             # noqa: BLE001 - a mirror being down is a normal condition
            if "429" in str(e):
                _set_mirror_cooldown()     # full retry cycle exhausted: stop hammering this window
                log_fetch(con, "reddit_fallback", url, fetched, accepted,
                          f"error: {type(e).__name__}: {e} (mirror cooldown started)")
            else:
                log_fetch(con, "reddit_fallback", url, fetched, accepted,
                          f"error: {type(e).__name__}: {e}")
            continue
        posts = res.get("data") if isinstance(res, dict) else res
        if not isinstance(posts, list):
            posts = []
        for p in posts:
            cleaned = _clean_reddit_post(p or {})
            fetched += 1
            if cleaned and accept_filter(cleaned):
                cleaned["source"] = "reddit_fallback"          # provenance: mirror, not reddit.com
                cleaned["collection_method"] = "public_mirror_api"
                accepted += 1
                ingest(con, [cleaned], "reddit_fallback")
        log_fetch(con, "reddit_fallback", url, fetched, accepted,
                  "ok (public mirror; verify against live reddit.com before heavy use)")
        sleeper(REQUEST_PAUSE_S)
        sleeper(REQUEST_PAUSE_S)
    return {"fetched": fetched, "accepted": accepted}


def _clean_reddit_comment(c: dict) -> dict | None:
    """Rebuild ONE reddit COMMENT into our MINIMAL whitelist. Same author-minimization rule
    as _clean_reddit_post: the commenter's username/id is DISCARDED before ingest even if the
    mirror returned it. Title, text and community name are kept."""
    if not c:
        return None
    body = (c.get("body") or "").strip()
    if not body or body in ("[deleted]", "[removed]"):
        return None
    subreddit = c.get("subreddit") or ""
    cid = c.get("id") or ""
    link = (c.get("link_id") or "").split("_", 1)[-1]
    if not cid or not link:
        return None
    url = f"https://www.reddit.com/r/{subreddit}/comments/{link}/_/{cid}" if subreddit else ""
    if not url:
        return None
    ts = datetime.fromtimestamp(int(c.get("created_utc") or 0), tz=timezone.utc) if c.get("created_utc") else None
    return {"source": "reddit_comments",
            "community": ("https://www.reddit.com/r/" + subreddit)[:80],
            "title": _snippet(body[:120]),
            "snippet": _snippet(body[:600]),
            "url": url,
            "observed_at": ts.isoformat(timespec="seconds") if ts else None,
            "collection_method": "public_mirror_comment",
            "access_status": "public_endpoint"}


def fetch_reddit_comments(con, queries: list[dict], get_json=_http_json,
                          sleeper=time.sleep) -> dict:
    """COMMENT-depth discovery via the same PUBLIC mirror (api.pullpush.io — different, freely
    documented public service; nothing of reddit.com crawled or bypassed here). NEXT STOP in the
    key-blocking path: the actual question often sits in a COMMENT, not the post record, so this
    is the highest-value stretch of the signal run — people describing their own coverage need
    without using the phrase that makes the POST taxonomy match. Author fields are dropped in
    _clean_reddit_comment BEFORE ingest; accept_filter + URL-dedupe still apply; polite pause.

    config gate: config/sources.json > reddit_comments > {"enabled": true|false}."""
    cfg = {}
    try:
        cfg = (json.loads((ROOT / "config" / "sources.json").read_text(encoding="utf-8"))
               or {}).get("reddit_comments") or {}
    except (OSError, ValueError):
        pass
    if cfg.get("enabled") is False:
        return {"fetched": 0, "accepted": 0,
                "skipped": "disabled by config (reddit_comments.enabled=false)"}
    cool = _mirror_cooldown_iso()
    if cool and cool > datetime.now(timezone.utc).isoformat(timespec="seconds"):
        return {"fetched": 0, "accepted": 0,
                "skipped": f"mirror cooldown active until {cool} (skip hammering the rate window)"}
    base = (cfg.get("endpoint") or "https://api.pullpush.io/reddit/search/comment/").rstrip("/") + "/"
    fetched = accepted = 0
    for q in queries:
        params = {"q": _or_search(q.get("terms") or []), "size": min(int(q.get("limit", 25)), 100),
                  "sort": "desc", "sort_type": "created",
                  "after": str(int(_min_created_epoch()))}
        if q.get("sub"):
            params["subreddit"] = q["sub"]
        url = base + "?" + urllib.parse.urlencode(params)
        try:
            sleeper(MIRROR_PAUSE_S)
            res = _mirror_get(url, get_json)
        except Exception as e:              # noqa: BLE001 - network conditions are reportable
            if "429" in str(e):
                _set_mirror_cooldown()       # full retry cycle exhausted: stop hammering this window
                log_fetch(con, "reddit_comments", url, fetched, accepted,
                          f"error: {type(e).__name__}: {e} (mirror cooldown started)")
            else:
                log_fetch(con, "reddit_comments", url, fetched, accepted,
                          f"error: {type(e).__name__}: {e}")
            continue
        posts = res.get("data") if isinstance(res, dict) else res
        if not isinstance(posts, list):
            posts = []
        for p in posts:
            cleaned = _clean_reddit_comment(p or {})
            fetched += 1
            if cleaned and accept_filter(cleaned):
                accepted += 1
                ingest(con, [cleaned], "reddit_comments")
        log_fetch(con, "reddit_comments", url, fetched, accepted,
                  "ok (public mirror; VERIFY on live reddit.com before any public use)")
        sleeper(REQUEST_PAUSE_S)
    return {"fetched": fetched, "accepted": accepted}


# default Stack Exchange "insurance" search packs per site (site param of the DX api)
STACK_QUERIES_FALLBACK = {
    # coverage questions people genuinely ask (verified live: money.SE is the COBRA/PTC pool)
    "money": ["cobra", "obamacare", "health insurance marketplace", "premium tax credit",
              "aseguranza medica"],
    "workplace": ["health insurance cobra", "losing employer health insurance"],
}


def _stack_get(url: str) -> dict | list:
    """One polite GET of a fixed DX api endpoint. Stack's mlb always serves GZIP-encoded JSON:
    we handle the content-encoding delta here, not in callers."""
    import gzip as _gz
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json",
                                               "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:   # noqa: S310 - fixed public endpoint
        raw = resp.read()
    if raw[:2] == b"\x1f\x8b":
        raw = _gz.decompress(raw)
    return json.loads(raw.decode("utf-8", "replace"))


def _strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def fetch_stackexchange(con, sites_queries: dict[str, list[str]] | None = None,
                        get_json=None, sleeper=time.sleep) -> dict:
    """Stack Exchange PUBLIC API question search (help.health.stackexchange / money / workplace)
     --search/excerpts via the documented api (api.stackexchange.com). No key required for this
     low volume; a quota counter is read from the response and logged so fetch-log shows the
     remaining budget. These are HAND-ROUNDED questions with a real topic tag-records: person-
     shaped asks INSIDE public threads. Same accept_filter + URL-dedupe."""
    get_json = get_json or _stack_get
    if sites_queries is not None:          # explicit param wins (tests / CLI overrides)
        site_packs = {str(k): [q for q in v if q] for k, v in sites_queries.items() if v}
    else:
        site_packs = None
    cfg = {}
    try:
        cfg = (json.loads((ROOT / "config" / "sources.json").read_text(encoding="utf-8"))
               or {}).get("stackexchange") or {}
    except (OSError, ValueError):
        pass
    if cfg.get("enabled") is False:
        return {"fetched": 0, "accepted": 0, "skipped": "disabled by config (stackexchange.enabled=false)"}
    if site_packs is None:
        site_packs = {}
        cfg_sites = cfg.get("sites") or list(STACK_QUERIES_FALLBACK.keys())
        cfg_queries = cfg.get("queries") if isinstance(cfg.get("queries"), dict) else {}
        for _s in cfg_sites:
            site_packs[_s] = (cfg_queries.get(_s) or STACK_QUERIES_FALLBACK.get(_s) or [])
    fetched = accepted = 0
    for site, qpack in site_packs.items():
        for q in qpack:
            params = urllib.parse.urlencode({
                "order": "desc", "sort": "creation", "q": q, "site": site + ".stackexchange",
                "pagesize": 25})
            url = "https://api.stackexchange.com/2.3/search/excerpts?" + params
            try:
                res = get_json(url)
            except Exception as e:          # noqa: BLE001
                log_fetch(con, "stackexchange", url, fetched, accepted,
                          f"error: {type(e).__name__}: {e}")
                continue
            quota = ""
            try:
                quota = f" (quota_remaining={res.get('quota_remaining')})" if isinstance(res, dict) else ""
            except Exception:               # noqa: BLE001
                quota = ""
            rows = []
            for it in (res.get("items") or []):
                # the excerpts endpoint does NOT return ready-made links; build the durable
                # permalink from the ids it does return (question/answer canonical forms)
                qid, aid = it.get("question_id"), it.get("answer_id")
                if not qid:
                    continue
                if it.get("item_type") == "answer" and aid:
                    link = f"https://{site}.stackexchange.com/a/{aid}"
                else:
                    link = f"https://{site}.stackexchange.com/questions/{qid}"
                created = it.get("creation_date")
                if created and int(created) < _min_created_epoch():
                    continue                       # recency at the SOURCE: skip archive rows
                observed = (datetime.fromtimestamp(int(created), tz=timezone.utc)
                            .isoformat(timespec="seconds")) if created else None
                cleaned = {"source": "stackexchange",
                           "community": f"https://{site}.stackexchange.com",
                           "title": _strip_html(it.get("title")),
                           "snippet": _strip_html(it.get("excerpt"))[:500],
                           "url": link,
                           "observed_at": observed,
                           "collection_method": "public_api",
                           "access_status": "public_endpoint"}
                fetched += 1
                if accept_filter(cleaned):
                    accepted += 1
                    rows.append(cleaned)
            if rows:
                ingest(con, rows, "stackexchange")
            log_fetch(con, "stackexchange", url, fetched, accepted, "ok" + quota)
            sleeper(REQUEST_PAUSE_S)
    return {"fetched": fetched, "accepted": accepted}


# ---------------------------------------------------------------- mirror pacing
# PullPush's public mirror rate-limits a query BURST hard (verified live Oct 28: several
# consecutive 2 s-spaced calls all 429'd, then the same calls answered after a single pause).
# A sweep that outruns the mirror collects NOTHING — so mirror calls pace before each request
# and retry the SAME query with a growing pause instead of skip-to-next on 429.
MIRROR_PAUSE_S = 12.0
MIRROR_RETRIES = 3
MIRROR_RETRY_BASE_S = 20.0
# When the mirror 429s a FULL retry cycle, its per-IP window is burned: hammering it again
# right away just re-bursts. A file-based cooldown makes every later fetcher (and scheduled
# sweep) SKIP the mirror for a while, instead of stacking more 429s. Pure-local state file.
MIRROR_COOLDOWN_MIN = 20
MIRROR_COOLDOWN_FILE = ROOT / "out" / "mirror_cooldown.json"


def _mirror_cooldown_iso() -> str:
    """ISO UTC datetime until which the mirror must not be touched; '' = clear. Never raises."""
    try:
        return (json.loads(MIRROR_COOLDOWN_FILE.read_text(encoding="utf-8")) or {}).get("until", "") or ""
    except (OSError, ValueError):
        return ""


def _set_mirror_cooldown(minutes: int = MIRROR_COOLDOWN_MIN) -> None:
    until = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="seconds")
    MIRROR_COOLDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
    MIRROR_COOLDOWN_FILE.write_text(json.dumps({"until": until}), encoding="utf-8")


def _mirror_get(url: str, get_json=None) -> dict | list:
    """One mirror GET with mid-run pacing + 429 backoff-retry. Raises only after MIRROR_RETRIES
    consecutive 429s (or a non-429 error), so the caller's except branch logs the real state."""
    get_json = get_json or _http_json
    last: Exception | None = None
    for attempt in range(MIRROR_RETRIES):
        try:
            return get_json(url)
        except Exception as e:              # noqa: BLE001 - retried only when it's the burst limit
            last = e
            if "429" not in str(e):
                raise
            time.sleep(MIRROR_RETRY_BASE_S * (attempt + 1))
    raise last if last else RuntimeError("mirror exhausted retries")


def fetch_mastodon(con, cfg: dict | None = None, get_json=_http_json, sleeper=time.sleep) -> dict:
    """PUBLIC Mastodon hashtag timelines — a credential-free person-shaped source the existing
    sweep has no adapter for. GET https://<instance>/api/v1/timelines/tag/<tag> is the same
    public, credential-free JSON an instance's own web UI reads; no scraping UI, no key.

    Anything the payload carries beyond our whitelist (author handle, boost context, counts) is
    DISCARDED by _clean_mastodon BEFORE ingest: same author-minimization as every other
    source. Taxonomy accept_filter + URL-dedupe + review gates hold unchanged, and a topic
    match classifies the row kind=INTEREST by the INTEREST_SOURCES rule.

    Config gate (config/sources.json > mastodon): {"enabled": true, "instance": ..., "tags": [...]}.
    Sparse by design: needs-coverage asks are a small slice of Fediverse chatter.
    """
    if cfg is None:
        try:
            cfg = (json.loads((ROOT / "config" / "sources.json").read_text(encoding="utf-8")) or {})
        except (OSError, ValueError):
            cfg = {}
    mcfg = (cfg.get("mastodon") or {}) if isinstance(cfg, dict) else {}
    if mcfg.get("enabled") is False:
        return {"fetched": 0, "accepted": 0, "skipped": "disabled by config (mastodon.enabled=false)"}
    instance = (mcfg.get("instance") or "https://mastodon.social").rstrip("/")
    tags = [t for t in (mcfg.get("tags") or []) if isinstance(t, str) and t.strip()]
    if not tags:
        return {"fetched": 0, "accepted": 0, "skipped": "no tags configured (config/sources.json > mastodon > tags)"}
    fetched = accepted = 0
    for tag in tags:
        url = f"{instance}/api/v1/timelines/tag/{urllib.parse.quote(tag.lower())}?limit=40"
        try:
            res = get_json(url)
        except Exception as e:              # noqa: BLE001 - instance down is a reported condition
            log_fetch(con, "mastodon", url, fetched, accepted, f"error: {type(e).__name__}: {e}")
            continue
        if not isinstance(res, list):
            log_fetch(con, "mastodon", url, fetched, accepted, "error: unexpected payload type")
            continue
        rows = []
        for toot in res:
            cleaned = _clean_mastodon(toot, instance)
            if not cleaned:
                continue
            fetched += 1
            text = cleaned["title"] + " " + (cleaned["snippet"] or "")
            if accept_filter(cleaned) and not _generic_only_noise(text):
                accepted += 1
                rows.append(cleaned)
        if rows:
            ingest(con, rows, "mastodon")
        log_fetch(con, "mastodon", url, fetched, accepted, "ok")
        sleeper(REQUEST_PAUSE_S)
    return {"fetched": fetched, "accepted": accepted}


def _clean_mastodon(toot: dict, instance: str) -> dict | None:
    """Rebuild ONE public toot into the MINIMAL whitelist, discarding everything the source
    payload carries beyond it (author handle, boost provenance, counts) — the poster's
    identity is dropped by construction, exactly like reddit comment rows. Boosted (reblog)
    toots are skipped so we keep originals only."""
    if not isinstance(toot, dict) or (toot.get("reblog") or {}).get("id"):
        return None
    content = _strip_html(toot.get("content") or "")
    if not content:
        return None
    url = (toot.get("url") or "").strip()
    if not url or "://" not in url:
        return None
    return {"source": "mastodon",
            "community": urllib.parse.urlsplit(instance).netloc,
            "title": _snippet(content[:120]),
            "snippet": _snippet(content[:600]),
            "url": url,
            "observed_at": (toot.get("created_at") or "")[:19] or None,
            "collection_method": "public_api",
            "access_status": "public_endpoint"}


# ------------------------------------------------------- Bluesky / Lemmy (new free sources)
# Both verified against their public documentation Oct 2026: the public Bluesky AppView serves
# app.bsky.feed.searchPosts WITHOUT authentication (public.api.bsky.app, sort=latest, quoted
# phrases, since=YYYY-MM-DD), and Lemmy instances expose anonymous /api/v3/search reads over
# comments and posts. Both are person-shaped: same whitelist-clean (poster identity dropped),
# accept_filter, URL-dedupe and review gates as every other source.

BLUESKY_QUERIES = [   # quoted-phrase search intent; ES variants keep the Spanish lane covered
    {"q": '"health insurance"'},
    {"q": '"health insurance" "quotes"'},
    {"q": '"open enrollment" insurance'},
    {"q": '"seguro médico"'},
    {"q": '"necesito seguro"'},
    # active-need shapes: posting that you JUST lost Medicaid / need coverage is decision-point
    {"q": '"need health insurance"'},
    {"q": '"losing medicaid"'},
    {"q": '"lost my medicaid"'},
    {"q": '"self employed" "health insurance"'},
    {"q": '"necesito aseguranza"'},
    {"q": '"no tengo seguro médico"'},
]


def fetch_bluesky(con, cfg: dict | None = None, get_json=_http_json, sleeper=time.sleep) -> dict:
    """PUBLIC Bluesky post search — anonymous, credential-free. The public AppView endpoint
    (https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts) answers plain GETs with
    sort=latest, quoted-phrase q, since=<date> and lang filtering (search guide:
    bsky.social/about/blog/05-31-2024-search). Recency is at the SOURCE via since=<cutoff>.
    No cursor paging: anonymous pagination is unreliable per Bluesky docs, so each query
    takes the single freshest page and honest volume is preferred over hammering.
    Config gate (config/sources.json > bluesky): {"enabled": true, "queries": [...]}.
    """
    if cfg is None:
        try:
            cfg = (json.loads((ROOT / "config" / "sources.json").read_text(encoding="utf-8")) or {})
        except (OSError, ValueError):
            cfg = {}
    bcfg = (cfg.get("bluesky") or {}) if isinstance(cfg, dict) else {}
    if bcfg.get("enabled") is False:
        return {"fetched": 0, "accepted": 0, "skipped": "disabled by config (bluesky.enabled=false)"}
    # api.bsky.app (the direct AppView) answers anonymous GETs; public.api.bsky.app serves
    # the same xrpc from a CDN edge that 403s server/datacenter IPs (live-verified Oct 2026).
    base = (bcfg.get("instance") or "https://api.bsky.app").rstrip("/")
    queries = bcfg.get("queries") or BLUESKY_QUERIES
    fetched = accepted = 0
    for q in queries:
        params = {"q": str((q if isinstance(q, str) else q.get("q")) or "").strip(),
                  "sort": "latest", "limit": 100, "since": _iso_date()}
        if isinstance(q, dict) and q.get("lang"):
            params["lang"] = q["lang"]
        if not params["q"]:
            continue
        url = f"{base}/xrpc/app.bsky.feed.searchPosts?" + urllib.parse.urlencode(params)
        try:
            res = get_json(url)
        except Exception as e:              # noqa: BLE001 - availability is a reportable condition
            log_fetch(con, "bluesky", url, fetched, accepted, f"error: {type(e).__name__}: {e}")
            continue
        rows = []
        for post in ((res or {}).get("posts") if isinstance(res, dict) else None) or []:
            cleaned = _clean_bsky(post)
            if not cleaned:
                continue
            fetched += 1
            text = cleaned["title"] + " " + (cleaned["snippet"] or "")
            if accept_filter(cleaned) and not _generic_only_noise(text):
                accepted += 1
                rows.append(cleaned)
        if rows:
            ingest(con, rows, "bluesky")
        log_fetch(con, "bluesky", url, fetched, accepted, "ok")
        sleeper(REQUEST_PAUSE_S)
    return {"fetched": fetched, "accepted": accepted}


def _clean_bsky(post: dict) -> dict | None:
    """Whitelist-clean ONE search result: the record text, the durable bsky.app permalink
    (built from the at-uri's DID — not the poster's handle) and the post time. Everything
    the payload carries beyond that — author handle, creator DID display, like/repost
    counts, reply context — is discarded BEFORE ingest, same author-minimization as the
    reddit/mastodon cleaners."""
    if not isinstance(post, dict):
        return None
    rec = post.get("record") or {}
    text = (rec.get("text") or "").strip()
    uri = (post.get("uri") or "").strip()
    # at://<did>/app.bsky.feed.post/<rkey> — the DID is a stable resource id, not a handle
    parts = uri.split("/")
    if not text or len(parts) < 5 or not uri.startswith("at://") or parts[3] != "app.bsky.feed.post":
        return None
    return {"source": "bluesky",
            "community": "bsky.app",
            "title": _snippet(text[:120]),
            "snippet": _snippet(text[:600]),
            "url": f"https://bsky.app/profile/{parts[2]}/post/{parts[-1]}",
            "observed_at": (rec.get("created_at") or "")[:19] or None,
            "collection_method": "public_api",
            "access_status": "public_endpoint"}


LEMMY_QUERIES = [   # anonymous search terms; community names stay the community label
    {"q": "health insurance"},
    {"q": "cobra insurance"},
    {"q": "seguro médico"},
    {"q": "medicaid"},
    {"q": "need health insurance"},
    {"q": "self employed health insurance"},
]


def fetch_lemmy(con, cfg: dict | None = None, get_json=_http_json, sleeper=time.sleep) -> dict:
    """PUBLIC Lemmy COMMENT search — anonymous, credential-free. lemmy.world / lemmy.ml etc.
    answer GET /api/v3/search?q=...&type_=Comments&sort=New&listing_type=All without auth
    (live-verified Oct 2026; rate limits are per-instance operator config, so pacing is
    polite per-call). Comment content is the person-shaped layer: same inline-discipline,
    whitelist, accept_filter and review gates as every other collector.
    Config gate (config/sources.json > lemmy): {"enabled": true, "instances": [...],
    "queries": [...]}. """
    if cfg is None:
        try:
            cfg = (json.loads((ROOT / "config" / "sources.json").read_text(encoding="utf-8")) or {})
        except (OSError, ValueError):
            cfg = {}
    lcfg = (cfg.get("lemmy") or {}) if isinstance(cfg, dict) else {}
    if lcfg.get("enabled") is False:
        return {"fetched": 0, "accepted": 0, "skipped": "disabled by config (lemmy.enabled=false)"}
    queries = lcfg.get("queries") or LEMMY_QUERIES
    if not queries:
        return {"fetched": 0, "accepted": 0, "skipped": "no queries configured (lemmy.queries)"}
    instances = [i for i in (lcfg.get("instances") or ["https://lemmy.world"]) if str(i).strip()]
    fetched = accepted = 0
    for inst in instances:
        base = str(inst).rstrip("/")
        for q in queries:
            url = (f"{base}/api/v3/search?" + urllib.parse.urlencode(
                {"q": (q.get("q") or "").strip(), "type_": "Comments", "sort": "New",
                 "listing_type": "All", "limit": 50}))
            try:
                res = get_json(url)
            except Exception as e:          # noqa: BLE001 - instance down is a reported condition
                log_fetch(con, "lemmy", url, fetched, accepted, f"error: {type(e).__name__}: {e}")
                continue
            rows = []
            # /api/v3/search with type_=Comments answers {type_, comments, posts, ...}
            views = ((res or {}).get("comments") if isinstance(res, dict)
                     else (res or []) if isinstance(res, list) else []) or []
            for view in views:
                cleaned = _clean_lemmy(view)
                if not cleaned:
                    continue
                fetched += 1
                if accept_filter(cleaned):
                    accepted += 1
                    rows.append(cleaned)
            if rows:
                ingest(con, rows, "lemmy")
            log_fetch(con, "lemmy", url, fetched, accepted, "ok")
            sleeper(REQUEST_PAUSE_S)
    return {"fetched": fetched, "accepted": accepted}


def _clean_lemmy(view: dict) -> dict | None:
    """Whitelist-clean ONE comment view: the comment text, its durable ap_id permalink,
    publish time and the COMMUNITY name (a public forum, not a person). Comment author,
    creator id, score and any other payload fields are discarded BEFORE ingest."""
    if not isinstance(view, dict):
        return None
    c = view.get("comment") or {}
    text = _strip_html(c.get("content") or "")
    url = (c.get("ap_id") or "").strip()
    if not text or not url or "://" not in url or "/comment/" not in url:
        return None
    return {"source": "lemmy",
            "community": ((view.get("community") or {}).get("name") or "lemmy")[:80],
            "title": _snippet(text[:120]),
            "snippet": _snippet(text[:600]),
            "url": url,
            "observed_at": (c.get("published") or "")[:19] or None,
            "collection_method": "public_api",
            "access_status": "public_endpoint"}


def fetch_hn_comments(con, query: str, get_json=_http_json, sleeper=time.sleep) -> dict:
    """Hacker News COMMENT search (Algolia tags=comment). The comment layer below the story is
    where plain personal finance conversations about job loss, coverage, COBRA actually happen.
    Same author-dropping as post rows: comments classify as INTEREST when a taxonomy topic runs
    (see INTEREST_SOURCES — person-shaped by the same rule as reddit rows)."""
    url = "https://hn.algolia.com/api/v1/search_by_date?" + urllib.parse.urlencode(
        {"query": query, "tags": "comment", "hitsPerPage": 40,
         "numericFilters": f"created_utc_i>{int(_min_created_epoch())}"})
    try:
        res = get_json(url)
    except Exception as e:                  # noqa: BLE001
        log_fetch(con, "hn_comments", url, 0, 0, f"error: {type(e).__name__}: {e}")
        return {"fetched": 0, "accepted": 0}
    fetched = accepted = 0
    for h in (res.get("hits") or []):
        body = (h.get("comment_text") or "").strip()
        story = (h.get("story_title") or "").strip()
        if not body:
            continue
        title = ((story + " - ") if story else "") + _snippet(body[:120])
        fetched += 1
        cleaned = {"source": "hn_comments", "community": "news.ycombinator.com",
                   "title": title,
                   "snippet": _snippet(body[:600]),
                   "url": f"https://news.ycombinator.com/item?id={h.get('objectID')}",
                   "observed_at": h.get("created_at"),
                   "collection_method": "public_api", "access_status": "public_endpoint"}
        if accept_filter(cleaned):
            accepted += 1
            ingest(con, [cleaned], "hn_comments")
    log_fetch(con, "hn_comments", url, fetched, accepted, "ok")
    return {"fetched": fetched, "accepted": accepted}


def fetch_hn(con, query: str, get_json=_http_json) -> dict:
    """Hacker News via its public Algolia search API (stories mentioning the query)."""
    url = "https://hn.algolia.com/api/v1/search_by_date?" + urllib.parse.urlencode(
        {"query": query, "tags": "story", "hitsPerPage": 20,
         "numericFilters": f"created_utc_i>{int(_min_created_epoch())}"})
    try:
        res = get_json(url)
    except Exception as e:                # noqa: BLE001
        log_fetch(con, "hackernews", url, 0, 0, f"error: {type(e).__name__}: {e}")
        return {"fetched": 0, "accepted": 0}
    fetched = accepted = 0
    for h in (res.get("hits") or []):
        title = (h.get("title") or "").strip()
        ext = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        if not title:
            continue
        fetched += 1
        cleaned = {"source": "hackernews", "community": "news.ycombinator.com", "title": title,
                   "snippet": _snippet((h.get("story_text") or "")[:600]), "url": ext,
                   "observed_at": h.get("created_at"),
                   "collection_method": "public_api", "access_status": "public_endpoint"}
        if accept_filter(cleaned):
            accepted += 1
            ingest(con, [cleaned], "hackernews")
    log_fetch(con, "hackernews", url, fetched, accepted, "ok")
    return {"fetched": fetched, "accepted": accepted}


def _clean_rss_entry(xml_text: str, channel_label: str) -> dict | None:
    """Parse ONE entry of a YouTube channel's public Atom upload feed into our whitelist.
    The uploader/channel name is the COMMUNITY label (a public brand), never stored as a
    person identifier; video author handles are discarded along with everything else
    outside the whitelist."""
    import xml.etree.ElementTree as ET
    ns_a = "http://www.w3.org/2005/Atom"
    ns_yt = "http://www.youtube.com/xml/schemas/2015"
    ns_media = "http://search.yahoo.com/mrss/"
    root = ET.fromstring(xml_text)
    entries = root.findall(f"{{{ns_a}}}entry")
    out = []
    for e in entries:
        link = next((l.attrib.get("href") for l in e.findall(f"{{{ns_a}}}link") if l.attrib.get("rel") == "alternate"), "")
        title = (e.findtext(f"{{{ns_a}}}title") or "").strip()
        published = (e.findtext(f"{{{ns_a}}}published") or "").strip() or None
        vid = e.findtext(f"{{{ns_yt}}}videoId") or ""
        desc = ""
        mg = e.find(f"{{{ns_media}}}group")
        if mg is not None:
            md = mg.find(f"{{{ns_media}}}description")
            desc = (md.text or "") if md is not None else ""
        if not title or not link:
            continue
        out.append({"source": "youtube", "community": (channel_label or "youtube channel")[:80],
                    "title": title, "snippet": _snippet(desc[:600]), "url": link,
                    "observed_at": published, "video_id": vid,
                    "collection_method": "rss", "access_status": "public_endpoint"})
    return out


def fetch_youtube(con, channel_ids: list[str], fetch_text=None, sleeper=time.sleep) -> dict:
    """Collect recent uploads from PUBLIC YouTube channels via their public Atom feeds
    (https://www.youtube.com/feeds/videos.xml?channel_id=...). This is a no-credential,
    rate-limited read of what the channel already publishes to any RSS reader - the same
    feed opml subscriptions use. Still an explicit operator action (--network) and still
    filtered by the SAME taxonomy accept_filter. Keywords in titles/descriptions decide
    whether a video becomes a signal; topics carry over unchanged.

    Note: search across ALL of YouTube would require the (keyed) Data API - that path is
    documented as NEEDS_KEY in engine/sources.py and is deliberately not implemented.
    """
    def _http_text(url):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/atom+xml"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:   # noqa: S310 - fixed public endpoints
            return resp.read().decode("utf-8", "replace")

    got_text = fetch_text or _http_text
    fetched = accepted = 0
    for cid in (channel_ids or []):
        cid = (cid or "").strip()
        if not cid:
            continue
        url = f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
        try:
            raw = got_text(url)
        except Exception as e:              # noqa: BLE001
            log_fetch(con, "youtube", url, fetched, accepted, f"error: {type(e).__name__}: {e}")
            continue
        # channel label = the feed's own <title> (the public channel name, not an author handle)
        import xml.etree.ElementTree as ET
        try:
            root = ET.fromstring(raw)
            label = (root.findtext("{http://www.w3.org/2005/Atom}title") or "youtube channel")[:80]
        except ET.ParseError as e:
            log_fetch(con, "youtube", url, 0, 0, f"parse error: {e}")
            continue
        entries = _clean_rss_entry(raw, label)
        for cleaned in entries:
            fetched += 1
            if accept_filter(cleaned):
                accepted += 1
                ingest(con, [cleaned], "youtube")
        log_fetch(con, "youtube", url, fetched, accepted, "ok")
        sleeper(REQUEST_PAUSE_S)
    return {"fetched": fetched, "accepted": accepted}


def _or_search(terms: list[str]) -> str:
    return " OR ".join(f'"{t}"' for t in terms if t)


# -------------------------------------------------------------------- default search pack
def default_queries() -> list[dict]:
    """The DISCOVERY query pack: where people actually ASK for coverage help. Reddit-internal
    search stays polite (2 s pause, <=100 items), the queries are question- and need-shaped
    ("i lost my", "recommendation", "cant afford", Spanish first-person needs), and every
    returned row is still author-whitelisted, taxonomy-filtered and review-gated. Reddit
    searches are public-API reads of posts people already made to strangers - never message
    logs or DMs.

    Operator edits live in config/sources.json > reddit_queries; this pack is the fallback so
    a fresh checkout discovers out of the box. English groups hit the subreddits where the
    research (docs/LEADGEN_RESEARCH.md) says the pools actually post; Spanish groups search
    all-of-Reddit because the communities vary (r/es, r/preguntas, r/Miami...).
    """
    cfg = {}
    try:
        cfg = (json.loads((ROOT / "config" / "sources.json").read_text(encoding="utf-8")) or {})
    except (OSError, ValueError):
        pass
    if isinstance(cfg.get("reddit_queries"), list):
        return [dict(q) for q in cfg["reddit_queries"] if isinstance(q, dict)]
    return [
        # -- EN communities: coverage questions as people actually ask them
        {"sub": "HealthInsurance", "terms": ["cobra", "discontinued", "open enrollment", "subsidy",
                                             "i lost my", "recommendation", "cant afford", "can't afford"],
         "t": "month", "limit": 30},
        {"sub": "Insurance", "terms": ["health insurance", "cobra", "self employed"],
         "t": "month", "limit": 25},
        {"sub": "personalfinance", "terms": ["health insurance cobra", "health insurance subsidy"],
         "t": "month", "limit": 25},
        {"sub": "SelfEmployed", "terms": ["health insurance", "cobra", "marketplace"],
         "t": "month", "limit": 25},
        {"sub": "unemployment", "terms": ["health insurance"], "t": "month", "limit": 25},
        {"sub": "florida", "terms": ["health insurance", "open enrollment", "seguro medico"],
         "t": "month", "limit": 25},
        {"sub": "miami", "terms": ["health insurance", "seguro", "aseguranza"], "t": "month", "limit": 25},
        # -- ES all-Reddit: first-person need phrasing (communities vary)
        {"sub": "", "terms": [_strip_wild(es) for es in
                              (TOPICS["job_loss"]["kw_es"] + TOPICS["enroll_help"]["kw_es"]
                               + TOPICS["cost_savings"]["kw_es"][:2])[:14]], "t": "month", "limit": 30},
        {"sub": "", "terms": [_strip_wild(es) for es in
                              (TOPICS["carrier_exit"]["kw_es"] + TOPICS["timing"]["kw_es"]
                               + TOPICS["self_employed"]["kw_es"][:2])[:14]], "t": "month", "limit": 30},
    ]


def _strip_wild(t: str) -> str:
    return t


def default_comment_queries() -> list[dict]:
    """The COMMENT-depth pack: a SMALLER set of high-value need phrases than the post pack.
    Comments are where the actual question lives ("I'm currently on cobra and can't afford
    renewal", "seguros para autoempleados"), but every mirror call costs the rate budget:
    a burst of 12s-spaced comment queries 429s half the sweep (measured live Oct 28), so
    this pack deliberately trades breadth for actually completing.
    Operator edits live in config/sources.json > reddit_comment_queries."""
    cfg = {}
    try:
        cfg = (json.loads((ROOT / "config" / "sources.json").read_text(encoding="utf-8")) or {})
    except (OSError, ValueError):
        pass
    if isinstance(cfg.get("reddit_comment_queries"), list):
        return [dict(q) for q in cfg["reddit_comment_queries"] if isinstance(q, dict)]
    return [
        {"sub": "", "terms": ["looking for health insurance", "need health insurance",
                              "health insurance quotes"], "t": "month", "limit": 40},
        {"sub": "HealthInsurance", "terms": ["cobra", "cant afford", "can't afford"],
         "t": "month", "limit": 40},
        {"sub": "", "terms": ["seguro medico", "aseguranza", "necesito seguro"],
         "t": "month", "limit": 40},
    ]


def fetch_google_news(con, queries: list[str] | None = None, fetch_text=None,
                      sleeper=time.sleep) -> dict:
    """Collect market-context headlines from Google News PUBLIC RSS
    (news.google.com/rss/search?q=<terms>). No credentials, no scraping UI: this is the
    same feed any RSS reader shows. News headlines are CONTEXT for carrier-exit / subsidy
    content timing — never a consumer intent row: they ingest with the same URL-dedupe
    key but carry source 'google_news' and only survive the taxonomy accept_filter when
    the headline itself clearly names a coverage change. Operator-explicit (--network) LA.
    """
    import xml.etree.ElementTree as ET

    def _http_text(url):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:   # noqa: S310 - fixed public endpoint
            return resp.read().decode("utf-8", "replace")

    fetch_text = fetch_text or _http_text
    queries = queries or [
        # carrier exits + market rules we publish content about (EN/ES terms sit in the taxonomy)
        "health insurance company leaving marketplace",
        "ACA marketplace plan discontinued",
        "aseguradora deja mercado de seguros",
    ]
    fetched = accepted = 0
    for q in queries:
        q = (q or "").strip()
        if not q:
            continue
        url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote_plus(q)
               + "&hl=en-US&gl=US&ceid=US:en")
        try:
            raw = fetch_text(url)
        except Exception as e:              # noqa: BLE001
            log_fetch(con, "google_news", q, fetched, accepted, f"error: {type(e).__name__}: {e}")
            continue
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as e:
            log_fetch(con, "google_news", q, 0, 0, f"parse error: {e}")
            continue
        rows = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pubd = (item.findtext("pubDate") or "").strip()
            pub = item.find("source")
            source_label = (pub.text or "").strip() if pub is not None else ""
            if not title or not link or "://" not in link:
                continue
            cleaned = {"source": "google_news",
                       "community": (source_label or "google_news result")[:80],
                       "title": title[:300], "snippet": _snippet(""),
                       "url": link[:400], "access_status": "public_endpoint",
                       "collection_method": "news_rss",
                       "observed_at": _rfc822_iso(pubd)}
            fetched += 1
            if accept_filter(cleaned):
                accepted += 1
                rows.append(cleaned)
        if rows:
            ingest(con, rows, "google_news")
        log_fetch(con, "google_news", q, fetched, accepted, "ok")
        sleeper(REQUEST_PAUSE_S)
    return {"fetched": fetched, "accepted": accepted}


def _rfc822_iso(s: str) -> str:
    """Email-format pubDate -> ISO-8601 UTC; unparseable -> '' (ingest fills now)."""
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(s).astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return ""
