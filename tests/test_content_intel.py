"""Content-intel tests: reviewed signals become EN/ES draft briefs; nothing connects."""
import importlib, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ROOT = Path(__file__).resolve().parents[1]


def _setup(tmp):
    os.environ["SIGNALS_DB"] = str(tmp / "signals.db")
    os.environ["LEADENGINE_DB"] = str(tmp / "t.db")
    os.environ["ADMIN_TOKEN"] = "t"
    os.environ["INTEL_DB"] = str(tmp / "none.db")
    import engine.signals as sig
    importlib.reload(sig)
    import engine.content_intel as ci
    importlib.reload(ci)
    import app as appmod
    importlib.reload(appmod)
    return sig, ci, appmod


def test_unreviewed_signals_never_become_ideas(tmp_path):
    sig, ci, _ = _setup(tmp_path)
    con = sig.connect(tmp_path / "signals.db")
    sig.ingest(con, [{"title": "cobra too expensive what to do",
                      "url": "https://reddit.com/r/HealthInsurance/comments/z1/", "lang_hint": "en"}])
    assert ci.ideas(con) == []                      # still NEW - no draft without review
    sid = con.execute("SELECT id FROM signals").fetchone()[0]
    sig.review(con, sid, "REVIEWED", "answers learn__cobra; feeds X021")
    ideas = ci.ideas(con)
    assert len(ideas) == 1 and ideas[0]["topic"] == "cobra"
    b = ideas[0]
    assert b["state"] == "NEEDS_MORE_EVIDENCE"      # 1 signal < 3
    assert b["draft_en"]["title"] and b["draft_es"]["title"]
    assert "/check?a=life_change" in b["draft_en"]["cta"]
    assert "chequeo" in b["draft_es"]["cta"].lower()


def test_ideas_map_angle_and_reference_existing_pages(tmp_path):
    sig, ci, _ = _setup(tmp_path)
    con = sig.connect(tmp_path / "signals.db")
    # three reviewed self-employed signals -> READY_TO_DRAFT, angle= self_employed
    for i, (title, url) in enumerate([
            ("self employed health insurance options?", "https://reddit.com/r/freelance/comments/a1/"),
            ("1099 workers how do you get coverage", "https://reddit.com/r/gigwork/comments/a2/"),
            ("freelancer needs a plan", "https://www.reddit.com/r/freelance/comments/a3/")]):
        sig.ingest(con, [{"title": title, "url": url, "lang_hint": "en"}])
        sid = con.execute("SELECT id FROM signals WHERE url=?", (url,)).fetchone()[0]
        sig.review(con, sid, "REVIEWED", "idea evidence")
    ideas = ci.ideas(con)
    assert ideas and ideas[0]["topic"] == "self_employed" and ideas[0]["angle"] == "self_employed"
    assert ideas[0]["state"] == "READY_TO_DRAFT"
    assert "health-insurance-self-employed" in ideas[0]["existing_en_pages"]
    # evidence sample links stay public URLs; no contact fields exist anywhere in briefs
    blob = repr(ideas)
    assert "phone_e164" not in blob and "mailto:" not in blob


def test_admin_ideas_page(tmp_path):
    sig, ci, appmod = _setup(tmp_path)
    c = appmod.app.test_client()
    page = c.get("/admin/content-ideas?token=t").get_data(as_text=True)
    assert "draft" in page or "No reviewed signals yet" in page
    # nav exposes the new page
    base = c.get("/admin?token=t").get_data(as_text=True)
    assert "/admin/content-ideas" in base
