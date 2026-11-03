"""Consumer-intent discovery: grading, the state gate, the engagement route and the conversion link.

These tests pin the CORRECTION, not just the code:

  * a public post is accepted on INTENT (a person asking for coverage in Florida), not on whether it
    happens to publish an email or phone - requiring a contact is what made every discovered consumer
    dead-end at NEW_PROSPECT
  * a treatment need ("I need IVIG"), a broker pitch, an op-ed, a stale thread (2017) and an explicit
    other-state post are all REJECTED with a recorded reason
  * an accepted row records intent strength, state relevance, the engagement route, freshness
  * QUALIFIED no longer requires a published contact; it authorizes OFFERING the public check link,
    never contacting anyone
  * the issued /go link is attributable, and a lead that arrives through it is linked back to the
    prospect ONLY when a real consent row exists
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import prospector, discovery        # noqa: E402


def con(tmp_path):
    return prospector.connect(tmp_path / "t.db")


def row(url="https://www.reddit.com/comments/a1", title="", text="", source_platform="reddit_archive",
        community="reddit:r/HealthInsurance", observed_at="2026-10-20T10:00:00", geo="", lang="en"):
    return {"url": url, "title": title, "text": text, "observed_at": observed_at, "author": "",
            "community": community, "platform": source_platform, "terms": "test fixture", "geo": geo,
            "lang": lang}


FL_ASK = "I just moved to Miami and I am looking for a health insurance plan for my family. What are my options?"
FL_JOB = "I lost my job last month in Tampa and COBRA ends in March. I need to figure out coverage."
MEDICAL = "There are no approved treatments for what I have, so insurance coverage is not likely. I need IVIG every month."
BROKER = "As a licensed agent I can get you the best rates guaranteed. DM me for a quote today."
OPED = "Congress should fix this. The article shows how Medicare Advantage scams the government."
STALE_ASK = "I am looking for health insurance in Florida after losing my job. Any recommendations?"


# ---------------------------------------------------------------- classification
def test_florida_active_ask_is_a_consumer_with_active_intent():
    r = prospector.classify(FL_ASK)
    assert r["need"] and r["verdict"] == "CONSUMER"
    assert r["strength"] == "ACTIVE"
    assert prospector.extract_geo(FL_ASK) == "FL"          # city mention maps to the state


def test_job_loss_is_at_least_a_strong_signal():
    r = prospector.classify(FL_JOB)
    assert r["need"] and r["strength"] in ("ACTIVE", "STRONG")


def test_treatment_need_is_not_coverage_shopping():
    r = prospector.classify(MEDICAL)
    assert not r["need"] and r["verdict"] == "MEDICAL_NEED"


def test_broker_and_op_ed_are_rejected_with_a_reason():
    assert prospector.classify(BROKER)["verdict"] == "AGENT"
    op = prospector.classify(OPED)
    assert not op["need"] and op["reason"]


def test_spanish_first_person_ask_is_understood():
    r = prospector.classify("Me despidieron en Miami y necesito seguro medico para mi familia. Que opciones tengo?")
    assert r["need"] and r["strength"] == "ACTIVE"


# ---------------------------------------------------------------- ingestion gates
def test_ingest_stores_intent_state_and_engagement(tmp_path):
    c = con(tmp_path)
    stats = prospector.ingest(c, [row(text=FL_ASK)], "reddit_archive_search")
    assert stats["accepted"] == 1 and stats["new"] == 1 and stats["engagement_ready"] == 1
    r = dict(c.execute("SELECT * FROM public_prospects").fetchone())
    assert r["intent_strength"] == "ACTIVE"
    assert r["state_relevant"] == "TARGET"
    assert r["engagement"] == "THREAD_REPLY"
    assert r["status"] == "NEW_PROSPECT"                    # intent alone is not a lead, by design


def test_explicit_other_state_is_rejected_and_counted(tmp_path):
    c = con(tmp_path)
    stats = prospector.ingest(c, [row(text="I live in Ohio and need health coverage for next year, what are my options?")],
                              "reddit_archive_search")
    assert stats["new"] == 0 and stats["out_of_state"] == 1
    assert any("outside FL" in k for k in stats["reasons"]), stats["reasons"]


def test_stale_post_is_rejected(tmp_path):
    c = con(tmp_path)
    stats = prospector.ingest(c, [row(text=STALE_ASK, observed_at="2017-03-01T00:00:00")], "reddit_archive_search")
    assert stats["new"] == 0 and stats["stale"] == 1
    assert any("stale" in k for k in stats["reasons"]), stats["reasons"]


def test_no_date_is_not_treated_as_stale(tmp_path):
    c = con(tmp_path)
    stats = prospector.ingest(c, [row(text=FL_ASK, observed_at="")], "stackexchange")
    assert stats["new"] == 1                                # unknown date is unknown, not old


def test_same_post_twice_is_a_duplicate(tmp_path):
    c = con(tmp_path)
    prospector.ingest(c, [row(text=FL_ASK)], "reddit_archive_search")
    stats = prospector.ingest(c, [row(text=FL_ASK)], "reddit_archive_search")
    assert stats["new"] == 0 and stats["duplicates"] == 1


# ---------------------------------------------------------------- the corrected ladder
def _one(c):
    return c.execute("SELECT id FROM public_prospects").fetchone()["id"]


def test_qualify_works_without_any_published_contact(tmp_path):
    """The old rule required CONTACT_AVAILABLE, which no discussion platform exposes."""
    c = con(tmp_path)
    prospector.ingest(c, [row(text=FL_ASK)], "reddit_archive_search")
    pid = _one(c)
    assert prospector.qualify(c, pid, "tried to qualify before verifying") is False
    assert prospector.verify(c, pid, "opened the thread: real person, Florida, asking now") is True
    assert prospector.qualify(c, pid, "public thread, answer is allowed, offer the check link") is True


def test_qualify_still_refuses_a_row_with_no_engagement_route(tmp_path):
    c = con(tmp_path)
    prospector.ingest(c, [row(text=FL_ASK, url="https://example.org/classified/1",
                              source_platform="manual")], "manual")
    pid = _one(c)
    prospector.verify(c, pid, "opened the page: real person asking for coverage")
    assert prospector.qualify(c, pid, "no thread, no consent - must stay refused") is False


def test_issue_link_requires_qualified_and_is_idempotent(tmp_path):
    c = con(tmp_path)
    prospector.ingest(c, [row(text=FL_ASK)], "reddit_archive_search")
    pid = _one(c)
    assert prospector.issue_link(c, pid, "https://site.example") is None      # not qualified yet
    prospector.verify(c, pid, "opened the thread: real person, current need")
    prospector.qualify(c, pid, "public thread, reply allowed")
    link = prospector.issue_link(c, pid, "https://site.example")
    assert link and link["url"].startswith("https://site.example/go/")
    link_row = c.execute("SELECT * FROM links WHERE code=?", (link["code"],)).fetchone()
    assert link_row["utm_medium"] == "community" and link_row["utm_content"] == "p%d" % pid
    again = prospector.issue_link(c, pid, "https://site.example")
    assert again["existing"] is True and again["code"] == link["code"]


def test_a_lead_through_the_link_marks_the_prospect_routing_eligible(tmp_path):
    """The whole point: a public thread becomes measurable, and only the person's own consented
    submission turns the row into a routable lead."""
    c = con(tmp_path)
    prospector.ingest(c, [row(text=FL_ASK)], "reddit_archive_search")
    pid = _one(c)
    prospector.verify(c, pid, "opened the thread: real person, current need")
    prospector.qualify(c, pid, "public thread, reply allowed")
    code = prospector.issue_link(c, pid, "https://site.example")["code"]

    c.execute("""INSERT INTO leads (lead_id, created_at, is_test, campaign, product, landing_url, utm_source,
                 utm_medium, utm_content, link_code, phone, phone_source, phone_valid, lead_type,
                 qual_status, routing_status, contact_scope)
                 VALUES ('L1','2026-10-31T00:00:00',1,'oep-2027-health','health_aca','https://site.example/check',
                         'reddit','community','p%d',?,'3055550100','CONSUMER_SUBMITTED',1,'PHONE_LEAD',
                         'QUALIFIED','PENDING','PHONE_AND_EMAIL')""" % pid, (code,))
    c.execute("""INSERT INTO consents (lead_id, ts, consent_version, consent_text, seller_named, checkbox_checked,
                 scope, ip, user_agent, page_url, form_snapshot_sha256, phone, email)
                 VALUES ('L1','2026-10-31T00:00:00','v4','I agree that an agency may contact me','Agency X',1,
                         'PHONE_AND_EMAIL','127.0.0.1','ua','https://site.example/check','sha','3055550100','a@example.com')""")
    c.commit()
    assert prospector.attribute_lead(c, "L1") is True
    r = c.execute("SELECT status, lead_id FROM public_prospects WHERE id=?", (pid,)).fetchone()
    assert r["status"] == "ROUTING_ELIGIBLE" and r["lead_id"] == "L1"


def test_a_lead_without_a_consent_row_is_never_linked(tmp_path):
    c = con(tmp_path)
    prospector.ingest(c, [row(text=FL_ASK)], "reddit_archive_search")
    pid = _one(c)
    prospector.verify(c, pid, "opened the thread: real person, current need")
    prospector.qualify(c, pid, "public thread, reply allowed")
    code = prospector.issue_link(c, pid, "https://site.example")["code"]
    c.execute("""INSERT INTO leads (lead_id, created_at, is_test, link_code, phone, phone_source, phone_valid,
                 lead_type, qual_status, routing_status)
                 VALUES ('L2','2026-10-31T00:00:00',1,?, '3055550111','CONSUMER_SUBMITTED',1,
                 'PHONE_LEAD','QUALIFIED','PENDING')""", (code,))
    c.commit()
    assert prospector.attribute_lead(c, "L2") is False
    assert c.execute("SELECT status FROM public_prospects WHERE id=?", (pid,)).fetchone()["status"] == "QUALIFIED"


def test_unrelated_lead_is_ignored(tmp_path):
    c = con(tmp_path)
    c.execute("""INSERT INTO leads (lead_id, created_at, is_test, link_code, qual_status, routing_status)
                 VALUES ('L3','2026-10-31T00:00:00',1,'some-other-code','QUALIFIED','PENDING')""")
    c.commit()
    assert prospector.attribute_lead(c, "L3") is False


# ---------------------------------------------------------------- adapters, offline
def test_reddit_archive_search_parses_and_targets_a_board_with_a_query(tmp_path):
    import json
    seen = []

    def fake(url):
        seen.append(url)
        return json.dumps({"data": [{"id": "abc", "title": "Moving to Miami", "selftext": FL_ASK,
                                     "author": "someone", "subreddit": "HealthInsurance",
                                     "created_utc": 1793402055}]})

    res = discovery.reddit_archive_search([("HealthInsurance", "miami")], per_query=10, fetcher=fake, delay=0)
    assert "subreddit=HealthInsurance" in seen[0] and "query=miami" in seen[0]
    assert res["fetched"] == 1 and res["rows"][0]["url"] == "https://www.reddit.com/comments/abc"
    assert res["rows"][0]["observed_at"].startswith("2026-")


def test_stackexchange_search_parses_questions():
    import json

    def fake(url):
        assert "site=money" in url
        return json.dumps({"items": [{"title": "How do I get health insurance after a layoff?",
                                      "link": "https://money.stackexchange.com/q/1",
                                      "body": "<p>I lost my job in Florida and need coverage.</p>",
                                      "creation_date": 1793402055}]})

    res = discovery.stackexchange_search([("money", "health insurance florida")], fetcher=fake, delay=0)
    assert res["fetched"] == 1
    assert res["rows"][0]["community"] == "stackexchange:money"
    assert "Florida" in res["rows"][0]["text"]


def test_lemmy_and_mastodon_adapters_parse_public_payloads():
    import json

    def lemmy(url):
        return json.dumps({"posts": [{"post": {"id": 9, "name": "ACA question", "body": FL_ASK,
                                               "published": "2026-10-20T10:00:00Z",
                                               "community": {"name": "health"}}}]})

    def masto(url):
        return json.dumps([{"url": "https://mastodon.social/@x/1", "created_at": "2026-10-20T10:00:00Z",
                            "content": "<p>" + FL_ASK + "</p>"}])

    a = discovery.lemmy_search("https://lemmy.world", ["health insurance"], fetcher=lemmy, delay=0)
    b = discovery.mastodon_tags("https://mastodon.social", ["healthinsurance"], fetcher=masto, delay=0)
    assert a["fetched"] == 1 and a["rows"][0]["url"] == "https://lemmy.world/post/9"
    assert b["fetched"] == 1 and b["rows"][0]["url"].startswith("https://mastodon.social/")


def test_reddit_rss_is_not_used_because_robots_disallows_it():
    """reddit.com answers 200 on /r/*/.rss but its robots.txt disallows that path. The engine must not
    read it, and the permitted archive route must be the one wired into the default sweep."""
    assert "reddit.com" not in "".join(discovery.DEFAULT_SOURCES)
    assert "reddit_archive_search" in discovery.DEFAULT_SOURCES


# ---------------------------------------------------------------- ad copy vs a person
def test_second_person_ad_copy_is_not_a_consumer():
    """A Bluesky insurance promo ('Whether you're self-employed, between jobs, or covering your
    family') was stored as an ACTIVE prospect because a truncated title ('Health I 🏥') looked like
    the pronoun I. Advertising addresses the reader; a consumer states their own stake."""
    ad = ("Whether you're self-employed, between jobs, or covering your family, Health I 🏥 "
          "Looking for Health Insurance in Florida?")
    r = prospector.classify(ad)
    assert not r["need"] and r["verdict"] in ("PROMO", "NOT_CONSUMER")


def test_a_bare_capital_i_is_not_a_first_person_stake():
    r = prospector.classify("Health I 🏥 Looking for Health Insurance in Florida?")
    assert not r["need"]


def test_first_person_ask_still_passes_after_the_bare_i_rule():
    assert prospector.classify("I need health insurance and my job ends Friday. What are my options?")["need"]


# ---------------------------------------------------------------- duplicate identity
def test_the_same_permalink_from_two_adapters_is_one_prospect(tmp_path):
    """A board firehose and a scoped search read the same thread; the identity is the URL, not the
    adapter that happened to find it."""
    c = con(tmp_path)
    url = "https://www.reddit.com/comments/abc123"
    a = prospector.ingest(c, [row(url=url, text=FL_ASK)], "reddit_archive")
    b = prospector.ingest(c, [row(url=url + "/?utm_source=x", text=FL_ASK)], "reddit_archive_search")
    assert a["new"] == 1 and b["new"] == 0 and b["duplicates"] == 1
    assert c.execute("SELECT COUNT(*) FROM public_prospects").fetchone()[0] == 1


def test_regrade_marks_a_cross_adapter_duplicate_and_keeps_the_original(tmp_path):
    c = con(tmp_path)
    url = "https://www.reddit.com/comments/abc999"
    prospector.ingest(c, [row(url=url, text=FL_ASK)], "reddit_archive")
    # a row stored by an older build under a source-qualified key
    first = c.execute("SELECT prospect_key FROM public_prospects").fetchone()[0]
    c.execute("""INSERT INTO public_prospects (prospect_key, source, source_url, stated_need,
                 discovered_at, status, dup_status, created_at, updated_at)
                 VALUES ('other-key','reddit_archive_search',?,?, '2026-10-31T00:00:00','NEW_PROSPECT',
                         'UNIQUE','2026-10-31T00:00:00','2026-10-31T00:00:00')""", (url, FL_ASK))
    c.commit()
    s = prospector.regrade(c)
    assert s["dedupe"]["marked"] == 1
    kept = c.execute("SELECT status, prospect_key FROM public_prospects ORDER BY id LIMIT 1").fetchone()
    dup = c.execute("SELECT status, dup_of FROM public_prospects ORDER BY id DESC LIMIT 1").fetchone()
    assert kept["status"] != "DUPLICATE" and kept["prospect_key"] == first
    assert dup["status"] == "DUPLICATE" and dup["dup_of"] == first


def test_regrade_rejects_an_ad_row_and_grades_the_rest(tmp_path):
    c = con(tmp_path)
    ad = ("Whether you're self-employed, between jobs, or covering your family, Health I 🏥 "
          "Looking for Health Insurance in Florida?")
    prospector.ingest(c, [row(url="https://bsky.app/profile/a/post/1", text=FL_ASK)], "signal_store")
    c.execute("""INSERT INTO public_prospects (prospect_key, source, source_url, stated_need, geo,
                 discovered_at, status, dup_status, created_at, updated_at)
                 VALUES ('old-key','signal_store','https://bsky.app/profile/a/post/2',?,'FL',
                         '2026-10-31T00:00:00','NEW_PROSPECT','UNIQUE','2026-10-31T00:00:00',
                         '2026-10-31T00:00:00')""", (ad,))
    c.commit()
    s = prospector.regrade(c)
    assert s["rejected"] == 1
    live = c.execute("SELECT intent_strength, state_relevant, engagement, status FROM public_prospects "
                     "WHERE prospect_key='old-key'").fetchone()
    assert live["status"] == "REJECTED" and "advertising" in \
        c.execute("SELECT status_reason FROM public_prospects WHERE prospect_key='old-key'").fetchone()[0]
    ok = c.execute("SELECT intent_strength, engagement FROM public_prospects WHERE prospect_key!='old-key'"
                   ).fetchone()
    assert ok["intent_strength"] == "ACTIVE" and ok["engagement"] == "THREAD_REPLY"


def test_actionable_queue_is_keyed_on_intent_not_on_a_published_contact(tmp_path):
    c = con(tmp_path)
    prospector.ingest(c, [row(url="https://www.reddit.com/comments/q1", text=FL_ASK)], "reddit_archive")
    q = prospector.actionable(c)
    assert len(q) == 1 and q[0]["contact_status"] != "CONTACT_AVAILABLE"
    assert q[0]["engagement"] == "THREAD_REPLY"


def test_the_query_cap_rotates_across_boards_instead_of_starving_them():
    """A boards-major slice spent a 12-request cap entirely on the first two boards, so r/florida,
    r/tampa and r/Miami - where an actual Florida consumer posts - were never queried."""
    subs = ["HealthInsurance", "ACA", "florida", "tampa", "Miami", "orlando"]
    pairs = discovery.search_pairs(subs, ["florida", "lost job", "cobra", "laid off"], 8)
    boards = [p[0] for p in pairs]
    assert len(pairs) == 8 and len(set(boards)) == 6          # every board gets a turn
    assert len({p[1] for p in pairs}) >= 2                    # and more than one situation is tried
    assert len(discovery.search_pairs([], ["x"], 5)) == 0


def test_confidence_reflects_intent_not_just_a_published_contact():
    """The old score was 0.15 for every row without an email/phone - i.e. every consumer looked like
    noise. A verified ACTIVE consumer in a target state with a public thread must outrank a vague
    mention, and a contact is a bonus rather than the foundation."""
    assert prospector.intent_confidence("ACTIVE", "THREAD_REPLY", "TARGET") > \
           prospector.intent_confidence("WEAK", "", "UNKNOWN")
    assert prospector.intent_confidence("STRONG", "THREAD_REPLY", "TARGET") >= 0.6
    assert prospector.intent_confidence("ACTIVE", "THREAD_REPLY", "TARGET", True) > \
           prospector.intent_confidence("ACTIVE", "THREAD_REPLY", "TARGET", False)
    assert 0.0 <= prospector.intent_confidence("") <= 0.95


def test_ingest_does_not_floor_a_contactless_consumer_at_noise(tmp_path):
    c = con(tmp_path)
    prospector.ingest(c, [row(url="https://www.reddit.com/comments/cf1", text=FL_ASK)], "reddit_archive")
    r = c.execute("SELECT confidence FROM public_prospects").fetchone()
    assert r[0] >= 0.5


def test_vehicle_insurance_is_not_health_coverage():
    """A consumer-phrase sweep admitted car-insurance posts because bare 'coverage' matched. A window
    about a vehicle/home with no health token is not an opportunity for this product."""
    auto = ("When I went to the dealer, they told me I need proof of insurance before they'll let me "
            "buy it, and I have nobody in my family to add me to their policy.")
    assert not prospector.classify(auto)["need"]
    prem = ("My policy, with much better coverage, is a lot cheaper than hers. we both have good "
            "driving records, no tickets. How will this need to be done?")
    assert not prospector.classify(prem)["need"]
    # and a real health ask that happens to mention a car must still pass
    real = "I lost my job and my car is paid off, but I need health insurance for my family. What are my options?"
    assert prospector.classify(real)["need"]


def test_advice_to_an_audience_is_not_a_personal_ask():
    advice = ("If anyone reading this is searching for an affordable health insurance, please whatever "
              "you do, do not make the mistake my family made.")
    assert not prospector.classify(advice)["need"]


def test_the_posters_own_state_outranks_a_relatives_state():
    """'i'm 22 living in NJ, and my father who lives in Florida' was tagged FLORIDA - the wrong
    person's state - and entered the servable queue."""
    t = "i'm 22 living in NJ, and my father who lives in Florida retired last year, and we lost our insurance."
    assert prospector.state_code(prospector.extract_geo(t)) == "NJ"
    assert prospector.state_code(prospector.extract_geo("My wife and I live in north east Florida.")) == "FL"
    assert prospector.state_code(prospector.extract_geo("I'm moving to Tampa in March.")) == "FL"


def test_travel_visitor_insurance_is_not_a_us_health_consumer():
    t = ("I'm travelling from India to the US for 2 weeks and need travel medical insurance. "
         "I'm looking for good medical coverage.")
    r = prospector.classify(t)
    assert not r["need"] and r["verdict"] == "TRAVEL"
