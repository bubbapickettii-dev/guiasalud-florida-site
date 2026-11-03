"""Admin > Sources: the arrival report an operator reads to see where real demand comes from.

The contract this file locks in:

  - the report is the ordered arrival funnel per acquisition source: visitors -> check started ->
    check completed -> contact submitted -> valid contact -> consent+phone -> qualified ->
    routing-eligible -> delivered
  - every number comes from rows that already exist (first-party events + the lead row). No second
    attribution system, no derived guesswork.
  - TEST rows never appear on the real tab and real rows never appear on the TEST tab
  - development traffic (loopback / private LAN origins) is never counted as a consumer, but it is
    still visibly separated in traffic_split instead of silently disappearing
  - the biggest drop per source is named, so "brought visits" and "brought requests" look different

Bookkeeping only: synthetic visitor/event/lead rows in a temp DB.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_billing import make_app            # noqa: E402
from engine import funnel                    # noqa: E402

TS = "2026-10-20T10:00:00"
PUBLIC = "https://guiasalud.example"


def visitor(con, vid, source="google", test=0, landing=PUBLIC + "/check", partner=None):
    con.execute("""INSERT INTO visitors (visitor_id, first_seen, last_seen, first_utm_source, partner_id,
                   landing_url, is_test) VALUES (?,?,?,?,?,?,?)""",
                (vid, TS, TS, source, partner, landing, test))


def event(con, vid, name, source="google", test=0, step=None, page=PUBLIC + "/check", partner=None):
    con.execute("""INSERT INTO events (ts, visitor_id, event, step, utm_source, partner_id, page_url, is_test)
                   VALUES (?,?,?,?,?,?,?,?)""", (TS, vid, name, step, source, partner, page, test))


def lead(con, lid, *, source="google", qual="QUALIFIED", routing="DELIVERED", valid=1, test=0,
         landing=PUBLIC + "/check", agent_status=None, price=None):
    con.execute("""INSERT INTO leads (lead_id, created_at, is_test, landing_url, utm_source, phone, phone_source,
                   phone_valid, lead_type, qual_status, routing_status, agent_status, price)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (lid, TS, test, landing, source, "3055550110" if valid else None,
                 "CONSUMER_SUBMITTED" if valid else None, valid,
                 "PHONE_LEAD" if valid else "EMAIL_ONLY_INQUIRY", qual, routing, agent_status, price))


def google_journey(con, n, source="google", test=0, landing=PUBLIC + "/check"):
    """n complete consumer journeys: visit, start, complete, submit phone, valid, consent+phone."""
    tag = "pub" if landing.startswith(PUBLIC) else landing.split("//", 1)[-1].split("/", 1)[0].replace(":", "_").replace(".", "_")
    for i in range(n):
        vid = f"v-{source}-{tag}-{test}-{i}"
        visitor(con, vid, source, test, landing)
        event(con, vid, "page_view", source, test, page=landing)
        event(con, vid, "tool_start", source, test, page=landing)
        event(con, vid, "tool_complete", source, test, page=landing)
        event(con, vid, "contact_submit", source, test, step="phone_submitted", page=landing)
        event(con, vid, "contact_submit", source, test, step="phone_valid", page=landing)
        lead(con, f"L-{source}-{tag}-{test}-{i}", source=source, test=test, landing=landing)


# ---------- the arrival funnel itself ----------

def test_arrival_funnel_counts_each_stage_per_source(tmp_path):
    m = make_app(tmp_path)
    google_journey(m.con, 3, "google")
    google_journey(m.con, 1, "facebook")                     # visits + everything, one journey
    for i in range(4):                                        # a source that brings visits but no requests
        visitor(m.con, f"v-tiktok-{i}", "tiktok")
        event(m.con, f"v-tiktok-{i}", "page_view", "tiktok")
    m.con.commit()

    rows = {r["k"]: r for r in funnel.source_table(m.con)}
    g, f, t = rows["google"], rows["facebook"], rows["tiktok"]
    assert (g["visits"], g["form_starts"], g["check_completions"]) == (3, 3, 3)
    assert (g["phones_submitted"], g["valid_phones"], g["consented_phone_leads"]) == (3, 3, 3)
    assert (g["qualified"], g["routing_eligible"], g["delivered"]) == (3, 3, 3)
    assert (f["visits"], f["delivered"]) == (1, 1)
    assert (t["visits"], t["consented_phone_leads"], t["qualified"]) == (4, 0, 0)
    # the source with visits and nothing else must say where it leaks, not look like a small google
    assert "Visitors → Check started" in t["worst_drop"]
    assert g["worst_drop"] == ""                             # a source with no drop is not accused of one


def test_stage_totals_add_up_across_sources(tmp_path):
    m = make_app(tmp_path)
    google_journey(m.con, 2, "google")
    google_journey(m.con, 1, "partner-qr")
    m.con.commit()
    rows = funnel.source_table(m.con)
    tot = funnel.stage_totals(rows)
    assert tot["visits"] == 3 and tot["consented_phone_leads"] == 3 and tot["delivered"] == 3
    assert all(tot[k] == 3 for k in ("form_starts", "check_completions", "qualified", "routing_eligible"))


def test_duplicates_and_unqualified_stay_out_of_qualified(tmp_path):
    m = make_app(tmp_path)
    google_journey(m.con, 2, "google")
    lead(m.con, "L-dup", source="google", qual="DUPLICATE", routing="NOT_ROUTABLE")
    lead(m.con, "L-unq", source="google", qual="UNQUALIFIED", routing="NOT_ROUTABLE")
    m.con.commit()
    g = {r["k"]: r for r in funnel.source_table(m.con)}["google"]
    assert g["qualified"] == 2 and g["routing_eligible"] == 2 and g["delivered"] == 2
    assert g["duplicates"] == 1


def test_routing_eligible_excludes_not_routable(tmp_path):
    m = make_app(tmp_path)
    google_journey(m.con, 1, "google")
    lead(m.con, "L-nr", source="google", qual="QUALIFIED", routing="NOT_ROUTABLE")
    m.con.commit()
    g = {r["k"]: r for r in funnel.source_table(m.con)}["google"]
    assert g["qualified"] == 2 and g["routing_eligible"] == 1 and g["delivered"] == 1


# ---------- real vs test vs internal ----------

def test_test_traffic_never_mixes_with_real(tmp_path):
    m = make_app(tmp_path)
    google_journey(m.con, 2, "google", test=0)
    google_journey(m.con, 5, "uat-demo", test=1)
    m.con.commit()

    real = {r["k"]: r for r in funnel.source_table(m.con, "real")}
    test = {r["k"]: r for r in funnel.source_table(m.con, "test")}
    assert set(real) == {"google"} and real["google"]["visits"] == 2
    assert set(test) == {"uat-demo"} and test["uat-demo"]["visits"] == 5

    split = funnel.traffic_split(m.con)
    assert split["real_visitors"] == 2 and split["test_visitors"] == 5
    assert split["real_leads"] == 2 and split["test_leads"] == 5


def test_local_dev_origin_is_not_a_consumer(tmp_path):
    m = make_app(tmp_path)
    google_journey(m.con, 2, "google")
    google_journey(m.con, 3, "google", landing="http://localhost:8000/check")   # the developer's own clicks
    google_journey(m.con, 1, "google", landing="http://192.168.1.20:8000/check")
    m.con.commit()

    g = {r["k"]: r for r in funnel.source_table(m.con)}["google"]
    assert g["visits"] == 2 and g["consented_phone_leads"] == 2 and g["delivered"] == 2
    split = funnel.traffic_split(m.con)
    assert split["internal_visitors"] == 4                    # counted as internal, never as real
    assert split["real_visitors"] == 2


# ---------- the page an operator actually opens ----------

def test_admin_sources_page_renders_both_tabs(tmp_path):
    m = make_app(tmp_path)
    google_journey(m.con, 2, "google")
    visitor(m.con, "v-tiktok-0", "tiktok")               # visits, never starts the check -> a real 0 further down
    event(m.con, "v-tiktok-0", "page_view", "tiktok")
    google_journey(m.con, 1, "uat-demo", test=1)
    m.con.commit()
    c = m.app.test_client()

    real = c.get("/admin/sources?token=t")
    assert real.status_code == 200
    html = real.get_data(as_text=True)
    assert "google" in html and "tiktok" in html
    assert "uat-demo" not in html
    for label in ("Visitors", "Check started", "Check completed", "Contact submitted", "Valid contact",
                  "Consent + phone", "Qualified", "Routing-eligible", "Delivered to agency"):
        assert label in html, label

    test_html = c.get("/admin/sources?token=t&test=1").get_data(as_text=True)
    assert "uat-demo" in test_html and "TEST DATA" in test_html


def test_admin_sources_requires_token(tmp_path):
    m = make_app(tmp_path)
    assert m.app.test_client().get("/admin/sources").status_code in (401, 403, 404)


# ---------- the link builder that makes a channel measurable ----------

def test_operator_created_link_carries_its_attribution_and_ignores_qa_clicks(tmp_path):
    """A channel is only measurable if the link that carries it is tagged. The admin link builder is how
    an operator tags a subreddit answer, a flyer QR or an org newsletter without hand-editing URLs."""
    import re
    m = make_app(tmp_path)
    c = m.app.test_client()
    r = c.post("/admin/channels/new?token=t",
               data={"label": "Miami library flyer QR", "utm_source": "org", "utm_medium": "partner",
                     "utm_content": "flyer-a", "lang": "es", "landing": "/aprende"})
    code = re.search(r"made=([\w-]+)", r.headers["Location"]).group(1)
    row = dict(m.con.execute("SELECT * FROM links WHERE code=?", (code,)).fetchone())
    assert (row["utm_source"], row["utm_medium"], row["utm_content"]) == ("org", "partner", "flyer-a")

    hop = c.get(f"/go/{code}")
    assert hop.status_code == 302
    target = hop.headers["Location"]
    for part in ("utm_source=org", "utm_medium=partner", "utm_content=flyer-a", f"lc={code}", "/aprende"):
        assert part in target, (part, target)
    assert m.con.execute("SELECT clicks FROM links WHERE code=?", (code,)).fetchone()[0] == 1
    c.get(f"/go/{code}?test=1")                       # our own QA click is never a click
    assert m.con.execute("SELECT clicks FROM links WHERE code=?", (code,)).fetchone()[0] == 1
    assert f"/go/{code}" in c.get(f"/admin/channels?token=t&made={code}").get_data(as_text=True)


def test_operator_link_builder_refuses_untaggable_or_offsite_links(tmp_path):
    m = make_app(tmp_path)
    c = m.app.test_client()
    bad = [{"label": "no medium", "utm_source": "reddit"},
           {"label": "offsite", "utm_source": "reddit", "utm_medium": "community", "landing": "https://elsewhere.example/x"},
           {"label": "unknown partner", "utm_source": "reddit", "utm_medium": "community", "partner_id": "not-registered"}]
    for form in bad:
        loc = c.post("/admin/channels/new?token=t", data=form).headers["Location"]
        assert "err=" in loc, form
    assert m.con.execute("SELECT COUNT(*) FROM links WHERE note IN ('no medium','offsite','unknown partner')").fetchone()[0] == 0


def test_arrival_report_groups_sources_into_channels(tmp_path):
    """The operator needs to see the KIND of route (search, community, partner, direct), not just a list
    of 20 utm_source strings — that is what tells them which acquisition channel to invest in."""
    m = make_app(tmp_path)
    google_journey(m.con, 2, "google")           # organic search
    google_journey(m.con, 1, "reddit")           # social / community
    google_journey(m.con, 1, "(direct)")         # typed or unknown
    m.con.commit()
    ch = {r["k"]: r for r in funnel.source_table(m.con, "real", dim="channel")}
    assert ch["search"]["visits"] == 2 and ch["search"]["consented_phone_leads"] == 2
    assert ch["social_community"]["visits"] == 1
    assert ch["direct"]["visits"] == 1
    assert funnel.source_table(m.con, "real", dim="channel")[0]["k"] in ch
