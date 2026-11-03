"""IDENTITY RESOLUTION — the rules that decide whether a discovered consumer has a real name and a
real number behind them.

These tests pin the BEHAVIOUR the pipeline is judged on, not the implementation:

  * a name exists only where the person identified THEMSELVES ("my name is", "me llamo", "call me")
    - "I'm tired", a handle, a city and an org word are all refused
  * a single unconfirmed mention is PROBABLE, never VERIFIED; two independent sources that agree
    verify the name, two that DISAGREE produce CONFLICT and no chosen name at all
  * a phone is stored only when the same identity published it with ownership wording; toll-free
    numbers, business/agency lines and quoted third-party numbers are refused WITH the reason
  * a number whose owner is not established stays a labelled candidate, never a usable contact
  * publishing a number is never consent: contact_permission and marketing_eligibility are separate
    columns and marketing stays BLOCKED_NO_CONSENT
  * the original discovery evidence is never rewritten by this stage
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import identity, prospector          # noqa: E402


# ---------------------------------------------------------------- fixtures
def db(tmp_path):
    return prospector.connect(tmp_path / "t.db")


def seed(con, **over):
    base = {
        "prospect_key": "k1", "source": "reddit_archive_search", "platform": "reddit_archive",
        "community": "reddit:r/tampa", "source_url": "https://www.reddit.com/comments/abc",
        "author_handle": "Quiet-Signal-4821",
        "stated_need": "I lost my job in Tampa and need health insurance coverage. What are my options?",
        "observed_at": "2026-10-01T10:00:00", "discovered_at": "2026-10-31T00:00:00",
        "contact_methods": "[]", "contact_status": "CONTACT_NOT_AVAILABLE",
        "permission_status": "NONE", "permission_evidence": "", "status": "NEW_PROSPECT",
        "confidence": 0.6, "evidence": '{"post_text": "I lost my job and need coverage"}',
        "is_test": 0, "intent_strength": "ACTIVE", "state_relevant": "TARGET", "geo": "FL",
    }
    base.update(over)
    cols = ",".join(base)
    con.execute(f"INSERT INTO public_prospects ({cols}) VALUES ({','.join('?' * len(base))})",
                tuple(base.values()))
    con.commit()
    return con.execute("SELECT max(id) FROM public_prospects").fetchone()[0]


def fake(posts=(), comments=()):
    """Archive-shaped fetcher: posts/comments are (title, body) pairs for the SAME author."""
    def f(url):
        if "/posts/search" in url:
            return {"data": [{"id": f"p{i}", "subreddit": "tampa", "title": t, "selftext": b,
                              "permalink": f"/r/tampa/comments/p{i}/post/"}
                             for i, (t, b) in enumerate(posts)]}
        if "/comments/search" in url:
            return {"data": [{"id": f"c{i}", "subreddit": "florida", "body": b,
                              "permalink": f"/r/florida/comments/c{i}/comment/"}
                             for i, (_t, b) in enumerate(comments)]}
        raise RuntimeError("HTTP 404")
    return f


def nope(_url):
    raise RuntimeError("no page fetched in this test")


def resolve(con, pid, fetcher, **kw):
    return identity.resolve(con, pid, fetch_json=fetcher, fetch_text=nope,
                            sleeper=lambda _s: None, probes=kw.pop("probes", False), **kw)


def row(con, pid):
    return dict(con.execute("SELECT * FROM public_prospects WHERE id=?", (pid,)).fetchone())


# ---------------------------------------------------------------- name rules
def test_name_only_from_self_identification():
    claims = identity.name_claims("My name is John Smith and I need coverage.",
                                  source_url="u1", source_kind="archive_post")
    assert [c["normalized"] for c in claims] == ["John Smith"]
    assert claims[0]["strength"] >= 0.85 and claims[0]["source_url"] == "u1"


def test_first_name_requires_a_known_name_not_an_adjective():
    ok = identity.name_claims("Hi all, I'm Jessica and I lost my job.", source_url="u", source_kind="x")
    assert any(c["normalized"] == "Jessica" for c in ok)
    for text in ("I'm tired of paying so much", "I'm looking for a plan",
                 "I'm moving to Florida next month", "I'm uninsured right now"):
        assert not identity.name_claims(text, source_url="u", source_kind="x"), text


def test_handle_city_and_org_words_are_never_names():
    for raw in ("Quiet-Signal-4821", "Tampa", "Insurance Agency", "LLC", "Sorry", "26"):
        assert identity.valid_name(raw) == "", raw
    assert identity.valid_name("maría lópez") == "María López"


def test_spanish_self_identification():
    claims = identity.name_claims("Hola, me llamo Jose Ramirez y necesito seguro medico.",
                                  source_url="u", source_kind="archive_post")
    assert claims and claims[0]["normalized"] == "Jose Ramirez"


# ---------------------------------------------------------------- phone rules
def test_self_published_number_is_accepted_and_normalized():
    ok, bad = identity.phone_claims(
        "I'm shopping for a plan and text me at (813) 555-0142 if you know one.",
        source_url="u", source_kind="archive_post")
    assert [c["normalized"] for c in ok] == ["8135550142"]
    assert not bad


def test_toll_free_business_and_borrowed_numbers_are_refused_with_a_reason():
    ok, bad = identity.phone_claims("call me at 800-555-0142 about a plan", source_url="u",
                                    source_kind="x")
    assert not ok and "toll-free" in bad[0]["reject_reason"]
    ok, bad = identity.phone_claims("call our agency at 813-555-0142 for a quote", source_url="u",
                                    source_kind="x")
    assert not ok and "business/agency" in bad[0]["reject_reason"]
    # a number somebody QUOTED is not theirs: the extractor's third-party fence drops it entirely
    ok, bad = identity.phone_claims("I received a call from 813-555-0999 and caller id was odd",
                                    source_url="u", source_kind="x")
    assert not ok


def test_number_without_ownership_wording_is_refused():
    ok, bad = identity.phone_claims("the plan line is 813-555-0142 and the office hours vary",
                                    source_url="u", source_kind="x")
    assert not ok


# ---------------------------------------------------------------- place rules
def test_florida_city_named_about_the_self():
    claims = identity.place_claims("I just moved to Brandon and need coverage.", source_url="u",
                                   source_kind="x")
    assert [c["normalized"] for c in claims] == ["Brandon, FL"]
    assert not identity.place_claims("I live in Georgia", source_url="u", source_kind="x")


# ---------------------------------------------------------------- decisions
def test_single_mention_is_probable_never_verified():
    name, status, _, conf, why = identity.decide_name([
        {"normalized": "John Smith", "source_url": "u1", "source_kind": "archive_post",
         "method": "'my name is'", "strength": 0.9}])
    assert (name, status) == ("John Smith", "PROBABLE") and conf < 0.9 and "not corroborated" in why


def test_two_independent_sources_verify_and_a_first_name_prefix_is_not_a_conflict():
    name, status, _, _, _ = identity.decide_name([
        {"normalized": "John Smith", "source_url": "u1", "source_kind": "archive_post",
         "method": "'my name is'", "strength": 0.9},
        {"normalized": "John Smith", "source_url": "u2", "source_kind": "platform_profile",
         "method": "profile name", "strength": 0.75}])
    assert (name, status) == ("John Smith", "VERIFIED")
    name, status, _, _, why = identity.decide_name([
        {"normalized": "John", "source_url": "u1", "source_kind": "archive_post",
         "method": "\"I'm <common first name>\"", "strength": 0.55},
        {"normalized": "John Smith", "source_url": "u2", "source_kind": "handle_probe",
         "method": "GitHub profile name", "strength": 0.6}])
    assert (name, status) == ("John Smith", "VERIFIED"), why


def test_disagreeing_sources_produce_a_conflict_and_no_name():
    name, status, _, _, why = identity.decide_name([
        {"normalized": "John Smith", "source_url": "u1", "source_kind": "archive_post",
         "method": "'my name is'", "strength": 0.9},
        {"normalized": "Michael Johnson", "source_url": "u2", "source_kind": "archive_post",
         "method": "'my name is'", "strength": 0.9}])
    assert name == "" and status == "CONFLICT" and "disagree" in why


def test_number_is_not_a_usable_contact_until_its_owner_is_verified():
    cand = {"normalized": "8135550142", "source_url": "u1", "source_kind": "archive_post",
            "method": "self-published with ownership wording", "strength": 0.85}
    phone, status, _, _, why = identity.decide_phone([cand], "PROBABLE", "John Smith", [])
    assert (phone, status) == ("8135550142", "PROBABLE") and "NOT a usable contact" in why
    phone, status, _, conf, _ = identity.decide_phone([cand], "VERIFIED", "John Smith", [])
    assert (phone, status) == ("8135550142", "VERIFIED") and conf >= 0.8


# ---------------------------------------------------------------- end-to-end on a row
def test_single_public_source_resolves_a_candidate_but_never_claims_verification(tmp_path):
    con = db(tmp_path)
    pid = seed(con)
    before = row(con, pid)
    r = resolve(con, pid, fake(posts=[
        ("Need advice", "My name is John Smith. I live in Brandon and text me at 813-555-0142 "
                        "if you know a plan.")]))
    after = row(con, pid)
    assert r["name"] == "John Smith" and r["name_status"] == "PROBABLE"
    assert r["phone"] == "8135550142" and r["phone_status"] == "PROBABLE"
    assert r["match_status"] == "PARTIAL" and r["location"] == "Brandon, FL"
    # publishing a number is not permission: two separate columns, both conservative
    assert r["permission"] == "SELF_PUBLISHED"
    assert r["eligibility"] == "BLOCKED_UNVERIFIED"     # contact exists; NO marketing permission
    # the original discovery evidence is untouched
    assert after["evidence"] == before["evidence"] and after["stated_need"] == before["stated_need"]
    assert after["source_url"] == before["source_url"]
    # provenance: every claim points at the page it came from
    claims = [dict(x) for x in con.execute(
        "SELECT claim_kind, normalized, source_url, source_kind, status FROM identity_claims "
        "WHERE prospect_id=?", (pid,))]
    assert {(c["claim_kind"], c["normalized"]) for c in claims} >= {
        ("NAME", "John Smith"), ("PHONE", "8135550142"), ("LOCATION", "Brandon, FL")}
    assert all(c["source_url"].startswith("https://www.reddit.com/") for c in claims)


def test_two_independent_sources_verify_the_name_and_the_number(tmp_path):
    con = db(tmp_path)
    pid = seed(con)
    r = resolve(con, pid, fake(
        posts=[("Coverage help", "My name is John Smith and I need a plan. "
                                "Text me at 813-555-0142 with options.")],
        comments=[("someone asked", "My name is John Smith, still looking for coverage in Florida.")]))
    assert r["name_status"] == "VERIFIED" and r["phone_status"] == "VERIFIED"
    assert r["match_status"] == "MATCHED"
    rep = identity.report(con)
    assert rep["usable"] == 1 and rep["name_verified"] == 1 and rep["phone_verified"] == 1
    assert rep["marketed"] == 0                    # no consent row -> not marketable


def test_conflicting_identities_are_refused_rather_than_merged(tmp_path):
    con = db(tmp_path)
    pid = seed(con)
    r = resolve(con, pid, fake(
        posts=[("help", "My name is John Smith and text me at 813-555-0142.")],
        comments=[("also me", "My name is Michael Johnson, same situation.")]))
    assert r["name"] == "" and r["name_status"] == "CONFLICT" and r["match_status"] == "REJECTED"
    assert "disagree" in r["match_reason"]
    assert r["phone_status"] == "PROBABLE"          # a number whose owner is disputed is not usable
    assert row(con, pid)["identity_name"] == ""


def test_refused_numbers_keep_their_reason_for_the_operator(tmp_path):
    con = db(tmp_path)
    pid = seed(con)
    r = resolve(con, pid, fake(posts=[
        ("hi", "My name is John Smith, I need coverage. Call our agency at 813-555-0142 for a quote.")]))
    assert r["phone"] == "" and r["phone_status"] == "REJECTED"
    bad = [dict(x) for x in con.execute(
        "SELECT value, reject_reason FROM identity_claims WHERE prospect_id=? AND status='REJECTED'",
        (pid,))]
    assert bad and "business/agency" in bad[0]["reject_reason"]


def test_handle_probe_corroborates_the_dossier_name(tmp_path):
    con = db(tmp_path)
    pid = seed(con)

    def fetcher(url):
        if "/posts/search" in url:
            return {"data": [{"id": "p0", "subreddit": "tampa", "title": "need a plan",
                              "selftext": "My name is John Smith, lost my job, need coverage.",
                              "permalink": "/r/tampa/comments/p0/post/"}]}
        if "/comments/search" in url:
            return {"data": []}
        if "api.github.com/users/" in url:
            return {"name": "John Smith", "location": "Brandon, FL", "blog": ""}
        raise RuntimeError("HTTP 404")
    r = resolve(con, pid, fetcher, probes=True)
    assert r["name_status"] == "VERIFIED" and r["match_status"] == "MATCHED"
    kinds = {x[0] for x in con.execute(
        "SELECT DISTINCT source_kind FROM identity_claims WHERE prospect_id=? AND claim_kind='NAME'",
        (pid,))}
    assert {"archive_post", "handle_probe"} <= kinds
    assert "Brandon, FL" in r["location"]


def test_agent_shaped_material_never_becomes_a_consumer_contact(tmp_path):
    con = db(tmp_path)
    pid = seed(con, stated_need="As a licensed agent I can get you the best rates. Call our office.")
    r = resolve(con, pid, fake(posts=[
        ("quote", "My name is John Smith, licensed agent, call me at 813-555-0142 for a quote.")]))
    assert r["name"] == "" and r["phone"] == ""


def test_resolution_is_rerunnable_and_counted(tmp_path):
    con = db(tmp_path)
    ids = [seed(con, prospect_key=f"k{i}", source_url=f"https://www.reddit.com/comments/{i}")
           for i in range(3)]
    s = identity.resolve_queue(con, ids=ids, limit=3, fetch_json=fake(
        posts=[("x", "My name is John Smith, I need a plan, text me at 813-555-0142.")]),
        fetch_text=nope, sleeper=lambda _s: None)
    assert s["rows_scanned"] == 3 and s["names_found"] == 3 and s["errors"] == 0
    s2 = identity.resolve_queue(con, ids=ids, limit=3, fetch_json=fake(posts=[]),
                                fetch_text=nope, sleeper=lambda _s: None)
    assert s2["rows_scanned"] == 3 and s2["errors"] == 0
    assert identity.report(con)["rows_attempted"] == 3
    assert json.loads(row(con, ids[0])["identity_evidence"])["sources"]


# ---------------------------------------------------------------- admin surface
def test_admin_page_shows_resolved_identity_and_its_refusals(tmp_path):
    from tests.test_engine import make_app
    m = make_app(tmp_path)
    pid = seed(m.con, prospect_key="admin1")
    m.con.execute(
        "UPDATE public_prospects SET identity_name='John Smith', identity_name_status='VERIFIED', "
        "identity_name_source='https://www.reddit.com/comments/p1', identity_name_confidence=0.92, "
        "identity_phone='8135550142', identity_phone_status='VERIFIED', "
        "identity_phone_source='https://www.reddit.com/comments/p1', location_resolved='Brandon, FL', "
        "match_status='MATCHED', match_reason='corroborated by 2 independent public sources', "
        "contact_permission='SELF_PUBLISHED', marketing_eligibility='BLOCKED_UNVERIFIED', "
        "identity_evidence=? WHERE id=?",
        (json.dumps({"rejected_claims": [{"kind": "PHONE", "value": "800-555-0142",
                                          "why": "toll-free/premium range 800"}]}), pid))
    m.con.commit()
    body = m.app.test_client().get("/admin/people?token=t").get_data(as_text=True)
    for fragment in ("John Smith", "8135550142", "Brandon, FL", "MATCHED",
                     "BLOCKED_UNVERIFIED", "toll-free/premium range 800", "resolve identity"):
        assert fragment in body, fragment


def test_admin_resolve_endpoint_reports_the_outcome(tmp_path, monkeypatch):
    from tests.test_engine import make_app
    m = make_app(tmp_path)
    pid = seed(m.con, prospect_key="admin2")
    monkeypatch.setattr(identity, "resolve", lambda con, pid, probes=False: {
        "status": "ok", "name": "John Smith", "name_status": "VERIFIED",
        "phone": "8135550142", "phone_status": "VERIFIED", "match_status": "MATCHED",
        "permission": "SELF_PUBLISHED", "eligibility": "BLOCKED_UNVERIFIED",
        "location": "Brandon, FL"})
    r = m.app.test_client().post(f"/admin/people/{pid}/resolve?token=t", data={"probes": "1"},
                                 follow_redirects=True)
    body = r.get_data(as_text=True)
    assert r.status_code == 200 and "name VERIFIED (John Smith)" in body
    assert "marketing BLOCKED_UNVERIFIED" in body


def test_pseudonyms_and_roles_are_never_names():
    for raw in ("Moderator", "Newbie", "Girl Lurker", "Air Fryer Aristocrat"):
        assert identity.valid_name(raw) == "", raw
    for name, method in (("Girl Lurker", "public author flair"),
                         ("Midnight", "Bluesky profile display name")):
        n, s, _, _, why = identity.decide_name([
            {"normalized": name, "source_url": "u", "source_kind": "platform_profile",
             "method": method, "strength": 0.45}])
        assert n == "" and s == "UNRESOLVED" and why
    # a two-token, person-shaped display name is a CANDIDATE (0.5), never verified on its own
    n, s, _, c, _ = identity.decide_name([
        {"normalized": "Margaret Citta", "source_url": "u", "source_kind": "platform_profile",
         "method": "Bluesky profile display name", "strength": 0.75}])
    assert (n, s, c) == ("Margaret Citta", "PROBABLE", 0.5)


def test_the_consumers_own_submission_is_the_only_phone_route(tmp_path):
    con = db(tmp_path)
    pid = seed(con, lead_id="LADOPT1")
    assert identity.adopt_consented_lead(con, pid) is False        # no lead row yet
    con.execute("INSERT INTO leads (lead_id, created_at, is_test, first_name, last_name, phone, "
                "phone_source) "
                "VALUES ('LADOPT1', datetime('now'), 0, 'John', 'Smith', '(813) 555-0142', "
                "'CONSUMER_SUBMITTED')")
    con.commit()
    assert identity.adopt_consented_lead(con, pid) is False        # lead, but no consent row
    con.execute("INSERT INTO consents (lead_id, ts, consent_version, consent_text) "
                "VALUES ('LADOPT1', datetime('now'), 'v1', "
                "'I agree a licensed agent may contact me about coverage')")
    con.commit()
    assert identity.adopt_consented_lead(con, pid) is True
    r = row(con, pid)
    assert r["identity_name"] == "John Smith" and r["identity_phone"] == "8135550142"
    assert r["identity_name_status"] == "VERIFIED" and r["identity_phone_status"] == "VERIFIED"
    assert r["contact_permission"] == "CONSENTED" and r["marketing_eligibility"] == "ELIGIBLE_CONSENTED"
    rep = identity.report(con)
    assert rep["usable"] == 1 and rep["marketed"] == 1


def test_test_leads_are_never_adopted(tmp_path):
    con = db(tmp_path)
    pid = seed(con, lead_id="LADOPT2")
    con.execute("INSERT INTO leads (lead_id, created_at, is_test, first_name, last_name, phone, "
                "phone_source) "
                "VALUES ('LADOPT2', datetime('now'), 1, 'Test', 'User', '8135550000', "
                "'CONSUMER_SUBMITTED')")
    con.execute("INSERT INTO consents (lead_id, ts, consent_version, consent_text) "
                "VALUES ('LADOPT2', datetime('now'), 'v1', 'test consent text')")
    con.commit()
    assert identity.adopt_consented_lead(con, pid) is False
    assert row(con, pid)["identity_phone"] == ""
