"""Focused tests for the PUBLIC PROFILE contact adapter (engine/public_profiles.py).

What is under test — the promises the adapter makes to the rest of the system:
  1. A contact is stored ONLY when the SAME public account published it on its OWN public
     profile AND voiced a first-person coverage need in a recent post.
  2. Nothing is inferred: no bio contact -> no row; a website link alone is not a contact.
  3. Organisation-shaped profiles (agency/broker/llc) are counted and NEVER stored as
     consumer prospects.
  4. One row per public account (not per post); the same contact value published by a second
     account is stored as DUPLICATE.
  5. Contact discovery never creates a lead: the /check link stays refused until an operator
     verifies and qualifies the row.
"""
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engine.public_profiles as pp          # noqa: E402
import engine.prospect_contacts as pcon      # noqa: E402


def _recent(days=1):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")[:19]


def _store(tmp_path):
    return pcon.connect(tmp_path / "people.db")


def _cfg(**over):
    # ONE query per platform: the fakes answer every query with the same page, so the default
    # query list would just replay the fixture.
    cfg = {"enabled": True, "pause_seconds": 0,
           "queries": {"bluesky": [{"q": "need health insurance"}],
                       "mastodon": ["healthinsurance"], "lemmy": ["need health insurance"]}}
    cfg.update(over)
    return cfg


# ------------------------------------------------------------------ bluesky
def _bsky_getter(posts, profiles, calls=None):
    def get_json(url):
        if calls is not None:
            calls.append(url)
        if "searchPosts" in url:
            return {"posts": posts}
        if "getProfile" in url:
            did = urllib.parse.parse_qs(url.split("?", 1)[1])["actor"][0]
            if did not in profiles:
                raise KeyError(did)
            return profiles[did]
        raise AssertionError("unexpected url " + url)
    return get_json


def _post(did="did:plc:alice", handle="alice.test", text="I lost my job and I need health insurance",
          at=None, rkey="r1"):
    return {"uri": f"at://{did}/app.bsky.feed.post/{rkey}",
            "author": {"did": did, "handle": handle},
            "record": {"text": text, "created_at": at or _recent()}}


def test_bluesky_profile_contact_is_stored_with_evidence(tmp_path):
    ppl = _store(tmp_path)
    get = _bsky_getter(
        [_post()],
        {"did:plc:alice": {"description": "Miami. Email me at alice.smith@example.com please.",
                           "handle": "alice.test"}})
    s = pp.scan_bluesky(ppl, cfg=_cfg(), get_json=get, sleeper=lambda *_: None)
    assert s["accepted"] == 1 and s["with_email"] == 1, s
    row = dict(ppl.execute("SELECT * FROM prospect_people").fetchone())
    assert row["contact_origin"] == "profile"
    assert row["source"] == "bluesky_profile"
    assert row["contact_status"] == "CONTACT_AVAILABLE"
    assert row["profile_url"] == "https://bsky.app/profile/alice.test"
    assert row["source_url"].startswith("https://bsky.app/profile/did:plc:alice/post/r1")
    assert "alice.smith@example.com" in row["contact_methods"]
    assert "alice.smith@example.com" in row["contact_evidence"]     # the published phrase
    assert row["permission_status"] == "INVITED_INQUIRIES"          # "email me" in the bio
    assert row["stage"] == "CONTACT_FOUND"
    assert row["rank_score"] > 0.4
    # discovery is not a lead: the /check link is still refused at this stage
    link, why = pcon.check_link(row)
    assert link == "" and "no operator-reviewed basis" in why


def test_bluesky_bio_without_contact_stores_nothing(tmp_path):
    ppl = _store(tmp_path)
    get = _bsky_getter([_post()], {"did:plc:alice": {"description": "Just a person in Miami."}})
    s = pp.scan_bluesky(ppl, cfg=_cfg(), get_json=get, sleeper=lambda *_: None)
    assert s["accepted"] == 0 and s["no_contact"] == 1, s
    assert ppl.execute("SELECT COUNT(*) FROM prospect_people").fetchone()[0] == 0


def test_bluesky_website_link_alone_is_not_a_contact(tmp_path):
    ppl = _store(tmp_path)
    get = _bsky_getter([_post()],
                       {"did:plc:alice": {"description": "my site: https://alice.example.org"}})
    s = pp.scan_bluesky(ppl, cfg=_cfg(), get_json=get, sleeper=lambda *_: None)
    assert s["accepted"] == 0, s
    assert ppl.execute("SELECT COUNT(*) FROM prospect_people").fetchone()[0] == 0


def test_linked_personal_page_contact_is_labelled(tmp_path):
    ppl = _store(tmp_path)
    get = _bsky_getter([_post()],
                       {"did:plc:alice": {"description": "Miami. site: https://alice.example.org",
                                          "handle": "alice.test"}})
    seen = []

    def fetch_text(url):
        seen.append(url)
        if url.endswith("/robots.txt"):
            return "User-agent: *\nAllow: /\n"
        return ("<html><body><h1>Alice</h1>"
                "<p>Write to me: alice.smith@example.org</p></body></html>")

    s = pp.scan_bluesky(ppl, cfg=_cfg(page_hops=2), get_json=get,
                        sleeper=lambda *_: None, fetch_text=fetch_text)
    assert s["accepted"] == 1 and s["page_contacts"] == 1, s
    assert any("robots.txt" in u for u in seen)
    row = dict(ppl.execute("SELECT * FROM prospect_people").fetchone())
    assert row["contact_origin"] == "profile_link"
    assert "alice.smith@example.org" in row["contact_methods"]
    assert "linked page https://alice.example.org" in row["contact_evidence"]


def test_linked_page_respects_robots(tmp_path):
    ppl = _store(tmp_path)
    # a host the other test never touched: the robots cache is per-host for 300s by design
    get = _bsky_getter([_post()],
                       {"did:plc:alice": {"description": "see https://blocked.example.net"}})

    def fetch_text(url):
        if url.endswith("/robots.txt"):
            return "User-agent: *\nDisallow: /\n"
        raise AssertionError("a disallowed page must not be fetched")

    s = pp.scan_bluesky(ppl, cfg=_cfg(page_hops=2), get_json=get,
                        sleeper=lambda *_: None, fetch_text=fetch_text)
    assert s["accepted"] == 0 and s["page_robots_blocked"] >= 1, s
    assert ppl.execute("SELECT COUNT(*) FROM prospect_people").fetchone()[0] == 0


def test_org_shaped_profile_is_not_a_consumer(tmp_path):
    ppl = _store(tmp_path)
    get = _bsky_getter(
        [_post()],
        {"did:plc:alice": {"description": "ABC Insurance Agency LLC - call us 305-555-0199 "
                                         "or info@abcagency.example for a free quote"}})
    s = pp.scan_bluesky(ppl, cfg=_cfg(), get_json=get, sleeper=lambda *_: None)
    assert s["accepted"] == 0 and s["org_skipped"] == 1, s
    assert s["org_examples"] and s["org_examples"][0]["profile_url"].endswith("alice.test")
    assert ppl.execute("SELECT COUNT(*) FROM prospect_people").fetchone()[0] == 0


def test_one_row_per_public_account_and_duplicate_contact(tmp_path):
    ppl = _store(tmp_path)
    posts = [_post(rkey="r1"), _post(rkey="r2"),
             _post(did="did:plc:bob", handle="bob.test", rkey="r3")]
    profiles = {
        "did:plc:alice": {"description": "email alice.smith@example.com", "handle": "alice.test"},
        # a SECOND account publishing the SAME email is a duplicate, not a new individual
        "did:plc:bob": {"description": "contact me: alice.smith@example.com", "handle": "bob.test"},
    }
    get = _bsky_getter(posts, profiles)
    s = pp.scan_bluesky(ppl, cfg=_cfg(), get_json=get, sleeper=lambda *_: None)
    assert s["accepted"] == 2, s                 # alice (r1) + bob (r3); r2 is a duplicate post
    assert s["unique_individuals"] == 1 and s["duplicates"] >= 1, s
    rows = [dict(r) for r in ppl.execute(
        "SELECT person_key, dup_status, dup_of FROM prospect_people ORDER BY id")]
    assert len(rows) == 2
    assert rows[0]["dup_status"] == "UNIQUE" and rows[1]["dup_status"] == "DUPLICATE"
    assert rows[1]["dup_of"] == rows[0]["person_key"]


def test_stale_and_unrelated_posts_are_not_prospected(tmp_path):
    ppl = _store(tmp_path)
    get = _bsky_getter(
        [_post(at=_recent(days=90), rkey="old"),
         _post(text="New marketplace rules announced today", rkey="news")],
        {"did:plc:alice": {"description": "email alice@example.com"}})
    s = pp.scan_bluesky(ppl, cfg=_cfg(), get_json=get, sleeper=lambda *_: None)
    assert s["accepted"] == 0, s
    assert s["stale_skipped"] == 1 and s["skipped_no_need"] >= 1, s
    assert ppl.execute("SELECT COUNT(*) FROM prospect_people").fetchone()[0] == 0


# ------------------------------------------------------------------ mastodon
def test_mastodon_profile_fields_phone(tmp_path):
    ppl = _store(tmp_path)
    status = {"content": "<p>I lost my job, I need health insurance for my family</p>",
              "account": {"id": "42", "acct": "bob"},
              "url": "https://mastodon.social/@bob/1", "created_at": _recent()}

    def get_json(url):
        if "timelines/tag/" in url:
            return [status]
        if "/api/v1/accounts/42" in url:
            return {"id": "42", "url": "https://mastodon.social/@bob",
                    "note": "<p>South Florida</p>",
                    "fields": [{"name": "Phone", "value": "<p>call me 305-555-0142</p>"}]}
        raise AssertionError(url)

    s = pp.scan_mastodon(ppl, cfg=_cfg(queries={"mastodon": ["healthinsurance"]}),
                         get_json=get_json, sleeper=lambda *_: None)
    assert s["accepted"] == 1 and s["with_phone"] == 1, s
    row = dict(ppl.execute("SELECT * FROM prospect_people").fetchone())
    assert row["source"] == "mastodon_profile"
    assert "305-555-0142" in row["contact_methods"]
    assert row["stage"] in ("CONTACT_FOUND", "DISCOVERED")       # "call me" invites -> found
    assert row["permission_status"] == "INVITED_INQUIRIES"


# ------------------------------------------------------------------ lemmy
def test_lemmy_bio_email(tmp_path):
    ppl = _store(tmp_path)

    def get_json(url):
        if "/api/v3/search" in url:
            return {"comments": [{"comment": {"content": "<p>Necesito seguro medico, perdi mi trabajo</p>",
                                              "ap_id": "https://lemmy.world/comment/9",
                                              "creator_id": 7, "published": _recent()},
                                  "creator": {"id": 7}}]}
        if "/api/v3/user" in url:
            return {"person": {"name": "carol", "bio": "<p>escribeme a carol.r@example.com</p>"}}
        raise AssertionError(url)

    s = pp.scan_lemmy(ppl, cfg=_cfg(queries={"lemmy": ["seguro medico"]}),
                      get_json=get_json, sleeper=lambda *_: None)
    assert s["accepted"] == 1 and s["with_email"] == 1, s
    row = dict(ppl.execute("SELECT * FROM prospect_people").fetchone())
    assert row["source"] == "lemmy_profile" and row["lang"] == "es"
    assert row["profile_url"] == "https://lemmy.world/u/carol"
    assert row["permission_status"] in ("SELF_PUBLISHED", "INVITED_INQUIRIES")


# ------------------------------------------------------------------ gates + helpers
def test_network_error_is_reported_not_swallowed(tmp_path):
    ppl = _store(tmp_path)

    def get_json(url):
        raise OSError("connection reset")

    s = pp.scan_bluesky(ppl, cfg=_cfg(queries={"bluesky": [{"q": "x"}]}),
                        get_json=get_json, sleeper=lambda *_: None)
    assert s["errors"] == 1 and s["error_notes"], s
    assert s["accepted"] == 0


def test_disabled_config_does_no_network(tmp_path):
    ppl = _store(tmp_path)

    def boom(url):
        raise AssertionError("network must not be touched when disabled")

    out = pp.scan_all(ppl, cfg={"enabled": False}, get_json=boom, sleeper=lambda *_: None)
    assert all("disabled by config" in (r.get("skipped") or "") for r in out), out


def test_need_shaped_and_org_helpers():
    assert pp.need_shaped("I lost my job and need health insurance")
    assert not pp.need_shaped("New marketplace rules announced today")
    assert not pp.need_shaped("COBRA coverage explained for employers")
    # live-verified false positives from the first production scan: mentioning coverage is
    # not needing it, and an already-covered statement is not a prospect
    assert not pp.need_shaped(
        "Note that I'm not saying all studios are bad, but the benefits of being in a studio "
        "in terms of extra stuff freelancers don't get are quite limited, in places where we "
        "don't need health insurance.")
    assert not pp.need_shaped(
        "I know I'm lucky that my contracting plate is full, and that I have options for "
        "health insurance that most self-employed folks don't have.")
    # a real Spanish need still passes
    assert pp.need_shaped("Perdi mi trabajo y necesito seguro medico para mi familia")
    assert pp.org_shaped("Sunshine Insurance Agency") and not pp.org_shaped("just a nurse in Miami")
    assert "email me at a@b.com" in pp.evidence_phrase("x email me at a@b.com y", "a@b.com")
    assert pp.evidence_phrase("nothing here", "a@b.com") == ""


def test_yield_summary_splits_post_and_profile(tmp_path):
    ppl = _store(tmp_path)
    get = _bsky_getter([_post()], {"did:plc:alice": {"description": "email alice@example.com"}})
    pp.scan_bluesky(ppl, cfg=_cfg(), get_json=get, sleeper=lambda *_: None)
    y = pp.yield_summary(ppl)
    assert y["profile_contacts_total"] == 1 and y["profile_contacts_unique"] == 1, y
    assert y["by_source"][0]["contact_origin"] == "profile"
