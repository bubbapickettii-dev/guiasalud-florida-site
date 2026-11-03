"""Deployment facts: the public origin and the storage the database sits on.

Two failure modes these tests exist for:

  * a deployment that serves canonical URLs, a sitemap, robots and QR codes pointing at a host that
    does not exist (or, worse, at localhost) because nobody pasted a domain in;
  * a deployment that ACCEPTS a lead, consent record, buyer application or support message on a
    filesystem the host wipes on the next restart — a promise it cannot keep (consent evidence is
    kept 10 years; buyer/payment rows are money).

Resolution lives in ONE place (engine/hosting.py): BASE_URL > RENDER_EXTERNAL_URL > config/site.json
for the origin, and LEADENGINE_STORAGE / path inference for the storage. The free Render blueprint
(render-free.yaml) never attaches a disk, so it must land in the read-only preview branch, while the
paid blueprint (render.yaml, persistent disk) must not.

Every app probe runs in a clean subprocess with its own temp DB: no test touches the staging database.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import launch_check                    # noqa: E402
from engine import hosting             # noqa: E402

PY = sys.executable


# ------------------------------------------------------------------ the resolver itself
def test_origin_prefers_base_url_then_the_host_then_config():
    site = {"base_url": "http://localhost:8000"}
    assert hosting.public_origin(site, {}) == "http://localhost:8000"
    assert hosting.public_origin(site, {"RENDER_EXTERNAL_URL": "https://giasalud.onrender.com/"}) \
        == "https://giasalud.onrender.com"
    assert hosting.public_origin(site, {"RENDER_EXTERNAL_URL": "https://giasalud.onrender.com",
                                        "BASE_URL": "https://giasalud.com/"}) == "https://giasalud.com"
    assert hosting.origin_source(site, {"RENDER_EXTERNAL_URL": "https://a.onrender.com"}) == "RENDER_EXTERNAL_URL"
    assert hosting.origin_source(site, {"BASE_URL": "https://giasalud.com"}) == "BASE_URL"
    assert hosting.origin_source(site, {}) == "config/site.json"
    # only a real scheme://host counts — a bare host or a placeholder never becomes a canonical URL
    assert hosting.is_absolute_origin("https://giasalud.onrender.com") is True
    assert hosting.is_absolute_origin("giasalud.onrender.com") is False
    assert hosting.is_absolute_origin("https://x.com/path") is False
    assert hosting.is_absolute_origin("") is False


def test_storage_mode_is_declared_or_inferred_from_the_path():
    assert hosting.storage_mode("/var/data/leads.db", {}) == "persistent"
    assert hosting.storage_mode("/home/operator/data/leads.db", {}) == "persistent"
    assert hosting.storage_mode("/tmp/leads.db", {}) == "ephemeral"            # free host: no disk
    assert hosting.storage_mode(r"C:\app\data\leads.db", {}) == "ephemeral"
    # an explicit declaration wins (a mount this code cannot recognise, e.g. a Docker volume)
    assert hosting.storage_mode(r"D:\vol\leads.db", {"LEADENGINE_STORAGE": "persistent"}) == "persistent"
    assert hosting.storage_mode("/var/data/leads.db", {"LEADENGINE_STORAGE": "ephemeral"}) == "ephemeral"


def test_container_image_copies_the_runtime_files_the_app_imports():
    """The Dockerfile is a shipped deployment path (Render can build from it). Leaving launch_check.py
    out made /admin/readiness a 500 inside the running container — same code, same import, found only
    by actually booting the image. Pin the runtime files it must carry."""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    for name in ("app.py", "launch_check.py", "backup_db.py"):
        assert name in dockerfile, name
    for tree in ("engine", "config", "static", "templates"):
        assert f"COPY {tree} {tree}" in dockerfile, tree
    assert "gunicorn app:app" in dockerfile and "/healthz" in dockerfile


def test_blueprints_match_the_two_supported_deployments():
    """The free blueprint must NOT claim a disk (it would fail) and must declare ephemeral storage;
    the paid one must attach the disk and declare it persistent."""
    free = (ROOT / "render-free.yaml").read_text(encoding="utf-8")
    assert "plan: free" in free and "runtime: python" in free
    assert "disk:" not in free and "LEADENGINE_STORAGE" in free and "ephemeral" in free
    assert "healthCheckPath: /healthz" in free
    paid = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "LEADENGINE_STORAGE, value: persistent" in paid or "value: persistent" in paid
    assert "mountPath: /var/data" in paid and "plan: starter" in paid


# ------------------------------------------------------------------ the free deployment, end to end
_FREE_SNIPPET = """
import json, sys
sys.path.insert(0, __ROOT__)
import app
c = app.app.test_client()
home = c.get('/').get_data(as_text=True)
sm = c.get('/sitemap.xml').get_data(as_text=True)
lead = c.post('/api/lead', json={'campaign': 'mortgage-protection'})
out = {
    'health': c.get('/healthz').get_json(),
    'canonical_ok': '<link rel="canonical" href="https://giasalud.onrender.com/">' in home,
    'og_url_ok': 'https://giasalud.onrender.com/' in home,
    'sitemap_ok': 'https://giasalud.onrender.com/check?a=es_check' in sm,
    'robots': c.get('/robots.txt').get_data(as_text=True),
    'footer_location': 'Miami, Florida' in home,
    'en': c.get('/check').status_code, 'es': c.get('/check?a=es_check').status_code,
    'aprende': c.get('/aprende').status_code, 'learn': c.get('/learn').status_code,
    'privacidad': c.get('/privacidad').status_code, 'terminos': c.get('/terminos').status_code,
    'agents': c.get('/for-agents').status_code, 'agencias': c.get('/para-agencias').status_code,
    'demo_portal': c.get('/agent/demo-health-token').status_code,
    'admin_no_token': c.get('/admin').status_code,
    'lead_status': lead.status_code, 'lead_error': (lead.get_json() or {}).get('error'),
    'contact_refused': 'does not store messages' in c.post('/contact', data={'message': 'hola'}).get_data(as_text=True),
    'optout_refused': 'Nothing was recorded' in c.post('/optout', data={'email': 'x@y.co'}).get_data(as_text=True),
    'apply_refused': 'does not store applications' in c.post('/for-agents/apply', data={
        'agency': 'A', 'contact_name': 'B', 'contact_email': 'c@d.co', 'licensed_states': 'FL'}).get_data(as_text=True),
    'redirect': c.get('/check?x=1', headers={'X-Forwarded-Proto': 'http'}).headers.get('Location'),
    'no_redirect': c.get('/check', headers={'X-Forwarded-Proto': 'https'}).status_code,
    'no_header': c.get('/check').status_code,
}
print('RESULT ' + json.dumps(out))
"""

_PERSISTENT_SNIPPET = """
import json, sys
sys.path.insert(0, __ROOT__)
import app
c = app.app.test_client()
out = {
    'health': c.get('/healthz').get_json(),
    'contact_stored': 'We received your message' in c.post('/contact', data={'message': 'hola'}).get_data(as_text=True),
}
print('RESULT ' + json.dumps(out))
"""


def _probe(snippet: str, env_overrides: dict, tmp: Path) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("STRIPE_", "LEADENGINE_", "INTEL_", "ADMIN_", "RENDER_", "BASE_URL", "ALLOW_"))}
    env.update({"PYTHONIOENCODING": "utf-8", "INTEL_DB": ""})
    env.update(env_overrides)
    r = subprocess.run([PY, "-c", snippet.replace("__ROOT__", repr(str(ROOT)))],
                       capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=180)
    line = next((ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")), "")
    assert line, f"probe did not run:\n{r.stdout[-1500:]}\n{r.stderr[-1500:]}"
    return json.loads(line[len("RESULT "):])


def test_free_tier_deployment_is_a_working_read_only_public_site(tmp_path):
    """A free Render service (assigned origin, no disk, production guards) must serve the real
    bilingual site with correct canonicals/sitemap/robots — and refuse every write it could not keep."""
    out = _probe(_FREE_SNIPPET, {
        "LEADENGINE_ENV": "production", "ADMIN_TOKEN": "x" * 40,
        "LEADENGINE_DB": str(tmp_path / "leads.db"), "LEADENGINE_STORAGE": "ephemeral",
        "RENDER_EXTERNAL_URL": "https://giasalud.onrender.com",
    }, tmp_path)
    h = out["health"]
    assert h["ok"] is True and h["environment"] == "production"
    assert h["base_url"] == "https://giasalud.onrender.com"
    assert h["base_url_source"] == "RENDER_EXTERNAL_URL"
    assert h["storage"] == "ephemeral" and h["read_only_preview"] is True
    assert h["accepts_submissions"] is False
    # public metadata all points at the assigned origin, never at localhost or a staging host
    assert out["canonical_ok"] and out["og_url_ok"] and out["sitemap_ok"]
    assert "localhost" not in json.dumps(out["health"]) and out["footer_location"]
    # indexing stays off until launch, and the crawl policy stays self-consistent
    assert h["indexing"] is False
    assert "Disallow: /" in out["robots"] and "Sitemap:" not in out["robots"]
    # consumer flows exist in both languages
    for k in ("en", "es", "learn", "aprende", "privacidad", "terminos", "agents", "agencias"):
        assert out[k] == 200, k
    # private surfaces stay private: demo portals are refused in production, admin needs the token
    assert out["demo_portal"] == 404 and out["admin_no_token"] == 403
    # writes are refused with a nameable reason instead of being stored and lost
    assert out["lead_status"] == 503 and out["lead_error"] == "preview_read_only"
    assert out["contact_refused"] and out["optout_refused"] and out["apply_refused"]
    # plain-http requests are redirected to https, and only when the proxy says they were http
    assert out["redirect"] == "https://giasalud.onrender.com/check?x=1"
    assert out["no_redirect"] == 200 and out["no_header"] == 200


def test_persistent_host_still_collects_normally(tmp_path):
    """The same app on a real disk (paid Render plan / PythonAnywhere) must NOT be in preview mode:
    records are kept, so the contact form stores and acknowledges."""
    out = _probe(_PERSISTENT_SNIPPET, {
        "LEADENGINE_ENV": "production", "ADMIN_TOKEN": "x" * 40,
        "LEADENGINE_DB": str(tmp_path / "leads.db"), "LEADENGINE_STORAGE": "persistent",
        "BASE_URL": "https://giasalud.com",
    }, tmp_path)
    h = out["health"]
    assert h["storage"] == "persistent" and h["read_only_preview"] is False
    assert h["accepts_submissions"] is True and h["base_url_source"] == "BASE_URL"
    assert out["contact_stored"] is True


# ------------------------------------------------------------------ the checklist agrees
def test_launch_check_names_the_origin_source_and_the_ephemeral_hazard(monkeypatch):
    monkeypatch.setenv("LEADENGINE_ENV", "production")
    monkeypatch.setenv("ADMIN_TOKEN", "x" * 40)
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://giasalud.onrender.com")
    monkeypatch.setenv("LEADENGINE_DB", "/tmp/leads.db")
    monkeypatch.delenv("BASE_URL", raising=False)
    monkeypatch.delenv("LEADENGINE_STORAGE", raising=False)
    block, _cat, base = launch_check.collect()
    assert base == "https://giasalud.onrender.com"
    assert block["1 Domain"] == []                                          # the assigned origin is READY
    assert any("EPHEMERAL" in x for x in block["2 Hosting"])                # but the disk still is not
    # declaring a persistent disk clears that item; the origin stays a fact, not a blocker
    monkeypatch.setenv("LEADENGINE_DB", "/var/data/leads.db")
    block2, _c2, base2 = launch_check.collect()
    assert base2 == "https://giasalud.onrender.com"
    assert not any("EPHEMERAL" in x for x in block2["2 Hosting"])


def test_identity_has_no_outstanding_placeholder_except_the_pending_inbox(tmp_path):
    """Gia Salud / Miami, Florida are configured; the support inbox is the one open item and no page
    may show an address before it exists."""
    block, _cat, _base = launch_check.collect()
    legal = " ".join(block["4 Legal review"])
    assert "LEGAL_ENTITY missing" not in legal and "LOCATION missing" not in legal
    assert "Support inbox not configured" in legal
