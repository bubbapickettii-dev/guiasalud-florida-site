"""ACQUISITION ASSETS — the bridge from a public intent signal to tracked content ready to publish.

What this module is: a REVIEW QUEUE of publishable content drafts. Each asset carries the
source signal (platform, language, topic, the original public URL), a piece of
platform-appropriate copy (helpful public comment / video-description text in EN or ES)
embedding ONE tracked /go link into the real consumer /check journey, and a workflow status:
PREPARED -> APPROVED (operator sign-off) -> DEPLOYED (operator actually posted it somewhere)
or SKIPPED (with a required reason). Nothing here ever posts, sends, or contacts anyone;
DEPLOYED is a bookkeeping status the operator flips after THEY publish the copy by hand.

Nothing is automatic past the draft: publishing externally — a comment, a reply, a video
description — is always an operator action on THEIR OWN platform accounts. A lead exists
only when a person clicks through and voluntarily completes /check with consent.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .content_intel import _COPY
from .signals import TOPICS

ROOT = Path(__file__).resolve().parents[1]
STATUSES = ("PREPARED", "APPROVED", "DEPLOYED", "SKIPPED")
_SHOP_CATS = ("shopping", "question")      # only these shapes become assets
_MIN_INTENT = 2.0

ASSET_SCHEMA = """
CREATE TABLE IF NOT EXISTS content_assets (
  asset_id TEXT PRIMARY KEY,               -- A + 8-hex
  signal_id INTEGER,                       -- nullable: topic-generic assets exist too
  source TEXT, lang TEXT, topic TEXT,      -- provenance snapshot (the signal row stays in signals.db)
  platform_style TEXT,                     -- COMMENT | DESC_LINK | POST
  headline TEXT, body TEXT,                -- the operator's copy-and-publish draft
  disclosure TEXT,                         -- the "we run a free check site" honesty line
  link_code TEXT,                          -- /go/<code> from the links table (same code family as signal links)
  utm_url TEXT,                            -- the FULL attributed URL stored with the draft
  source_url TEXT,                         -- the ORIGINAL public post, kept for operator context
  status TEXT DEFAULT 'PREPARED',
  created_at TEXT, decided_at TEXT, note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_acq_status ON content_assets(status);
CREATE UNIQUE INDEX IF NOT EXISTS ux_acq_signal ON content_assets(signal_id) WHERE signal_id IS NOT NULL;
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(ASSET_SCHEMA)
    con.commit()


def _conn(db_path: Path | None) -> sqlite3.Connection:
    con = sqlite3.connect(db_path or (ROOT / "data" / "leads.db"))
    con.row_factory = sqlite3.Row
    ensure_schema(con)
    return con


# ---------------------------------------------------------------- platform-appropriate shaping
PLATFORM_STYLES = {
    # source -> (style, max_body). COMMENT: short first-person-helpful public reply.
    # DESC_LINK: video/channel description text where linkouts are normal.
    "reddit": ("COMMENT", 600), "reddit_comments": ("COMMENT", 600), "reddit_fallback": ("COMMENT", 600),
    "bluesky": ("COMMENT", 300), "lemmy": ("COMMENT", 500), "mastodon": ("COMMENT", 500),
    "hackernews": ("COMMENT", 500), "hn_comments": ("COMMENT", 500),
    "stackexchange": ("COMMENT", 700), "youtube": ("DESC_LINK", 500),
}


def _style_for(source: str) -> tuple[str, int]:
    return PLATFORM_STYLES.get((source or "").lower(), ("POST", 700))


def _disclosure(lang: str) -> str:
    return ("Disclosure: I run a free eligibility-check site; no charge, nothing sold to you, "
            "and nobody contacts you unless you ask."
            if lang != "es" else
            "Aviso: tengo un sitio gratis para verificar elegibilidad; sin cobro, no te vendo nada, "
            "y nadie te contacta si no lo pides.")


def _cta(lang: str, url: str) -> str:
    return (f"Run the free check here: {url}" if lang != "es" else f"Haz el chequeo gratis aquí: {url}")


def _draft_copy(topic: str, lang: str, style: str, url: str) -> tuple[str, str, str]:
    """(headline, body, disclosure) — deterministic, facts-only frames from content_intel._COPY.
    No plan availability, savings or eligibility CLAIMS: the CTA starts the visitor's own check."""
    cp = _COPY.get(topic) or _COPY["timing"]
    if lang == "es":
        headline = cp["es_title"]
        beats = cp["beats_es"]
        hook = cp["es_hook"]
    else:
        headline = cp["en_title"]
        beats = cp["beats_en"]
        hook = cp["en_hook"]
    disclosure = _disclosure(lang)
    keep = 2 if style == "COMMENT" else len(beats)
    body = " ".join([hook] + beats[:keep] + [_cta(lang, url)])
    return headline, body, disclosure


def draft_asset(con: sqlite3.Connection, sig: dict, base_url: str = "") -> dict:
    """Build ONE asset from ONE signal row. Idempotent per signal (unique index on signal_id).
    `con` is the LEADS-DB connection (links + content_assets live there with attribution)."""
    ensure_schema(con)
    lang = (sig.get("lang") or "en")
    lang = "es" if lang == "es" else "en"
    topic = sig.get("topic") if sig.get("topic") in TOPICS else "timing"
    source = sig.get("source") or "web"
    style, _max = _style_for(source)
    # tracked link: same "s<id>" code family the signal campaign-link bridge uses — one row in links
    code = f"s{sig['id']}"
    link = con.execute("SELECT code, clicks FROM links WHERE code=?", (code,)).fetchone()
    if not link:
        camp = f"intent_{(sig.get('topic') or 'none')[:24]}_{lang}"
        con.execute("INSERT OR IGNORE INTO links (code, campaign, channel, utm_source, utm_medium, "
                    "utm_campaign, utm_content, partner_id, note, created_at, clicks) VALUES (?,?,?,?,?,?,?,?,?,?,0)",
                    (code, "health_aca", "seo", f"signal_{source[:20]}", "organic", camp,
                     f"signal_{sig['id']}", None, f"asset for signal #{sig['id']} {(sig.get('url') or '')[:180]}", now()))
        con.commit()
    utm_url = f"{(base_url or '').rstrip('/')}/go/{code}"
    headline, body, disclosure = _draft_copy(topic, lang, style, utm_url)
    aid = "A" + uuid.uuid4().hex[:10]
    con.execute("INSERT OR IGNORE INTO content_assets (asset_id, signal_id, source, lang, topic, platform_style, "
                "headline, body, disclosure, link_code, utm_url, source_url, status, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'PREPARED', ?)",
                (aid, sig["id"], source, lang, topic, style, headline[:200], body, disclosure,
                 code, utm_url, (sig.get("url") or "")[:400], now()))
    con.commit()
    return {"asset_id": aid, "headline": headline}


def draft_topic_asset(con: sqlite3.Connection, topic: str, lang: str, base_url: str = "",
                      resource_note: str = "") -> dict:
    """Topic-generic asset (no single signal): use it when the need is evidenced across several
    reviewed signals but no ONE post should anchor the copy, or when a language has no reviewed
    signal yet (the ES South-Florida wave). Still review-gated: signal_id stays NULL and the
    campaign link uses a code that points at the TOPIC, not a person's post."""
    ensure_schema(con)
    assert topic in TOPICS and lang in ("en", "es")
    style = "POST"
    code = f"t{topic[:6]}{lang}"
    link = con.execute("SELECT code FROM links WHERE code=?", (code,)).fetchone()
    if not link:
        camp = f"topic_{topic}_{lang}"
        hook_url = f"{base_url.rstrip('/')}/check?a={TOPICS[topic]['angle']}"
        con.execute("INSERT OR IGNORE INTO links (code, campaign, channel, utm_source, utm_medium, "
                    "utm_campaign, utm_content, partner_id, note, created_at, clicks) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,0)",
                    (code, "health_aca", "seo", f"topic_{topic[:16]}", "organic", camp, "topic_asset",
                     None, f"topic-generic asset ({lang}). {resource_note}"[:240], now()))
        con.commit()
        utm_url = f"{base_url.rstrip('/')}/go/{code}"
    else:
        utm_url = f"{base_url.rstrip('/')}/go/{code}"
    headline, body, disclosure = _draft_copy(topic, lang, style, utm_url)
    aid = "A" + uuid.uuid4().hex[:10]
    con.execute("INSERT INTO content_assets (asset_id, signal_id, source, lang, topic, platform_style, "
                "headline, body, disclosure, link_code, utm_url, source_url, status, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'PREPARED', ?)",
                (aid, None, "topic", lang, topic, style, headline[:200], body, disclosure,
                 code, utm_url, resource_note[:400], now()))
    con.commit()
    return {"asset_id": aid, "headline": headline}


def prepare_pending(con: sqlite3.Connection, con_sig: sqlite3.Connection, limit: int = 25,
                    only_cat: tuple[str, ...] = _SHOP_CATS) -> dict:
    """Sweep the SIGNAL store for ready candidates and draft one asset each. Ready = status
    REVIEWED or ACTIONED (a human inspected it), intent_cat actively shopping/question, and no
    asset yet. Duplicates, enrolled, news and generic rows never become assets."""
    cats = ",".join("?" for _ in only_cat)
    rows = [dict(r) for r in con_sig.execute(
        f"SELECT id, source, lang, topic, title, url, intent FROM signals "
        f"WHERE status IN ('REVIEWED','ACTIONED') AND kind='INTEREST' "
        f"AND intent_cat IN ({cats}) AND intent >= ? ORDER BY intent DESC LIMIT ?", (*only_cat, _MIN_INTENT, limit))]
    have = {r["signal_id"] for r in con.execute(
        "SELECT signal_id FROM content_assets WHERE signal_id IS NOT NULL")}
    made, skipped = [], 0
    for s in rows:
        if s["id"] in have:
            skipped += 1
            continue
        made.append(draft_asset(con, s)["headline"])
    return {"candidates": len(rows), "prepared": len(made), "already_had_asset": skipped}


def set_status(con: sqlite3.Connection, asset_id: str, status: str, note: str = "") -> bool:
    """Workflow gates are the evidence rules: SKIPPED needs a why-note; DEPLOYED needs WHERE
    the copy was actually posted and is final (the operator's publish record for that /go link)."""
    assert status in STATUSES
    if status in ("SKIPPED", "DEPLOYED") and not note:
        return False                          # missing evidence note: the decision is not recorded
    cur = con.execute("UPDATE content_assets SET status=?, decided_at=?, note=? WHERE asset_id=? AND status!='DEPLOYED'",
                      (status, now(), note[:400], asset_id))
    con.commit()
    return cur.rowcount == 1


def list_assets(con: sqlite3.Connection, status: str | None = None, limit: int = 200) -> list[dict]:
    if status:
        rows = con.execute("SELECT * FROM content_assets WHERE status=? ORDER BY created_at DESC LIMIT ?",
                           (status, limit)).fetchall()
    else:
        rows = con.execute("SELECT * FROM content_assets ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def candidate_counts(con_sig: sqlite3.Connection) -> dict:
    """Queue health for the admin header: how many reviewed signals still lack an asset."""
    c = con_sig.execute(
        "SELECT COUNT(*) n FROM signals WHERE status IN ('REVIEWED','ACTIONED') AND kind='INTEREST' "
        "AND intent_cat IN ('shopping','question') AND intent >= 2.0").fetchone()[0] \
        if hasattr(con_sig, "execute") else 0
    return {"ready_signals": c}
