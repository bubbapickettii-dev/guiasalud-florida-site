"""PLATFORM ADAPTERS-IN-WAITING — official-API paths for Meta + TikTok only.

The platforms in this module cannot honestly be "working" today: every feasible route
through them is behind credentialed official APIs that require an approved app (Meta
Graph App Review; TikTok Research/Commercial Content approval). Scraping them would
violate platform ToS, so we WILL NOT. What this module provides is the real adapter
skeleton so that, the day credentials land, collection starts by setting env vars —
no rewrite, same whitelist/review loop as every other source.

Rules baked in:
  - capability detection only by env vars (NEVER inferred, NEVER faked);
  - when a token is missing, every entry point returns {"status": "NEEDS_CREDENTIALS",
    "missing": [...]} and performs zero network calls;
  - when present, calls go to the OFFICIAL endpoints documented by the platform;
  - anything normalized for storage passes through the SAME ingest whitelist as all
    other sources (usernames and contact fields never reach data/signals.db);
  - nothing here contacts or delivers to anyone; no pipeline imports (AST-guarded).

Meta Graph paths:       ig_hashtag_search + /ig_hashtag/{tag}/top_media (business account),
                        /{page-id}/posts?fields=permalink_url,message,created_time (FB pages)
TikTok Research API:    POST /v2/research/video/query/ (approvals by platform rules)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import signals as sig

ROOT = Path(__file__).resolve().parents[1]

MISSING_ST = "NEEDS_CREDENTIALS"


def _state(env_name: str, docs_note: str) -> dict:
    """Shared renderer for the honest blocked state."""
    return {"status": MISSING_ST, "missing": [env_name], "docs": docs_note}


# --------------------------------------------------------------------------- Meta (IG + FB)
def fetch_instagram_hashtag(con, tag: str, *, client=None) -> dict:
    """Graph API hashtag media for an IG Business account we own. Requires:
    IG_GRAPH_ACCESS_TOKEN + IG_OWNED_USER_ID (app review + instagram_business_manage_insights
    scope on our own account). Hashtag search is limited to the account owner's own
    dashboard data via the official policy — we never touch strangers' media."""
    tok = (os.environ.get("IG_GRAPH_ACCESS_TOKEN") or "").strip()
    user = (os.environ.get("IG_OWNED_USER_ID") or "").strip()
    if not tok or not user:
        s = _state("IG_GRAPH_ACCESS_TOKEN",
                   "Meta Graph App Review + an IG Business account we own")
        s["missing"].append("IG_OWNED_USER_ID")
        return s
    if client is None:
        client = _graph_client()
    media = _ig_hashtag_media(tok, user, tag, client)
    rows = _normalize_mentions(media, source="instagram", community="#" + tag[:79])
    if not rows:
        return {"status": "empty", "added": 0}
    res = sig.ingest(con, rows, "instagram")
    return {"status": "ingested", "added": res["added"]}


def fetch_facebook_pages(con, page_ids: list[str], *, client=None) -> dict:
    """Graph API page posts (public pages, fields=permalink_url, message,
    created_time). Requires: FB_GRAPH_ACCESS_TOKEN (Pages Read Public Content +
    business verification)."""
    tok = (os.environ.get("FB_GRAPH_ACCESS_TOKEN") or "").strip()
    if not tok:
        return _state("FB_GRAPH_ACCESS_TOKEN", "Meta Graph app + business verification")
    if client is None:
        client = _graph_client()
    rows = []
    for pid in page_ids:
        rows += _normalize_mentions(_fb_page_posts(tok, pid, client), "facebook", pid)
    return {"status": "ingested", "added": sig.ingest(con, rows[:40], "facebook")["added"]
            if rows else 0}


# --------------------------------------------------------------------------- TikTok
def fetch_tiktok_keyword(con, keyword: str, *, client=None, start: int = 7) -> dict:
    """TikTok Research API video query. Requires: TIKTOK_RESEARCH_CLIENT_KEY +
    TIKTOK_RESEARCH_CLIENT_SECRET (approved research app)."""
    key = (os.environ.get("TIKTOK_RESEARCH_CLIENT_KEY") or "").strip()
    sec = (os.environ.get("TIKTOK_RESEARCH_CLIENT_SECRET") or "").strip()
    if not key or not sec:
        s = _state("TIKTOK_RESEARCH_CLIENT_KEY",
                   "TikTok Research API approval (application + data-access review)")
        s["missing"].append("TIKTOK_RESEARCH_CLIENT_SECRET")
        return s
    if client is None:
        client = _tiktok_client(key, sec)
    vids = list(_tiktok_query(client, keyword, start))
    rows = _normalize_mentions(vids, "tiktok", "" )
    if not rows:
        return {"status": "empty", "added": 0}
    res = sig.ingest(con, rows[:40], "tiktok")
    return {"status": "ingested", "added": res["added"]}


# --------------------------------------------------------------------------- helpers
def _graph_client():            # pragma: no cover - network only when token exists
    import urllib.request
    class _C:
        base = "https://graph.facebook.com/v21.0"
        @staticmethod
        def get(path, params):
            q = "&".join(f"{k}={params[k]}" for k in params)
            req = urllib.request.Request(f"{_C.base}/{path}?{q}&access_token="
                                         f"{os.environ.get('IG_GRAPH_ACCESS_TOKEN', '') or os.environ.get('FB_GRAPH_ACCESS_TOKEN','')}",
                                         headers={"User-Agent": "GSAlludeDiscovery/1.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:   # noqa: S310 - official API, credentialed
                return json.loads(resp.read().decode("utf-8", "replace"))
    return _C


def _ig_hashtag_media(tok, user, tag, client):   # pragma: no cover - credentialed only
    h = client.get(f"ig/{user}/ig_hashtag_search", {"q": tag, "user_id": user, "type": "top"})
    ids = [row["id"] for row in h.get("data", [])[:25]]
    rows = []
    for mid in ids:
        pub = client.get(mid, {"fields": "permalink,media_type"}).get("permalink", "")
        rows.append({"id": mid, "permalink": pub, "title": "IG media about " + tag,
                     "created_time": "", "message": ""})
    return rows


def _fb_page_posts(tok, page_id, client):   # pragma: no cover - credentialed only
    rows = client.get(page_id + "/posts", {"fields": "permalink_url,message,created_time",
                                           "limit": 25})
    return [{"permalink": r.get("permalink_url", ""),
             "title": (r.get("message") or "")[:200],
             "created_time": r.get("created_time", ""),
             "message": r.get("message") or ""}
            for r in rows.get("data", [])
            if any(w in (r.get("message") or "").lower() for w in
                   ("seguro", "insurance", "cobrar", "cobra", "obamacare", "plan"))]


def _tiktok_client(key: str, sec: str):   # pragma: no cover - credentialed-only
    import urllib.request
    class _T:
        base = "https://open.tiktokapis.com"

        @staticmethod
        def post(path, body, use_key=True):
            req = urllib.request.Request(
                _T.base + path, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json",
                         "client_key": key, "client_secret": sec})
            with urllib.request.urlopen(req, timeout=20) as resp:   # noqa: S310 - official API, credentialed
                return json.loads(resp.read().decode("utf-8", "replace"))
    return _T


def _tiktok_query(client, keyword: str, start):
    out = client.post("/v2/research/video/query/",
                      {"query": {"keyword": keyword}, "start_date": start,
                       "search_code": "0", "max_count": 20})
    for v in out.get("data", {}).get("videos", []):
        desc = (v.get("video_description") or "")[:200]
        yield {"permalink": f"https://www.tiktok.com/video/{v.get('id', '')}",
               "title": desc, "created_time": _to_iso(v.get("create_time")), "message": ""}


def _to_iso(created):   # pragma: no cover
    try:
        import datetime as dt
        return dt.datetime.fromtimestamp(int(created), dt.timezone.utc).isoformat("seconds")
    except Exception:       # noqa: BLE001
        return ""


# --------------------------------------------------------------------------- normalize
def _normalize_mentions(items, source: str, community: str) -> list[dict]:
    """Platform rows -> signals.ingest rows. URL required (sig.ingest's rule)."""
    out = []
    for it in items or []:
        url = it.get("permalink") or it.get("url") or ""
        if not url or "://" not in url:
            continue
        out.append({
            "source": source,
            "community": (community or it.get("page_id", "") or "")[:80],
            "title": (it.get("title") or "")[:300],
            "url": url[:400],
            "snippet": (it.get("message") or it.get("video_description") or "")[:1000],
            "observed_at": it.get("created_time") or "",
            "collection_method": "platform_api",
            "access_status": "credentialed_official_api",
        })
    return out
