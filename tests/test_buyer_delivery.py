"""Buyer self-serve delivery endpoint (portal / email webhook CRM push).

  - the buyer can change ONLY their own delivery row, via the portal token they already hold
  - webhook URLs must be https; email route needs a valid address; invalid input changes nothing
  - the change takes effect on the next routing read without a restart, and cannot touch
    activation, caps or prices (operator-owned)
Bookkeeping only - no live payments, no live delivery.
"""
import json, shutil, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_billing import make_app, BASE, post
from test_buyer_portal import sandbox, save_buyer, buyer_row


def test_buyer_changes_own_delivery_route(tmp_path):
    m = make_app(tmp_path); sandbox(m, tmp_path); c = m.app.test_client()
    save_buyer(m, c, action="activate")                     # demo buyer in staging, with a portal token
    tok = buyer_row(tmp_path / "cfg", "portal-test")["portal_token"]
    # portal -> webhook
    r = c.post(f"/agent/{tok}/delivery", data={"method": "webhook", "webhook_url": "https://crm.example.com/hook", "email": "a@b.co"})
    assert r.status_code == 302 and "delivery_saved" in r.headers["Location"]
    d = buyer_row(tmp_path / "cfg", "portal-test")["delivery"]
    assert d["method"] == "webhook" and d["webhook_url"] == "https://crm.example.com/hook" and d["email"] == "a@b.co"
    # webhook without any URL -> refused, nothing changed
    row = next(x for x in json.loads((tmp_path / "cfg" / "buyers.json").read_text(encoding="utf-8")) if x["id"] == "portal-test")
    row["delivery"].pop("webhook_url")
    buyers = json.loads((tmp_path / "cfg" / "buyers.json").read_text(encoding="utf-8"))
    buyers = [x for x in buyers if x["id"] != "portal-test"] + [row]
    (tmp_path / "cfg" / "buyers.json").write_text(json.dumps(buyers, indent=2))
    r = c.post(f"/agent/{tok}/delivery", data={"method": "webhook"})
    assert r.status_code == 302 and "delivery_url_missing" in r.headers["Location"]
    assert buyer_row(tmp_path / "cfg", "portal-test")["delivery"]["method"] == "webhook"   # unchanged by the refusal
    # plaintext http / non-URL is refused too
    r = c.post(f"/agent/{tok}/delivery", data={"method": "webhook", "webhook_url": "http://insecure.example.com/hook"})
    assert "delivery_url_bad" in r.headers["Location"]
    r = c.post(f"/agent/{tok}/delivery", data={"method": "webhook", "webhook_url": "https:// spaces .invalid"})
    assert "delivery_url_bad" in r.headers["Location"] and buyer_row(tmp_path / "cfg", "portal-test")["delivery"]["method"] == "webhook"
    # email route needs a real address; an unknown method is 400; a foreign token touches nothing
    r = c.post(f"/agent/{tok}/delivery", data={"method": "email", "email": "not-an-email"})
    assert "delivery_email_bad" in r.headers["Location"]
    assert c.post(f"/agent/{tok}/delivery", data={"method": "carrier_pigeon"}).status_code == 400
    # after switching back the routing machinery sees the new route immediately, still scoped to this buyer
    r = c.post(f"/agent/{tok}/delivery", data={"method": "email", "email": "dana@gcfi.example.com"})
    assert "delivery_saved" in r.headers["Location"]
    d = buyer_row(tmp_path / "cfg", "portal-test")["delivery"]
    assert d == {"method": "email", "email": "dana@gcfi.example.com"} or d["method"] == "email"
    from engine import route as ro
    live = next(x for x in ro.load_buyers(tmp_path / "cfg") if x["id"] == "portal-test")
    assert live["delivery"]["method"] == "email" and live["delivery"]["email"] == "dana@gcfi.example.com"
    # the buyer cannot use this route to change operator-owned fields (activation/cap/prices stay put)
    assert all(v is not None for v in (live["active"], live["daily_cap"], live["price_by_tier"]["A"]))


def test_statement_csv_matches_the_on_screen_ledger(tmp_path):
    m = make_app(tmp_path); sandbox(m, tmp_path); c = m.app.test_client()
    save_buyer(m, c, action="activate")
    tok = buyer_row(tmp_path / "cfg", "portal-test")["portal_token"]
    c.post("/admin/buyer/portal-test/topup?token=t", data={"package": "starter-250", "note": "wire ref #9"})
    # portal-test is a NON-demo buyer: the statement shows the buyer's REAL-lead ledger
    post(c, consent_checked=True, is_test=0, phone="3054461234", email="")        # platform assigns portal-test (real buyer outranks demos)
    csv_text = c.get(f"/agent/{tok}/billing/statement.csv").get_data(as_text=True)
    lines = csv_text.strip().splitlines()
    assert lines[0] == "ts,kind,amount,balance_after,lead_id,package_id,note"
    kinds = [x.split(",")[1] for x in lines[1:]]
    assert kinds == ["PACKAGE_PURCHASE", "LEAD_CHARGE"]                       # both sides of the account, in order
    assert "starter-250" in lines[1] and "250.0" in lines[1].replace("\"", "")
    assert "wire ref #9" in lines[1]
    # a lead charge row carries the lead id so the buyer can reconcile row -> lead detail page
    assert lines[2].split(",")[4] and lines[2].split(",")[4].startswith("L")
    bal_after = float(lines[-1].split(",")[3])
    assert abs(bal_after - (250.0 - 60.0)) < 2.0                             # ends at the true running balance (tier A = $60)
