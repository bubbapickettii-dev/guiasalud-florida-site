"""LOCAL OLLAMA CLIENT — the no-dependency bridge to this machine's local models.

Why this module exists: the app venv must stay stable (requirements.txt is small and
deliberate), and the AI path must never be a silent dependency. Everything here is:

  * stdlib only — urllib to http://127.0.0.1:11434 (Ollama's local API), no SDK,
    no pip install required, no keys, no billing. Nothing leaves this machine except
    the prompt text the caller builds.
  * honest — every probe returns a named status (READY / NEEDS_SETUP / FAILED) with
    the exact reason, so /admin/integrations can show real state instead of guesses.
  * PII-safe at the caller level — generate() just sends a prompt. Keep-consumer-PII-out
    enforced in engine.web_extract (public web pages only) and tools.collect.ai_smoke
    (synthetic text). This module never reads leads.db / buyer / consent data.

Model selection: OLLAMA_MODEL env > config/sources.json > ollama.model > first
installed model (auto-discovered via /api/tags). A model is only considered usable
after a tiny prompt/round-trip test (_model_responds), not just because /api/tags
lists it.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "config" / "sources.json"
DEFAULT_URL = "http://127.0.0.1:11434"
UA = "GSAlludeLocalAI/1.0 (localhost-only; no external calls)"


class OllamaError(RuntimeError):
    """Raised with an operator-readable reason; the CLI/admin render it verbatim."""


def _cfg() -> dict:
    try:
        return (json.loads(CFG_PATH.read_text(encoding="utf-8")) or {}).get("ollama") or {}
    except (OSError, ValueError):
        return {}


def ollama_url() -> str:
    return (os.environ.get("OLLAMA_URL") or _cfg().get("url") or DEFAULT_URL).strip()


def configured_model() -> str:
    """'' means: auto-select the first installed model that answers."""
    return (os.environ.get("OLLAMA_MODEL") or _cfg().get("model") or "").strip()


def open_post(url: str, payload: dict, timeout: float) -> dict:
    """Single HTTP POST, injectable seam for tests. Returns parsed JSON."""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local API
        return json.loads(resp.read().decode("utf-8", "replace"))


def open_get(url: str, timeout: float = 3.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local API
        return json.loads(resp.read().decode("utf-8", "replace"))


def list_models(post=None) -> list[dict]:
    """/api/tags -> [{name, size_bytes}]. Raises OllamaError with a setup hint."""
    post = post or open_get
    try:
        d = post(ollama_url() + "/api/tags", 3.0)
    except Exception as e:                  # noqa: BLE001
        raise OllamaError(
            f"Ollama not reachable at {ollama_url()} ({type(e).__name__}). "
            "Fix: start the Ollama desktop app or run `ollama serve`. "
            "Download: https://ollama.com/download") from e
    models = d.get("models") if isinstance(d, dict) else None
    if not isinstance(models, list):
        raise OllamaError("Ollama answered but /api/tags did not return a model list")
    out = []
    for m in models:
        name = (m.get("name") or m.get("model") or "")
        if name:
            out.append({"name": name, "size_bytes": m.get("size")})
    return out


def _model_responds(model: str, post=open_post) -> bool:
    """The real usability test: a one-word prompt must come back. Not cached —
    cheap locally, and catches a model that is listed but fails to load (OOM etc.)."""
    try:
        d = post(ollama_url() + "/api/generate", {"model": model,
                 "prompt": "Reply with exactly one word: ready", "stream": False}, 90.0)
        return bool((d.get("response") or "").strip())
    except Exception:                       # noqa: BLE001
        return False


def select_model(models: list[dict], responds=lambda m: _model_responds(m)) -> str:
    """Env/config model first; else auto-pick the first listed model that responds.
    Empty string when nothing responds (caller reports NEEDS_SETUP)."""
    want = configured_model()
    if want:
        return want if any(m["name"] == want or m["name"].startswith(want)
                           for m in models) else ""
    for m in models:
        if responds(m["name"]):
            return m["name"]
    return ""


def generate(prompt: str, *, model: str = "", timeout: float = 120.0,
             post=open_post) -> dict:
    """One /api/generate call; format:json asks Ollama for structured output.
    Returns {"text", "model", "raw"}. Raises OllamaError with an actionable message."""
    model = model or configured_model()
    if not model:
        # auto mode: resolve the first installed model that actually answered a
        # test prompt (same logic health/select_model use on this live machine)
        try:
            model = select_model(list_models())
        except OllamaError:
            raise
        if not model:
            raise OllamaError(
                "no Ollama model selected: pass model=, set OLLAMA_MODEL, set "
                "config/sources.json > ollama > model, or `ollama pull` a model")
    try:
        d = post(ollama_url() + "/api/generate",
                 {"model": model, "prompt": prompt, "stream": False,
                  "format": "json", "options": {"temperature": 0}}, timeout)
    except urllib.error.HTTPError as e:
        raise OllamaError(f"Ollama HTTP {e.code} for model {model!r}: "
                          f"{e.read().decode('utf-8', 'replace')[:200]}") from e
    except Exception as e:                  # noqa: BLE001
        raise OllamaError(f"Ollama call failed ({type(e).__name__}): {e}. "
                          "Check `ollama list` and that the desktop app is running.") from e
    return {"text": (d.get("response") or "").strip(),
            "model": model, "raw": d}


def generate_json(prompt: str, **kw) -> dict:
    """generate() + strict JSON parse. Raises OllamaError on garbage output."""
    r = generate(prompt, **kw)
    try:
        return {"json": json.loads(r["text"]), "model": r["model"]}
    except ValueError as e:
        raise OllamaError(
            f"model {r['model']} returned non-JSON: {r['text'][:160]!r}. "
            "Pick a model that follows the JSON format instruction, e.g. "
            "`ollama pull qwen2.5:14b`.") from e


# ---------------------------------------------------------------- one-shot health
def health(check_model=True) -> dict:
    """FULL local-AI health snapshot for the CLI (`collect.py ai-health`) and
    /admin/integrations. Each part answers READY / NEEDS_SETUP / FAILED with reason.
    Default checks that the selected model actually responds (the honest test)."""
    url = ollama_url()
    st = {"ollama_url": url, "running": False, "models": [], "selected_model": "",
          "model_responds": None, "overall": "NEEDS_SETUP", "reason": ""}
    try:
        st["models"] = list_models()
        st["running"] = True
    except OllamaError as e:
        st["reason"] = str(e)
        st["overall"] = "NEEDS_SETUP"
        return st
    if not st["models"]:
        st["reason"] = ("Ollama is running but no models are installed. "
                        "Fix: `ollama pull qwen2.5:14b` (~9 GB) or any instruct model.")
        return st
    m = select_model(st["models"])
    st["selected_model"] = m
    if not m:
        st["reason"] = (f"models installed ({', '.join(x['name'] for x in st['models'])}) "
                        "but none answered a test prompt — is one still downloading / OOM?")
        st["overall"] = "FAILED"
        return st
    if check_model:
        ok = _model_responds(m)
        st["model_responds"] = ok
        if not ok:
            st["reason"] = (f"model {m!r} did not answer a test prompt "
                            "(failed to load / still downloading?)")
            st["overall"] = "FAILED"
            return st
    st["overall"] = "READY"
    st["reason"] = f"local model {m!r} is live on {url}"
    return st
