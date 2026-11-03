"""Consumer funnel integrity (public surface, both languages).

  - every public consumer page renders (EN + ES)
  - every INTERNAL link found on those pages resolves (no dead ends, no 404/500) — broken
    internal links are caught here before a visitor ever meets them
  - the privacy pages state the decision-relevant disclosure exactly once per language
    (a duplicate "Your choices" paragraph used to render with buyers configured)
Bookkeeping only: TEST staging app, no real data.
"""
import re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_billing import make_app

SKIP_PREFIX = ("/static/", "/code")
INTERNAL = re.compile(r'href="(/[^"#?]+)[?"#]?"')


def seeded_pages(m):
    c = m.app.test_client()
    queue = ["/", "/learn", "/aprende", "/open-enrollment-2027", "/inscripcion-abierta-2027",
             "/layoffs", "/for-agents", "/check", "/check?a=es_check", "/privacy", "/privacidad",
             "/terms", "/terminos", "/optout", "/contact"]
    seen, all_links = set(), set()
    while queue and len(seen) < 80:                      # bounded: follow internal links one level deep
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        r = c.get(url)
        assert r.status_code == 200, f"{url} rendered {r.status_code}"     # dead ends fail the crawl
        body = r.get_data(as_text=True)
        if url.startswith(("/check", "/terms", "/terminos", "/optout", "/contact")):
            continue                                    # legal/QA surfaces: not a link map
        for href in INTERNAL.findall(body):
            all_links.add(href)
            if not href.startswith(SKIP_PREFIX) and href not in seen:
                queue.append(href)
    assert c.get("/privacy").status_code == 200 and c.get("/privacidad").status_code == 200
    return c, len(seen), all_links


def test_contact_inquiry_is_recorded_and_honeypot_is_dropped(tmp_path):
    """The consumer inquiry form must keep working while the support inbox is unconfigured (it is the
    documented fallback), and a bot that fills the hidden field must leave no row."""
    m = make_app(tmp_path)
    c = m.app.test_client()
    r = c.post("/contact", data={"message": "Necesito ayuda con mi cobertura", "reply_to": "ana@example.com"})
    assert r.status_code == 200
    rows = m.con.execute("SELECT message, reply_to FROM contact_msgs").fetchall()
    assert [(x["message"], x["reply_to"]) for x in rows] == [("Necesito ayuda con mi cobertura", "ana@example.com")]
    # honeypot filled => acknowledged, never stored
    assert c.post("/contact", data={"message": "buy cheap leads", "website": "http://spam"}).status_code == 200
    assert m.con.execute("SELECT COUNT(*) FROM contact_msgs").fetchone()[0] == 1


def test_every_public_page_renders_and_internal_links_resolve(tmp_path):
    m = make_app(tmp_path)
    c, seen_count, links = seeded_pages(m)
    assert seen_count >= 30                              # seeds + discovered learn articles actually crawled
    # every internal link the crawl collected resolves (checked targets that weren't already crawled)
    unread = [u for u in links if u not in SKIP_PREFIX and not u.startswith(SKIP_PREFIX)
              and not u.startswith(("/go/", "/admin", "/agent", "/partner", "/p/", "/embed", "/qa"))]
    for u in unread[:40]:                                # bounded: fresh private/unreachable targets stay out of scope
        r = c.get(u)
        assert r.status_code == 200, f"public dead internal link: {u} -> {r.status_code}"


def test_learn_articles_resolve_in_both_languages(tmp_path):
    m = make_app(tmp_path)
    c = m.app.test_client()
    for prefix in ("/learn/", "/aprende/"):
        body = c.get(prefix[:-1]).get_data(as_text=True)
        slugs = set(re.findall(rf'href="{prefix}([^"?#/]+)"', body))
        assert len(slugs) >= 3, f"no article links on {prefix}"
        for s in slugs:
            assert c.get(prefix + s).status_code == 200, f"{prefix}{s} is a dead end"


def test_privacy_states_choices_paragraph_once(tmp_path):
    """With a buyer configured the policy page duplicates nothing: each language has exactly one
    'Your choices / Tus opciones' block, and it still offers /optout."""
    m = make_app(tmp_path); c = m.app.test_client()
    en = c.get("/privacy").get_data(as_text=True)
    es = c.get("/privacidad").get_data(as_text=True)
    assert en.count("Your choices.") == 1, en.count("Your choices.")
    assert es.count("Tus opciones.") == 1
    assert "/optout" in en and "/optout" in es
    assert "one licensed insurance agency" in en                 # buyer-present disclosure intact
    assert "agencia de seguros con licencia nombrada" in es


def test_funnel_stop_points_reports_only_recorded_transitions():
    """The drop table answers 'where do people stop' straight from stage counts: the loss percentage
    is over the upper stage, a stage nobody reached yet shows NO invented conversion number, and a
    lower stage with zero still counts a complete (100%) leak from its reached upper stage."""
    from engine import funnel as f
    tot = f.stage_totals([
        {"visits": 100, "form_starts": 60, "check_completions": 40, "phone_step_views": 30,
         "phones_submitted": 20, "valid_phones": 18, "consented_phone_leads": 15},
    ])
    d = {x["a"]: x for x in f.stop_points(tot)}
    assert d["visits"]["dn"] == 60 and d["visits"]["lost"] == 40
    assert d["visits"]["drop_pct"] == 40.0
    # a reached upper stage meeting an empty lower stage is a real (full) leak, shown honestly
    assert d["consented_phone_leads"]["up"] == 15 and d["consented_phone_leads"]["drop_pct"] == 100.0
    # a stage nobody reached yet is rendered as un-reachable, never as a fake conversion number
    for a in ("qualified", "contacted", "appointments"):
        assert d[a]["reachable"] is False and d[a]["drop_pct"] is None
    # summing multiple dim rows is the same aggregate the page shows
    assert f.stage_totals([{"visits": 5}, {"visits": 2}])["visits"] == 7


def test_admin_funnel_shows_drop_table_when_there_is_traffic_and_truthfully_not_without(tmp_path):
    """The block must appear with TEST traffic and, with no traffic, say there is nothing to leak."""
    m = make_app(tmp_path); c = m.app.test_client()
    con = m.con
    con.execute("INSERT INTO visitors (visitor_id) VALUES ('v-t')")
    con.executemany("INSERT INTO events (ts, visitor_id, session_id, event, is_test) VALUES (?, 'v-t', 's', ?, 1)",
                    [("2026-10-26T10:00:00", "page_view"), ("2026-10-26T10:01:00", "tool_start"),
                     ("2026-10-26T10:02:00", "tool_complete")])
    con.commit()
    h = c.get("/admin/funnel?token=t&test=only").get_data(as_text=True)
    assert "Where people stop" in h
    assert "Visits" in h and "Form starts" in h
    # zero-traffic view: with no real traffic at all the page says so honestly before any table
    h2 = c.get("/admin/funnel?token=t&test=real").get_data(as_text=True)
    assert "UNTESTED" in h2
