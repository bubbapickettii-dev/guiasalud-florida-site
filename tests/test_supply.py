"""Lead-supply registry tests (config/lead_supply.json + /admin/intel/supply).

The registry is the researched record of external supply paths. Two invariants:
  1. EVERY external source states a resale status, and every unrestricted one is explicitly
     NOT AUTHORIZED (buying a lead is not permission to redistribute it).
  2. The admin page renders the resale warning so no operator reads the registry as a
     shopping list of things to ingest without a signed agreement.
"""
import json, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ROOT = Path(__file__).resolve().parents[1]


def _supply():
    return json.loads((ROOT / "config" / "lead_supply.json").read_text(encoding="utf-8"))


def test_every_source_has_resale_and_verification():
    d = _supply()
    assert len(d["sources"]) >= 15
    for s in d["sources"]:
        assert "resale_status" in s and s["resale_status"].strip()
        assert "verified" in s and s["verified"].get("date") and s["verified"].get("level")
        # non-first-party, non-infra rows must not claim resale rights by default
        if s["id"] not in ("first_party",):
            if s["category"] not in ("consent_infra", "routing_software", "carrier_partners"):
                assert "NOT AUTHORIZED" in s["resale_status"].upper() or s["resale_status"].lower().startswith("n/a"), s["id"]
        assert "https://" in s["url"]


def test_first_party_is_the_only_fully_authorized_row():
    d = _supply()
    fully = [s["id"] for s in d["sources"] if "Full authority" in s.get("resale_status", "")]
    assert fully == ["first_party"]


def test_admin_supply_page_renders_warning():
    os.environ["LEADENGINE_DB"] = str(ROOT / "data" / "smoke_leads.db")
    os.environ["ADMIN_TOKEN"] = "t"
    os.environ["INTEL_DB"] = str(ROOT / "data" / "none_intel.db")
    import importlib
    import app as appmod
    importlib.reload(appmod)
    c = appmod.app.test_client()
    page = c.get("/admin/intel/supply?token=t").get_data(as_text=True)
    assert "buying a lead is NOT permission" in page
    assert "NOT AUTHORIZED" in page
    assert "IntegrityCONNECT" in page or "integrity.com" in page
    assert "TrustedForm" in page
