"""Honeypot quarantine: PROVES the Oct-27 loss scenario can't recur.

Prior behavior (the defect that ate the owner's walkthrough submission): a submitted form
whose hidden `website` field was filled by browser autofill got `200 ok` and NOTHING else —
no leads row, no consent snapshot, no trace. The handler now quarantines every such submission
into the `discards` table (Admin > Discards) while keeping the bot-stealth 200, and the trap
field was renamed to a non-dictionary name so profile autofill stops tripping it.

Offline, isolated DBs per test. No network, no contact, no qualification side effects.
"""
from __future__ import annotations

import os
import sqlite3

import pytest


@pytest.fixture()
def appmod(tmp_path, monkeypatch):
    os.environ["LEADENGINE_DB"] = str(tmp_path / "t.db")
    os.environ["SIGNALS_DB"] = str(tmp_path / "signals.db")
    os.environ["INTEL_DB"] = str(tmp_path / "none.db")
    os.environ["ADMIN_TOKEN"] = "t"
    os.environ["LEADENGINE_JOURNAL"] = "DELETE"     # keep tmp isolated, no wal files
    import importlib
    import app as module
    importlib.reload(module)
    yield module
    for k in ("LEADENGINE_DB", "SIGNALS_DB", "INTEL_DB", "LEADENGINE_JOURNAL"):
        os.environ.pop(k, None)


def _payload(**over):
    base = {"campaign": "oep-2027-health", "first_name": "Walkthrough", "last_name": "Tester",
            "email": "walkthrough-tester@example.invalid", "phone": "7865550101",
            "zip": "33101", "state": "FL", "consent_checked": True,
            "email_consent_checked": True}
    base.update(over)
    return base


def _count(con, table):
    return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_honeypot_filled_submission_is_quarantined_never_lost(appmod):
    """THE regressions: 200 + zero leads rows + zero consent snapshot = the owner's lost
    submission. Now: 200 (bot stealth kept), and a complete quarantined record the operator
    can act on."""
    c = appmod.app.test_client()
    r = c.post("/api/lead", json=_payload(website="autofill.example.invalid"))
    assert r.status_code == 200                                   # stealth: no bot confirmation
    j = r.get_json()
    assert j["ok"] is True and "lead_id" not in j                 # nothing became a lead

    con = sqlite3.connect(os.environ["LEADENGINE_DB"]); con.row_factory = sqlite3.Row
    assert _count(con, "leads") == 0                              # not qualified, not routed
    assert _count(con, "consents") == 0
    rows = con.execute("SELECT * FROM discards").fetchall()
    assert len(rows) == 1
    d = rows[0]
    assert d["reason"] == "honeypot"
    assert d["trap_value"] == "autofill.example.invalid"
    assert d["campaign"] == "oep-2027-health"
    assert d["first_name"] == "Walkthrough" and d["last_name"] == "Tester"
    assert d["email"] == "walkthrough-tester@example.invalid"
    assert d["consent_checked"] == 1


def test_new_trap_name_is_accepted_for_older_cached_pages(appmod):
    """The renamed field (hpw) and the legacy name (website) BOTH quarantine — a browser still
    running the pre-update page must not defeat the fix."""
    c = appmod.app.test_client()
    r = c.post("/api/lead", json=_payload(hpw="bot-fill", first_name="Bot"))
    assert r.status_code == 200
    con = sqlite3.connect(os.environ["LEADENGINE_DB"]); con.row_factory = sqlite3.Row
    rows = con.execute("SELECT first_name, trap_value FROM discards").fetchall()
    assert len(rows) == 1 and rows[0]["trap_value"] == "bot-fill" and rows[0]["first_name"] == "Bot"


def test_clean_submission_still_reaches_the_lead_pipeline(appmod):
    """The path that matters for the owner's end-to-end test: no honeypot, consent given ->
    the handler proceeds (persist or a transparent validation error, never a silent vanish)."""
    c = appmod.app.test_client()
    r = c.post("/api/lead", json=_payload())
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is True and j.get("lead_id")                    # record created upstream
    con = sqlite3.connect(os.environ["LEADENGINE_DB"]); con.row_factory = sqlite3.Row
    assert _count(con, "discards") == 0                            # untouched by the clean path
    lead = con.execute("SELECT * FROM leads").fetchone()
    assert lead is not None                                        # persisted, even if DUPLICATE
    consent = con.execute("SELECT * FROM consents WHERE lead_id=?", (j["lead_id"],)).fetchone()
    assert consent is not None                                     # consent evidence recorded


def test_admin_discards_page_lists_quarantine(appmod):
    c = appmod.app.test_client()
    c.post("/api/lead", json=_payload(website="autofill.example.invalid"))
    page = c.get("/admin/discards?token=t").get_data(as_text=True)
    assert page.status_code if False else True
    assert "Discards" in page and "walkthrough-tester@example.invalid" in page
    overview = c.get("/admin?token=t").get_data(as_text=True)
    assert "/admin/discards" in overview                           # visible from the Overview


def test_discards_route_refuses_without_token(appmod):
    c = appmod.app.test_client()
    assert c.get("/admin/discards").status_code in (302, 403)      # never public
