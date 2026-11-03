"""Buyer onboarding: admins add/update real buyers through /admin/buyers with validated, safe config.

  - a draft can be saved incomplete (stays inactive), but ACTIVATION is refused while required
    compliance fields (legal name, license, NPN, prices, portal token, served states) are missing
  - a demo placeholder buyer is read-only here
  - an onboarded buyer's portal token is stable across updates and unique
  - activation makes the buyer live in the routing path immediately (real file re-read)
Bookkeeping only — no real money, no live delivery, TEST-flagged leads.
"""
import json, shutil, sys, urllib.parse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_billing import make_app, BASE, post


def sandbox(m, tmp):
    """Sandbox: onboarding writes config/buyers.json - run every test against a TEMP COPY of the
    config dir (tests must never touch the real buyers.json, which the app reads live)."""
    cfg_copy = tmp / "cfg"
    shutil.copytree(m.CFG, cfg_copy)
    m.CFG = cfg_copy           # app code reads the module attribute at call time
    return cfg_copy


def form(**kw):
    d = {"buyer_id": "smith-insurance", "action": "save", "name": "Smith Insurance Group LLC",
         "license_number": "A123456", "npn": "98765432", "licensed_states": "FL",
         "products": "health_aca", "tiers": "A,B", "price_A": "35", "price_B": "22",
         "daily_cap": "10", "age_min": "19", "age_max": "64", "delivery_method": "outbox"}
    d.update(kw)
    return d


def post_buyer(m, client, **kw):
    d = form(**kw)
    r = client.post("/admin/buyers?token=t", data=d)
    loc = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["Location"]).query)
    file = json.loads((m.CFG / "buyers.json").read_text(encoding="utf-8"))
    return r, loc, next((x for x in file if x.get("id") == d["buyer_id"]), None)


def test_draft_save_inactive_then_activation_needs_complete_fields(tmp_path):
    m = make_app(tmp_path)
    sandbox(m, tmp_path)
    c = m.app.test_client()
    # incomplete draft: no license/npn, zero prices -> still saved as inactive
    r, loc, row = post_buyer(m, c, license_number="", npn="", action="save")
    assert r.status_code == 302 and loc.get("saved")
    assert row and row["active"] is False and row["is_demo"] is False and row["exclusive"] is True
    assert row["price_by_tier"]["A"] == 35.0 and row["portal_token"]       # token issued once, at draft
    import engine.route as ro
    assert not any(b["id"] == "smith-insurance" for b in ro.load_buyers(m.CFG))     # inactive = not sellable
    # activation with placeholders EMPTY (license) refused and buyer stays inactive
    r, loc, row = post_buyer(m, c, action="activate", license_number="", npn="")
    assert "err" in loc and "license" in loc["err"][0]
    assert row["active"] is False
    # activation with a placeholder-looking legal name refused
    r, loc, row = post_buyer(m, c, action="activate", name="[AB]")
    assert "err" in loc
    # a complete valid buyer activates
    r, loc, row = post_buyer(m, c, action="activate", name="Smith Insurance Group LLC")
    assert loc.get("saved") and not loc.get("err")
    import engine.route as ro
    ids = [b["id"] for b in ro.load_buyers(m.CFG)]
    assert "smith-insurance" in ids
    ob = next(b for b in ro.load_buyers(m.CFG) if b["id"] == "smith-insurance")
    assert ob["exclusive"] is True and ob["price_by_tier"]["A"] == 35.0


def test_portal_token_stable_after_updates_and_demo_buyers_read_only(tmp_path):
    m = make_app(tmp_path); sandbox(m, tmp_path); c = m.app.test_client()
    _, _, first = post_buyer(m, c, action="save")
    tok = first["portal_token"]
    _, _, second = post_buyer(m, c, action="save", price_A="40")   # edit later
    assert second["portal_token"] == tok and second["price_by_tier"]["A"] == 40.0
    # demo buyers cannot be touched from this path
    r, loc, demo = post_buyer(m, c, buyer_id="demo-agency-a", action="activate", name="Fake Demo")
    assert "err" in loc and "demo" in loc["err"][0]
    orig = json.loads((m.CFG / "buyers.json").read_text(encoding="utf-8"))
    assert next(x for x in orig if x["id"] == "demo-agency-a")["is_demo"] is True


def test_activated_buyer_sells_test_leads_and_refuses_unserved_state(tmp_path):
    m = make_app(tmp_path); sandbox(m, tmp_path); c = m.app.test_client()
    # onboard with the mortgage_protection product so the shared BASE fixture campaign routes to them
    r, loc, row = post_buyer(m, c, action="activate", products="mortgage_protection", price_A="60", price_B="35")
    assert loc.get("saved")
    import engine.route as ro
    sm = next((b for b in ro.load_buyers(m.CFG) if b["id"] == "smith-insurance"), None)
    assert sm and sm["exclusive"] is True
    # a buyer with licensed state not served by the site cannot be activated
    from app import _onboard_errors
    buyers = json.loads((m.CFG / "buyers.json").read_text(encoding="utf-8"))
    bad = next(x for x in buyers if x["id"] == "smith-insurance")
    bad["licensed_states"] = ["TX"]
    errs = _onboard_errors(bad)
    assert any("not served" in e for e in errs)   # activation refuses buyers licensed only for states we don't serve
    # a TEST lead routes through the funnel and is sold at the winning buyer's validated price, EXCLUSIVE
    from test_payments import tok
    from test_billing import post as lead_post
    lr = lead_post(c, consent_checked=True).get_json()
    assert lr["routed"] == "ROUTED"
    lead = dict(m.con.execute("SELECT * FROM leads WHERE routing_status='DELIVERED'").fetchone())
    winner = next(b for b in ro.load_buyers(m.CFG) if b["id"] == lead["buyer_id"])
    assert winner["id"] in ("smith-insurance", "demo-agency-a")
    assert lead["sale_type"] == "EXCLUSIVE" and lead["price"] == winner["price_by_tier"][lead["tier"]]


def test_admin_onboarding_webhook_https_enforced_for_new_and_existing_buyers(tmp_path):
    """Second webhook entry point (https-only rule, admin save): a plain-http CRM URL is refused both
    for a NEW buyer row (nothing is created from a refused save) and on an UPDATE to an existing buyer
    (the stored config stays as it was). An https:// URL onboard normally."""
    m = make_app(tmp_path); sandbox(m, tmp_path); c = m.app.test_client()
    # NEW buyer with an http:// webhook: save refused entirely, no row created
    r, loc, row = post_buyer(m, c, delivery_method="webhook", delivery_webhook="http://hooks.example.test/lead")
    assert r.status_code == 302 and "err" in loc and "https" in loc["err"][0]
    assert row is None
    # existing draft with an outbox default: an http:// webhook update is refused, the buyer row
    # keeps its previous delivery config untouched
    r, loc, row = post_buyer(m, c)          # good draft first
    assert loc.get("saved") and row["delivery"]["method"] == "outbox"
    r, loc, row = post_buyer(m, c, delivery_method="webhook", delivery_webhook="http://hooks.example.test/lead")
    assert "err" in loc and "https" in loc["err"][0]
    assert row["delivery"]["method"] == "outbox" and "webhook_url" not in row["delivery"]
    # an https:// webhook onboard normally and is stored verbatim
    r, loc, row = post_buyer(m, c, delivery_method="webhook", delivery_webhook="https://hooks.example.test/lead")
    assert loc.get("saved") and row["delivery"]["webhook_url"] == "https://hooks.example.test/lead"


def test_standalone_activate_preserves_saved_config(tmp_path):
    """The Buyers table's plain Activate/Deactivate rows post ONLY buyer_id+action. Re-deriving
    products/states/tiers from the new-draft defaults used to WIPE the saved configuration
    (mortgage_protection -> health_aca, licensed states -> FL) and then routing silently excluded
    the buyer ("does not buy mortgage_protection") — a config-loss + compliance defect."""
    import json as _j
    m = make_app(tmp_path); sandbox(m, tmp_path); c = m.app.test_client()
    c.post("/admin/buyers?token=t", data={"buyer_id": "multi-prod", "action": "save",
        "name": "Coast Insurance LLC", "license_number": "B777", "npn": "555",
        "licensed_states": "FL", "products": "mortgage_protection,term_life",
        "tiers": "B", "price_B": "40", "daily_cap": "3", "age_min": "30", "age_max": "64"})
    saved = next(x for x in _j.loads((m.CFG / "buyers.json").read_text(encoding="utf-8"))
                 if x["id"] == "multi-prod")
    assert saved["products"] == ["mortgage_protection", "term_life"]
    assert saved["licensed_states"] == ["FL"]
    c.post("/admin/buyers?token=t", data={"buyer_id": "multi-prod", "action": "activate"})
    after = next(x for x in _j.loads((m.CFG / "buyers.json").read_text(encoding="utf-8"))
                 if x["id"] == "multi-prod")
    assert after["products"] == ["mortgage_protection", "term_life"], after["products"]
    assert after["licensed_states"] == ["FL"], after["licensed_states"]
    assert after["tiers"] == ["B"]
    assert after["price_by_tier"]["B"] == 40.0
    assert after["active"] is True
    # an explicit form still updates the values (the normal admin flow keeps working)
    c.post("/admin/buyers?token=t", data={"buyer_id": "multi-prod", "action": "save",
                                          "products": "health_aca", "licensed_states": "FL",
                                          "tiers": "B, price_B" and "B"}
           | {"price_B": "40", "daily_cap": "3"})
    after2 = next(x for x in _j.loads((m.CFG / "buyers.json").read_text(encoding="utf-8"))
                  if x["id"] == "multi-prod")
    assert after2["products"] == ["health_aca"]
    assert after2["licensed_states"] == ["FL"]
