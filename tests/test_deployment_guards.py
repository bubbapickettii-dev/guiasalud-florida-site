"""Deployment-preparation guards (controlled staging -> production safety).

Covers the fail-fast checks that run at import time in app.py plus the payment-rail
approval logic in engine/payments.py:

  - a wrong ADMIN_TOKEN in production still refuses to start (existing guard, pinned here)
  - sk_live_ Stripe keys refuse to start OUTSIDE production (accidental live rail in staging)
  - an unknown-prefix STRIPE_SECRET_KEY refuses to start in ANY mode (garbage/typo key would
    create sessions nobody can fulfill)
  - a half-configured rail (secret key without webhook secret, or vice versa in production)
    refuses to start: buyers could pay but the credit could never be verified
  - INTEL_DB is refused in production: the public-record prospect DB stays on the operator's
    machine (consumer-privacy posture documented in launch_check.py / docs/DEPLOYMENT.md)
  - rail approval is an AND: LEADENGINE_ENV=production alone never auto-approves sk_live_;
    the operator must also set marketplace.payments.live_payments
  - launch_check.py prints the four deployment categories

Each app-import test runs in a clean subprocess with its OWN temp DB: no test touches the
real staging sqlite file, and no test ever sends data anywhere.
"""
import subprocess
import sys
import tempfile
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

_IMPORT_SNIPPET = """import sys; sys.path.insert(0, {root!r})
import app  # noqa: F401  - module import IS the startup guard
print('STARTED')
"""


def _import_app(env_overrides: dict, tmp: Path) -> tuple[int, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("STRIPE_", "LEADENGINE_", "INTEL_", "ADMIN_"))}
    env.update(env_overrides)
    env["LEADENGINE_DB"] = str(tmp / "g.db")
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([PY, "-c", _IMPORT_SNIPPET.format(root=str(ROOT))],
                       capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=120)
    return r.returncode, (r.stdout + r.stderr)


def test_startup_refuses_wrong_admin_token_in_production(tmp_path):
    code, out = _import_app({"LEADENGINE_ENV": "production", "ADMIN_TOKEN": "change-me", "INTEL_DB": ""}, tmp_path)
    assert code != 0 and "Refusing to start" in out and "ADMIN_TOKEN" in out


def test_startup_refuses_live_stripe_key_outside_production(tmp_path):
    code, out = _import_app({"LEADENGINE_ENV": "staging", "ADMIN_TOKEN": "x" * 40,
                             "STRIPE_SECRET_KEY": "sk_live_abc", "STRIPE_WEBHOOK_SECRET": "whsec_abc"}, tmp_path)
    assert code != 0 and "sk_live_" in out and "production" in out


def test_startup_refuses_unknown_stripe_key_prefix(tmp_path):
    code, out = _import_app({"ADMIN_TOKEN": "staging", "STRIPE_SECRET_KEY": "sk_whoops_abc",
                             "STRIPE_WEBHOOK_SECRET": "whsec_abc"}, tmp_path)
    assert code != 0 and "neither sk_test" in out


def test_startup_refuses_key_without_webhook_secret(tmp_path):
    code, out = _import_app({"ADMIN_TOKEN": "x" * 40, "STRIPE_SECRET_KEY": "sk_test_abc"}, tmp_path)
    assert code != 0 and "STRIPE_WEBHOOK_SECRET" in out


def test_startup_refuses_webhook_secret_without_key_in_production(tmp_path):
    code, out = _import_app({"LEADENGINE_ENV": "production", "ADMIN_TOKEN": "x" * 40,
                             "STRIPE_WEBHOOK_SECRET": "whsec_abc", "INTEL_DB": ""}, tmp_path)
    assert code != 0 and "without STRIPE_SECRET_KEY" in out


def test_startup_refuses_intel_db_in_production(tmp_path):
    code, out = _import_app({"LEADENGINE_ENV": "production", "ADMIN_TOKEN": "x" * 40,
                             "STRIPE_SECRET_KEY": "sk_test_abc", "STRIPE_WEBHOOK_SECRET": "whsec_abc",
                             "INTEL_DB": str(tmp_path / "leadgen.db")}, tmp_path)
    assert code != 0 and "INTEL_DB" in out and "stays on your machine" in out


def test_startup_allows_test_rail_in_staging(tmp_path):
    code, out = _import_app({"ADMIN_TOKEN": "staging", "STRIPE_SECRET_KEY": "sk_test_abc",
                             "STRIPE_WEBHOOK_SECRET": "whsec_abc"}, tmp_path)
    assert code == 0 and "STARTED" in out, out[-400:]


def test_rail_approval_is_and_not_or(tmp_path):
    """sk_live_ in production STILL needs marketplace.payments.live_payments (operator's explicit,
    deliberate flag) - the production env alone must never auto-approve real charges."""
    os.environ.pop("STRIPE_SECRET_KEY", None)
    import engine.payments as pay
    live = {"payments": {"live_payments": True}}
    try:
        os.environ["STRIPE_SECRET_KEY"] = "sk_live_abc"
        os.environ["LEADENGINE_ENV"] = "production"
        assert pay.mode() == "live"
        assert pay.rail_approved({}) is False                       # production env alone: refused
        assert pay.rail_approved({"payments": {}}) is False         # flag at a wrong spot: refused
        assert pay.rail_approved(live) is True                      # flag + production: approved
        os.environ["LEADENGINE_ENV"] = "staging"
        assert pay.rail_approved(live) is False                     # flag alone without production: refused
        os.environ["STRIPE_SECRET_KEY"] = "sk_test_abc"
        assert pay.rail_approved({}) is True                        # TEST rail is always approved
    finally:
        os.environ.pop("STRIPE_SECRET_KEY", None)
        os.environ.pop("LEADENGINE_ENV", None)


_PORTAL_SNIPPET = """import sys, os
sys.path.insert(0, {root!r})
import app
c = app.app.test_client()
for tok in ('demo-health-token', 'demo-agency-a-token', 'CHANGE-ME-agent-health-1'):
    print(tok, c.get('/agent/' + tok).status_code)
print('home', c.get('/').status_code)
print('check', c.get('/check').status_code)
"""


def _portal_statuses(env_overrides: dict, tmp: Path) -> str:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("STRIPE_", "LEADENGINE_", "INTEL_", "ADMIN_"))}
    env.update(env_overrides)
    env["LEADENGINE_DB"] = str(tmp / "portal.db")
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([PY, "-c", _PORTAL_SNIPPET.format(root=str(ROOT))], capture_output=True, text=True,
                       cwd=str(ROOT), env=env, timeout=180)
    assert r.returncode == 0, (r.stdout + r.stderr)[-800:]
    return r.stdout


def test_demo_and_draft_portals_are_closed_in_production(tmp_path):
    """A demo fixture's token is documented in docs/USER_WALKTHROUGH.md, and a draft row's token is literally
    CHANGE-ME-…: neither may open a desk on a LIVE deployment. The same demo token still works in staging,
    where the fixture is a labelled local rehearsal, so tests and walkthroughs keep working."""
    prod = _portal_statuses({"LEADENGINE_ENV": "production", "ADMIN_TOKEN": "x" * 40, "INTEL_DB": ""}, tmp_path)
    assert "demo-health-token 404" in prod
    assert "demo-agency-a-token 404" in prod
    assert "CHANGE-ME-agent-health-1 404" in prod
    assert "home 200" in prod and "check 200" in prod      # consumer pages unaffected by the desk gate

    staging = _portal_statuses({"ADMIN_TOKEN": "staging"}, tmp_path)
    assert "demo-health-token 200" in staging                 # labelled fixture, local rehearsal only
    assert "CHANGE-ME-agent-health-1 404" in staging          # a draft has no desk in any environment


_ELIGIBILITY_SNIPPET = """import sys, os, json, pathlib
sys.path.insert(0, {root!r})
import app
cfg = pathlib.Path(os.environ['PROBE_CFG']); cfg.mkdir(parents=True, exist_ok=True)
base = {{"is_demo": False, "active": True, "priority": 1, "licensed_states": ["FL"],
        "products": ["health_aca"], "tiers": ["A"], "price_by_tier": {{"A": 40}}, "daily_cap": 5,
        "age_min": 18, "age_max": 64, "delivery": {{"method": "outbox"}}}}
buyers = [
    {{**base, "id": "unverified", "name": "Unverified Agency LLC", "portal_token": "tok-unverified"}},
    {{**base, "id": "verified", "name": "Verified Agency LLC", "portal_token": "tok-verified",
      "_verification": {{"license": {{"by": "op", "ref": "FL DFS 2026-11-02"}},
                        "npn": {{"by": "op", "ref": "NPN lookup 2026-11-02"}}}}}},
    {{**base, "id": "demo-x", "name": "Internal Test Desk X", "is_demo": True, "portal_token": "tok-demo"}},
]
(cfg / "buyers.json").write_text(json.dumps(buyers), encoding="utf-8")
app.CFG = cfg
print('real', sorted(b['id'] for b in app._buyers(False)))
print('test', sorted(b['id'] for b in app._buyers(True)))
"""


def _eligibility(env_overrides: dict, tmp: Path) -> str:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("STRIPE_", "LEADENGINE_", "INTEL_", "ADMIN_"))}
    env.update(env_overrides)
    env["LEADENGINE_DB"] = str(tmp / "elig.db")
    env["PROBE_CFG"] = str(tmp / "cfg")
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([PY, "-c", _ELIGIBILITY_SNIPPET.format(root=str(ROOT))], capture_output=True, text=True,
                       cwd=str(ROOT), env=env, timeout=180)
    assert r.returncode == 0, (r.stdout + r.stderr)[-800:]
    return r.stdout


def test_real_leads_need_a_verified_buyer_in_production(tmp_path):
    """On a live deployment a REAL lead may only go to a buyer whose licence + NPN checks are recorded:
    an agency that was activated with a typed-in licence number (unverified) cannot buy consumer data,
    and a demo fixture never can. TEST traffic keeps both, so any buyer can still be rehearsed."""
    prod = _eligibility({"LEADENGINE_ENV": "production", "ADMIN_TOKEN": "x" * 40, "INTEL_DB": ""}, tmp_path)
    assert "real ['verified']" in prod
    assert "test ['demo-x', 'unverified', 'verified']" in prod

    staging = _eligibility({"ADMIN_TOKEN": "staging"}, tmp_path)
    assert "real ['demo-x', 'unverified', 'verified']" in staging   # staging keeps its labelled rehearsals


def test_launch_check_prints_categories(tmp_path):
    """The checklist distinguishes the four states: READY / REQUIRES CONFIGURATION /
    REQUIRES EXTERNAL VERIFICATION / REQUIRES MY APPROVAL. BASE_URL is forced to an
    unconfigured value so the domain group produces an item and the layout holds even
    if the operator env carries a real domain."""
    r = subprocess.run([PY, str(ROOT / "launch_check.py")], capture_output=True, text=True,
                       cwd=str(ROOT), timeout=120,                        env={**{k: v for k, v in os.environ.items()
                                if not k.startswith("STRIPE_") and k not in ("BASE_URL", "LEADENGINE_ENV", "ADMIN_TOKEN")},
                             "STRIPE_SECRET_KEY": "", "BASE_URL": "http://localhost:8000",
                             "LEADENGINE_ENV": "production", "ADMIN_TOKEN": "x" * 20,  # env-only; proves the READY branch
                             "GA4_ID": "qa-test-ga",  # satisfies the tracking group: first-party tracking needs no pixel
                             "PYTHONIOENCODING": "utf-8"})
    out = r.stdout
    assert r.returncode == 0
    for cat in ("READY", "REQUIRES CONFIGURATION", "REQUIRES EXTERNAL VERIFICATION", "REQUIRES MY APPROVAL"):
        assert cat in out, cat
    assert "mode=off" in out and "LEADENGINE_LIVE_DELIVERY is off" in out   # rail + delivery state reported
    # a satisfied group prints READY (hosting needs persistent DB we cannot fake on Windows;
    # tracking group clears once an analytics id is configured)
    assert "== 5 Tracking: READY" in out
