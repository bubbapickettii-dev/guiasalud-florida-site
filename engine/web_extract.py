"""PUBLIC WEB EXTRACTION — the ScrapeGraphAI-integrated reader for our discovery loop.

Ground rules (docs/SOURCES.md): this module collects PUBLIC WEB PAGES only, normalized
into the EXISTING signals schema (data/signals.db via engine.signals) — no second
signal store, no contact fields, same review workflow, same whitelist. Nothing here
imports the leads pipeline (AST-guarded like signals.py).

Capability detection is honest, three-way:
  sga_lib_installed — is `scrapegraphai` importable? (importlib probe, never a blind import)
  provider          — which model provider could power SGA graphs:
                        "ollama"    → our local Ollama (probed at 127.0.0.1:11434, no cost)
                        "openai"    → OPENAI_API_KEY exists, but usage IS billed: never
                                      used automatically (operator approval required)
                        "none"      → no provider; only the fallback reader
  effective_mode    — "sga" (lib + local Ollama) > "ollama_json" (local Ollama alone:
                      direct instruction-driven extraction over the fetched page, NO
                      scrapegraphai needed — stdlib HTTP to the local API) > the stdlib
                      "fallback_reader" that works TODAY. All three feed the SAME loop
                      (same schema, dedupe, review).

The fallback reader is a real working collector now: fetch a page (robots.txt
respected, polite UA, bounded timeout), strip scripts/styles, extract title + visible
text + same-domain links, classify EN/ES + topic through the signals taxonomy, ingest
with the same URL dedupe key. When ScrapeGraphAI + a local model become available,
`extract_sga()` upgrades extraction to instruction-driven structured JSON with zero
changes elsewhere.

Same-domain "multi-page research" (`collect_site`) follows discovered links (bounded
N pages, one request per page, PAUSE_S between fetches, robots-checked).

Network happens only when the CALLER decides (explicit --network CLI flag; operator
action). Every fetch is injectable (`fetch_text`) so the whole path tests offline.
"""
from __future__ import annotations

import html.parser as html_
import importlib.util
import json
import os
import re
import time
import urllib.request
import urllib.robotparser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from . import signals as sig
from . import ollama_client as oai

ROOT = Path(__file__).resolve().parents[1]
SOURCES_CFG = ROOT / "config" / "sources.json"

UA = ("GSAlludeWebReader/1.0 (public-content discovery for our own product research; "
      "robots respected; no personal harvesting)")
HTTP_TIMEOUT_S = 20
PAUSE_S = 2.0                                  # politeness floor between same-host fetches
OLLAMA_URL = "http://127.0.0.1:11434"

DEFAULT_INSTRUCTIONS = (
    "Extract from this page: the main topic; any concrete facts about health-insurance "
    "enrollment, subsidies, coverage changes, deadlines or costs; whether the content "
    "reads as a consumer QUESTION or as authoritative information. JSON only.")


# ------------------------------------------------------------------ capability status
def sga_status(probe_ollama: bool = True) -> dict:
    """Detection only — installs nothing, spends nothing, external network never used
    (the Ollama probe is localhost). probe_ollama=False for hermetic tests."""
    lib = importlib.util.find_spec("scrapegraphai") is not None
    provider = "none"
    if probe_ollama and _ollama_alive():
        provider = "ollama"
    elif (os.environ.get("OPENAI_API_KEY") or "").strip():
        # a key exists but OpenAI usage is billed; never auto-used
        provider = "openai_available_not_approved"
    if lib and provider == "ollama":
        mode = "sga"
    elif provider == "ollama":
        # scrapegraphai absent, but the LOCAL MODEL IS THERE: direct instruction-driven
        # extraction still upgrades the loop (no second dependency, no pip install)
        mode = "ollama_json"
    else:
        mode = "fallback_reader"
    return {"sga_lib_installed": lib, "provider": provider, "effective_mode": mode,
            "ollama": oai.health(check_model=False) if provider == "ollama" else None,
            "fallback_notes": "stdlib HTML reader: robots-checked, classified EN/ES, "
                              "ingested into the standard signal loop"}


def _ollama_alive() -> bool:
    try:
        req = urllib.request.Request(OLLAMA_URL + "/api/tags", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=3) as resp:    # noqa: S310 - localhost only
            return resp.status == 200
    except Exception:                       # noqa: BLE001 - not installed / not running
        return False


# ------------------------------------------------------------------ fetching
def _real_fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
        # noqa: S310 - operator-chosen public URL, robots-checked by the caller
        return resp.read().decode("utf-8", "replace")


_ROBOTS_CACHE: dict[str, tuple[float, bool]] = {}
_ROBOTS_TTL_S = 300.0                       # politeness: don't re-fetch per page of one run


def _robots_allowed(url: str, fetch_text) -> bool:
    """robots.txt gate per URL; same fetch injection keeps tests offline. Cache by host
    for a short TTL so a site walk fetches robots.txt once per host, not per page.
    Unreachable robots file is treated conservatively as allowed (not a license to
    hammer — the page fetch itself still respects the pause)."""
    sp = urlsplit(url)
    cache_key = f"{sp.scheme}://{sp.netloc}"
    at = _ROBOTS_CACHE.get(cache_key)
    now = time.time()
    if at and now - at[0] < _ROBOTS_TTL_S:
        return at[1]
    try:
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(fetch_text(f"{cache_key}/robots.txt").splitlines())
        ok = rp.can_fetch(UA, url)
    except Exception:                       # noqa: BLE001
        ok = True
    _ROBOTS_CACHE[cache_key] = (now, ok)
    return ok


# ------------------------------------------------------------------ HTML reader (fallback)
class PageReader(html_.HTMLParser):
    """Stdlib-only visible-text + link extraction. Script/style blocks dropped;
    only same-domain navigation links kept for discovery."""

    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.host = urlsplit(base_url).netloc.replace("www.", "").lower()
        self.title = ""
        self._title_on = False
        self.skip = 0                       # >0 while inside script/style/hidden markup
        self._chunks: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg", "template"):
            self.skip += 1
            return
        if tag == "title":
            self._title_on = True
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v:
                    u = urljoin(self.base, v.strip())
                    if (u.startswith(("http://", "https://"))
                            and urlsplit(u).netloc.replace("www.", "").lower() == self.host
                            and not re.search(r"\.(jpg|png|gif|svg|css|js|ico|woff2?)(\?|$)",
                                              urlsplit(u).path, re.I)
                            and u not in self.links):
                        self.links.append(u)

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg", "template") and self.skip:
            self.skip -= 1
        elif tag == "title":
            self._title_on = False

    def handle_data(self, data):
        if self._title_on:
            self.title = " ".join((self.title, data.strip())).strip()[:300]
            return
        if self.skip:
            return
        t = data.strip()
        if t:
            self._chunks.append(t)

    def text(self, limit: int = 4000) -> str:
        return re.sub(r"\s+", " ", " ".join(self._chunks)).strip()[:limit]


def read_page(raw_html: str, url: str) -> dict:
    """raw HTML -> {title, text, links}. No network; pure function."""
    r = PageReader(url)
    try:
        r.feed(raw_html)
        r.close()
    except Exception:                       # noqa: BLE001 - malformed HTML: take what we got
        pass
    return {"title": r.title, "text": r.text(), "links": r.links}


# ------------------------------------------------------------------ classification + ingest
def to_signal_row(url: str, page: dict, *, source: str = "webreader",
                  collection_method: str = "public_page_read",
                  observed_at: str = "") -> dict:
    """Normalize one extracted page into the shared signal-schema row
    (same whitelist keys engine.signals.ingest accepts)."""
    text = " ".join(x for x in (page.get("title"), page.get("text")) if x).strip()
    host = urlsplit(url).netloc[:80]
    if host.startswith("www."):
        host = host[4:]
    return {"source": source, "url": url, "title": (page.get("title") or url)[:300],
            "snippet": sig.classify(text).get("snippet", text[:240]),
            "community": host,
            "collection_method": collection_method, "access_status": "public_page",
            "observed_at": observed_at, "lang_hint": ""}


# ------------------------------------------------- extraction metadata store
# One table, lazily created, in signals.db: instruction-driven extraction results,
# keyed to the signal URL. This is REVIEWER-EVIDENCE data (what the local model said
# about the page) — it never contains author identifiers and is never exported.
_EXTRACT_DDL = """CREATE TABLE IF NOT EXISTS signal_extract (
    id INTEGER PRIMARY KEY,
    signal_url TEXT UNIQUE NOT NULL,
    mode TEXT NOT NULL,
    model TEXT,
    ok INTEGER NOT NULL,
    payload TEXT,
    error TEXT,
    ts TEXT NOT NULL DEFAULT (datetime('now'))
)"""


def _save_extract(con, signal_url: str, mode: str, *, model: str = "", ok: bool,
                  payload: str = "", error: str = "") -> int:
    con.execute(_EXTRACT_DDL)
    con.execute(
        "INSERT INTO signal_extract (signal_url, mode, model, ok, payload, error) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(signal_url) DO UPDATE SET mode=excluded.mode, "
        "model=excluded.model, ok=excluded.ok, payload=excluded.payload, "
        "error=excluded.error, ts=excluded.ts",
        (signal_url, mode, model, 1 if ok else 0, payload[:20000], (error or "")[:500]))
    con.commit()
    return con.execute("SELECT id FROM signal_extract WHERE signal_url=?",
                       (signal_url,)).fetchone()[0]


def _extract_store_row(con, signal_url: str) -> dict | None:
    try:
        r = con.execute("SELECT * FROM signal_extract WHERE signal_url=?",
                        (signal_url,)).fetchone()
        return dict(r) if r else None
    except Exception:                       # noqa: BLE001 - table may not exist yet
        return None


def collect_page(con, url: str, *, fetch_text=None, source: str = "webreader",
                 title: str | None = None, text: str | None = None,
                 instructions: str = "") -> dict:
    """One page through the loop: robots -> read -> classify -> ingest (URL dedupe).
    `fetch_text` injectable; `title`/`text` pre-extracted (single-fetch path from
    collect_site). Offline-safe."""
    fetch_text = fetch_text or _real_fetch_text
    if not _robots_allowed(url, fetch_text):
        return {"status": "robots_denied", "url": url}
    if title is None or text is None:
        try:
            page = read_page(fetch_text(url), url)
        except Exception as e:              # noqa: BLE001
            return {"status": "fetch_error", "reason": f"{type(e).__name__}: {e}"}
        title, text = page["title"], page["text"]
    if title is None and text is None:
        return {"status": "empty_page", "url": url}
    row = to_signal_row(url, {"title": title or "", "text": text or "", "links": []},
                        source=source)
    res = sig.ingest(con, [row], source)
    # The AI pass decides live (NOT the hermetic-probe status): reach a local model
    # when one answers. Only PUBLIC PAGE text goes to the model: never lead/
    # consent/PII fields.
    ai_capable = _ollama_alive()
    if ai_capable and importlib.util.find_spec("scrapegraphai") is not None:
        want_mode = "sga"                      # full graph engine via the lib when present
    elif ai_capable:
        want_mode = "ollama_json"              # direct local-model extraction, no lib needed
    else:
        want_mode = "fallback_reader"
    out = {"status": "ingested", "added": res["added"], "title": (title or url)[:120],
           "extract_mode": want_mode}
    # AI pass: run the instruction-driven extraction on fresh pages when a local
    # model is reachable. Best-effort — a failed/hitless extraction never rolls
    # back the signal, it is recorded in signal_extract for reviewer evidence.
    if res["added"] and want_mode in ("sga", "ollama_json"):
        _ollama_page_extract(con, url, title or "", text or "",
                             instructions or DEFAULT_INSTRUCTIONS)
        got = _extract_store_row(con, url)
        if got:
            out["ai_extract"] = "ok" if got["ok"] else "failed"
            if not got["ok"]:
                out["ai_error"] = (got["error"] or "")[:160]
    return out


def _ollama_page_extract(con, url: str, title: str, text: str,
                         instructions: str) -> None:
    """Direct local-Ollama structured extraction over the already-fetched page text.
    Failure is DURABLE but non-fatal: stored in signal_extract, collection continues."""
    body = (title + "\n\n" + text)[:4000]        # bound the prompt; local model, CPU cost
    prompt = (f"{instructions}\n\nPAGE TEXT START\n{body}\nPAGE TEXT END\n\n"
              "Respond with a JSON object only.")
    try:
        r = oai.generate_json(prompt, timeout=180.0)
        _save_extract(con, url, "ollama_json", model=r["model"], ok=True,
                      payload=json.dumps(r["json"], ensure_ascii=False)[:8000])
    except Exception as e:                  # noqa: BLE001 - collect continues anyway
        _save_extract(con, url, "ollama_json", ok=False, error=f"{type(e).__name__}: {e}")


def collect_site(con, seed_url: str, *, max_pages: int = 5, fetch_text=None,
                 source: str = "webreader", instructions: str = "") -> dict:
    """Seed page + up to max_pages-1 same-domain discovered pages. Exactly one fetch
    per page plus one robots.txt per host; PAUSE_S between fetches; breadth-first."""
    fetch_text = fetch_text or _real_fetch_text
    if not _robots_allowed(seed_url, fetch_text):
        return {"status": "robots_denied", "url": seed_url}
    seen: set[str] = set()
    queue: list[str] = [seed_url]
    host = urlsplit(seed_url).netloc.replace("www.", "").lower()
    fetched = added = 0
    results = []
    while queue and fetched < max_pages:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            page = read_page(fetch_text(url), url)
        except Exception as e:              # noqa: BLE001
            results.append({"url": url, "status": "fetch_error",
                            "reason": f"{type(e).__name__}: {e}"})
            continue
        res = collect_page(con, url, fetch_text=fetch_text, source=source,
                           title=page["title"], text=page["text"],
                           instructions=instructions)
        results.append({"url": url, **res})
        if res["status"] != "ingested":
            continue
        fetched += 1
        added += res.get("added", 0)
        for link in page["links"][:20]:
            if (urlsplit(link).netloc.replace("www.", "").lower() == host
                    and link not in seen):
                queue.append(link)
        if queue and fetched < max_pages:
            time.sleep(PAUSE_S)
    return {"status": "ok", "fetched": fetched, "added": added, "results": results}


# ------------------------------------------------------------------ ScrapeGraphAI path
def extract_sga(url: str, instructions: str = DEFAULT_INSTRUCTIONS, *,
                allow_openai: bool = False) -> dict:
    """Instruction-driven structured extraction via ScrapeGraphAI's SmartScraperGraph.

    Honest guards: raises NotImplementedError (with the exact missing dependency)
    unless scrapegraphai is installed AND a local Ollama provider answers. An
    OpenAI-backed graph additionally requires allow_openai=True AND a key — and that
    kwarg is only ever set from operator-approved config, because OpenAI usage is
    billed. No charges can happen from just importing this module.
    """
    if importlib.util.find_spec("scrapegraphai") is None:
        raise NotImplementedError(
            "scrapegraphai is not installed (it needs Python >=3.12 + playwright "
            "`playwright install`); the fallback reader keeps the loop working meanwhile")
    if not _ollama_alive():
        if not allow_openai:
            raise NotImplementedError(
                f"no local model provider available: Ollama not reachable at {OLLAMA_URL}; "
                "OpenAI nodes are billed and were not approved (allow_openai never default)")
        if not (os.environ.get("OPENAI_API_KEY") or "").strip():
            raise NotImplementedError("allow_openai was passed but OPENAI_API_KEY is empty")
    from scrapegraphai.graphs import SmartScraperGraph   # late import after the probes
    # model is the ACTUALLY-SELECTED local model (ollama_client picks env/config/
    # auto-discovered); never a hardcoded name that may not be pulled on this machine
    try:
        health = oai.health(check_model=False)
        model = health.get("selected_model") or ""
    except Exception:                       # noqa: BLE001
        model = ""
    if not model:
        raise NotImplementedError(
            "no Ollama model selected: run `python tools/collect.py ai-health` to see "
            "installed models, then set OLLAMA_MODEL or config/sources.json > ollama > model")
    graph = SmartScraperGraph(
        prompt=instructions, source=url,
        config={"llm": ({"model": f"ollama/{model}", "model_tokens": 8192, "format": "json"}
                        if _ollama_alive() else
                        {"model": "openai/gpt-4o-mini", "api_key": os.environ["OPENAI_API_KEY"]}),
                "verbose": False, "headless": True})
    out = graph.run()
    return out if isinstance(out, dict) else {"result": out}
