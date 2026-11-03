"""What the HOST says about this deployment — resolved once, every consumer reads it.

Two facts about a deployment must never be guessed locally:

  * **the public origin.** Every canonical tag, hreflang alternate, sitemap entry, robots
    ``Sitemap:`` line, QR code, guide PDF and prepared partner link is built from it. A wrong
    value silently poisons search results and printed material, so there is exactly one
    resolution order and no module builds an origin of its own.
  * **whether the database file survives a restart.** Consent evidence must be kept for years
    and buyer/payment ledgers are money: running them on a filesystem the host wipes is data
    loss, not a configuration detail. The app refuses public submissions on such a host
    (``app.READONLY_PREVIEW``) instead of storing records it cannot keep.

Origin order (first non-empty wins):

  1. ``BASE_URL``             — the operator's own setting (any host, any domain)
  2. ``RENDER_EXTERNAL_URL``  — injected by Render on every web service; always the
                                ``https://<service>.onrender.com`` origin
  3. ``config/site.json``     — ``base_url`` (staging default, dev/test overrides)

Storage mode:

  ``LEADENGINE_STORAGE=persistent|ephemeral`` declares it outright and wins. Otherwise it is
  inferred from the DB path: a path under a mounted volume (``/data``, ``/var/data``, ``/home``,
  ``/mnt``, ``/srv``) is treated as persistent; a repo directory, ``/tmp`` or a Windows temp dir
  is treated as ephemeral. Only a production run acts on the answer.
"""
from __future__ import annotations

import os
import re

# Paths that are a mounted volume (a persistent disk) on the hosts this repo documents.
PERSISTENT_PREFIXES = ("/data", "/var/data", "/home/", "/mnt/", "/srv/")

# Host-injected variables that carry the deployment's own public origin, in priority order.
HOST_ORIGIN_ENV = "RENDER_EXTERNAL_URL"

_ORIGIN_RE = re.compile(r"^https?://[^/\s]+$", re.I)


def is_absolute_origin(url: str) -> bool:
    """True only for a real ``scheme://host`` with no path — never for a bare host or a placeholder."""
    return bool(_ORIGIN_RE.match((url or "").strip()))


def _env(env):
    return os.environ if env is None else env


def public_origin(site: dict | None = None, env=None) -> str:
    """The absolute public origin links are built from, or '' when nothing usable is configured."""
    e = _env(env)
    for key in ("BASE_URL", HOST_ORIGIN_ENV):
        v = (e.get(key) or "").strip()
        if v:
            return v.rstrip("/")
    return ((site or {}).get("base_url") or "").strip().rstrip("/")


def origin_source(site: dict | None = None, env=None) -> str:
    """Where the origin above came from, for /healthz and Admin > Readiness (never inferred by eye)."""
    e = _env(env)
    if (e.get("BASE_URL") or "").strip():
        return "BASE_URL"
    if (e.get(HOST_ORIGIN_ENV) or "").strip():
        return HOST_ORIGIN_ENV
    if ((site or {}).get("base_url") or "").strip():
        return "config/site.json"
    return ""


def origin_label(site: dict | None = None, env=None) -> str:
    """A one-line, human-usable description of the origin and its source."""
    origin = public_origin(site, env) or "(unset)"
    src = origin_source(site, env)
    return f"{origin} (from {src})" if src else origin


def storage_declared(env=None) -> str:
    """What the operator DECLARED, or '' — the difference between a fact and a guess.

    Only a declaration can switch the app into the read-only public preview, because an inferred
    answer can be wrong in both directions (a Docker volume under /app/data, a Windows server on a
    D: drive). A guess is still reported and warned about, never acted on silently."""
    declared = (_env(env).get("LEADENGINE_STORAGE") or "").strip().lower()
    return declared if declared in ("persistent", "ephemeral") else ""


def storage_mode(db_path, env=None) -> str:
    """``'persistent'`` or ``'ephemeral'`` for the database file — declared, else inferred by path."""
    declared = storage_declared(env)
    if declared:
        return declared
    path = str(db_path or "").replace("\\", "/")
    if not path:
        return "ephemeral"
    return "persistent" if path.startswith(PERSISTENT_PREFIXES) else "ephemeral"
