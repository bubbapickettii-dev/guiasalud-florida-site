"""Core launch-readiness guards (2026-11-02).

Four behaviours that were only half-built before this pass:

  1. CONSUMER OPT-OUT IS ENFORCED. A person on the opt-out list can never have a later form fill
     turned into a sold lead, and an opt-out form submitted with no identifier can never render the
     "we recorded it, you will not be contacted again" success message.
  2. PRODUCTION NEVER NAMES A BUYER WHO CANNOT PAY. The staging switch may be off (rehearsals), but a
     live deployment enforces prepaid credits on REAL traffic — otherwise the consumer consents to an
     agency that can never be billed for the lead.
  3. OPERATORS EDIT BUYERS THROUGH THE EXISTING FORM. The pre-filled form carries the SAVED tiers,
     prices, caps and contact details, so saving cannot silently reset them; token rotation is a
     dedicated action that leaves activation and balances untouched.
  4. ROBOTS/SITEMAP AGREE. Crawling disallowed means no sitemap advertisement and no canonical built
     from a missing origin.

Every test runs on a TEMP copy of config/ plus a temp DB: no real buyer, consumer, token or ledger
row is read or written.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_billing import make_app, post                # noqa: E402  (shared fixture builders)
from test_onboard import sandbox, post_buyer           # noqa: E402


def page(c, path):
    r = c.get(path)
    assert r.status_code == 200, (path, r.status_code)
    return r.get_data(as_text=True)


# ------------------------------------------------------------------ 1. consumer opt-out is enforced
def test_optout_with_no_identifier_is_never_reported_as_done(tmp_path):
    m = make_app(tmp_path)
    c = m.app.test_client()
    body = c.post("/optout", data={"email": "", "phone": ""}).get_data(as_text=True)
    assert "Nothing was recorded" in body
    assert "You won't be contacted by us or our agent partners again" not in body
    assert m.con.execute("SELECT COUNT(*) FROM optouts").fetchone()[0] == 0


def test_optout_accepts_a_country_code_number(tmp_path):
    m = make_app(tmp_path)
    c = m.app.test_client()
    c.post("/optout", data={"phone": "+1 (305) 555-0142"})
    row = m.con.execute("SELECT key, kind FROM optouts").fetchone()
    assert row["key"] == "3055550142" and row["kind"] == "phone"


def test_a_previous_optout_can_never_be_sold_a_new_lead(tmp_path):
    """The opt-out page promises 'we will stop all contact'. A later form fill must not undo it."""
    m = make_app(tmp_path)
    c = m.app.test_client()
    phone = "3055550199"
    c.post("/optout", data={"phone": phone})
    r = post(c, consent_checked=True, phone=phone).get_json()
    assert r["ok"] is True and r["opted_out"] is True
    assert r["routed"] == "NOT_ROUTABLE"
    lead = m.con.execute("SELECT buyer_id, routing_status, qual_status FROM leads").fetchone()
    assert lead["buyer_id"] is None and lead["routing_status"] == "NOT_ROUTABLE"
    assert m.con.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0
    assert m.con.execute("SELECT COUNT(*) FROM buyer_ledger").fetchone()[0] == 0
    # the refusal is recorded so an operator can see WHY this request was not passed on
    reason = m.con.execute("SELECT reason FROM routing_decisions ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert "consumer_opted_out" in reason


def test_a_fresh_consumer_is_unaffected_by_the_optout_list(tmp_path):
    m = make_app(tmp_path)
    c = m.app.test_client()
    c.post("/optout", data={"phone": "3055550142"})
    r = post(c, consent_checked=True, phone="3055550177").get_json()
    assert r["opted_out"] is False and r["routed"] == "ROUTED"


def test_empty_contact_message_is_refused_instead_of_acknowledged(tmp_path):
    m = make_app(tmp_path)
    c = m.app.test_client()
    body = c.post("/contact", data={"message": "   "}).get_data(as_text=True)
    assert "Please write a message before sending" in body
    assert "Thank you. We received your message" not in body
    assert not (tmp_path / "outbox" / "inquiries").exists()          # nothing notified, nothing stored


def test_a_consumer_inquiry_is_stored_notified_and_readable_by_the_operator(tmp_path):
    """A support channel nobody can read is a broken support channel: store + notify + list."""
    m = make_app(tmp_path)
    c = m.app.test_client()
    body = c.post("/contact", data={"message": "Please correct the 2027 income figure on the savings page.",
                                    "reply_to": "ana@example.com"}).get_data(as_text=True)
    assert "Thank you. We received your message" in body
    row = m.con.execute("SELECT message, reply_to FROM contact_msgs").fetchone()
    assert row["reply_to"] == "ana@example.com"
    assert list((tmp_path / "outbox" / "inquiries").glob("*.eml"))    # dry-run notification on disk
    html = page(c, "/admin/inquiries?token=t")
    assert "Please correct the 2027 income figure" in html and "ana@example.com" in html
    assert m.con.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0   # an inquiry is never a lead


# --------------------------------------------------- 2. production never names a buyer who cannot pay
def test_production_enforces_credits_even_with_the_config_switch_off(tmp_path):
    m = make_app(tmp_path)
    m.MARKETPLACE = {**m.MARKETPLACE, "enforce_credits": False}
    m.PRODUCTION = True
    one = [{"id": "agency-real"}]
    assert m._blocked_buyers(one, False) == frozenset({"agency-real"})    # unfunded: never named in consent
    m.billing.record_package(m.con, "agency-real", {"id": "pack", "amount_usd": 40}, is_test=0)
    assert m._blocked_buyers(one, False) == frozenset()                   # funded: routable again
    # the TEST lane keeps the staging behaviour, so demo rehearsals are never starved by credits
    assert m._blocked_buyers(one, True) == frozenset()


def test_staging_honours_the_switch_so_rehearsals_stay_creditless(tmp_path):
    m = make_app(tmp_path)
    m.MARKETPLACE = {**m.MARKETPLACE, "enforce_credits": False}
    m.PRODUCTION = False
    assert m._blocked_buyers([{"id": "agency-real"}], False) == frozenset()
    m.MARKETPLACE = {**m.MARKETPLACE, "enforce_credits": True}
    assert m._blocked_buyers([{"id": "agency-real"}], False) == frozenset({"agency-real"})


def test_the_admin_row_names_the_production_credit_blocker(tmp_path):
    """An operator must see WHY an active buyer is suddenly not receiving leads on a live deployment —
    from the same blocker list routing computes, not a parallel opinion."""
    m = make_app(tmp_path)
    sandbox(m, tmp_path)
    c = m.app.test_client()
    post_buyer(m, c, action="save")
    buyers = json.loads((m.CFG / "buyers.json").read_text(encoding="utf-8"))
    for b in buyers:
        if b["id"] == "smith-insurance":
            b["active"] = True
    (m.CFG / "buyers.json").write_text(json.dumps(buyers), encoding="utf-8")
    m.MARKETPLACE = {**m.MARKETPLACE, "enforce_credits": False}
    m.PRODUCTION = True
    bstat, _ = m._buyer_state_rows()
    row = next(x for x in bstat if x["id"] == "smith-insurance")
    assert any("no prepaid credits" in x for x in row["blockers"]), row["blockers"]
    assert "not receiving new leads" in page(c, "/admin?token=t")


# --------------------------------------------- 3. operators edit buyers through the existing form
def test_saved_buyer_values_are_prefilled_and_a_plain_save_keeps_them(tmp_path):
    m = make_app(tmp_path)
    sandbox(m, tmp_path)
    c = m.app.test_client()
    _r, _loc, row = post_buyer(m, c, tiers="A,C", price_A="31", price_C="9", daily_cap="7",
                               age_min="25", age_max="60", contact_name="Dana Ruiz",
                               contact_email="dana@example.com", contact_phone="305-555-0107")
    assert row["tiers"] == ["A", "C"] and row["price_by_tier"]["C"] == 9.0
    assert row["contact"]["email"] == "dana@example.com"

    html = page(c, "/admin?token=t&f=smith-insurance")
    assert 'name="tiers" value="A,C"' in html                     # the tiers reset bug: never "A,B"
    assert 'name="daily_cap"' in html and 'value="7"' in html
    assert "Dana Ruiz" in html and "dana@example.com" in html     # contact is visible + editable
    assert "Rotate portal link" in html

    # the standalone Activate/Deactivate buttons post only id+action: nothing may be reset
    c.post("/admin/buyers?token=t", data={"buyer_id": "smith-insurance", "action": "save"})
    row = next(x for x in json.loads((m.CFG / "buyers.json").read_text(encoding="utf-8"))
               if x["id"] == "smith-insurance")
    assert row["tiers"] == ["A", "C"]
    assert row["price_by_tier"]["A"] == 31.0 and row["price_by_tier"]["C"] == 9.0
    assert row["daily_cap"] == 7 and row["age_min"] == 25 and row["age_max"] == 60
    assert row["contact"]["name"] == "Dana Ruiz"


def test_operator_can_rotate_a_portal_link_without_changing_the_account(tmp_path):
    m = make_app(tmp_path)
    sandbox(m, tmp_path)
    c = m.app.test_client()
    _r, _loc, row = post_buyer(m, c, action="activate")
    old = row["portal_token"]
    assert row["active"] is True and c.get(f"/agent/{old}").status_code == 200

    c.post("/admin/buyers?token=t", data={"buyer_id": "smith-insurance", "action": "rotate_token"})
    row = next(x for x in json.loads((m.CFG / "buyers.json").read_text(encoding="utf-8"))
               if x["id"] == "smith-insurance")
    assert row["portal_token"] and row["portal_token"] != old     # a leaked link dies immediately
    assert row["active"] is True and row["price_by_tier"]["A"] == 35.0
    assert c.get(f"/agent/{old}").status_code == 404
    assert c.get(f"/agent/{row['portal_token']}").status_code == 200


def test_production_admin_explains_the_verification_gate(tmp_path):
    """An ACTIVE buyer without recorded licence/NPN checks receives no real leads on a live
    deployment. The row must say so, or it looks healthy while nothing arrives."""
    m = make_app(tmp_path)
    sandbox(m, tmp_path)
    c = m.app.test_client()
    post_buyer(m, c, action="save")
    buyers = json.loads((m.CFG / "buyers.json").read_text(encoding="utf-8"))
    for b in buyers:
        if b["id"] == "smith-insurance":
            b["active"] = True
    (m.CFG / "buyers.json").write_text(json.dumps(buyers), encoding="utf-8")
    m.PRODUCTION = True
    row = next(x for x in m._buyer_state_rows()[0] if x["id"] == "smith-insurance")
    assert any("licence/NPN checks not recorded" in x for x in row["blockers"]), row["blockers"]
    assert "licence/NPN checks not recorded" in page(c, "/admin?token=t")
    assert not m._independently_verified(next(b for b in buyers if b["id"] == "smith-insurance"))


# --------------------------------------------------------------- 4. robots / sitemap stay coherent
def test_robots_never_advertises_a_sitemap_while_crawling_is_disallowed(tmp_path):
    m = make_app(tmp_path)
    c = m.app.test_client()
    m.ALLOW_INDEXING = False
    body = page(c, "/robots.txt")
    assert "Disallow: /" in body and "Sitemap:" not in body
    m.ALLOW_INDEXING = True
    body = page(c, "/robots.txt")
    assert "Disallow: /admin" in body and "Sitemap: http://localhost:8000/sitemap.xml" in body


def test_no_shipped_asset_carries_a_staging_origin():
    """Ad kits and print/JSON deliverables are handed to platforms and partners as-is. A loopback URL
    inside one of them is a staging link published as the permanent destination, so the repo must
    never ship one (the kits use the {BASE_URL} placeholder the build script fills)."""
    root = Path(__file__).resolve().parents[1] / "assets"
    bad = []
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in (".csv", ".json", ".html", ".txt", ".md"):
            s = p.read_text(encoding="utf-8", errors="ignore")
            if "localhost" in s or "127.0.0.1" in s:
                bad.append(str(p.relative_to(root)))
    assert not bad, bad


def test_no_canonical_is_emitted_when_config_has_no_absolute_origin(tmp_path):
    m = make_app(tmp_path)
    c = m.app.test_client()
    m.SITE["base_url"] = ""
    assert 'rel="canonical"' not in page(c, "/")
    m.SITE["base_url"] = "https://gsallude.com"
    assert 'rel="canonical" href="https://gsallude.com/"' in page(c, "/")
