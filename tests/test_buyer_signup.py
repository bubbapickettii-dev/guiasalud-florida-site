"""Public buyer-application flow (the sell-side funnel): /for-agents.

  - an agency applies via the public page -> ONE inactive draft buyer in config/buyers.json
    (never clobbers a demo/active buyer), with the application contact stored for review
  - validation refuses empty/placeholder submissions; honeypot is ignored
  - a repeat application corrects the ONE draft instead of piling up duplicates
  - the operator gets a DRY-RUN notification email with the applicant as Reply-To
  - Admin > Buyers surfaces APPLIED applications and prefills the onboarding form
Bookkeeping only: no activation, no routing changes, no consumer contact.
"""
import json, shutil, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_billing import make_app

FORM = {"agency": "Gulf Coast Family Insurance LLC", "contact_name": "Dana Reyes", "contact_email": "dana@gcfi.example.com",
        "contact_phone": "3055550199", "license_number": "A99887", "npn": "4443332", "licensed_states": "FL",
        "products": "health_aca", "expected_volume": "5-10", "note": "Solo agent, FFM registered"}


def sandbox(m, tmp):
    cfg = tmp / "cfg"
    shutil.copytree(m.CFG, cfg)
    m.CFG = cfg          # the application writes buyers.json: tests stay on a temp copy
    return cfg


def buyers_file(cfg):
    return json.loads((cfg / "buyers.json").read_text(encoding="utf-8"))


def apply(c, **over):
    d = dict(FORM); d.update(over)
    return c.post("/for-agents/apply", data=d).get_data(as_text=True)


def test_application_creates_inactive_draft_with_review_data(tmp_path):
    m = make_app(tmp_path); cfg = sandbox(m, tmp_path); c = m.app.test_client()
    page = apply(c)
    assert "Application received" in page
    b = next(x for x in buyers_file(cfg) if x["name"] == "Gulf Coast Family Insurance LLC")
    assert b["active"] is False and b["is_demo"] is False and b["id"].startswith("gulf-coast")
    assert not b.get("portal_token")                       # token is issued at onboarding, never by the public form
    assert b["price_by_tier"] == {"A": 0, "B": 0, "C": 0}   # prices are set at onboarding, never by the browser
    assert b["_application"]["contact_email"] == "dana@gcfi.example.com"
    assert b["delivery"]["email"] == "dana@gcfi.example.com" and b["delivery"]["method"] == "outbox"
    # the routing machinery never sees an inactive draft: the preview exposes no buyer at all and the
    # platform's assignment excludes anything inactive
    r = c.get("/api/route-preview?test=1&campaign=oep-2027-health&state=FL&age_range=35-44").get_json()
    assert r.get("available") is True and "buyer_id" not in r
    # operator notification: one DRY-RUN .eml per apply, applicant as Reply-To, no tokens in it
    eml = (tmp_path / "outbox" / "applications" / f"{b['id']}.eml").read_text()
    assert "[Buyer application]" in eml and "dana@gcfi.example.com" in eml and "Reply-To: dana" in eml
    assert "portal_token" not in eml and "t@example" not in eml and "admin" not in eml.split("Note")[0]


def test_honeypot_and_validation_refuse_garbage(tmp_path):
    m = make_app(tmp_path); cfg = sandbox(m, tmp_path); c = m.app.test_client()
    before = len(buyers_file(cfg))
    resp = c.post("/for-agents/apply", data=dict(FORM, website="http://spam.example.com"))
    assert len(buyers_file(cfg)) == before                 # bots fill the honeypot: nothing saved
    assert resp.get_json() == {"ok": True}
    page = apply(c, agency="", note="")
    assert "Application not saved" in page and "agency name is required" in page
    assert len(buyers_file(cfg)) == before
    page = apply(c, licensed_states="ZZ,XW", note="")
    assert "served here" in page and len(buyers_file(cfg)) == before     # unserved states can't be accepted
    # a consent-usable name: the operator must see the real legal name, placeholders are refused
    page = apply(c, agency="[Demo Agency]", note="")
    assert "no placeholder brackets" in page and len(buyers_file(cfg)) == before


def test_repeat_application_updates_one_draft(tmp_path):
    m = make_app(tmp_path); cfg = sandbox(m, tmp_path); c = m.app.test_client()
    apply(c, note="first note")
    page = apply(c, contact_email="new@gcfi.example.com", expected_volume="10-20", note="updated NPN")
    assert "Application updated on file" in page
    drafts = [x for x in buyers_file(cfg) if x.get("_application")]
    assert len(drafts) == 1
    b = drafts[0]
    assert b["_application"]["note"].endswith("updated NPN") and "update:" in b["_application"]["note"]
    assert b["delivery"]["email"] == "new@gcfi.example.com"
    emails = list((tmp_path / "outbox" / "applications").glob("*.eml"))
    assert len(emails) == 1 and f"{b['id']}.eml" == emails[0].name       # one file per draft: the latest state wins
    assert "updated NPN" in emails[0].read_text()
    # collision protection: a second, differently-cased agency whose slug matches a demo buyer keeps its own id
    demo = next(x["id"] for x in buyers_file(cfg) if x.get("is_demo"))
    slug_part = demo.replace("-", " ")
    page = apply(c, agency=slug_part.upper(), contact_email="other@example.com", note="")
    assert "Application received" in page
    ids = [x["id"] for x in buyers_file(cfg)]
    assert sum(1 for i in ids if i.startswith(demo)) == 2 and demo in ids    # original demo row untouched


def test_admin_surfaces_applied_and_prefills_onboarding(tmp_path):
    m = make_app(tmp_path); sandbox(m, tmp_path); c = m.app.test_client()
    apply(c)
    bid = next(x["id"] for x in buyers_file(tmp_path / "cfg") if x.get("_application"))
    page = c.get(f"/admin?token=t&f={bid}#onboard").get_data(as_text=True)
    assert "APPLIED" in page and "dana@gcfi.example.com" in page and "Review application" in page
    assert f'value="{bid}"' in page and 'value="Gulf Coast Family Insurance LLC"' in page      # form prefilled for review
    assert 'value="A99887"' in page and 'value="4443332"' in page
    # without the ?f= link the form stays blank (no accidental overwrite of another buyer)
    page = c.get("/admin?token=t").get_data(as_text=True)
    assert 'value="Gulf Coast' not in page and "APPLIED" in page
    # the operator can activate the draft through the normal onboarding path once fields are completed
    r = c.post("/admin/buyers?token=t", data={"buyer_id": bid, "action": "activate", "name": "Gulf Coast Family Insurance LLC",
                                              "license_number": "A99887", "npn": "4443332", "licensed_states": "FL",
                                              "products": "health_aca", "tiers": "A,B", "price_A": "55", "price_B": "33",
                                              "daily_cap": "5", "age_min": "30", "age_max": "64"})
    assert r.status_code == 302
    b = next(x for x in buyers_file(tmp_path / "cfg") if x["id"] == bid)
    assert b["active"] is True and b["_application"]["contact_email"] == "dana@gcfi.example.com"   # provenance kept


def test_application_records_delivery_preference_with_webhook_validation(tmp_path):
    m = make_app(tmp_path); cfg = sandbox(m, tmp_path); c = m.app.test_client()
    apply(c, delivery_method="webhook", delivery_webhook="https://crm.gcfi.example.com/hooks/new-lead")
    b = next(x for x in buyers_file(cfg) if x.get("_application"))
    assert b["delivery"]["method"] == "webhook" and b["delivery"]["webhook_url"].startswith("https://crm.")
    assert b["_application"]["delivery_preference"] == "webhook" and b["_application"]["delivery_webhook"].startswith("https://")
    # an invalid / non-https webhook URL is refused instead of being silently stored
    before = [x["_application"]["delivery_webhook"] for x in buyers_file(cfg) if x.get("_application")][0]
    page = apply(c, delivery_method="webhook", delivery_webhook="not a url")
    assert "webhook delivery needs an https:// URL" in page
    assert [x["_application"]["delivery_webhook"] for x in buyers_file(cfg) if x.get("_application")][0] == before
    page = apply(c, delivery_method="webhook", delivery_webhook="http://crm.gcfi.example.com/hooks/new-lead")
    assert "webhook delivery needs an https:// URL" in page          # plain http is refused: consumer contact data
    assert [x["_application"]["delivery_webhook"] for x in buyers_file(cfg) if x.get("_application")][0] == before


def test_admin_records_and_withdraws_verification(tmp_path):
    """Supplied ≠ verified: the operator records an EXTERNAL check with a named reference; forgetting
    withdraws it. Verification never activates the buyer or changes routing by itself."""
    m = make_app(tmp_path); cfg = sandbox(m, tmp_path); c = m.app.test_client()
    apply(c)
    bid = next(x["id"] for x in buyers_file(cfg) if x.get("_application"))
    page = c.get(f"/admin?token=t&f={bid}#onboard").get_data(as_text=True)
    assert "pending&nbssp;FL DFS" in page or "pending" in page     # starts as NOT verified
    r = c.post(f"/admin/buyer/{bid}/verify?token=t", data={"what": "license", "ref": "FL DFS licensee search 2026-10-22: active, 2-30 license"})
    assert r.status_code == 302
    b = next(x for x in buyers_file(cfg) if x["id"] == bid)
    assert b["_verification"]["license"]["ref"].startswith("FL DFS")
    page = c.get(f"/admin?token=t&f={bid}#onboard").get_data(as_text=True)
    assert '✓ FL DFS' in page and b["active"] is False      # verified but still a draft: verification ≠ activation
    # bad verification type is refused (never invented)
    assert c.post(f"/admin/buyer/{bid}/verify?token=t", data={"what": "hair_color", "ref": "x"}).status_code == 400
    # withdraw explicitly
    c.post(f"/admin/buyer/{bid}/verify?token=t", data={"what": "license", "action": "forget"})
    b = next(x for x in buyers_file(cfg) if x["id"] == bid)
    assert not b.get("_verification")
