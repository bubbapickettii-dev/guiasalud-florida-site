"""SGA RUNNER — runs a ScrapeGraphAI graph in the C:/gs-test/.ai-scrape venv.

WHY THIS EXISTS: the GS Allude app venv must stay small and stable (requirements.txt
is deliberate), while ScrapeGraphAI pulls a large langchain dependency tree. The
separate venv lives at a SHORT path (C:/gs-test/.ai-scrape) because Windows fails to
load some DLLs when a venv sits in a deeply-nested directory
("'..._xxhash.pyd': The filename or extension is too long").

PROTOCOL (stdin/stdout, one invocation per graph):
  stdin : {"url": "...", "instructions": "...", "model": "..."}   (model optional)
  stdout: {"ok": true, "result": {...}}  or  {"ok": false, "error": "..."}

SAFETY: this process fetches ONLY the URL the caller passes (a public web page the
collector picked) and talks to the LOCAL Ollama API. It never reads project data:
no leads.db, no buyers, no tokens, no consumer PII is even openable from here.
The caller (engine.web_extract.extract_sga_subprocess) is the only intended user.
"""
from __future__ import annotations

import json
import sys
import urllib.request

try:
    req = json.loads(sys.stdin.read() or "{}")
    url = (req.get("url") or "").strip()
    instructions = (req.get("instructions") or "").strip()
    model = (req.get("model") or "").strip() or "qwen2.5:14b"
    if not url or not instructions:
        sys.stdout.write(json.dumps({"ok": False, "error": "url and instructions are required"}))
        sys.exit(0)
    from scrapegraphai.graphs import SmartScraperGraph   # noqa: E402 - inside the ai venv
    graph = SmartScraperGraph(
        prompt=instructions, source=url,
        config={"llm": {"model": f"ollama/{model}", "model_tokens": 8192,
                        "format": "json"},
                "verbose": False, "headless": True})
    out = graph.run()
    if not isinstance(out, dict):
        out = {"result": out}
    sys.stdout.write(json.dumps({"ok": True, "result": out}, ensure_ascii=False, default=str))
except Exception as e:                          # noqa: BLE001 - the error IS the output
    sys.stdout.write(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
finally:
    # never hang the caller if the graph swallowed stdin weirdly
    try:
        sys.stdout.flush()
    except Exception:                           # noqa: BLE001
        pass
