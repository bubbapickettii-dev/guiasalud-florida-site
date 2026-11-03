"""Focused tests for the consumer-prospect contact pipeline (engine/prospect_contacts.py).

Boundaries under test:
  1. Extraction is evidence-only: plain + obfuscated published contacts come out; .gov/.edu,
     system mailboxes, uninvited numbers, dates/prices and negated verbs never do.
  2. A post is never consent: no automated path reaches PERMISSION_VERIFIED; verify requires
     an operator note, and a row without a contact stays CONTACT_NOT_AVAILABLE forever.
  3. Dedupe: the same post collapses by person_key; the same PUBLISHED email/phone in a
     different post marks the later row DUPLICATE with a pointer, keeping both source URLs.
  4. Persona separation passes through: nothing stored here is the lead pipeline, and no
     module here imports route/notify/billing/requests/twilio.
"""
import ast
import importlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ROOT = Path(__file__).resolve().parents[1]


def make_modules(tmp):
    os.environ["SIGNALS_DB"] = str(tmp / "signals_test.db")
    os.environ["PEOPLE_DB"] = str(tmp / "people_test.db")
    import engine.signals as sig
    import engine.prospect_contacts as pc
    return importlib.reload(sig), importlib.reload(pc)


def _seed_signal(con, url, title, snippet):
    """Insert one reddit INTEREST-shaped signal row into the signal store under test."""
    from datetime import datetime, timezone
    import engine.signals as sig
    row = {"source": "reddit", "community": "https://www.reddit.com/r/HealthInsurance",
           "title": title, "snippet": snippet, "url": url,
           "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "collection_method": "public_api", "access_status": "public_endpoint"}
    return sig.ingest(con, [row], "reddit")


def test_extract_published_contacts_only(tmp_path):
    sig, pc = make_modules(tmp_path)
    e = pc.extract_contacts("Lost my employer coverage. Contact me at Jane.Doe@Gmail.com "
                            "or (305) 555-0142 if you can help compare plans.")
    assert e["emails"] == ["jane.doe@gmail.com"]           # normalized lower-case
    assert e["phones"] == ["(305) 555-0142"]
    assert e["contact_status"] == "CONTACT_AVAILABLE"
    e2 = pc.extract_contacts("Escribeme a maria.g at hotmail dot com si puedes ayudar.")
    assert e2["emails"] == ["maria.g@hotmail.com"]
    # restricted: system mailbox + .gov domain rejected; the office phone IS published
    # here ("Call the office at ..."), so it extracts — the negation guard only rejects
    # numbers whose every nearby verb is negated (covered two asserts down)
    e3 = pc.extract_contacts("Call the office at support@health.gov or 305-555-9999.")
    assert e3["emails"] == [] and e3["phones"] == ["305-555-9999"]
    # a purely negated mention never becomes a contact
    e5 = pc.extract_contacts("I need coverage but nobody should call me at 305-555-2222.")
    assert e5["phones"] == [] and e5["contact_status"] == "CONTACT_NOT_AVAILABLE"
    # dates/prices never become phones
    e4 = pc.extract_contacts("Coverage ended $600 per month and ends 12/31 for my 33101 zip.")
    assert e4["phones"] == []


def test_permission_evidence_shapes(tmp_path):
    sig, pc = make_modules(tmp_path)
    assert pc.permission_evidence("Feel free to reach me at a.b@x.com") != ""
    assert pc.permission_evidence("Just my random thoughts on deductibles.") == ""


def test_scan_ladder_dedupe_and_gates(tmp_path):
    sig, pc = make_modules(tmp_path)
    scon = sig.connect(tmp_path / "s.db")
    ppl = pc.connect(tmp_path / "p.db")
    # 1: a personal need WITH published contact -> DISCOVERED...CONTACT_FOUND, never further
    _seed_signal(scon, "https://www.reddit.com/r/HI/comments/a1/",
                 "lost job coverage, need new plan",
                 "I lost my job and my employer insurance. Need a new plan. "
                 "My email is jane.d@gmail.com if anyone can help.")
    res = pc.scan_signals(scon, ppl)
    assert res["added"] == 1
    row = [dict(r) for r in ppl.execute("SELECT * FROM prospect_people")][0]
    assert row["stage"] == "CONTACT_FOUND" and row["contact_status"] == "CONTACT_AVAILABLE"
    assert row["permission_status"] != "OPERATOR_VERIFIED"   # a scraper never verifies
    assert row["stage"] != "PERMISSION_VERIFIED"             # that's an operator rung only
    assert row["contact_evidence"].strip() != ""             # exact phrase recorded
    assert row["contact_evidence"] != "not provided"
    # 2: same post re-scanned -> skipped (person_key dedupe)
    assert pc.scan_signals(scon, ppl)["added"] == 0
    # 3: another post publishing the SAME email -> DUPLICATE with pointer
    _seed_signal(scon, "https://www.reddit.com/r/Insurance/comments/b2/",
                 "still shopping for coverage after cobra ends",
                 "I need affordable coverage too - comparing cobra and marketplace plans. "
                 "Contact me at Jane.D@gmail.com.")
    r2 = pc.scan_signals(scon, ppl)
    assert r2["added"] == 1
    row2 = [dict(r) for r in ppl.execute(
        "SELECT * FROM prospect_people WHERE dup_status='DUPLICATE'")][0]
    assert row2["dup_of"] == row["person_key"]
    # 4: news/irrelevant/enrolled rows skipped even if they carry a contact form
    _seed_signal(scon, "https://www.reddit.com/r/news/comments/c3/",
                 "open enrollment starts",
                 "Here's how 2027 ACA options work; questions to news@daily.com")
    before = ppl.execute("SELECT COUNT(*) FROM prospect_people").fetchone()[0]
    pc.scan_signals(scon, ppl)
    assert ppl.execute("SELECT COUNT(*) FROM prospect_people").fetchone()[0] == before
    # 5: row with no published contact stays CONTACT_NOT_AVAILABLE
    _seed_signal(scon, "https://www.reddit.com/r/HI/comments/d4/",
                 "how do people afford insurance",
                 "I need help choosing a plan for my family but do not know where to start.")
    pc.scan_signals(scon, ppl)
    row4 = [dict(r) for r in ppl.execute(
        "SELECT * FROM prospect_people WHERE contact_status='CONTACT_NOT_AVAILABLE'")]
    assert row4 if row4 else False
    if row4:
        assert row4[0]["stage"] == "DISCOVERED"
        assert json.loads(row4[0]["contact_methods"] or "[]") == []   # nothing published -> nothing stored


def test_verified_and_qualified_are_operator_only(tmp_path):
    sig, pc = make_modules(tmp_path)
    scon = sig.connect(tmp_path / "s.db")
    ppl = pc.connect(tmp_path / "p.db")
    _seed_signal(scon, "https://www.reddit.com/r/HI/comments/e5/",
                 "layoff, need help",
                 "I lost my job coverage and need a plan. Contact me at k.m@outlook.com.")
    pc.scan_signals(scon, ppl)
    pid = ppl.execute("SELECT id FROM prospect_people").fetchone()[0]
    # refuses without a real operator note
    assert not pc.verify_permission(ppl, pid, "")
    assert not pc.verify_permission(ppl, pid, "short")
    assert pc.verify_permission(ppl, pid, "opened the permalink: the author invites inquiries")
    row = dict(ppl.execute("SELECT * FROM prospect_people").fetchone())
    assert row["stage"] == "PERMISSION_VERIFIED" and row["permission_status"] == "OPERATOR_VERIFIED"
    # a no-contact row can never be verified or qualified
    _seed_signal(scon, "https://www.reddit.com/r/HI/comments/f6/",
                 "same need, no contact given",
                 "I need help choosing a plan for my family; where do I start?")
    pc.scan_signals(scon, ppl)
    pid2 = ppl.execute(
        "SELECT id FROM prospect_people WHERE contact_status='CONTACT_NOT_AVAILABLE'").fetchone()[0]
    assert not pc.verify_permission(ppl, pid2, "anything the operator writes down here")
    assert not pc.decide(ppl, pid2, "QUALIFIED", "cannot qualify without a contact")
    # QUALIFIED needs a reason on a valid row
    assert not pc.decide(ppl, pid, "QUALIFIED", "")
    assert pc.decide(ppl, pid, "QUALIFIED", "verified: need is current-health-ACA specific")
    row = dict(ppl.execute("SELECT * FROM prospect_people WHERE id=?", (pid,)).fetchone())
    assert row["stage"] == "QUALIFIED"
    # invalid stage refused; PERMISSION_VERIFIED only via verify_permission
    assert not pc.set_stage(ppl, pid, "PERMISSION_VERIFIED")
    assert pc.set_stage(ppl, pid, "READY_FOR_CONSUMER_ONBOARDING")


def test_wide_excerpt_contacts_are_minable(tmp_path):
    """A contact published only in the TAIL of a long comment (beyond the 240-char
    display snippet) must still reach the miner: ingest stores a wider excerpt for rows
    that carry a full body."""
    from datetime import datetime, timezone
    sig, pc = make_modules(tmp_path)
    scon = sig.connect(tmp_path / "s.db")
    ppl = pc.connect(tmp_path / "p.db")
    head = "I was laid off in March and my employer coverage ends next month. "
    filler = ("I have been comparing premiums and deductibles for weeks and honestly the "
              "costs feel impossible while I sort out unemployment paperwork. ") * 2
    tail = "Congrats if you read this far — text me at 305-555-7788 if you can help."
    row = {"source": "reddit_comments", "community": "https://www.reddit.com/r/HealthInsurance",
           "title": "laid off, coverage ends", "snippet": head, "body": head + filler + tail,
           "url": "https://www.reddit.com/r/HI/comments/g7/_/x",
           "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    assert sig.ingest(scon, [row], "reddit_comments")["added"] == 1
    stored = [dict(r) for r in scon.execute("SELECT snippet FROM signals")][0]["snippet"]
    assert "305-555-7788" in stored
    res = pc.scan_signals(scon, ppl)
    assert res["added"] == 1
    prow = [dict(r) for r in ppl.execute("SELECT * FROM prospect_people")][0]
    assert prow["stage"] == "CONTACT_FOUND"
    methods = json.loads(prow["contact_methods"] or "[]")
    assert any(m["kind"] == "phone" and "555-7788" in m["value"] for m in methods)


def test_stale_new_rows_never_become_prospects(tmp_path):
    """Recency gate: an old post (beyond the 30-day window) that was NOT already stored
    is skipped as a NEW prospect — current needs only. Already-stored rows are unaffected."""
    from datetime import datetime, timedelta, timezone
    sig, pc = make_modules(tmp_path)
    scon = sig.connect(tmp_path / "s.db")
    ppl = pc.connect(tmp_path / "p.db")
    old = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat(timespec="seconds")
    row = {"source": "reddit", "community": "r/HI", "title": "need coverage",
           "snippet": "I need health insurance for my family but the premiums are scary.",
           "url": "https://www.reddit.com/r/HI/comments/h8/", "observed_at": old}
    sig.ingest(scon, [row], "reddit")
    res = pc.scan_signals(scon, ppl)
    assert res["added"] == 0 and res["stale_skipped"] == 1
    assert ppl.execute("SELECT COUNT(*) FROM prospect_people").fetchone()[0] == 0


def test_no_lead_pipeline_imports(tmp_path):
    """AST boundary: the people module never imports lead/contact machinery, and the
    collect CLI stays collection-side only (no route/notify/billing)."""
    for path, banned in ((ROOT / "engine" / "prospect_contacts.py",
                          {"requests", "smtplib", "twilio", "engine.route", "engine.notify",
                           "engine.billing"}),
                         (ROOT / "tools" / "collect.py",
                          {"engine.route", "engine.notify", "engine.billing"})):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module)
        assert not (mods & banned), f"{path.name} imports a banned module: {mods & banned}"
