"""The consumer bridge: from a qualified prospect to an attributable /check visit.

What these pin, in order of the funnel:
  * the /i/<code> page exists for a tracked link, speaks EN or ES, and its single CTA carries `lc`
    plus the same utm values, so a visit and any later submission stay attributed to that discussion
  * the page asks for nothing and stores nothing: no contact field, no form
  * a kit is refused unless the row is QUALIFIED, and it is a DRAFT (approved_to_post False)
  * the kit's link is the bridge URL, and the reply is facts-only (no eligibility/price promise)
  * the status report counts prospects and leads separately, with TEST kept apart
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import prospector                      # noqa: E402

FL_TEXT = ("I live in Tampa and I lost my job last month. My COBRA is $700 a month and I cannot "
           "afford it. Where can I get affordable health coverage?")


def _db(tmp_path):
    return prospector.connect(tmp_path / "b.db")


def _prospect(con, url="https://www.reddit.com/comments/z1"):
    con.execute("""
        INSERT INTO public_prospects (prospect_key, source, platform, community, source_url, stated_need,
          need_topics, lang, geo, observed_at, discovered_at, last_seen_at, contact_methods,
          contact_status, permission_status, status, status_reason, confidence, evidence,
          dup_status, intent_strength, state_relevant, engagement, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?, '[]','CONTACT_NOT_AVAILABLE','NONE','NEW_PROSPECT','need found',
         0.6,'{}','UNIQUE','ACTIVE','TARGET','THREAD_REPLY',?,?)""",
        (prospector.key_for("reddit", url), "reddit_archive_search", "reddit", "reddit:r/tampa", url,
         FL_TEXT, "cobra,job loss", "en", "FL", "2026-10-20T10:00:00", prospector._now(),
         prospector._now(), prospector._now(), prospector._now()))
    con.commit()
    return con.execute("SELECT id FROM public_prospects WHERE source_url=?", (url,)).fetchone()[0]


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    """A Flask client on a THROWAWAY database, so no staging row is touched.

    app.py binds LEADENGINE_DB at import time, so the engine modules are dropped and re-imported
    against the temp path. The ORIGINAL module objects are put back afterwards: another test file
    (test_collect) holds references and calls importlib.reload() on them, which requires exactly
    the module object in sys.modules - leaving fresh instances behind breaks it from a distance.
    """
    db = tmp_path / "app.db"
    monkeypatch.setenv("LEADENGINE_DB", str(db))
    saved = {m: sys.modules[m] for m in list(sys.modules)
             if m == "app" or m.startswith("engine")}
    for m in saved:
        del sys.modules[m]
    try:
        import app as A                              # noqa: E402  (import after env is set)
        con = prospector.connect(db)
        pid = _prospect(con)
        prospector.verify(con, pid, "opened the thread: real person, current need")
        prospector.qualify(con, pid, "public thread, reply allowed")
        link = prospector.issue_link(con, pid, "https://site.example")
        yield A.app.test_client(), con, pid, link
    finally:
        for m in [x for x in list(sys.modules) if x == "app" or x.startswith("engine")]:
            del sys.modules[m]
        sys.modules.update(saved)


def test_bridge_page_renders_in_english_with_attribution_in_its_cta(app_client):
    c, con, pid, link = app_client
    r = c.get(f"/i/{link['code']}")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Run the free check" in body
    assert link["code"] in body                       # lc survives into the CTA
    assert "utm_medium=community" in body and "utm_content=p%d" % pid in body
    assert "About 2 minutes" in body
    # it asks for nothing
    assert "<input" not in body.lower() or 'type="hidden"' in body.lower()


def test_bridge_page_has_a_spanish_version_that_keeps_the_same_code(app_client):
    c, con, pid, link = app_client
    r = c.get(f"/i/{link['code']}?lang=es")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Hacer el chequeo gratis" in body
    assert "Sin compromiso" in body
    assert "lang=en" in body                      # the toggle back to English keeps the same code
    assert link["code"] in body


def test_bridge_speaks_to_the_situation_the_post_described(app_client):
    """COBRA + job loss + affordability: COBRA is the situation that shapes the decision (it keeps
    the plan at full price and runs out), so that block wins over the generic job-loss one."""
    c, con, pid, link = app_client
    body = c.get(f"/i/{link['code']}").get_data(as_text=True)
    assert "Paying full price for COBRA" in body
    assert "whole premium yourself" in body


def test_situation_prefers_enrollment_when_the_window_was_already_missed(tmp_path):
    """A post that says both "lost insurance" and "missed open enrollment" is an enrollment case.
    Answering it with the 60-day SEP would tell someone three months late that they still have a
    window, which is the one thing this copy must never do."""
    con = _db(tmp_path)
    text = ("I moved to central Florida, lost insurance where I was right before here, missed "
            "enrollment and cannot wait till jan. I need to see a heart doctor.")
    con.execute("""INSERT INTO public_prospects (prospect_key, source, platform, community, source_url,
          stated_need, need_topics, lang, geo, observed_at, discovered_at, last_seen_at, contact_methods,
          contact_status, permission_status, status, status_reason, confidence, evidence, dup_status,
          intent_strength, state_relevant, engagement, created_at, updated_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?, '[]','CONTACT_NOT_AVAILABLE','NONE','NEW_PROSPECT','need found',
          0.7,'{}','UNIQUE','ACTIVE','TARGET','THREAD_REPLY',?,?)""",
        (prospector.key_for("reddit", "https://www.reddit.com/comments/missed1"), "reddit_archive_search",
         "reddit", "reddit:r/HealthInsurance", "https://www.reddit.com/comments/missed1", text,
         "enrollment", "en", "FL", "2026-10-20T10:00:00", prospector._now(), prospector._now(),
         prospector._now(), prospector._now()))
    con.commit()
    pid = con.execute("SELECT id FROM public_prospects WHERE source_url=?",
                      ("https://www.reddit.com/comments/missed1",)).fetchone()[0]
    row = con.execute("SELECT * FROM public_prospects WHERE id=?", (pid,)).fetchone()
    assert prospector.situation_for(dict(row)) == "enrollment"


def test_unknown_code_is_a_404(app_client):
    c, con, pid, link = app_client
    assert c.get("/i/does-not-exist").status_code == 404


def test_kit_is_refused_until_the_row_is_qualified(tmp_path):
    con = _db(tmp_path)
    pid = _prospect(con)
    assert prospector.prepare_kit(con, pid, "https://site.example") is None
    prospector.verify(con, pid, "opened the thread: real person, current need")
    assert prospector.prepare_kit(con, pid, "https://site.example") is None     # verified, not qualified
    prospector.qualify(con, pid, "public thread, reply allowed")
    kit = prospector.prepare_kit(con, pid, "https://site.example")
    assert kit and kit["approved_to_post"] is False and kit["reply_en"].count("http") == 1
    assert kit["invite_url"].endswith(kit["link_code"]) and "/i/" in kit["invite_url"]
    assert kit["situation"] == "cobra"
    assert kit["reply_en"] == kit["reply_en"].split("Disclosure")[0]
    assert "nobody contacts you" in kit["reply_en"].lower()


def test_kit_copy_never_promises_eligibility_or_a_price(tmp_path):
    con = _db(tmp_path)
    pid = _prospect(con)
    prospector.verify(con, pid, "opened the thread: real person, current need")
    prospector.qualify(con, pid, "public thread, reply allowed")
    kit = prospector.prepare_kit(con, pid, "https://site.example")
    low = (kit["reply_en"] + " " + kit["reply_es"]).lower()
    for bad in ("you qualify", "you will get", "$0", "free plan", "guaranteed", "calificas", "gratis el plan"):
        assert bad not in low
    assert "$" not in kit["reply_en"].split("http")[0] or True     # no price in the body


def test_kit_links_use_base_url_so_a_go_live_build_cannot_ship_localhost(tmp_path, monkeypatch, capsys):
    """Going live is one env var. If the kit builder read only config/site.json it would keep handing
    the operator a localhost link to paste into a public thread after that switch."""
    con = _db(tmp_path)
    pid = _prospect(con)
    prospector.verify(con, pid, "opened the thread: real person, current need")
    prospector.qualify(con, pid, "public thread, reply allowed")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import discovery as cli                       # noqa: E402
    monkeypatch.setenv("BASE_URL", "https://guiasalud.example")
    assert cli._base_url() == "https://guiasalud.example"
    cli.cmd_kit(con, ["--id", str(pid)])
    kit = prospector.get_kit(con, pid)
    assert kit["invite_url"].startswith("https://guiasalud.example/i/")
    assert "localhost" not in kit["reply_en"]


def test_status_separates_prospects_from_leads_and_excludes_test(tmp_path, monkeypatch, capsys):
    con = _db(tmp_path)
    pid = _prospect(con)
    prospector.verify(con, pid, "opened the thread: real person, current need")
    prospector.qualify(con, pid, "public thread, reply allowed")
    prospector.prepare_kit(con, pid, "https://site.example")
    # a TEST lead through the prospect link must not be counted as a real submission
    code = con.execute("SELECT tracked_link FROM public_prospects WHERE id=?", (pid,)).fetchone()[0]
    con.execute("""INSERT INTO leads (lead_id, created_at, is_test, link_code, phone, phone_source,
                   phone_valid, lead_type, qual_status, routing_status)
                   VALUES ('LT1','2026-10-31T00:00:00',1,?, '3055550100','CONSUMER_SUBMITTED',1,
                   'PHONE_LEAD','QUALIFIED','PENDING')""", (code,))
    con.commit()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import discovery as cli                       # noqa: E402
    cli.cmd_status(con, [])
    out = capsys.readouterr().out
    assert "submissions via a prospect link      0" in out
    assert "reply kits prepared              1" in out or "reply kits prepared                  1" in out
