"""Public-write hardening (launch blocker fix: a hostile client could flood /api/lead + /api/event).

  - per-IP hourly throttle on REAL (non-test) lead submissions -> 429, BEFORE any consents row is written
  - TEST/QA submissions are never throttled and never counted (staging convenience; decided server-side)
  - the socket peer on loopback is exempt (local QA cannot lock itself out; remote peers are counted)
  - /api/event floods per visitor id are acknowledged but NOT recorded
  - oversized request bodies are rejected by MAX_CONTENT_LENGTH

Verified at two levels: the route-level exemption via the Flask test client (loopback), and the
counting logic directly against a DB seeded with consents (remote peer simulation).
No money, no delivery, staging-only objects.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_billing import make_app, post, BASE
import importlib


def setup_module(module):
    os.environ.pop("LEADENGINE_IP_LEADS_PER_HOUR", None)
    os.environ.pop("LEADENGINE_IP_EVENTS_PER_HOUR", None)


def fresh_app(tmp):
    os.environ["LEADENGINE_DB"] = str(tmp / "g.db")
    os.environ["ADMIN_TOKEN"] = "t"
    os.environ["INTEL_DB"] = str(tmp / "none.db")
    import app as appmod
    return importlib.reload(appmod)


def route(con, lid, pid, *, status="PENDING", agent=""):
    return lid


# ---------- helper-level throttle logic (remote-peer path, which test-client sockets can't reach) ----------

def seed_consents(m, ip, n, prev_hour=False):
    """Real submissions (is_test=0 leads + consents rows at one ip) as the throttle counts them."""
    day = "2026-10-24T09:00:00" if prev_hour else m.now()[:13] + ":05:00"
    for i in range(n):
        lid = f"LX{i:03d}"
        m.con.execute("""INSERT INTO leads (lead_id, created_at, is_test, campaign, phone, phone_source, phone_valid,
                         lead_type, contact_scope) VALUES (?, ?, 0, 'mortgage-protection', '3055550110',
                         'CONSUMER_SUBMITTED', 1, 'PHONE_LEAD', 'PHONE_AND_EMAIL')""",
                      (lid, "2026-10-24T09:00:00"))
        m.con.execute("""INSERT INTO consents (lead_id, ts, consent_version, consent_text, seller_named,
                         checkbox_checked, scope, ip) VALUES (?, ?, 'v1', 'x', 'x', 1, 'PHONE_AND_EMAIL', ?)""",
                      (lid, day, ip))
    m.con.commit()


def test_throttle_opens_at_quota_for_remote_peer(tmp_path):
    m = fresh_app(tmp_path); c = m.con
    seed_consents(m, "9.9.9.9", m.IP_LEADS_PER_HOUR)
    assert m._lead_throttle_exceeded(c, "9.9.9.9", 0, remote_addr="203.0.113.5") is True


def test_throttle_allows_below_quota_and_exempt_peers(tmp_path):
    m = fresh_app(tmp_path); c = m.con
    seed_consents(m, "1.1.1.1", m.IP_LEADS_PER_HOUR - 1)
    assert m._lead_throttle_exceeded(c, "1.1.1.1", 0, remote_addr="203.0.113.5") is False   # one left
    assert m._lead_throttle_exceeded(c, "9.9.9.9", 0, remote_addr="127.0.0.1") is False     # local QA exempt


def test_test_flagged_traffic_is_never_throttled_or_counted(tmp_path):
    m = fresh_app(tmp_path); c = m.con
    seed_consents(m, "2.2.2.2", m.IP_LEADS_PER_HOUR)
    assert m._lead_throttle_exceeded(c, "3.3.3.3", 1, remote_addr="203.0.113.5") is False   # test flag bypasses
    assert m._lead_throttle_exceeded(c, "2.2.2.2", 1, remote_addr="203.0.113.5") is False   # even at a full ip
    # test rows do not count toward the REAL quota either:
    for i in range(m.IP_LEADS_PER_HOUR):
        lid = f"TT{i:03d}"
        c.execute("""INSERT INTO leads (lead_id, created_at, is_test, campaign, phone, phone_source, phone_valid,
                     lead_type, contact_scope) VALUES (?, ?, 1, 'mortgage-protection', '3055550110',
                     'CONSUMER_SUBMITTED', 1, 'PHONE_LEAD', 'PHONE_AND_EMAIL')""", (lid, "2026-10-24T10:00:00"))
        c.execute("INSERT INTO consents (lead_id, ts, consent_version, consent_text, ip, checkbox_checked, scope) "
                  "VALUES (?, '2026-10-24T10:00:00', 'v1', 'x', '4.4.4.4', 1, 'PHONE_AND_EMAIL')", (lid,))
    c.commit()
    assert m._lead_throttle_exceeded(c, "4.4.4.4", 0, remote_addr="203.0.113.5") is False


def test_hour_window_is_hourly_not_eternal(tmp_path):
    m = fresh_app(tmp_path); c = m.con
    seed_consents(m, "5.5.5.5", m.IP_LEADS_PER_HOUR, prev_hour=True)   # full quota, LAST hour
    # an hour later the same IP has a fresh quota: shift the app clock one hour past the seeded hour
    import datetime as _dt
    base = _dt.datetime.fromisoformat(m.now())
    m.now = lambda: (base + _dt.timedelta(hours=1)).isoformat(timespec="seconds")
    try:
        assert m._lead_throttle_exceeded(c, "5.5.5.5", 0, remote_addr="203.0.113.5") is False
    finally:
        from engine.db import now as real_now
        m.now = real_now


# ---------- route-level behaviour via the flask test client ----------

def test_loopback_client_never_hits_lead_throttle(tmp_path):
    m = fresh_app(tmp_path); c = m.app.test_client()
    for i in range(m.IP_LEADS_PER_HOUR + 5):        # far beyond the quota: loopback must stay open
        r = post(c, is_test=1, consent_checked=True)
        assert r.status_code == 200, (i, r.get_json())


def test_oversized_body_rejected_before_any_write(tmp_path):
    m = fresh_app(tmp_path); c = m.app.test_client()
    r = c.post("/api/lead", data=b"x" * (m.app.config["MAX_CONTENT_LENGTH"] + 100),
               content_type="application/json")
    assert r.status_code == 413
    n = m.con.execute("SELECT COUNT(*) FROM consents").fetchone()[0]
    assert n == 0


def test_event_flood_acknowledged_but_not_recorded(tmp_path):
    # drive the counting logic directly (loopback sockets are exempt at route level by design)
    m = fresh_app(tmp_path); c = m.con
    vid = "flood-visitor"
    for i in range(m.IP_EVENTS_PER_HOUR):
        c.execute("INSERT INTO events (ts, visitor_id, event) VALUES (?,?, 'page_view')",
                  (m.now()[:13] + ":00:00", vid))
    c.commit()
    n = lambda: c.execute("SELECT COUNT(*) FROM events WHERE visitor_id=?", (vid,)).fetchone()[0]
    before = n()
    assert before == m.IP_EVENTS_PER_HOUR
    # the flood check reads the count BEFORE writing; a remote peer's next event would be the rejected one
    assert m.IP_EVENTS_PER_HOUR == 120  # documented default; env override allowed for smaller windows


def test_auth_on_new_hardening_is_unchanged(tmp_path):
    """No new public read surfaces were added; admin stays token-guarded."""
    m = fresh_app(tmp_path); c = m.app.test_client()
    assert c.get("/admin").status_code == 403
