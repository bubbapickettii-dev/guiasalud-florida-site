"""Production-mode integrity of the TEST/REAL boundary: client hints are never trusted there.

  - a submission whose payload claims is_test=1 is still sold as a REAL lead to a real (non-demo)
    buyer, entered in the REAL ledger; only the QA device cookie (staging machinery) could ever
    mark a lead TEST, and even that is ignored in production
  - the consent preview NEVER names a demo buyer from a forgeable ?test=1 flag in production
Bookkeeping only - no live payments, no live delivery.
"""
import importlib, json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_billing import BASE, post, make_app


def onboard_real_buyer(m, tmp_path):
    import shutil
    cfg = tmp_path / "cfg"                 # sandbox: onboarding writes buyers.json — never the real config
    if not (cfg / "buyers.json").exists():
        shutil.copytree(m.CFG, cfg)
        m.CFG = cfg
    c = m.app.test_client()
    tok_admin = os.environ.get("ADMIN_TOKEN", "t")
    c.post(f"/admin/buyers?token={tok_admin}", data={"buyer_id": "realbuy", "action": "activate", "name": "Real Health Agency LLC",
                                          "license_number": "A555", "npn": "77777", "licensed_states": "FL",
                                          "products": "mortgage_protection", "tiers": "A,B", "price_A": "60", "price_B": "35",
                                          "daily_cap": "5", "age_min": "30", "age_max": "64"})
    # a LIVE buyer must also carry the operator's recorded external checks: production routing refuses to
    # sell consumer data to an agency whose licence/NPN were only typed in, never looked up.
    for what, ref in (("license", "FL DFS lookup 2026-11-02"), ("npn", "NPN registry 2026-11-02")):
        c.post(f"/admin/buyer/realbuy/verify?token={tok_admin}", data={"what": what, "ref": ref})
    # a LIVE buyer must also HOLD PREPAID CREDITS: production always enforces credit-before-routing, so
    # an unfunded account is never named in a consumer's consent (tests/test_launch_core.py pins that).
    c.post(f"/admin/buyer/realbuy/topup?token={tok_admin}",
           data={"package": "weekly-1000", "note": "fixture: funds received"})
    buyers = json.loads((m.CFG / "buyers.json").read_text(encoding="utf-8"))
    real = next(x for x in buyers if x["id"] == "realbuy")
    assert real["_verification"].get("license") and real["_verification"].get("npn")
    return c, real["portal_token"]


class in_production:
    """Load the app with LEADENGINE_ENV=production (strong ADMIN_TOKEN required at startup) and restore."""

    def __init__(self, tmp):
        self.tmp = tmp

    def __enter__(self):
        os.environ["LEADENGINE_ENV"] = "production"
        os.environ["LEADENGINE_DB"] = str(self.tmp / "t.db")
        os.environ.pop("INTEL_DB", None)             # production rule: the prospect DB stays off the server (guard enforces)
        os.environ["ADMIN_TOKEN"] = "x" * 24        # production boot refuses weak/placeholder tokens
        import app as appmod
        importlib.reload(appmod)
        appmod.OUTBOX = self.tmp / "outbox"; appmod.SNAP = self.tmp / "snap"
        return appmod

    def __exit__(self, *a):
        os.environ.pop("LEADENGINE_ENV", None)
        os.environ.pop("INTEL_DB", None)             # never leak a production setting into the next test
        os.environ["ADMIN_TOKEN"] = "t"             # back to the staging token the harness uses


def test_production_ignores_client_test_claims(tmp_path):
    with in_production(tmp_path) as m:
        c, tok = onboard_real_buyer(m, tmp_path)
        # a real-shaped phone: production (rightly) rejects the fictional 555-01XX range the fixture uses
        r = post(c, consent_checked=True, is_test=1, phone="3056724417").get_json()   # forger claims TEST
        assert r["routed"] == "ROUTED"
        lead = m.con.execute("SELECT * FROM leads").fetchone()
        assert lead["is_test"] == 0                 # the claim is ignored server-side
        assert lead["buyer_id"] == "realbuy"        # sold to a real buyer, never a demo placeholder
        assert not next((b["id"] for b in json.loads((m.CFG / "buyers.json").read_text(encoding="utf-8"))
                         if b["id"] == lead["buyer_id"] and b.get("is_demo")), None)
        row = m.con.execute("SELECT is_test FROM buyer_ledger WHERE lead_id=? AND kind='LEAD_CHARGE'", (lead["lead_id"],)).fetchone()
        assert row and row["is_test"] == 0          # REAL ledger, not the TEST one


def test_production_preview_never_names_demo_buyer(tmp_path):
    """In production the preview exposes NO buyer at all (nothing to steer), and a payload that tries to
    name a demo buyer is ignored: the platform assigns the real on-boarded buyer."""
    with in_production(tmp_path) as m:
        c, tok = onboard_real_buyer(m, tmp_path)
        pv = c.get("/api/route-preview?campaign=mortgage-protection&state=FL&age_range=40-49&test=1").get_json()
        assert pv.get("available") is True
        assert "buyer_id" not in pv and "seller" not in pv
        r = post(c, consent_checked=True, email="qa@example.com", phone="3056724417",
                 buyer_id="demo-health-agent").get_json()                               # forged selection: ignored
        assert r["routed"] == "ROUTED"
        lead = m.con.execute("SELECT buyer_id FROM leads").fetchone()
        assert lead["buyer_id"] == "realbuy"
        cr = m.con.execute("SELECT seller_named, consent_text FROM consents").fetchone()
        assert "licensed agent partner" in cr["seller_named"] and "Real Health Agency" not in cr["consent_text"]


def test_staging_keeps_qa_cookie_and_payload_flag(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()          # LEADENGINE_ENV unset: staging behavior intact
    r = post(c, consent_checked=True, is_test=1).get_json()  # payload flag honored in staging QA
    assert r["routed"] == "ROUTED"
    lead = m.con.execute("SELECT is_test, buyer_id FROM leads").fetchone()
    assert lead["is_test"] == 1 and lead["buyer_id"]        # demo buyers serve TEST traffic in staging
    assert not m.con.execute("SELECT 1 FROM buyer_ledger WHERE is_test=0 AND kind='LEAD_CHARGE'").fetchone()
