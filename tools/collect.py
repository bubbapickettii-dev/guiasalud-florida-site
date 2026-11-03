"""COLLECT: one command that runs EVERY eligible signal collector, then reports.

This is the operational wrapper the integrations console points at. It reuses the
existing collectors — no second signal store, no new source, no new dependency:

  python tools/collect.py status            per-source signal + fetch-log status from data/signals.db
  python tools/collect.py run --network     sweep ALL eligible collectors (explicit operator action)
  python tools/collect.py report            writes out/collect_report.md (aggregate + last fetches)
  python tools/collect.py schedule-cmd      prints ready-to-use cron / Windows-scheduler lines
  python tools/collect.py schedule-install  register the DAILY sweep with Windows Task Scheduler
                                            (schtasks) at config/schedule.json > daily_time;
                                            cooldown + run lock prevent overlapping re-sweeps;
                                            remove anytime with schedule-remove. POSIX hosts print
                                            the cron line instead — no silent auto-install
  python tools/collect.py clusters-build    group collected signals into prioritized opportunity
                                            clusters via the LOCAL Ollama model (facts stay facts,
                                            model text is stored as labeled inference)
  python tools/collect.py clusters-list     print stored clusters [+ --status NEW|REVIEWED|DISMISSED]
  python tools/collect.py prospect-scan     mine PUBLISHED contacts from public INTEREST signals
                                            into data/prospect_people.db (evidence-gated,
                                            DISCOVERED->CONTACT_FOUND->...; never a lead)
  python tools/collect.py people-list       print the latest prospect rows [--stage STAGE]

The sweep covers exactly the sources the runtime registry (engine/sources.py) marks
COLLECTS, plus the honest skipped/error lines for everything else:
  reddit (public search JSON) · hackernews (Algolia) · channels' YouTube Atom feeds ·
  Google News RSS · web-extraction seeds (robots.txt respected, config > webreader) ·
  credential-gated platform adapters (IG/FB/TikTok: reported NEEDS_CREDENTIALS, zero network).
Dedup, retention, author-minimization and reviewers' evidence gates are all inherited
from engine.signals / engine.web_extract. Nothing here contacts anyone: the prospect
miner reads only what a person PUBLISHED in their own post (email/phone they themselves
offered for contacting) and stores it with the exact evidence — a row without that
stays CONTACT_NOT_AVAILABLE and never becomes a lead.

Scheduling: the sweep itself still only runs when invoked (repo rule: no network without
an explicit command — the scheduler task is just the OS invoking that SAME command daily).
config/schedule.json > {"enabled": true, "min_interval_hours": 20, "daily_time": "07:15"}
turns the cooldown on; schedule-install registers the task, schedule-remove un-registers it.
Nothing here spends, pings, or contacts anyone.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import signals as sig          # noqa: E402
from engine import platforms as plat       # noqa: E402
from engine import web_extract as webx     # noqa: E402
from engine import ollama_client as oai    # noqa: E402
from engine import signal_clusters as clus # noqa: E402
from engine import official_data as odata  # noqa: E402
from engine import social_scrapers as social  # noqa: E402
from engine import prospect_contacts as pcon   # noqa: E402 - consumer-prospect contact miner

DB = ROOT / "data" / "signals.db"
CFG = ROOT / "config" / "sources.json"
OUT = ROOT / "out"

# the optional AI venv lives at a SHORT path because Windows cannot load some venv
# DLLs from deeply nested directories ("The filename or extension is too long")
AI_VENV_PY = Path("C:/gs-test/.ai-scrape/Scripts/python.exe")
AI_VENV_DIR = Path("C:/gs-test/.ai-scrape")
AI_STATE = ROOT / "out" / "ai_state.json"          # last health/smoke result for the admin page


def _load_cfg() -> dict:
    try:
        return json.loads(CFG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def load_config() -> dict:
    return _load_cfg()


def run_all(con, cfg: dict | None = None, get_json=None, fetch_text=None,
            sleeper=None) -> list[dict]:
    """Run every eligible collector with the config values the code actually reads.
    Network injectables (`get_json`, `fetch_text`) make the whole sweep offline-testable;
    `sleeper` keeps politeness pauses invisible in tests. Returns one summary row per
    collector, in stable order — this is what the CLI prints and the report stores."""
    cfg = cfg if cfg is not None else _load_cfg()
    get_json = get_json or sig._http_json
    sleeper = sleeper or __import__("time").sleep
    out: list[dict] = []

    # config gate (config/sources.json > reddit.enabled): a network-blocked host must not
    # burn the sweep's minutes on slow 403 retries — the report shows the skip honestly.
    reddit_gate = (cfg.get("reddit") or {}).get("enabled")
    if reddit_gate is False:
        out.append({"source": "reddit", "fetched": 0, "accepted": 0,
                    "skipped": "disabled by config (reddit.enabled=false)"})
        out.append({"source": "reddit_fallback", "fetched": 0, "accepted": 0,
                    "skipped": "disabled with reddit (fallback needs the same upstream)"})
    else:
        r = sig.fetch_reddit(con, sig.default_queries(), get_json=get_json, sleeper=sleeper)
        out.append({"source": "reddit", **r})

        # Reddit fallback: the public PullPush mirror (free) fills what reddit.com's own
        # search denied — dedupe keeps only URLs reddit.com did not already store.
        try:
            rf = sig.fetch_reddit_fallback(con, sig.default_queries(), get_json=get_json, sleeper=sleeper)
            out.append({"source": "reddit_fallback", **rf})
        except Exception as e:             # noqa: BLE001 - one collector must never kill the sweep
            out.append({"source": "reddit_fallback", "fetched": 0, "accepted": 0,
                        "note": f"error: {type(e).__name__}: {e}"[:160]})

    hn = sig.fetch_hn(con, "health insurance", get_json=get_json)
    out.append({"source": "hackernews", **hn})

    chans = [c for c in (cfg.get("youtube_channels") or []) if c]
    if chans:
        yt = sig.fetch_youtube(con, chans, fetch_text=None if fetch_text is None else fetch_text,
                               sleeper=sleeper)
        out.append({"source": "youtube", **yt})
    else:
        out.append({"source": "youtube", "fetched": 0, "accepted": 0,
                    "note": "skipped: no channel ids configured (config/sources.json > youtube_channels)"})

    gn = sig.fetch_google_news(con, cfg.get("google_news_queries") or None,
                               fetch_text=None if fetch_text is None else fetch_text,
                               sleeper=sleeper)
    out.append({"source": "google_news", **gn})

    # webreader seeds: one bounded same-domain walk per seed, robots.txt respected
    wr = cfg.get("webreader") or {}
    max_pages = max(1, int(wr.get("max_pages_per_seed") or 5))
    for seed in (wr.get("seeds") or []):
        u = (seed.get("url") or "").strip()
        if not u:
            continue
        try:
            site = webx.collect_site(con, u, max_pages=max_pages, fetch_text=fetch_text,
                                     source="webreader",
                                     instructions=seed.get("instructions") or "")
        except Exception as e:             # noqa: BLE001 - a bad seed is a reported condition
            site = {"status": "error", "reason": f"{type(e).__name__}: {e}"}
        out.append({"source": f"webreader:{u}"[:200], "fetched": site.get("fetched", 0),
                    "added": site.get("added", 0), "status": site.get("status", "?"),
                    "note": (seed.get("instructions") or "")[:120]})

    # credential-gated platform adapters stay credential-gated even inside the sweep
    for name, fn in (("instagram", lambda: plat.fetch_instagram_hashtag(con, "seguro medico")),
                     ("facebook", lambda: plat.fetch_facebook_pages(con, [])),
                     ("tiktok", lambda: plat.fetch_tiktok_keyword(con, "seguro medico"))):
        try:
            stat = fn()
        except Exception as e:             # noqa: BLE001 - never let one adapter kill the sweep
            stat = {"status": "error", "missing": [], }
            stat["note"] = f"{type(e).__name__}: {e}"
        out.append({"source": "platform:" + name, "status": stat.get("status"),
                    "fetched": 0, "added": stat.get("added", 0),
                    "note": (", ".join(stat.get("missing", [])) or stat.get("note", ""))[:120]})

    # comment/Q&A depth: the actual individual-expression layers of the free community sources
    try:
        cm = sig.fetch_reddit_comments(con, sig.default_comment_queries(), get_json=get_json, sleeper=sleeper)
        out.append({"source": "reddit_comments", **cm})
    except Exception as e:                 # noqa: BLE001 - one collector must never kill the sweep
        out.append({"source": "reddit_comments", "fetched": 0, "accepted": 0,
                    "note": f"error: {type(e).__name__}: {e}"[:160]})
    try:
        hn = sig.fetch_hn_comments(con, "health insurance", get_json=get_json)
        out.append({"source": "hn_comments", **hn})
    except Exception as e:                 # noqa: BLE001
        out.append({"source": "hn_comments", "fetched": 0, "accepted": 0,
                    "note": f"error: {type(e).__name__}: {e}"[:160]})
    try:
        se = sig.fetch_stackexchange(con, get_json=None if get_json is sig._http_json else get_json,
                                     sleeper=sleeper)
        out.append({"source": "stackexchange", **se})
    except Exception as e:                 # noqa: BLE001
        out.append({"source": "stackexchange", "fetched": 0, "accepted": 0,
                    "note": f"error: {type(e).__name__}: {e}"[:160]})
    # Fediverse hashtag timelines: credential-free public person-shaped chatter
    try:
        md = sig.fetch_mastodon(con, cfg=cfg, get_json=get_json, sleeper=sleeper)
        out.append({"source": "mastodon", **md})
    except Exception as e:                 # noqa: BLE001
        out.append({"source": "mastodon", "fetched": 0, "accepted": 0,
                    "note": f"error: {type(e).__name__}: {e}"[:160]})
    # Bluesky public post search: anonymous AppView read, quoted phrases + since cutoff
    try:
        bs = sig.fetch_bluesky(con, cfg=cfg, get_json=get_json, sleeper=sleeper)
        out.append({"source": "bluesky", **bs})
    except Exception as e:                 # noqa: BLE001
        out.append({"source": "bluesky", "fetched": 0, "accepted": 0,
                    "note": f"error: {type(e).__name__}: {e}"[:160]})
    # Lemmy anonymous comment search over configured instances
    try:
        lm = sig.fetch_lemmy(con, cfg=cfg, get_json=get_json, sleeper=sleeper)
        out.append({"source": "lemmy", **lm})
    except Exception as e:                 # noqa: BLE001
        out.append({"source": "lemmy", "fetched": 0, "accepted": 0,
                    "note": f"error: {type(e).__name__}: {e}"[:160]})

    # paid service adapters (Apify / Bright Data): double-gated — no approval, no network
    try:
        out.extend(social.run_paid_social(con, cfg))
    except Exception as e:                 # noqa: BLE001
        out.append({"source": "paid", "fetched": 0, "added": 0,
                    "note": f"error: {type(e).__name__}: {e}"[:160]})

    # official public-data doors (CMS catalog + healthcare.gov content API): robots-safe,
    # one request each; a failure is a reported row, never a crashed sweep
    try:
        for row in odata.run_official_data(con, sleeper=sleeper):
            out.append(row)
    except Exception as e:                 # noqa: BLE001 - one collector must never kill the sweep
        out.append({"source": "official_cms", "fetched": 0, "accepted": 0,
                    "note": f"error: {type(e).__name__}: {e}"[:160]})

    st = webx.sga_status()
    out.append({"source": "webreader", "effective_mode": st["effective_mode"],
                "provider": st["provider"], "fetched": 0, "added": 0})
    return out


LOCK_PATH = OUT / "collect.lock"
LOCK_MINUTES = 30


def _acquire_run_lock() -> str | None:
    """Overlap guard for SCHEDULED sweeps: a fresh lock (with owning PID) refuses a second
    concurrent `run --network` instead of silently double-hitting sources. Stale locks
    (older than LOCK_MINUTES — a crashed run) are replaceable. Pure-local, no network."""
    import time as _t
    OUT.mkdir(exist_ok=True)
    if LOCK_PATH.exists():
        try:
            age_min = (_t.time() - LOCK_PATH.stat().st_mtime) / 60.0
            owner = LOCK_PATH.read_text(encoding="utf-8", errors="replace").strip()[:60]
            if age_min < LOCK_MINUTES:
                return (f"another sweep holds the run lock (owner {owner!r}, {age_min:.0f} min old) — "
                        f"refusing to overlap; delete {LOCK_PATH} if that run is dead")
        except OSError:
            pass
    LOCK_PATH.write_text(f"pid={__import__('os').getpid()}", encoding="utf-8")
    return None


def _release_run_lock() -> None:
    try:
        LOCK_PATH.unlink()
    except OSError:
        pass


def cmd_run(force: bool = False) -> list[dict]:
    blocker = _acquire_run_lock()
    if blocker:
        sys.exit("refused: " + blocker)
    sch = _load_schedule()
    if not force and _run_cooldown_exit(sch):
        _release_run_lock()          # a cooldown skip must not strand the run lock:
        return []                    # that would block every later sweep for 30+ min
    con = sig.connect(DB)
    try:
        rows = run_all(con)
        for r in rows:
            if r.get("note"):
                print(f"{r['source']}: {r.get('status', '')} fetched={r.get('fetched', 0)} "
                      f"added={r.get('added', 0)}  {r['note'][:110]}")
            else:
                print(f"{r['source']}: fetched={r.get('fetched', 0)} accepted={r.get('accepted', r.get('added', 0))}")
        # prospect-mining pass: every public INTEREST signal is scanned for a contact the
        # author EXPLICITLY published; rows land in data/prospect_people.db with the
        # evidence-gated ladder (DISCOVERED -> CONTACT_FOUND -> ...) — never a lead.
        try:
            mine = pcon.scan_signals(con, pcon.connect())
            rows.append({"source": "prospect_miner", **mine})
        except Exception as e:             # noqa: BLE001 - mining must not kill the sweep
            rows.append({"source": "prospect_miner", "fetched": 0, "accepted": 0,
                         "note": f"error: {type(e).__name__}: {e}"[:160]})
        _append_run_summary(rows, "ok")
        return rows
    finally:
        con.close()
        _release_run_lock()


def _con():
    return sig.connect(DB)


def cmd_status(con) -> None:
    print(json.dumps(sig.counts(con), indent=1))
    print("per-source:")
    for k, v in sig.per_source_counts(con).items():
        print(f"  {k}: {v}")
    last = con.execute("SELECT ts, source, query, fetched, accepted, note FROM fetch_log "
                       "ORDER BY id DESC LIMIT 5").fetchall()
    for r in last:
        print(f"  fetch {r['ts']} {r['source']}: fetched={r['fetched']} accepted={r['accepted']} "
              f"{(r['note'] or '')[:80]}")


def cmd_report(con) -> None:
    c = sig.counts(con)
    aggs = sig.aggregate(con)
    lines = ["# Collection report", "",
             f"Signals: " + ", ".join(f"{k} {v}" for k, v in c.items()), "",
             "SIGNALS ARE NOT LEADS: public posts reviewed for content planning; no author is",
             "identified and nobody is contacted. Not exported as contacts.", "",
             "| source | lang | topic | n | avg intent | actioned | irrelevant |",
             "|---|---|---|---|---|---|---|"]
    for a in aggs:
        lines.append(f"| {a['source']} | {a['lang']} | {a['topic']} | {a['n']} | "
                     f"{a['avg_intent']} | {a['actioned']} | {a['irrelevant']} |")
    lines += ["", "## Last fetches", "", "| ts | source | query | fetched | accepted | note |",
              "|---|---|---|---|---|---|"]
    for r in con.execute("SELECT ts, source, query, fetched, accepted, note FROM fetch_log "
                         "ORDER BY id DESC LIMIT 15"):
        lines.append(f"| {r['ts']} | {r['source']} | {(r['query'] or '')[:60]} | {r['fetched']} | "
                     f"{r['accepted']} | {(r['note'] or '')[:100]} |")
    (ROOT / "out").mkdir(exist_ok=True)
    outp = ROOT / "out" / "collect_report.md"
    outp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {outp}")


SCHEDULE_NOTE = ("-collection runs only when YOU schedule it: nothing is registered by this command")


# ------------------------------------------------------------------ local AI (Ollama)
def _venv_probe(python_exe: Path) -> dict:
    """Is the AI venv present and does scrapegraphai import inside it?"""
    if not python_exe.exists():
        return {"status": "NOT_PRESENT", "detail": f"{python_exe} does not exist"}
    try:
        p = __import__("subprocess").run(
            [str(python_exe), "-X", "utf8", "-c", "import scrapegraphai; print('ok')"],
            capture_output=True, text=True, timeout=60)
        if p.returncode == 0 and "ok" in (p.stdout or ""):
            return {"status": "READY", "detail": f"scrapegraphai imports in {python_exe}"}
        return {"status": "FAILED",
                "detail": f"import failed rc={p.returncode}: {(p.stderr or p.stdout or '')[:200]}"}
    except Exception as e:                  # noqa: BLE001
        return {"status": "FAILED", "detail": f"{type(e).__name__}: {e}"}


def _write_ai_state(section: dict) -> None:
    """MERGE into out/ai_state.json: ai-health writes {checks,...}, ai-smoke writes
    {smoke,...} — both survive, and both show on /admin/integrations."""
    OUT.mkdir(exist_ok=True)
    existing = {}
    try:
        existing = json.loads(AI_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    existing.update(section)
    existing["recorded_at"] = __import__("datetime").datetime.now().isoformat(timespec="seconds")
    AI_STATE.write_text(json.dumps(existing, indent=1, default=str), encoding="utf-8")


def load_ai_state() -> dict | None:
    try:
        return json.loads(AI_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def cmd_ai_health() -> dict:
    """python tools/collect.py ai-health

    READY / NEEDS_SETUP / FAILED for: app python, app venv pip, optional AI venv +
    scrapegraphai, Ollama connectivity, installed models, selected model response
    test. Writes out/ai_state.json for /admin/integrations. Zero paid calls; the
    only network is the LOCAL Ollama API."""
    import importlib
    import platform
    checks = []

    vi = sys.version_info
    checks.append({"name": "app_python",
                   "status": "READY" if vi >= (3, 12) else "FAILED",
                   "detail": f"{platform.python_version()} at {sys.executable}"
                             f"{' (>=3.12 OK)' if vi >= (3, 12) else ' (needs >=3.12)'}"})
    checks.append({"name": "app_pip", "status": "READY",
                   "detail": __import__("importlib.metadata", fromlist=["version"])
                             .version("pip")})

    sga_here = importlib.util.find_spec("scrapegraphai") is not None
    venv = _venv_probe(AI_VENV_PY)
    checks.append({"name": "ai_venv_scrapegraphai", **venv,
                   "detail": venv.get("detail") + (" | also importable in app venv" if sga_here else "")})

    h = oai.health()
    checks.append({"name": "ollama_running",
                   "status": "READY" if h["running"] else "NEEDS_SETUP",
                   "detail": h["reason" if not h["running"] else "reason"].strip()})
    models = ", ".join(m["name"] for m in h.get("models", [])) or "none installed"
    checks.append({"name": "ollama_models", "status": "READY" if h.get("models") else "NEEDS_SETUP",
                   "detail": models})
    sel = h.get("selected_model") or "(none selected)"
    responds = h.get("model_responds")
    checks.append({"name": "ollama_model_response",
                   "status": "READY" if responds else ("FAILED" if responds is False else "NEEDS_SETUP"),
                   "detail": f"selected={sel}; one-word prompt round-trip={'passed' if responds else 'not passed'}"})

    overall = "READY" if all(c["status"] == "READY" for c in checks) else (
        "FAILED" if any(c["status"] == "FAILED" for c in checks) else "NEEDS_SETUP")
    state = {"health_overall": overall, "checks": checks, "selected_model": h.get("selected_model", ""),
             "ollama_url": h.get("ollama_url"), "extract_mode_now": webx.sga_status()["effective_mode"]}
    _write_ai_state(state)
    print(f"LOCAL AI HEALTH: {overall}")
    for c in checks:
        print(f"  [{c['status']:>11}] {c['name']}: {c['detail']}")
    if overall != "READY":
        print("  fix hints: docs/LOCAL_AI_SETUP.md")
    return state


SYNTHETIC_PAGE = (
    "GS ALLUDE SYNTHETIC PAGE (no real person, no real source). "
    "Title: Marketplace plan comparison guide. "
    "Text: Open Enrollment runs Nov 1 to Dec 15. If you lost employer coverage you get "
    "a Special Enrollment Period of 60 days. Subsidies lower monthly premiums for "
    "qualifying households.")
SYNTHETIC_URL = "https://synthetic-gs-test.invalid/enroll-guide.html"


def cmd_ai_smoke() -> dict:
    """python tools/collect.py ai-smoke

    End-to-end AI extraction with a SYNTHETIC, non-sensitive page: (1) direct
    local-Ollama path -> JSON; (2) if the optional venv exists, the real
    ScrapeGraphAI subprocess path -> JSON. No live collection sweep, no external
    page, no PII: the page text is invented. Writes out/ai_state.json."""
    import importlib.util
    steps = []
    try:
        r = oai.generate_json(
            "Extract {synthetic: bool, mentions_enrollment: bool, deadline_month: string}. "
            "Text: " + SYNTHETIC_PAGE + "\nJSON only.", timeout=180)
        j = r["json"]
        ok = bool(j.get("synthetic") or j.get("mentions_enrollment"))
        steps.append({"step": "direct_ollama_json", "ok": ok, "model": r["model"],
                      "result": j, "error": "" if ok else "model answer did not mention expected fields"})
    except Exception as e:                  # noqa: BLE001
        steps.append({"step": "direct_ollama_json", "ok": False, "model": "",
                      "result": None, "error": str(e)[:200]})

    if AI_VENV_PY.exists():
        try:
            p = __import__("subprocess").run(
                [str(AI_VENV_PY), "-X", "utf8", str(ROOT / "tools" / "sga_runner.py")],
                input=json.dumps({"url": "https://example.com/",
                                  "instructions": ("Return JSON {page_topic: string, "
                                                   "is_health_insurance: bool}. One object only.")},
                                 ).encode("utf-8"), capture_output=True, timeout=300)
            out = json.loads((p.stdout or b"{}").decode("utf-8", "replace") or "{}")
            steps.append({"step": "scrapegraphai_subprocess", "ok": bool(out.get("ok")),
                          "model": "ollama/" + (load_ai_state() or {}).get("selected_model", "?"),
                          "result": out.get("result"), "error": out.get("error", "")[:200]})
        except Exception as e:              # noqa: BLE001
            steps.append({"step": "scrapegraphai_subprocess", "ok": False, "model": "",
                          "result": None, "error": f"{type(e).__name__}: {e}"[:200]})
    else:
        steps.append({"step": "scrapegraphai_subprocess", "ok": False, "model": "",
                      "result": None,
                      "error": "optional: venv not installed — direct Ollama path already "
                               "works; see docs/LOCAL_AI_SETUP.md to add full SGA"})

    ok_steps = [s for s in steps if s["ok"]]
    state = {"smoke_overall": "PASS" if ok_steps else "FAIL", "smoke": steps}
    _write_ai_state(state)
    print(f"AI SMOKE: {state['smoke_overall']}")
    for s in steps:
        print(f"  [{'PASS' if s['ok'] else 'FAIL'}] {s['step']} model={s.get('model') or '-'}"
              f"  result={json.dumps(s.get('result'), default=str)[:160] if s['ok'] else ''}"
              f"{(s.get('error') or '')[:120] if not s['ok'] else ''}")
    return state


def _load_schedule() -> dict:
    """config/schedule.json: {} when absent — every default stays 'off' without it."""
    try:
        return json.loads((ROOT / "config" / "schedule.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


RUNS_LOG = OUT / "collect_runs.jsonl"      # one JSON line per sweep attempt: the run history


def _last_run_ts() -> tuple[str, float]:
    """ts string + epoch of the last COMPLETED sweep from the run log (('' , 0) if none)."""
    try:
        last = ""
        for line in RUNS_LOG.read_text(encoding="utf-8").splitlines():
            if line.strip():
                last = line
        if not last:
            return ("", 0.0)
        j = json.loads(last)
        ts = str(j.get("ts") or "")
        ep = __import__("datetime").datetime.fromisoformat(ts).timestamp() if ts else 0.0
        return (ts, ep)
    except (OSError, ValueError):
        return ("", 0.0)


def _append_run_summary(rows: list[dict], status: str) -> None:
    OUT.mkdir(exist_ok=True)
    ts = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(timespec="seconds")
    rec = {"ts": ts, "status": status,
           "sources": [{k: r.get(k) for k in ("source", "status", "fetched", "accepted", "added", "note")
                        if r.get(k) is not None} for r in rows]}
    with RUNS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def _run_cooldown_exit(sch: dict) -> bool:
    """Cooldown discipline: if the schedule config says min_interval_hours and our last
    COMPLETED sweep is younger, skip this run (logged) instead of re-hitting every source.
    Reddit/HN/news calls are cached politely upstream, but the honest mechanism to 'respect
    source limits' is to not re-sweep AT ALL within the configured window."""
    try:
        hours = float(sch.get("min_interval_hours") or 0)
    except (TypeError, ValueError):
        hours = 0.0
    if not sch.get("enabled") or hours <= 0:
        return False
    ts, ep = _last_run_ts()
    if not ep or (__import__("time").time() - ep) >= hours * 3600:
        return False
    _append_run_summary([], "cooldown")
    print(f"skip: last successful sweep {ts} is inside the {hours:g}h cooldown "
          f"(config/schedule.json > min_interval_hours). Nothing fetched.")
    return True


def _cmd_schedule_install(name: str = "", daily_time: str = "") -> int:
    """Actually REGISTER the daily sweep with Windows Task Scheduler (schtasks). POSIX
    machines keep the printed cron line instead: registering cron programmatically across
    inits is its own project. The installed task runs the SAME locked sweep command with
    output appended to out/collect.log; nothing else changes."""
    sch = _load_schedule()
    name = (name or sch.get("task_name") or "GS-Allude-Collect").strip()
    daily_time = (daily_time or sch.get("daily_time") or "07:15").strip()[:5]
    cmd = f'{sys.executable} "{ROOT / "tools" / "collect.py"}" run --network >> "{OUT / "collect.log"}" 2>&1'
    if sys.platform != "win32":
        hh, mm = daily_time.split(":")[:2]
        print(f"POSIX host — add this line to crontab (daily {daily_time}):")
        print(f"{int(mm):2d} {int(hh):02d} * * * cd {ROOT} && {cmd}")
        return 1
    p = __import__("subprocess").run(
        ["schtasks", "/Create", "/F", "/TN", name, "/SC", "DAILY", "/ST", daily_time,
         "/TR", f'cmd /c cd /d "{ROOT}" && {cmd}'],
        capture_output=True, text=True)
    print(f"schedule-install: {'registered' if p.returncode == 0 else 'FAILED'} "
          f"task={name} daily {daily_time} -> {cmd}")
    if p.returncode:                                   # noqa: SIM108 - report, never hide
        print((p.stderr or p.stdout or "").strip()[:300])
        print("manual fallback: python tools/collect.py schedule-cmd")
    else:
        print("remove anytime with: schtasks /Delete /TN " + name + " /F "
              "(or: python tools/collect.py schedule-remove)")
    return p.returncode


def _cmd_schedule_remove(name: str = "") -> int:
    sch = _load_schedule()
    name = (name or sch.get("task_name") or "GS-Allude-Collect").strip()
    if sys.platform != "win32":
        print("POSIX: remove the matching daily line from your crontab.")
        return 1
    p = __import__("subprocess").run(["schtasks", "/Delete", "/TN", name, "/F"],
                                     capture_output=True, text=True)
    print(f"schedule-remove: {'removed' if p.returncode == 0 else 'not found / failed'} task={name}")
    return p.returncode


def cmd_schedule() -> str:
    exe = sys.executable
    here = ROOT
    cmd = f'"{exe}" "{here / "tools" / "collect.py"}" run --network'
    lines = [SCHEDULE_NOTE,
             "\n# POSIX cron (daily 07:15):",
             f"15 7 * * * cd /d {here} && {cmd} >> out/collect.log 2>&1",
             "\n# Windows Task Scheduler (daily 07:15; prints the line to use — does not register it):",
             f'schtasks /Create /TN GS-Allude-Collect /SC DAILY /ST 07:15 /TR '
             f'"cmd /c cd /d {here} && {cmd} >> out\\collect.log 2>&1"']
    text = "\n".join(lines)
    print(text)
    return text


def cmd_clusters_build() -> int:
    """python tools/collect.py clusters-build

    Distill the COLLECTED (not fetched-now) signal rows into deduplicated clusters
    keyed by topic+language, classify the recurring ones with the local Ollama
    model, and link each cluster to the EXISTING /admin/opportunities theses the
    topic map already grants. Refuses to build when Ollama is unavailable and
    honestly reports what happened. Never a network sweep, never a contact."""
    con = _con()
    try:
        ready = oai.list_models() and (oai.select_model(oai.list_models()) or True)
    except oai.OllamaError as e:
        print(f"clusters-build: local AI unavailable — nothing built. ({e})")
        print("hint: python tools/collect.py ai-health  |  docs/LOCAL_AI_SETUP.md")
        return 1
    if not ready or not oai.select_model(oai.list_models()):
        print("clusters-build: no Ollama model answers a test prompt — nothing built.")
        print("hint: python tools/collect.py ai-health")
        return 1
    res = clus.build_clusters(con)
    if res["skipped"]:
        print(f"clusters-build: {res['skipped']}")
        return 1
    print(f"clusters-build: {len(res['clusters'])} cluster(s) stored")
    for r in clus.list_clusters(con, "NEW"):
        print(f"  [{r['mean_intent']}] {r['cluster_key']} n={r['n_signals']} "
              f"model={r['model'] or '-'} theses={r['theses']} :: {r['title'][:90]}")
    return 0


def cmd_clusters_list(status: str = "") -> None:
    con = _con()
    try:
        rows = clus.list_clusters(con, status)
        if not rows:
            print("no clusters stored yet — run: python tools/collect.py clusters-build")
            return
        for r in rows:
            print(f"[{r['id']}] {r['status']:9} mean_intent={r['mean_intent']} n={r['n_signals']} "
                  f"{r['cluster_key']} · {r['title'][:80]}\n     why: {r['why'][:160]}")
    finally:
        con.close()


def cmd_prospect_scan() -> int:
    """python tools/collect.py prospect-scan — the same mining pass the sweep runs,
    standalone: useful after manual/posts-only sweeps or schema changes. Idempotent
    (same post -> same person_key -> skipped). Never contacts anyone."""
    scon = sig.connect(DB)
    try:
        ppl = pcon.connect()
        try:
            mine = pcon.scan_signals(scon, ppl)
            print("prospect-scan: " + json.dumps(mine))
            c = pcon.counts(ppl)
            print("people store: " + json.dumps(c))
            return 0
        finally:
            ppl.close()
    finally:
        scon.close()


def cmd_people_list(status: str = "", origin: str = "") -> None:
    ppl = pcon.connect()
    try:
        rows = pcon.list_people(ppl, stage=status, origin=origin, limit=40)
        if not rows:
            print("no prospect rows stored — run: python tools/collect.py prospect-scan")
            return
        for r in rows:
            c = json.loads(r["contact_methods"] or "[]")
            contacts = ", ".join(f"{m['kind']}:{m['value']}" for m in c) or "-"
            print(f"#{r['id']} [{r['stage']}] conf={r['confidence']} {r['source']} "
                  f"{r['lang']} {r['observed_at'][:10]} dup={r['dup_status']} "
                  f"origin={r.get('contact_origin') or 'post'}\n"
                  f"     contacts: {contacts}\n"
                  f"     need: {r['stated_need'][:150]}\n"
                  f"     url: {r['source_url']}")
            if r.get("profile_url"):
                print(f"     profile: {r['profile_url']}")
    finally:
        ppl.close()


def _funnel_from_prospects(leads_con) -> dict:
    """Consumer-side completion numbers joined from the EXISTING leads.db funnel, sliced to
    the discovery attribution only (utm_source starting prospect_/signal_ — the only utm
    prefixes the discovery bridge creates). Real traffic only; test rows are excluded."""
    ev = leads_con.execute(
        "SELECT COUNT(DISTINCT e.visitor_id) visits, "
        "       COUNT(DISTINCT CASE WHEN e.event='tool_complete' THEN e.visitor_id END) check_completions "
        "FROM events e WHERE e.is_test=0 "
        "AND (e.utm_source LIKE 'prospect_%' OR e.utm_source LIKE 'signal_%')").fetchone()
    ld = leads_con.execute(
        "SELECT SUM(lead_type='PHONE_LEAD' AND phone_valid=1) consented, "
        "       SUM(qual_status='QUALIFIED') qualified "
        "FROM leads WHERE is_test=0 "
        "AND (utm_source LIKE 'prospect_%' OR utm_source LIKE 'signal_%')").fetchone()
    return {"bridge_visits": ev["visits"] or 0,
            "check_completions": ev["check_completions"] or 0,
            "consented_leads": ld["consented"] or 0,
            "qualified_leads": ld["qualified"] or 0}


def cmd_people_metrics() -> int:
    """python tools/collect.py people-metrics — the measured discovery→consent funnel:
    signals → prospects → contacts → verified permission, with low-confidence rows
    counted separately, plus the consumer-side completions/consents from leads.db.
    Read-only over the three stores; never contacts anyone."""
    import engine.db as ldb
    scon = sig.connect(DB)
    ppl = pcon.connect()
    try:
        m = pcon.people_metrics(scon, ppl)
    finally:
        scon.close()
        ppl.close()
    try:
        lcon = ldb.connect(ROOT / "data" / "leads.db")
        try:
            m.update(_funnel_from_prospects(lcon))
        finally:
            lcon.close()
    except Exception as e:                 # noqa: BLE001 - leads.db may not exist yet locally
        m["consumer_funnel"] = f"unavailable: {type(e).__name__}: {e}"[:120]
    print(json.dumps(m, indent=1))
    (ROOT / "out").mkdir(exist_ok=True)
    (ROOT / "out" / "people_metrics.json").write_text(
        json.dumps(m, indent=1, default=str) + "\n", encoding="utf-8")
    print("wrote out/people_metrics.json")
    return 0


def main() -> None:
    a = sys.argv[1:]
    if a and a[0] == "people-metrics":
        sys.exit(cmd_people_metrics())
    if not a or a[0] == "status":
        con = _con()
        try:
            cmd_status(con)
        finally:
            con.close()
    elif a[0] == "run":
        if "--network" not in a:
            sys.exit("refused: the sweep is a REAL network action; pass --network explicitly")
        force = "--force" in a
        if a != ["run", "--network"] and a != ["run", "--network", "--force"]:
            sys.exit("usage: collect.py run --network [--force]")
        cmd_run(force=force)
    elif a[0] == "schedule-install":
        sys.exit(_cmd_schedule_install())
    elif a[0] == "schedule-remove":
        sys.exit(_cmd_schedule_remove())
    elif a[0] == "report":
        con = _con()
        try:
            cmd_report(con)
        finally:
            con.close()
    elif a[0] == "schedule-cmd":
        cmd_schedule()
    elif a[0] == "prospect-scan":
        sys.exit(cmd_prospect_scan())
    elif a[0] == "people-list":
        stage, origin = "", ""
        if len(a) >= 3 and a[1] == "--stage":
            stage = a[2]
        if len(a) >= 3 and a[1] == "--origin":
            origin = a[2]
        cmd_people_list(stage, origin)
    elif a[0] == "clusters-build":
        sys.exit(cmd_clusters_build())
    elif a[0] == "clusters-list":
        status = a[2] if len(a) >= 3 and a[1] == "--status" else ""
        cmd_clusters_list(status)
    elif a[0] == "official-data":
        # robots-safe official doors: CMS open-data catalog + healthcare.gov content API.
        con = _con()
        try:
            for r in odata.run_official_data(con):
                print(f"{r['source']}: fetched={r.get('fetched', 0)} accepted={r.get('accepted', 0)}")
        finally:
            con.close()
    elif a[0] == "social-paid":
        if "--network" not in a:
            sys.exit("refused: paid collection is a REAL network + REAL spend action; "
                     "pass --network explicitly")
        con = _con()
        try:
            for r in social.run_paid_social(con):
                print(f"{r['source']}: {r.get('status', '-')} fetched={r.get('fetched', 0)} "
                      f"added={r.get('added', 0)}  {(r.get('note') or '')[:110]}")
        finally:
            con.close()
    elif a[0] == "discovery":
        if "--network" not in a:
            sys.exit("refused: discovery is a REAL network action; pass --network explicitly")
        # person-shaped discovery only: community sources + comment/Q&A depth. No news,
        # no official data, no web extraction. The paid-service gates print their own rows.
        con = _con()
        try:
            n = sig.fetch_reddit(con, sig.default_queries())
            print(f"reddit: fetched={n['fetched']} accepted={n['accepted']}")
            m = sig.fetch_reddit_fallback(con, sig.default_queries())
            print(f"reddit_fallback: fetched={m['fetched']} accepted={m['accepted']}")
            cm = sig.fetch_reddit_comments(con, sig.default_queries())
            print(f"reddit_comments: fetched={cm['fetched']} accepted={cm['accepted']}"
                  + (" (" + cm.get("skipped", "") + ")" if cm.get("skipped") else ""))
            h = sig.fetch_hn(con, "health insurance")
            print(f"hackernews: fetched={h['fetched']} accepted={h['accepted']}")
            hc = sig.fetch_hn_comments(con, "health insurance")
            print(f"hn_comments: fetched={hc['fetched']} accepted={hc['accepted']}")
            se = sig.fetch_stackexchange(con)
            print(f"stackexchange: fetched={se['fetched']} accepted={se['accepted']}"
                  + (" (" + se.get("skipped", "") + ")" if se.get("skipped") else ""))
            for r in social.run_paid_social(con):
                print(f"{r['source']}: {r.get('status', '-')} fetched={r.get('fetched', 0)} "
                      f"added={r.get('added', 0)}  {(r.get('note') or '')[:110]}")
            k = sig.kind_counts(con)
            print(f"store: MARKET={k['MARKET']} INTEREST={k['INTEREST']}")
        finally:
            con.close()
    elif a[0] == "ai-health":
        cmd_ai_health()
    elif a[0] == "ai-smoke":
        cmd_ai_smoke()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
