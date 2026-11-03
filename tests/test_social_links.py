"""Organic-social tracking links: the destination rule is a compliance rule, not cosmetics.

A bio link that points at /for-agents (the BUYER application), /agent/<token> (a buyer's private
portal), /admin, or a staging host is a real launch defect: wrong audience, or a private URL published
to the world. `tools/social_links.py` refuses all of those, and this test pins that it keeps refusing.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import social_links as sl  # noqa: E402

CFG = json.loads((ROOT / "config" / "social.json").read_text(encoding="utf-8"))


def _profile(platform):
    return next(p for p in CFG["profiles"] if p["platform"] == platform)


@pytest.mark.parametrize("platform", ["instagram", "tiktok", "facebook"])
def test_each_platform_link_carries_its_own_launch_tagging(platform):
    row = sl.build(_profile(platform), "https://guiasalud.com", "/")
    assert row["status"] == "READY" and row["url"].startswith("https://guiasalud.com/?")
    q = dict(p.split("=") for p in row["url"].split("?", 1)[1].split("&"))
    assert q == {"utm_source": platform, "utm_medium": "organic_social", "utm_campaign": "gs_allude_launch"}


@pytest.mark.parametrize("bad", [
    "http://localhost:5055/",                      # staging host, plain http
    "https://127.0.0.1/",                          # loopback
    "https://guiasalud.ngrok-free.app/",           # tunnel
    "https://staging.guiasalud.com/",              # staging subdomain
    "https://guiasalud.com/for-agents",            # BUYER application
    "https://guiasalud.com/para-agencias",         # buyer application, ES
    "https://guiasalud.com/agent/demo-health-token",  # buyer portal
    "https://guiasalud.com/admin",                 # operator console
    "https://guiasalud.com/p/partner",             # partner entry
    "https://guiasalud.com/check?a=health_check",  # a query on an allowed path is fine...
])
def test_never_publishes_a_non_consumer_or_staging_destination(bad):
    if bad.endswith("health_check"):               # ...so this one is the allowed control case
        assert sl.validate(bad).startswith("https://guiasalud.com/check")
        return
    with pytest.raises(sl.BadDestination):
        sl.validate(bad)


def _cfg_without_domain(tmp_path):
    cfg = dict(CFG, domain="")
    p = tmp_path / "social.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return p


def test_without_a_permanent_domain_nothing_is_publishable(tmp_path):
    """No domain => the links artifact records the exact query string as a TEMPLATE and marks every link
    BLOCKED, so nothing temporary can be pasted into a bio. (Runs against a temp config copy, not the
    repo's, so configuring the real domain later does not break the suite.)"""
    out = tmp_path / "kit"
    rc = sl.main(["--cfg", str(_cfg_without_domain(tmp_path)), "--out", str(out), "--write"])
    assert rc == 0
    data = json.loads((out / "links.json").read_text(encoding="utf-8"))
    assert data["status"] == "BLOCKED" and data["domain"] == ""
    for row in data["links"]:
        assert row["status"] == "BLOCKED" and row["url"] == ""
        assert "utm_campaign=gs_allude_launch" in row["template"]
        assert row["template"].startswith("https://<PERMANENT-DOMAIN>/")
    assert "BLOCKED" in (out / "links.md").read_text(encoding="utf-8")


def test_a_configured_domain_makes_all_three_links_ready(tmp_path):
    out = tmp_path / "kit"
    rc = sl.main(["--cfg", str(_cfg_without_domain(tmp_path)), "--out", str(out), "--write",
                  "--domain", "https://guiasalud.com"])
    assert rc == 0
    data = json.loads((out / "links.json").read_text(encoding="utf-8"))
    assert data["status"] == "READY" and data["domain"] == "https://guiasalud.com"
    assert {r["utm"]["utm_source"] for r in data["links"]} == {"instagram", "tiktok", "facebook"}
    for row in data["links"]:
        assert row["url"].startswith("https://guiasalud.com/?utm_source=")


def test_domain_with_a_path_is_reduced_to_the_origin(tmp_path):
    rc = sl.main(["--cfg", str(_cfg_without_domain(tmp_path)), "--out", str(tmp_path / "kit"),
                  "--domain", "https://guiasalud.com/check?a=health_check"])
    assert rc == 0
