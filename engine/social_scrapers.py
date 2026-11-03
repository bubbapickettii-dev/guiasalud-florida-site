"""PAID SOCIAL-DISCOVERY ADAPTERS — Apify + Bright Data, GATED BY DESIGN.

The platforms this business actually needs (Instagram, TikTok, Facebook pages/groups,
SERP-forum search) have NO compliant credential-free collection access from a datacenter
network: the official APIs are behind platform approvals, and scraping them directly is
against the platforms' terms. The honest operational answer used across this industry is a
licensed commercial collection service — one of the two operators already researched:

  * Apify       — per-actor runs, paid per unit ("Actor" execution + dataset items)
  * Bright Data — per-dataset collection runs ("web data API"), paid per record

THE SPEND RULE (mirrors places.approved and search_web NEEDS_KEY elsewhere in this repo):
  The code NEVER charges anything and NEVER activates an account. Each entry point runs a
  double gate and does ZERO network I/O until BOTH are open:

  1. APPROVAL   — config/sources.json > paid_social > <service> > approved = true. This is a
                  deliberate operator record only ever set by a person who decided the budget;
                  the code refuses with NEEDS_APPROVAL otherwise and names the gate.
  2. CREDENTIAL — the service token is read from the process env (APIFY_TOKEN /
                  BD_DATA_API_TOKEN), never from the repo and never logged. Missing token =>
                  NEEDS_CREDENTIALS with the exact env-var name.

When both gates are open the run is one API call per configured actor; results flow into
engine.signals.ingest (dedupe, taxonomy accept_filter, author-minimizing whitelist, review
queue) EXACTLY like every other source. Items that carry a taxonomy topic classify as
INTEREST (person-shaped); the rest drop out — same discipline as the free collectors.

Nothing in this module contacts, messages, or enrolls anyone; it only reads public items the
licensed service returns and records them for operator review.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

from pathlib import Path

from . import signals as sig

ROOT = Path(__file__).resolve().parents[1]

APIFY_RUN_SYNC = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
BD_TRIGGER = "https://api.brightdata.com/datasets/v3/trigger"
BD_SNAPSHOT = "https://api.brightdata.com/datasets/v3/progress"

APIS = {
    "apify": {"env": "APIFY_TOKEN", "label": "Apify (actors marketplace)"},
    "brightdata": {"env": "BD_DATA_API_TOKEN", "label": "Bright Data (web data API)"},
}


def _paid_cfg(cfg: dict | None = None) -> dict:
    """Read config/sources.json > paid_social (never raises)."""
    if cfg is not None:
        return cfg if isinstance(cfg, dict) else {}
    try:
        return (json.loads((ROOT / "config" / "sources.json").read_text(encoding="utf-8"))
                or {}).get("paid_social") or {}
    except (OSError, ValueError):
        return {}


def _gate_row(service: str, reason: str, detail: str) -> dict:
    """Honest refused-row shape used by every entry point."""
    return {"source": "paid:" + service, "status": reason,
            "fetched": 0, "added": 0, "note": detail[:160]}


def _service_gate(service: str, cfg: dict) -> dict | None:
    """One shared double gate. Returns the refusal row when the service must not run; None
    means approved + credentialed and the run may proceed."""
    svc = (cfg.get(service) or {}) if isinstance(cfg, dict) else {}
    if svc.get("approved") is not True:
        return _gate_row(service, "NEEDS_APPROVAL",
                         f"config/sources.json > paid_social > {service} > approved is false "
                         f"(operator budget decision required)")
    token = (os.environ.get(APIS[service]["env"]) or "").strip()
    if not token:
        return _gate_row(service, "NEEDS_CREDENTIALS",
                         f"set env {APIS[service]['env']} to the service API token")
    return None


# --------------------------------------------------------------------------- normalization
def normalize_items(items: list[dict], source_label: str, community: str = "") -> list[dict]:
    """Service payloads -> signal rows via the SAME minimal whitelist as every other source.
    Author handles, profile ids, media arrays and contact fields are dropped on the floor: the
    classifier only receives varchar text + a durable public item URL. Items without a real
    permalink are skipped (duplicates and missing-URL rows never ingest)."""
    import datetime as dt
    out = []
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        url = (it.get("url") or it.get("outputUrl") or it.get("postUrl") or
               it.get("link") or it.get("commentUrl") or "").strip()
        if not url or "://" not in url:
            continue
        text = " ".join(x for x in (it.get("text"), it.get("description"), it.get("caption"),
                                    it.get("title"), it.get("message"), it.get("body"),
                                    it.get("comment_text")) if x)
        if not text.strip():
            continue
        observed = it.get("created_at") or it.get("createdAt") or it.get("timestamp") or ""
        if isinstance(observed, (int, float)):
            try:
                observed = dt.datetime.fromtimestamp(observed, dt.timezone.utc).isoformat(timespec="seconds")
            except (OverflowError, OSError, ValueError):
                observed = ""
        out.append({"source": source_label,
                    "community": (it.get("community") or community or source_label)[:80],
                    "title": sig._snippet(text[:300]),
                    "snippet": sig._snippet(text[:600]),
                    "url": url[:400],
                    "observed_at": observed or None,
                    "collection_method": "paid_service_api",
                    "access_status": "licensed_service"})
    return out


# --------------------------------------------------------------------------- Apify runner
def _apify_post(url: str, *, body: str = "", bearer: str = "") -> list:   # pragma: no cover
    req = urllib.request.Request(url, data=(body or "{}").encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {bearer}"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=330) as resp:   # noqa: S310 - licensed official API
        return json.loads(resp.read().decode("utf-8", "replace"))


def run_apify_actors(con, cfg: dict | None = None, fetch_json=None) -> list[dict]:
    """Run each APPROVED+CREDENTIALED actor listed in paid_social.apify.actors as ONE
    run-sync-get-dataset-items call and push result items through the shared normalize/ingest
    path. Actor payloads come from config (operator-tuned); the default ones target the actual
    Instagram/TikTok/Facebook/SERP needs. Never a person is contacted; never an account is
    created or charged. Returns one report row per actor (or one service-level refusal row)."""
    cfg = _paid_cfg(cfg)
    gate = _service_gate("apify", cfg)
    if gate:
        return [gate]
    fetch_json = fetch_json or _apify_post
    svc = cfg.get("apify") or {}
    actors = svc.get("actors") or {}
    rows = []
    for name, spec in actors.items():
        if not isinstance(spec, dict):
            continue
        actor = (spec.get("actor_id") or "").strip()
        if not actor:
            rows.append({"source": "paid:apify", "status": "NEEDS_SETUP",
                         "fetched": 0, "added": 0,
                         "note": f"actor '{name}' has no actor_id in config"})
            continue
        # second-stage comment actors run on OPERATOR-SELECTED post URLs: their input arrays
        # (directUrls/startUrls/postURLs) are operator-pasted after the post-collection stage.
        # An empty list means there is nothing to collect — refuse BEFORE spending a run.
        payload = spec.get("input") or {}
        if spec.get("_requires_urls") and not any(
            payload.get(k) for k in ("directUrls", "startUrls", "postURLs")):
            rows.append({"source": "paid:apify", "status": "NEEDS_SETUP",
                         "fetched": 0, "added": 0,
                         "note": f"actor '{name}' is a second-stage comment actor and needs post URLs "
                                 f"pasted into its input first (config > paid_social > apify > actors > {name})"})
            continue
        text_url = APIFY_RUN_SYNC.format(actor=actor)
        try:
            payload = spec.get("input") or {}
            items = fetch_json(text_url, method="POST", body=json.dumps(payload),
                               bearer=(os.environ.get("APIFY_TOKEN") or "").strip())
            source = spec.get("signal_source") or "apify"
            norm = normalize_items(items if isinstance(items, list) else [], source,
                                   spec.get("label") or name)
            added = 0
            for row in norm:
                if not sig.accept_filter(row):     # marketing/promo posts drop out, same as free sources
                    continue
                r = sig.ingest(con, [row], source)
                added += r["added"]
            rows.append({"source": "paid:apify", "status": "ok", "actor": actor,
                         "fetched": len(items) if isinstance(items, list) else 0,
                         "added": added, "label": name,
                         "note": f"actor {actor} run completed (run-sync)"})
        except Exception as e:             # noqa: BLE001 - a failed run is a reportable row
            rows.append({"source": "paid:apify", "status": "error", "actor": actor,
                         "fetched": 0, "added": 0, "label": name,
                         "note": f"{type(e).__name__}: {e}"[:160]})
    return rows


# --------------------------------------------------------------------------- Bright Data runner
def run_brightdata_datasets(con, cfg: dict | None = None, trigger=None, poll=None,
                            sleeper=None) -> list[dict]:
    """Trigger each APPROVED+CREDENTIALED dataset declared in paid_social.brightdata.datasets,
    poll its snapshot until it completes, and push result rows through the shared
    normalize/ingest path. dataset_id is an operator-pasted value (assigned when you create
    the dataset in the service console); the config shapes the remaining "inputs" list."""
    cfg = _paid_cfg(cfg)
    gate = _service_gate("brightdata", cfg)
    if gate:
        return [gate]
    trigger = trigger or _bd_trigger
    poll = poll or _bd_poll
    import time as _t
    sleeper = sleeper or _t.sleep
    svc = cfg.get("brightdata") or {}
    datasets = svc.get("datasets") or {}
    token = (os.environ.get("BD_DATA_API_TOKEN") or "").strip()
    rows = []
    for name, spec in datasets.items():
        if not isinstance(spec, dict):
            continue
        dsid = (spec.get("dataset_id") or "").strip()
        if not dsid:
            rows.append({"source": "paid:brightdata", "status": "NEEDS_SETUP",
                         "fetched": 0, "added": 0,
                         "note": f"dataset '{name}' has no dataset_id pasted in config"})
            continue
        try:
            snap = trigger(dsid, spec.get("inputs") or [], token)
            data = poll(snap, token, sleeper)
            source = spec.get("signal_source") or "brightdata"
            norm = normalize_items(data if isinstance(data, list) else [], source,
                                   spec.get("label") or name)
            added = 0
            for row in norm:
                if not sig.accept_filter(row):     # marketing/promo posts drop out, same as free sources
                    continue
                r = sig.ingest(con, [row], source)
                added += r["added"]
            rows.append({"source": "paid:brightdata", "status": "ok", "dataset": dsid,
                         "fetched": len(data) if isinstance(data, list) else 0,
                         "added": added, "label": name})
        except Exception as e:             # noqa: BLE001
            rows.append({"source": "paid:brightdata", "status": "error", "dataset": dsid,
                         "fetched": 0, "added": 0, "label": name,
                         "note": f"{type(e).__name__}: {e}"[:160]})
    return rows


def _bd_trigger(dataset_id: str, inputs: list, token: str) -> str:   # pragma: no cover
    params = urllib.parse.urlencode({"dataset_id": dataset_id, "format": "json",
                                     "uncompressed_webhook": "true"})
    req = urllib.request.Request(
        BD_TRIGGER + "?" + params, data=json.dumps(inputs or [{}]).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:   # noqa: S310 - licensed official API
        res = json.loads(resp.read().decode("utf-8", "replace"))
    return res["snapshot_id"]


def _bd_poll(snapshot_id: str, token: str, sleeper, max_polls: int = 30) -> list:  # pragma: no cover
    import urllib.request as _u
    for _ in range(max_polls):
        req = _u.Request(
            f"{BD_SNAPSHOT}/{snapshot_id}?format=json",
            headers={"Authorization": f"Bearer {token}"})
        with _u.urlopen(req, timeout=60) as resp:     # noqa: S310
            res = json.loads(resp.read().decode("utf-8", "replace"))
        if res.get("status") == "ready":
            return res.get("data") or []
        sleeper(10)
    raise TimeoutError(f"snapshot {snapshot_id} not ready after {max_polls} polls")


# --------------------------------------------------------------------------- sweep entry
def run_paid_social(con, cfg: dict | None = None) -> list[dict]:
    """The paid collectors' sweep entry: each service contributes its refusal or result rows.
    Used by tools/collect.py (run --network and social-paid) and the world unittest fake."""
    rows: list[dict] = []
    try:
        rows += run_apify_actors(con, cfg)
    except Exception as e:                 # noqa: BLE001 - one service never kills the sweep
        rows.append(_gate_row("apify", "error", f"{type(e).__name__}: {e}"))
    try:
        rows += run_brightdata_datasets(con, cfg)
    except Exception as e:                 # noqa: BLE001
        rows.append(_gate_row("brightdata", "error", f"{type(e).__name__}: {e}"))
    return rows
