"""DISCOVERY ADAPTERS — the active search side of acquisition.

Reads PERMITTED public pages for people openly describing a coverage need, and hands classified
findings to `engine.prospector`, which files them into `data/leads.db` (table `public_prospects`).
Nothing here contacts anyone, replies, DMs, emails or posts. Nothing bypasses a login wall, a
paywall, an API key or robots.txt: a 401/403/429 is recorded as an access restriction and the
source stops for this run.

Sources implemented (deliberately different platforms from the Bluesky/Mastodon/Lemmy profile work):
  citydata_forum  City-Data's public Health Insurance board. Large, US, active; people ask real
                  coverage questions there every day. robots.txt allows the board and thread pages
                  and only disallows /forum/search.php, which this adapter never touches.
  discourse       Any public Discourse community's /search.json (a read API intended for clients).
                  Configure instances in config/sources.json > discovery > discourse.

Reading rules baked in:
  * a delay between page fetches, and robots.txt honoured per host (cached per run);
  * every row keeps the permalink, the adapter id, the matched phrases and the source's terms note;
  * we store the PUBLIC username the page itself shows, never a guessed real name, and never an
    email we derived from a name;
  * a post is a need signal, not consent — prospector files it as NEW_PROSPECT at best.

Usage:
  engine.discovery.run(con)                       # every configured source, network ON
  engine.discovery.run(con, sources=("citydata",))# one source
The CLI wrapper (tools/discovery.py) is what an operator or cron actually calls.
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from . import prospector

ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "config" / "sources.json"
UA = "Mozilla/5.0 (compatible; GSLDiscovery/1.0; +https://example.org/bot)"
DEFAULT_DELAY_S = 1.5
# The consumer sweep an operator gets by default: every adapter that answers anonymously today.
DEFAULT_SOURCES = ("reddit_archive_search", "stackexchange", "lemmy", "mastodon", "citydata")
MAX_THREADS_PER_RUN = 10

# Boards we are willing to read, with the terms note each one carries into the row evidence.
CITYDATA_BOARDS = {
    "health-insurance": "https://www.city-data.com/forum/health-insurance/",
    "health-wellness": "https://www.city-data.com/forum/health-wellness/",
}
CITYDATA_TERMS = ("robots.txt allows /forum/ boards and thread pages (only /forum/search.php, "
                  "/forum/members and reply endpoints are disallowed); public read, no login")
DISCOURSE_TERMS = "public /search.json read API; no login, no posting; robots.txt honoured"


class AccessRestricted(Exception):
    """401/403/429 or robots.txt refusal: stop this source for the run and record why."""


class _Text(HTMLParser):
    """Visible text only: scripts, styles, noscript and SVG dropped."""

    SKIP = {"script", "style", "noscript", "svg", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())

    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.parts)).strip()


def visible_text(page: str) -> str:
    p = _Text()
    try:
        p.feed(page or "")
    except Exception:                       # noqa: BLE001 - malformed markup is not fatal
        return re.sub(r"<[^>]+>", " ", page or "")
    return p.text()


_ROBOTS: dict[str, tuple[float, bool]] = {}
_ROBOTS_TTL_S = 600.0


def robots_allows(url: str, fetcher=None) -> bool:
    """Per-host robots gate, cached for the run. Unreadable robots.txt is treated as allowed
    (we still pace requests); an explicit Disallow for us is honoured."""
    sp = urllib.parse.urlsplit(url)
    host = f"{sp.scheme}://{sp.netloc}"
    at = _ROBOTS.get(host)
    now = time.time()
    if at and now - at[0] < _ROBOTS_TTL_S:
        return at[1]
    ok = True
    try:
        txt = (fetcher or _http_text)(f"{host}/robots.txt", robots=False)
        rp = __import__("urllib.robotparser", fromlist=["RobotFileParser"]).RobotFileParser()
        rp.parse((txt or "").splitlines())
        ok = rp.can_fetch(UA, url)
    except Exception:                       # noqa: BLE001
        ok = True
    _ROBOTS[host] = (now, ok)
    return ok


def _http_text(url: str, robots: bool = True, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:   # noqa: S310 - public URL
            raw = resp.read()
            ctype = (resp.headers.get("Content-Type") or "").lower()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404, 429):
            raise AccessRestricted(f"HTTP {e.code}") from e
        raise
    charset = "utf-8"
    m = re.search(r"charset=([\w\-]+)", ctype)
    if m:
        charset = m.group(1)
    return raw.decode(charset, "replace")


def fetch(url: str, *, fetcher=None, robots: bool = True) -> str:
    if robots and not robots_allows(url, fetcher):
        raise AccessRestricted("robots.txt disallows this URL for our agent")
    return (fetcher or _http_text)(url)


# ------------------------------------------------------------------ City-Data forum
_TOPIC_HINT = re.compile(
    r"\b(insurance|coverage|covered|medicaid|medicare|cobra|aca|obamacare|marketplace|premium|"
    r"subsid\w*|enroll\w*|deductible|health plan|uninsured|afford)\b", re.IGNORECASE)
_THREAD_LINK = re.compile(r'href="(/forum/health[a-z-]*/(\d+)-[^"#?]*\.html)"[^>]*>(.{0,200}?)</a>',
                          re.S | re.IGNORECASE)


def citydata_threads(board_url: str, pages: int = 2, fetcher=None, delay: float = DEFAULT_DELAY_S) -> list[dict]:
    """Board index -> candidate threads (numeric id dedupe, title keyword prefilter)."""
    seen: dict[str, dict] = {}
    for i in range(1, max(1, pages) + 1):
        url = board_url if i == 1 else f"{board_url}index{i}.html"
        try:
            page = fetch(url, fetcher=fetcher)
        except AccessRestricted:
            if i == 1:
                raise
            break
        except Exception:                    # noqa: BLE001 - a missing deeper page is not fatal
            break
        for href, tid, raw_title in _THREAD_LINK.findall(page):
            title = html_mod.unescape(re.sub(r"<[^>]+>", "", raw_title)).strip()
            if not title or title.isdigit() or tid in seen:
                continue                       # pagination anchors ("2", "3") are not threads
            if not _TOPIC_HINT.search(title):
                continue
            seen[tid] = {"url": urllib.parse.urljoin("https://www.city-data.com", href), "title": title}
        if delay:
            time.sleep(delay)
    return list(seen.values())


_FIRST_POST = re.compile(r'id="post_message_\d+"[^>]*>(.*?)(?=<div id="post_message_|<!-- message -->|$)', re.S)


def first_post(page: str, title: str = "") -> str:
    """The OPENING POST only. A board thread page is mostly chrome, signatures and other people's
    replies; classifying all of that would turn 'someone mentioned insurance' into a prospect."""
    m = _FIRST_POST.search(page or "")
    if m:
        body = visible_text(m.group(1))
        if len(body) > 40:
            return body
    text = visible_text(page or "")
    if title:
        j = text.rfind(title[:40])          # last occurrence = the post header, not the breadcrumb
        if j > 0:
            return text[j:]
    return text


def _posted_at(page: str) -> str:
    """The date the page itself prints for the first post, ISO'd; '' when the source does not say."""
    for pat in (r'datetime="(\d{4}-\d{2}-\d{2})', r'(\d{2})-(\d{2})-(\d{4})'):
        m = re.search(pat, page)
        if not m:
            continue
        if len(m.groups()) == 1:
            return m.group(1) + "T00:00:00"
        mm, dd, yy = m.groups()
        return f"{yy}-{mm}-{dd}T00:00:00"
    return ""


def citydata(max_threads: int = MAX_THREADS_PER_RUN, pages: int = 2, fetcher=None,
             delay: float = DEFAULT_DELAY_S, boards: dict | None = None, skip: int = 4) -> dict:
    """Read the boards, open candidate threads, return classified-ready rows + run counters."""
    boards = boards or {"health-insurance": CITYDATA_BOARDS["health-insurance"]}
    rows: list[dict] = []
    fetched = 0
    errors = 0
    for name, board_url in boards.items():
        try:
            cands = citydata_threads(board_url, pages=pages, fetcher=fetcher, delay=delay)
        except AccessRestricted as e:
            return {"source": "citydata_forum", "rows": rows, "fetched": fetched, "errors": errors + 1,
                    "note": f"{name}: access restricted ({e})"}
        opened = 0
        # the first links are sticky announcements ('Start here', 'Let's talk Medigap'): real
        # questions live further down the board, so a run starts past them.
        for t in cands[skip:skip + max_threads]:
            try:
                page = fetch(t["url"], fetcher=fetcher)
            except AccessRestricted as e:
                errors += 1
                rows.append({"url": t["url"], "title": t["title"], "text": "",
                             "skip_reason": f"access restricted ({e})"})
                continue
            except Exception:                # noqa: BLE001
                errors += 1
                continue
            text = first_post(page, t["title"])
            rows.append({"url": t["url"], "title": t["title"], "text": text[:4000],
                         "observed_at": _posted_at(page), "community": f"city-data:{name}",
                         "platform": "city-data", "terms": CITYDATA_TERMS, "geo": ""})
            opened += 1
            if delay:
                time.sleep(delay)
        fetched += opened
    return {"source": "citydata_forum", "rows": rows, "fetched": fetched, "errors": errors, "note": ""}


# ------------------------------------------------------------------ Discourse
def discourse(instance: str, queries: list[str], per_query: int = 8, fetcher=None,
              delay: float = DEFAULT_DELAY_S) -> dict:
    """Read a public Discourse instance's search API for coverage-need posts."""
    base = instance.rstrip("/")
    rows: list[dict] = []
    fetched = 0
    errors = 0
    for q in queries:
        url = f"{base}/search.json?" + urllib.parse.urlencode({"q": q})
        try:
            body = fetch(url, fetcher=fetcher)
            data = json.loads(body)
        except AccessRestricted as e:
            return {"source": "discourse", "rows": rows, "fetched": fetched, "errors": errors + 1,
                    "note": f"{base}: access restricted ({e})"}
        except Exception:                    # noqa: BLE001 - bad JSON or network: count, continue
            errors += 1
            continue
        topics = {t.get("id"): t for t in (data.get("topics") or []) if isinstance(t, dict)}
        for post in (data.get("posts") or [])[:per_query]:
            topic = topics.get(post.get("topic_id")) or {}
            slug = topic.get("slug") or "t"
            url = f"{base}/t/{slug}/{post.get('topic_id')}"
            rows.append({"url": url, "title": topic.get("title") or "",
                         "text": (post.get("blurb") or "")[:2000],
                         "observed_at": (post.get("created_at") or topic.get("created_at") or "")[:19],
                         "author": post.get("username") or "",
                         "community": base.replace("https://", "").replace("http://", ""),
                         "platform": "discourse", "terms": DISCOURSE_TERMS, "geo": ""})
            fetched += 1
        if delay:
            time.sleep(delay)
    return {"source": "discourse", "rows": rows, "fetched": fetched, "errors": errors, "note": base}


# ------------------------------------------------------------------ Reddit public archive (no credential)
ARCHIVE_API = "https://arctic-shift.photon-reddit.com/api/posts/search"
ARCHIVE_FIELDS = "id,title,selftext,author,subreddit,created_utc"
ARCHIVE_TERMS = (
    "public Reddit archive API (arctic-shift): documented, credential-free, rate-limited to about "
    "one request per second and paced here at 1.5s; the archive serves already-public posts. "
    "Reddit's own JSON endpoints answer 403 to anonymous clients and PullPush returns an explicit "
    "'no free scraping for agents' refusal - neither is used, and neither is worked around")


def reddit_archive(subreddits: list[str], per_sub: int = 100, fetcher=None,
                   delay: float = 1.5) -> dict:
    """Newest posts from coverage-heavy public subreddits, read through the public archive API.

    The archive's full-text query is server-side slow and answers 422 under load, so this adapter
    asks for a plain recency listing and lets the classifier decide locally. Measured on live data:
    ~10% of the newest posts in these boards describe a coverage need, which is what makes the read
    worth the request budget.
    """
    rows: list[dict] = []
    fetched = 0
    errors = 0
    notes: list[str] = []
    for sub in subreddits:
        url = ARCHIVE_API + "?" + urllib.parse.urlencode(
            {"subreddit": sub, "limit": max(1, min(int(per_sub), 100)), "sort": "desc",
             "fields": ARCHIVE_FIELDS})
        try:
            body = (fetcher or _http_text)(url)
            data = json.loads(body or "{}")
        except AccessRestricted as e:
            notes.append(f"r/{sub}: access restricted ({e})")
            errors += 1
            continue
        except urllib.error.HTTPError as e:
            if e.code == 429:
                notes.append(f"r/{sub}: rate limited (HTTP 429) - stopping this adapter")
                errors += 1
                break
            notes.append(f"r/{sub}: HTTP {e.code}")
            errors += 1
            continue
        except Exception as e:              # noqa: BLE001 - one bad board never kills a sweep
            notes.append(f"r/{sub}: {type(e).__name__}")
            errors += 1
            continue
        posts = [p for p in (data.get("data") or []) if isinstance(p, dict)]
        for p in posts:
            text = (p.get("selftext") or "").strip()
            if not text or text in ("[removed]", "[deleted]"):
                continue
            pid = p.get("id") or ""
            if not pid:
                continue
            created = p.get("created_utc")
            try:
                observed = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(float(created)))
            except (TypeError, ValueError):
                observed = ""
            rows.append({"url": f"https://www.reddit.com/comments/{pid}",
                         "title": (p.get("title") or "")[:300], "text": text[:4000],
                         "observed_at": observed, "author": (p.get("author") or "")[:80],
                         "community": f"reddit:r/{p.get('subreddit') or sub}",
                         "platform": "reddit_archive", "terms": ARCHIVE_TERMS, "geo": ""})
            fetched += 1
        if delay:
            time.sleep(delay)
    return {"source": "reddit_archive", "rows": rows, "fetched": fetched, "errors": errors,
            "note": "; ".join(notes)[:250] or f"{len(subreddits)} board(s), {fetched} post(s) read"}


# ------------------------------------------------------------------ web search (supported APIs only)
SEARCH_TERMS = ("provider-sanctioned search API (Bing Web Search v7 / Brave / Google CSE); the "
                "engine's HTML results, captchas and rate walls are never touched")
# Query pack lives in config/sources.json > discovery > websearch > queries. Each query pairs a
# consumer intent phrase with a contact token, so the index only returns pages where somebody
# published an address or number NEXT TO the ask - the one combination a profile page never has.
_PROVIDER_ENV = {
    "bing": ("BING_SEARCH_KEY",),
    "brave": ("BRAVE_SEARCH_KEY",),
    "google_cse": ("GOOGLE_CSE_KEY", "GOOGLE_CSE_CX"),
}


def archive_post(post_id: str, fetcher=None) -> dict:
    """Read ONE archived public post in full, for operator verification.

    Why this and not the permalink: reddit.com's robots.txt is `User-agent: * -> Disallow: /`, so our
    agent does not fetch the thread page. The public archive (robots: `Disallow:` empty) stores the
    same post object - author handle, subreddit, created_utc, title, selftext, comment count - which
    is what an operator needs to confirm identity, need and currency. The permalink stays the human
    -readable source and is what gets stored on the row.
    """
    url = "https://arctic-shift.photon-reddit.com/api/posts/ids?ids=" + urllib.parse.quote(post_id)
    data = json.loads((fetcher or _http_text)(url) or "{}")
    posts = data.get("data") or []
    if not posts:
        return {}
    p = posts[0]
    return {"id": p.get("id"), "subreddit": p.get("subreddit"), "author": p.get("author"),
            "created_utc": p.get("created_utc"), "title": p.get("title") or "",
            "selftext": p.get("selftext") or "", "num_comments": p.get("num_comments"),
            "score": p.get("score"), "over_18": p.get("over_18"),
            "removed": p.get("removed_by_category") or "",
            "permalink": "https://www.reddit.com/comments/" + str(p.get("id"))}


def post_id_from_url(url: str) -> str:
    m = re.search(r"/comments/([a-z0-9]+)", (url or "").lower())
    return m.group(1) if m else ""


def _dotenv() -> dict:
    """`KEY=VALUE` lines from the project's .env (same file the app documents in .env.example).
    Read-only and best-effort: a missing or unreadable file just yields nothing."""
    out: dict[str, str] = {}
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.split("#", 1)[0].strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def search_keys(provider: str) -> dict:
    """Read the provider's credential from the environment (or .env). Absent keys mean the adapter
    reports a configuration gap instead of scraping a search-engine result page."""
    import os
    env = _dotenv()
    return {k: (os.environ.get(k) or env.get(k) or "").strip() for k in _PROVIDER_ENV.get(provider, ())}


def _search_request(provider: str, query: str, count: int, keys: dict) -> tuple[str, dict]:
    if provider == "bing":
        url = "https://api.bing.microsoft.com/v7.0/search?" + urllib.parse.urlencode(
            {"q": query, "count": count, "mkt": "en-US", "responseFilter": "Webpages"})
        return url, {"Ocp-Apim-Subscription-Key": keys["BING_SEARCH_KEY"]}
    if provider == "brave":
        url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(
            {"q": query, "count": count, "country": "us"})
        return url, {"X-Subscription-Token": keys["BRAVE_SEARCH_KEY"]}
    if provider == "google_cse":
        url = "https://www.googleapis.com/customsearch/v1?" + urllib.parse.urlencode(
            {"q": query, "num": min(count, 10), "key": keys["GOOGLE_CSE_KEY"],
             "cx": keys["GOOGLE_CSE_CX"]})
        return url, {}
    raise ValueError(f"unknown search provider {provider!r}")


def _search_results(provider: str, body: str) -> list[dict]:
    data = json.loads(body or "{}")
    if provider == "bing":
        return [{"url": r.get("url") or "", "title": r.get("name") or "",
                 "snippet": r.get("snippet") or ""}
                for r in ((data.get("webPages") or {}).get("value") or [])]
    if provider == "brave":
        return [{"url": r.get("url") or "", "title": r.get("title") or "",
                 "snippet": r.get("description") or ""}
                for r in ((data.get("web") or {}).get("results") or [])]
    return [{"url": r.get("link") or "", "title": r.get("title") or "",
             "snippet": r.get("snippet") or ""} for r in (data.get("items") or [])]


_MAIN = (r'<article[^>]*>(.*?)</article>',
         r'<(?:div|section)[^>]+class="[^"]*(?:messageBody|postbody|post_message|entry-content|'
         r'article-body|post-content|content-body)[^"]*"[^>]*>(.*?)</(?:div|section)>',
         r'<div[^>]+itemprop="(?:articleBody|text)"[^>]*>(.*?)</div>')


def _main_text(page: str) -> str:
    """The post body, not the site chrome: signature and menu text would otherwise read as author
    prose. Falls back to the whole visible page when no known container is present."""
    for pat in _MAIN:
        m = re.search(pat, page or "", re.S | re.IGNORECASE)
        if m:
            body = visible_text(m.group(1))
            if len(body) > 80:
                return body
    return visible_text(page or "")


def websearch(queries: list[str], *, provider: str = "bing", keys: dict | None = None,
              per_query: int = 10, fetcher=None, delay: float = DEFAULT_DELAY_S,
              allowed_url: str = "", api_fetcher=None) -> dict:
    """Query the provider's API, then READ each result page (robots-gated, paced) and hand the post
    text to the classifier. A result page that refuses us is counted, not retried.

    `allowed_url` is an optional regex limiting which result hosts are opened."""
    provider = (provider or "bing").lower()
    keys = keys if keys is not None else search_keys(provider)
    missing = [k for k in _PROVIDER_ENV.get(provider, ()) if not keys.get(k)]
    if missing:
        return {"source": "websearch", "rows": [], "fetched": 0, "errors": 0,
                "note": f"{provider}: no credential — set {', '.join(missing)}; the adapter will not "
                        f"scrape a search-engine result page"}
    rows: list[dict] = []
    fetched = 0
    errors = 0
    seen: set[str] = set()
    for q in queries:
        url, headers = _search_request(provider, q, per_query, keys)
        try:
            body = (api_fetcher or _http_text_with_headers)(url, headers)
        except AccessRestricted as e:
            return {"source": "websearch", "rows": rows, "fetched": fetched, "errors": errors + 1,
                    "note": f"{provider}: access restricted ({e})"}
        except Exception:                    # noqa: BLE001 - quota/network: count and continue
            errors += 1
            continue
        try:
            hits = _search_results(provider, body)[:per_query]
        except ValueError:
            errors += 1
            continue
        for h in hits:
            u = (h.get("url") or "").strip()
            if not u.startswith("http") or u in seen:
                continue
            if allowed_url and not re.search(allowed_url, u):
                continue
            seen.add(u)
            try:
                page = fetch(u, fetcher=fetcher)
            except AccessRestricted as e:
                errors += 1
                rows.append({"url": u, "title": h.get("title") or "", "text": "",
                             "skip_reason": f"access restricted ({e})"})
                continue
            except Exception:                # noqa: BLE001
                errors += 1
                continue
            rows.append({"url": u, "title": h.get("title") or "", "text": _main_text(page)[:4000],
                         "observed_at": _posted_at(page),
                         "community": "search:" + provider, "platform": provider,
                         "terms": f"{SEARCH_TERMS}; query: {q}", "geo": ""})
            fetched += 1
            if delay:
                time.sleep(delay)
    return {"source": "websearch", "rows": rows, "fetched": fetched, "errors": errors,
            "note": f"{provider}: {len(queries)} quer(ies), {len(seen)} result page(s)"}


def _http_text_with_headers(url: str, headers: dict, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:     # noqa: S310 - provider API
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (401, 402, 403, 429):
            raise AccessRestricted(f"HTTP {e.code}") from e
        raise


# ------------------------------------------------------------------ orchestration
# ------------------------------------------------------------------ credential-free community adapters
# Four sources that answer anonymous clients and let robots.txt allow the exact path we read. Each one
# returns rows shaped for prospector.ingest(); none of them contacts anyone, and none of them needs a
# key. Reddit itself is reached through the permitted third-party archive above, never through
# reddit.com (its robots.txt disallows /r/*/.rss even though the feed answers 200).
SE_TERMS = ("Stack Exchange /2.3 search API, anonymous quota; robots.txt allows /2.3; questions only, "
            "no accounts read, no posting")
LEMMY_TERMS = "Lemmy public /api/v3/search read API; anonymous; no login, no posting, no DMs"
MASTODON_TERMS = "Mastodon public tag timeline API (/api/v1/timelines/tag); anonymous; public posts only"


def search_pairs(subs: list[str], queries: list[str], limit: int) -> list[tuple]:
    """Rotate (board, query) instead of taking boards in order.

    The old slice was boards-major, so a 12-request cap spent every request on the first two boards
    (r/HealthInsurance and r/ACA) and never reached r/florida, r/tampa or r/Miami - the boards where
    an actual Florida consumer posts. Round-robin gives each board a turn, so the cap buys coverage
    instead of depth in one place.
    """
    subs = [s for s in subs if s]
    queries = [q for q in queries if q]
    out: list[tuple] = []
    seen: set[tuple] = set()
    if not subs or not queries:
        return out
    for i in range(max(1, int(limit)) * 4):
        pair = (subs[i % len(subs)], queries[(i // len(subs)) % len(queries)])
        if pair in seen:
            continue
        seen.add(pair)
        out.append(pair)
        if len(out) >= int(limit):
            break
    return out


def reddit_archive_search(pairs: list[tuple], per_query: int = 50, fetcher=None,
                          delay: float = 12.0) -> dict:
    """Subreddit-scoped full-text search on the permitted public archive.

    This is the correction to the earlier approach: reading the newest 100 posts of a board produced
    a mostly non-Florida, mostly non-shopping sample (only ~1 in 10 rows described a need, and just a
    handful named Florida). Pairing a coverage board with a state/city/situation token targets the
    people this marketplace can actually serve. The archive answers 422 'maybe slow down' under load,
    so requests are paced and the adapter backs off instead of hammering.
    """
    rows: list[dict] = []
    fetched = errors = 0
    notes: list[str] = []
    throttled = 0
    for sub, query in pairs:
        url = ARCHIVE_API + "?" + urllib.parse.urlencode(
            {"subreddit": sub, "query": query, "limit": max(1, min(int(per_query), 100)),
             "sort": "desc", "fields": ARCHIVE_FIELDS})
        data = {}
        for attempt in (1, 2):                       # one retry after a real backoff, never a spam loop
            try:
                data = json.loads((fetcher or _http_text)(url) or "{}")
                break
            except urllib.error.HTTPError as e:
                if e.code in (422, 429) and attempt == 1:
                    # the archive is volunteer-run and says 422 'maybe slow down' well before it
                    # says 429. Measured live: ~12 s between requests is safe, 3 s is not.
                    throttled += 1
                    time.sleep(min(30.0, max(delay * 1.5, 12.0)))
                    continue
                notes.append("r/%s '%s': HTTP %d" % (sub, query, e.code))
                errors += 1
                data = None
                break
            except AccessRestricted as e:
                notes.append("r/%s '%s': access restricted (%s)" % (sub, query, e))
                errors += 1
                data = None
                break
            except Exception as e:                  # noqa: BLE001 - one query never kills the sweep
                notes.append("r/%s '%s': %s" % (sub, query, type(e).__name__))
                errors += 1
                data = None
                break
        if data is None:
            continue
        if data.get("error"):
            notes.append("r/%s '%s': %s" % (sub, query, str(data["error"])[:60]))
            errors += 1
            continue
        for post in (data.get("data") or []):
            if not isinstance(post, dict):
                continue
            text = (post.get("selftext") or "").strip()
            if not text or text in ("[removed]", "[deleted]"):
                continue
            pid = post.get("id") or ""
            if not pid:
                continue
            try:
                observed = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(float(post.get("created_utc"))))
            except (TypeError, ValueError):
                observed = ""
            rows.append({"url": "https://www.reddit.com/comments/%s" % pid,
                         "title": (post.get("title") or "")[:300], "text": text[:4000],
                         "observed_at": observed, "author": (post.get("author") or "")[:80],
                         "community": "reddit:r/%s" % (post.get("subreddit") or sub),
                         "platform": "reddit_archive", "terms": ARCHIVE_TERMS, "geo": ""})
            fetched += 1
        if delay:
            time.sleep(delay)
    return {"source": "reddit_archive_search", "rows": rows, "fetched": fetched, "errors": errors,
            "note": ("; ".join(notes) or "%d targeted query/queries, %d post(s) read"
                     % (len(pairs), fetched))[:250]}


def stackexchange_search(pairs: list[tuple], per_query: int = 25, fetcher=None,
                         delay: float = 1.2) -> dict:
    """Stack Exchange questions that ask a coverage question in a target state.

    The official API needs no key at this volume. A question is weaker than a Reddit post (people ask
    hypothetically), so these rows are graded by the same classifier and a question with no stated
    location stays UNKNOWN rather than being assumed Florida.
    """
    rows: list[dict] = []
    fetched = errors = 0
    notes: list[str] = []
    for site, query in pairs:
        url = "https://api.stackexchange.com/2.3/search/advanced?" + urllib.parse.urlencode(
            {"order": "desc", "sort": "creation", "q": query, "site": site,
             "pagesize": max(1, min(int(per_query), 100)), "filter": "withbody"})
        try:
            data = json.loads((fetcher or _http_text)(url) or "{}")
        except AccessRestricted as e:
            notes.append("%s: access restricted (%s)" % (site, e))
            errors += 1
            continue
        except Exception as e:                      # noqa: BLE001
            notes.append("%s: %s" % (site, type(e).__name__))
            errors += 1
            continue
        for item in (data.get("items") or []):
            body = visible_text(item.get("body") or "")
            title = html_mod.unescape(item.get("title") or "")
            if not (body or title):
                continue
            try:
                observed = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(float(item.get("creation_date"))))
            except (TypeError, ValueError):
                observed = ""
            rows.append({"url": item.get("link") or "", "title": title[:300],
                         "text": (title + ". " + body)[:4000], "observed_at": observed,
                         "author": "", "community": "stackexchange:%s" % site,
                         "platform": "stackexchange", "terms": SE_TERMS, "geo": ""})
            fetched += 1
        if delay:
            time.sleep(delay)
    return {"source": "stackexchange", "rows": rows, "fetched": fetched, "errors": errors,
            "note": ("; ".join(notes) or "%d query/queries, %d question(s) read"
                     % (len(pairs), fetched))[:250]}


def lemmy_search(instance: str, queries: list[str], per_query: int = 20, fetcher=None,
                 delay: float = 2.0) -> dict:
    """Lemmy post search on a public instance. Posts are community threads: a reply is possible, so a
    genuine one is engagement-eligible even with no published contact."""
    rows: list[dict] = []
    fetched = errors = 0
    notes: list[str] = []
    base = (instance or "https://lemmy.world").rstrip("/")
    for query in queries:
        url = base + "/api/v3/search?" + urllib.parse.urlencode(
            {"q": query, "type_": "Posts", "sort": "New", "limit": max(1, min(int(per_query), 50))})
        try:
            data = json.loads((fetcher or _http_text)(url) or "{}")
        except AccessRestricted as e:
            notes.append("lemmy: access restricted (%s)" % e)
            errors += 1
            break
        except Exception as e:                      # noqa: BLE001
            notes.append("lemmy: %s" % type(e).__name__)
            errors += 1
            continue
        for item in (data.get("posts") or []):
            post = item.get("post") or {}
            name, body = (post.get("name") or ""), (post.get("body") or "")
            if not (name or body):
                continue
            pid, community = post.get("id"), ((post.get("community") or {}).get("name") or "lemmy")
            rows.append({"url": "https://lemmy.world/post/%s" % pid if pid else (post.get("ap_id") or ""),
                         "title": name[:300], "text": (name + ". " + body)[:4000],
                         "observed_at": (post.get("published") or "")[:19], "author": "",
                         "community": "lemmy:%s" % community, "platform": "lemmy",
                         "terms": LEMMY_TERMS, "geo": ""})
            fetched += 1
        if delay:
            time.sleep(delay)
    return {"source": "lemmy_search", "rows": rows, "fetched": fetched, "errors": errors,
            "note": ("; ".join(notes) or "%d query/queries, %d post(s) read"
                     % (len(queries), fetched))[:250]}


def mastodon_tags(instance: str, tags: list[str], per_tag: int = 25, fetcher=None,
                  delay: float = 1.5) -> dict:
    """Public Mastodon tag timelines. Tag surfaces skew toward announcements, so this source is
    expected to yield little; it is kept because it costs one request per tag and the classifier
    discards the noise."""
    rows: list[dict] = []
    fetched = errors = 0
    notes: list[str] = []
    base = (instance or "https://mastodon.social").rstrip("/")
    for tag in tags:
        url = base + "/api/v1/timelines/tag/%s?limit=%d" % (urllib.parse.quote(tag), max(1, min(int(per_tag), 40)))
        try:
            data = json.loads((fetcher or _http_text)(url) or "[]")
        except AccessRestricted as e:
            notes.append("mastodon: access restricted (%s)" % e)
            errors += 1
            break
        except Exception as e:                      # noqa: BLE001
            notes.append("mastodon: %s" % type(e).__name__)
            errors += 1
            continue
        for status in (data or []):
            text = visible_text(status.get("content") or "")
            if not text:
                continue
            rows.append({"url": status.get("url") or "", "title": text[:120],
                         "text": text[:4000], "observed_at": (status.get("created_at") or "")[:19],
                         "author": "", "community": "mastodon:%s#%s" % (base.split("//")[-1], tag),
                         "platform": "mastodon", "terms": MASTODON_TERMS, "geo": ""})
            fetched += 1
        if delay:
            time.sleep(delay)
    return {"source": "mastodon_tags", "rows": rows, "fetched": fetched, "errors": errors,
            "note": ("; ".join(notes) or "%d tag(s), %d post(s) read" % (len(tags), fetched))[:250]}


def mastodon_search(instance: str, queries: list[str], per_query: int = 20, fetcher=None,
                    delay: float = 2.0) -> dict:
    """Full-text search on a public Mastodon instance (`/api/v2/search?type=statuses`).

    MEASURED, 2026-10-31: every instance probed (mastodon.social, mstdn.social, fosstodon.org, mas.to,
    mastodon.world, infosec.exchange, hachyderm.io) answers this endpoint anonymously with HTTP 200
    and an EMPTY `statuses` array, even for a broad query like 'insurance'. Mastodon restricts
    full-text status search to authenticated clients, so this adapter records a truthful zero and is
    NOT part of the default sweep; the tag timelines are the working keyless Mastodon route.
    It is kept so the zero is visible in `discovery_runs` instead of the category silently vanishing.
    """
    rows: list[dict] = []
    fetched = errors = 0
    notes: list[str] = []
    base = (instance or "https://mastodon.social").rstrip("/")
    for q in queries:
        url = base + "/api/v2/search?" + urllib.parse.urlencode(
            {"q": q, "type": "statuses", "limit": max(1, min(int(per_query), 40)), "resolve": "false"})
        try:
            data = json.loads((fetcher or _http_text)(url) or "{}")
        except AccessRestricted as e:
            notes.append("mastodon search: access restricted (%s)" % e)
            errors += 1
            break
        except Exception as e:                      # noqa: BLE001
            notes.append("mastodon search '%s': %s" % (q[:20], type(e).__name__))
            errors += 1
            continue
        for status in (data.get("statuses") or []):
            if (status.get("visibility") or "public") != "public":
                continue                            # never store a non-public status
            text = visible_text(status.get("content") or "")
            if not text:
                continue
            acct = ((status.get("account") or {}).get("acct") or "").lstrip("@")
            rows.append({"url": status.get("url") or "", "title": text[:120],
                         "text": text[:4000], "observed_at": (status.get("created_at") or "")[:19],
                         "author": acct[:80],
                         "community": "mastodon:%s#search" % base.split("//")[-1],
                         "platform": "mastodon", "terms": MASTODON_TERMS,
                         "lang": (status.get("language") or "")[:5], "geo": ""})
            fetched += 1
        if delay:
            time.sleep(delay)
    return {"source": "mastodon_search", "rows": rows, "fetched": fetched, "errors": errors,
            "note": ("; ".join(notes) or "%d query/queries, %d public status(es) read"
                     % (len(queries), fetched))[:250]}


def load_cfg() -> dict:
    try:
        return (json.loads(CFG_PATH.read_text(encoding="utf-8")) or {}).get("discovery") or {}
    except (OSError, ValueError):
        return {}


def run(con, *, sources=None, max_threads: int | None = None, fetcher=None,
        delay: float | None = None, cfg: dict | None = None) -> list[dict]:
    """One discovery sweep. Every adapter's outcome is recorded in `discovery_runs` — including a
    zero — so a source that stops working is visible instead of quietly disappearing."""
    cfg = cfg if cfg is not None else load_cfg()
    if sources is None:
        sources = tuple(cfg.get("default_sources") or DEFAULT_SOURCES)
    delay = DEFAULT_DELAY_S if delay is None else delay
    max_threads = max_threads or int(cfg.get("max_threads_per_run") or MAX_THREADS_PER_RUN)
    out: list[dict] = []
    for name in sources:
        name = name.lower()
        try:
            if name in ("citydata", "citydata_forum"):
                boards = dict(CITYDATA_BOARDS)
                picked = (cfg.get("citydata") or {}).get("boards") or ["health-insurance"]
                res = citydata(max_threads=max_threads, pages=int(cfg.get("pages_per_board") or 3),
                               fetcher=fetcher, delay=delay,
                               boards={b: boards[b] for b in picked if b in boards},
                               skip=int(cfg.get("skip_per_board") or 4))
            elif name == "discourse":
                instances = (cfg.get("discourse") or {}).get("instances") or []
                queries = (cfg.get("discourse") or {}).get("queries") or []
                if not instances:
                    res = {"source": "discourse", "rows": [], "fetched": 0, "errors": 0,
                           "note": "no public instance configured (config/sources.json > discovery > discourse)"}
                else:
                    rows, fetched, errors = [], 0, 0
                    notes = []
                    for inst in instances:
                        try:
                            r = discourse(inst, queries, fetcher=fetcher, delay=delay)
                        except Exception as e:      # noqa: BLE001
                            errors += 1
                            notes.append(f"{inst}: {type(e).__name__}")
                            continue
                        rows += r["rows"]
                        fetched += r["fetched"]
                        errors += r["errors"]
                        notes.append(r.get("note") or "")
                    res = {"source": "discourse", "rows": rows, "fetched": fetched, "errors": errors,
                           "note": "; ".join(n for n in notes if n)[:250]}
            elif name in ("reddit", "reddit_archive"):
                rcfg = cfg.get("reddit_archive") or {}
                subs = [s for s in (rcfg.get("subreddits") or []) if s]
                if not subs:
                    res = {"source": "reddit_archive", "rows": [], "fetched": 0, "errors": 0,
                           "note": "no boards configured (config/sources.json > discovery > reddit_archive)"}
                else:
                    res = reddit_archive(subs, per_sub=int(rcfg.get("per_sub") or 100),
                                         fetcher=fetcher, delay=max(delay, 1.5))
            elif name == "reddit_archive_search":
                rcfg = cfg.get("reddit_archive") or {}
                subs = [s for s in (rcfg.get("subreddits") or []) if s]
                queries = [q for q in (rcfg.get("search_queries") or []) if q]
                pairs = search_pairs(subs, queries, int(rcfg.get("max_queries") or 12))
                if not pairs:
                    res = {"source": "reddit_archive_search", "rows": [], "fetched": 0, "errors": 0,
                           "note": "no boards/queries configured (config/sources.json > discovery > reddit_archive)"}
                else:
                    res = reddit_archive_search(pairs, per_query=int(rcfg.get("per_query") or 50),
                                                fetcher=fetcher, delay=max(delay, 3.0))
            elif name in ("stackexchange", "stackexchange_search"):
                scfg = cfg.get("stackexchange") or {}
                sites = [x for x in (scfg.get("sites") or []) if x]
                queries = [q for q in (scfg.get("queries") or []) if q]
                pairs = [(site, q) for site in sites for q in queries][:int(scfg.get("max_queries") or 12)]
                if not pairs:
                    res = {"source": "stackexchange", "rows": [], "fetched": 0, "errors": 0,
                           "note": "no sites/queries configured (config/sources.json > discovery > stackexchange)"}
                else:
                    res = stackexchange_search(pairs, per_query=int(scfg.get("per_query") or 25),
                                               fetcher=fetcher, delay=delay)
            elif name in ("lemmy", "lemmy_search"):
                lcfg = cfg.get("lemmy") or {}
                queries = [q for q in (lcfg.get("queries") or []) if q]
                res = (lemmy_search(lcfg.get("instance") or "https://lemmy.world", queries,
                                    per_query=int(lcfg.get("per_query") or 20), fetcher=fetcher,
                                    delay=max(delay, 2.0))
                       if queries else
                       {"source": "lemmy_search", "rows": [], "fetched": 0, "errors": 0,
                        "note": "no queries configured (config/sources.json > discovery > lemmy)"})
            elif name in ("mastodon", "mastodon_tags"):
                mcfg = cfg.get("mastodon") or {}
                tags = [t for t in (mcfg.get("tags") or []) if t]
                res = (mastodon_tags(mcfg.get("instance") or "https://mastodon.social", tags,
                                     per_tag=int(mcfg.get("per_tag") or 25), fetcher=fetcher, delay=delay)
                       if tags else
                       {"source": "mastodon_tags", "rows": [], "fetched": 0, "errors": 0,
                        "note": "no tags configured (config/sources.json > discovery > mastodon)"})
            elif name in ("mastodon_search",):
                mcfg = cfg.get("mastodon") or {}
                queries = [q for q in (mcfg.get("search_queries") or []) if q]
                res = (mastodon_search(mcfg.get("instance") or "https://mastodon.social", queries,
                                       per_query=int(mcfg.get("per_query") or 20), fetcher=fetcher,
                                       delay=max(delay, 2.0))
                       if queries else
                       {"source": "mastodon_search", "rows": [], "fetched": 0, "errors": 0,
                        "note": "no search_queries configured (config/sources.json > discovery > mastodon)"})
            elif name in ("websearch", "search"):
                wcfg = cfg.get("websearch") or {}
                res = websearch(
                    list(wcfg.get("queries") or []),
                    provider=wcfg.get("provider") or "bing",
                    per_query=int(wcfg.get("results_per_query") or 10),
                    fetcher=fetcher, delay=delay,
                    allowed_url=wcfg.get("allowed_url") or "",
                    api_fetcher=(cfg.get("_api_fetcher") or None))
            else:
                res = {"source": name, "rows": [], "fetched": 0, "errors": 1, "note": "unknown source"}
        except Exception as e:                  # noqa: BLE001 - an adapter crash is a recorded error
            res = {"source": name, "rows": [], "fetched": 0, "errors": 1,
                   "note": f"{type(e).__name__}: {e}"[:200]}
        usable = [r for r in res["rows"] if not r.get("skip_reason")]
        stats = prospector.ingest(con, usable, res["source"])
        stats["fetched"] = res.get("fetched", 0)
        stats["errors"] = stats["errors"] + res.get("errors", 0)
        prospector.record_run(con, res["source"], stats, res.get("note") or "")
        out.append({"source": res["source"], **stats, "note": res.get("note") or ""})
    return out
