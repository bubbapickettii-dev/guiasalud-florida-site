"""""Live end-to-end test of the SGA runner PROTOCOL (subprocess, not in-process).

Pipeline under test: JSON request over stdin -> tools.sga_runner.py (subprocess,
.ai-scrape venv at C:/gs-test) -> ScrapeGraphAI SmartScraperGraph -> local Ollama
(qwen2.5:14b) -> JSON result over stdout.

stdin/stdout are passed as BYTES on purpose: Windows text-mode pipes silently
re-encode non-ASCII JSON (and the runner protocol is plain ASCII JSON anyway).

Requires: Ollama running with a model installed; the C:/gs-test/.ai-scrape venv.
Uses a small, safe, REAL public page — no PII anywhere. Skips (does not fail)
when the optional dependencies are absent.
"""
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]   # ...

# NOTE: this test file lives at tests/manual/, so the repo root is TWO levels up;
# `parents[1]` points at tests/ and made the runner path "tests/tools/sga_runner.py"
RUNNER = Path("C:/gs-test/.ai-scrape/Scripts/python.exe")
RUNNER_VENV = Path("C:/gs-test/.ai-scrape")

if not RUNNER_VENV.exists():
    print("SKIP: C:/gs-test/.ai-scrape venv not installed (see docs/LOCAL_AI_SETUP.md)")
    sys.exit(0)
try:
    urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3).read()
except Exception:
    print("SKIP: Ollama not reachable at 127.0.0.1:11434 (start the Ollama app)")
    sys.exit(0)

req = {"url": "https://scrapegraphai.com/",
       "instructions": ("Return a JSON object {page_topic: string, "
                        "mentions_scraping: bool}. One object only.")}
p = subprocess.run([str(RUNNER), "-X", "utf8", str(ROOT / "tools" / "sga_runner.py")],
                   input=json.dumps(req).encode("utf-8"), capture_output=True,
                   timeout=300)
out = json.loads(p.stdout.decode("utf-8", "replace") or "{}") or \
    {"ok": False, "error": "empty stdout"}
print("returncode:", p.returncode)
print("stderr tail:", (p.stderr or b"").decode("utf-8", "replace")[-300:])
print("stdout head:", p.stdout.decode("utf-8", "replace")[:200])
print("ok:", out.get("ok"))
print("error:", out.get("error"))
print("result:", json.dumps(out.get("result"), ensure_ascii=False, default=str)[:300])
assert out.get("ok") is True, f"SGA runner failed: {out.get('error')}"
blob = json.dumps(out.get("result"), ensure_ascii=False, default=str).lower()
assert "scrap" in blob or "topic" in blob, f"unexpected result: {blob[:200]}"
print("\nSGA_RUNNER_OK — local model produced structured JSON for a real public page")
