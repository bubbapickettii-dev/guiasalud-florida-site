# LOCAL AI SETUP — GS Allude collection intelligence on THIS machine

*Written Oct 27, 2026 after a live setup + verification on this PC. Everything below
was executed, not guessed. Verified states are marked ✅.*

## 1. What is already installed (verified)

| Component | State |
|---|---|
| Windows 11 · Python 3.12.10 (system + project venv) | ✅ fine — ScrapeGraphAI needs ≥ 3.12 |
| Ollama 0.40.11, running at `http://127.0.0.1:11434` | ✅ starts with the desktop app / `ollama serve` |
| Model `qwen2.5:14b` (~9 GB, disk) | ✅ installed, answers EN + ES prompts, clean JSON |
| pip 25.0.1, git, curl | ✅ |
| PyPI reachable | ✅ |
| App venv `staging_source/leadengine_staging/.venv` | ✅ UNTOUCHED — no AI deps added there |
| AI venv `C:\gs-test\.ai-scrape` (Python 3.12.10) | ✅ has `scrapegraphai` installed |

## 2. What I configured in the project

* **`engine/ollama_client.py`** (new) — stdlib-only Ollama client: URL + model
  discovery, per-model response test, JSON generation. No SDK, no key, no cost.
* **`engine/web_extract.py`** — the collector now runs a local-AI extraction pass on
  every fresh webreader page when Ollama answers: mode `ollama_json` (direct) or
  `sga` (full ScrapeGraphAI subprocess). Results are stored in `signals.db →
  signal_extract` as reviewer evidence. If Ollama is off, the collection loop
  falls back to the plain reader — nothing breaks.
* **`tools/sga_runner.py`** — runs a ScrapeGraphAI graph in the optional AI venv
  via stdin/stdout (JSON protocol). It only ever touches the URL it is given +
  the local Ollama API. No project data is openable from that process.
* **`tools/collect.py`** — two new commands (below) + `out/ai_state.json`
  snapshots for the admin page.
* **`/admin/integrations`** — full local-AI panel: Python/pip versions, Ollama
  status, installed models + sizes, selected model, per-check health states, and
  the last smoke results.
* **`config/sources.json → "ollama"`** — `{url, model}` config block
  (`model: ""` = auto-pick the first installed model that answers). env override:
  `OLLAMA_MODEL` / `OLLAMA_URL`.

## 3. Commands to run (copy-paste)

```bat
cd staging_source\leadengine_staging

:: 1) health of the whole local-AI stack (also writes out/ai_state.json)
.venv\Scripts\python.exe -X utf8 tools\collect.py ai-health

:: 2) end-to-end AI smoke — SYNTHETIC page only, no live websites, no PII
.venv\Scripts\python.exe -X utf8 tools\collect.py ai-smoke

:: 3) confirm the admin page shows the panel (dev server required)
::    http://127.0.0.1:5000/admin/integrations?token=<ADMIN_TOKEN>

:: 4) live SGA-runner test (fetches ONE public webpage: scrapegraphai.com)
.venv\Scripts\python.exe -X utf8 tests\manual\test_sga_runner_live.py
```

Current verified output: `ai-health` → **READY** (all 6 checks), `ai-smoke` →
**PASS** (direct Ollama JSON + ScrapeGraphAI subprocess).

## 4. Collecting WITH the local model (when you want to)

The collection sweep is always an explicit operator action:

```bat
.venv\Scripts\python.exe -X utf8 tools\collect.py run --network   :: all collectors
.venv\Scripts\python.exe -X utf8 tools\signals_watch.py web       :: webreader seeds
.venv\Scripts\python.exe -X utf8 tools\collect.py schedule-cmd    :: prints cron/schtasks lines (registers nothing)
```

While Ollama is running, fresh webreader pages automatically get a structured
extraction pass; results are attached to the signal (admin → Integrations shows
the mode; reviewers see the JSON in `signal_extract`).

## 5. Installing another Ollama model (needs YOUR approval first)

Only needed if you uninstall qwen2.5:14b or want a smaller/faster model:

```bat
ollama pull llama3.2:3b        :: ~2.0 GB — much lighter, weaker JSON discipline
ollama pull qwen2.5:7b         :: ~4.7 GB — middle option
ollama pull qwen2.5:14b        :: ~9.0 GB — current model
```

Then pin it with `setx OLLAMA_MODEL "...name..."` (or
`config/sources.json → ollama → model`), and rerun `ai-health` to confirm it
answers. There is no per-request cost — models run on your machine.

## 6. Common errors → fixes

| Symptom | Cause | Fix |
|---|---|---|
| `health: NOT REACHABLE` | Ollama app closed | Start the Ollama desktop app (system tray) or run `ollama serve` in a terminal |
| `no models installed` | fresh Ollama | `ollama pull qwen2.5:14b` (9 GB disk — approve before pulling) |
| `model … did not answer` | model still downloading / OOM | close other GPU memory users, rerun `ai-health` after pull completes |
| `_xxhash` / any DLL "filename or extension too long" | venv at a deeply nested Windows path | keep the AI venv at `C:\gs-test\.ai-scrape` (already done); never recreate it under this repo's long project path |
| `error: empty stdout` in the SGA subprocess test | wrong script path (absolute vs relative) | run from the repo root; the runner lives at `staging_source/leadengine_staging/tools/sga_runner.py` |
| SGA result says `"content": "NA"` | page had little parseable text, or it renders via JS | use the direct `ollama_json` path (page text is pre-extracted) or give SGA a text-heavy URL |
| `scrapegraphai is not installed` inside the app venv | by design (app venv kept small) | that's fine — `ollama_json` mode works; SGA runs through the subprocess path |

## 7. How to undo the optional integration (app untouched)

Nothing in the app depends on this — deleting any of these leaves every existing
feature working (collection simply falls back to the stdlib reader):

1. Stop using the model: set `OLLAMA_MODEL` env to blank / set Ollama app to not autostart.
2. Remove the AI venv entirely: `rmdir /s /q C:\gs-test\.ai-scrape`
3. Remove `tools/sga_runner.py`, and the `ollama` block in `config/sources.json` if you want zero config residue.
4. The `signal_extract` table in `signals.db` can hold reviewer evidence; drop it with
   `sqlite3 data/signals.db "DROP TABLE IF EXISTS signal_extract;"` — signals themselves stay intact.

## 8. Privacy rules (these stay hard requirements)

* The local model receives **only the fetched PUBLIC page text** (title + visible
  text, bounded to 4,000 chars) plus the seed's extraction instructions.
* No leads.db, buyer, prospect, consent, token or credential data is ever opened
  or sent — the runner process physically cannot read them.
* Synthetic smoke text never interacts with real signal storage.
