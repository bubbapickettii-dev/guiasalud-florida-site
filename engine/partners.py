"""Partner distribution mechanics.

PARTNER -> unique link (/p/<id>) + QR -> co-branded resource -> consumer runs the Health Coverage Check
-> lead attributed to the partner -> partner sees AGGREGATE counts only -> agent gets the lead -> outcomes tracked.
Partners never receive individual consumer data. QR codes and guide PDFs are generated on request from the
CURRENT base URL, so moving to a real domain needs no rebuild.
"""
from __future__ import annotations

import io
import json
import os
import re
import secrets
from datetime import date, timedelta
from pathlib import Path

from .db import now


def _pdf_libs():
    """ReportLab is imported lazily: partner PDF generation needs it, nothing else in app must."""
    try:
        from reportlab.lib.colors import HexColor, white
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas
        from reportlab.lib.utils import ImageReader
        return HexColor, letter, canvas, ImageReader
    except ImportError as e:
        # Known Windows environment issue: Pillow's _imaging DLL cannot load when the
        # project path is very long ('Filename or extension is too long'). PDF is an
        # optional surface — the caller renders a clear operator message instead of a crash.
        raise PdfUnavailable(f"PDF generation unavailable in this environment: {e}") from e


class PdfUnavailable(Exception):
    """The optional PDF library stack cannot load in this environment."""


def _ink():
    """Resolve the guide palette once per call (kept lazy together with reportlab)."""
    HexColor, _, _, _ = _pdf_libs()
    return HexColor("#0f2238"), HexColor("#0b7a5a"), HexColor("#5a6b7d")
TYPES = ["re_school", "brokerage", "realtor_association", "salon_suites", "cdl_school", "trucking_services", "aba_agency",
         "cpa", "small_business_org", "employer_hr", "community_org", "creator", "newsletter", "local_business", "other"]
AUDIENCE_FOR_TYPE = {"re_school": "realtor", "brokerage": "realtor", "realtor_association": "realtor", "salon_suites": "beauty",
                     "cdl_school": "trucking", "trucking_services": "trucking", "aba_agency": "health_1099", "cpa": "business",
                     "small_business_org": "business", "employer_hr": "life_change"}
ANGLE_FOR_AUDIENCE = {"realtor": "self_employed", "beauty": "self_employed", "trucking": "self_employed",
                      "health_1099": "self_employed", "business": "self_employed", "life_change": "life_change"}


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:40]


def create(con, d: dict) -> dict:
    pid = d.get("partner_id") or slug(d["display_name"])
    base = pid
    i = 2
    while con.execute("SELECT 1 FROM partners WHERE partner_id=?", (pid,)).fetchone() and not d.get("partner_id"):
        pid = f"{base}-{i}"; i += 1
    aud = d.get("audience") or AUDIENCE_FOR_TYPE.get(d.get("type"), "other")
    lang = d.get("language") or "en"
    angle = d.get("default_angle") or (("es_" if lang == "es" else "") + ANGLE_FOR_AUDIENCE.get(aud, "health_check"))
    if lang == "es" and angle == "es_health_check":
        angle = "es_check"
    row = {"partner_id": pid, "display_name": d["display_name"], "type": d.get("type") or "other", "audience": aud,
           "default_angle": angle, "language": lang, "status": d.get("status") or "PROSPECT - not contacted",
           "token": d.get("token") or secrets.token_urlsafe(10), "cobrand_approved": 1 if d.get("cobrand_approved") else 0,
           "evidence": d.get("evidence", ""), "contact_hint": d.get("contact_hint", ""), "created_at": now()}
    con.execute(f"INSERT OR REPLACE INTO partners ({','.join(row)}) VALUES ({','.join('?' * len(row))})", list(row.values()))
    con.commit()
    return row


def import_json(con, path: Path) -> int:
    if not path.exists() or con.execute("SELECT COUNT(*) FROM partners").fetchone()[0]:
        return 0
    n = 0
    for p in json.loads(path.read_text(encoding="utf-8") or "[]"):
        create(con, {"partner_id": p["id"], "display_name": p["display_name"], "type": p.get("type"), "audience": p.get("audience"),
                     "language": p.get("lang", "en"), "evidence": p.get("evidence"), "contact_hint": p.get("contact_hint"),
                     "token": p.get("token"), "status": p.get("status"), "cobrand_approved": p.get("cobrand_approved")})
        n += 1
    return n


def stats(con, pid: str) -> dict:
    q = lambda sql: con.execute(sql, (pid,)).fetchone()[0]  # noqa: E731
    return {"visits": q("SELECT COUNT(*) FROM visitors WHERE partner_id=? AND is_test=0"),
            "used_check": q("SELECT COUNT(*) FROM visitors WHERE partner_id=? AND is_test=0 AND stage IN ('RESPONDER','SUBMITTED')"),
            "requested_help": q("SELECT COUNT(*) FROM leads WHERE partner_id=? AND is_test=0")}


def performance(con, test: int = 0) -> tuple[list[dict], dict | None]:
    """Registry-wide partner acquisition view: EVERY registered partner joined with the business funnel
    attributed by partner_id (partner pages /p/<id> stamp it on visitors, events and leads).
      - a partner with zero tracked activity is a real ZERO (the partner exists, nothing matched yet) —
        distinct from UNKNOWN, which we reserve for partner-level acquisition cost: no data can say it,
        so it stays '-' — never 0, never estimated
      - duplicate-flagged leads are counted as `dup` and never inside `qualified`
      - dispositions are honest: RETURNED, BAD_INFO and DNC separately; DNC is the buyer doing their
        job, not a bad outcome
      - test traffic excluded (test=1 shows the TEST rows instead)
    Second return: the attribution-gap card — real funnel volume that carries NO partner_id at all.
    """
    host_visit = con.execute("""SELECT COUNT(DISTINCT v.visitor_id) visits
          FROM visitors v WHERE v.is_test=? AND v.partner_id IS NULL""", (test,)).fetchone()[0]
    ldq = con.execute("""SELECT
          SUM(lead_type='PHONE_LEAD' AND COALESCE(phone_valid,0)=1) consented,
          SUM(qual_status='QUALIFIED') qualified, SUM(routing_status='DELIVERED') delivered
          FROM leads WHERE is_test=? AND partner_id IS NULL""", (test,)).fetchone()
    gap = {"visits": host_visit, "consented": ldq["consented"] or 0, "qualified": ldq["qualified"] or 0,
           "delivered": ldq["delivered"] or 0}
    ev = {r["k"]: dict(r) for r in con.execute("""SELECT COALESCE(e.partner_id, v.partner_id) k, COUNT(DISTINCT e.visitor_id) visits,
          COUNT(DISTINCT CASE WHEN e.event='tool_start' THEN e.visitor_id END) form_starts,
          COUNT(DISTINCT CASE WHEN e.event='tool_complete' THEN e.visitor_id END) check_done
          FROM events e LEFT JOIN visitors v ON v.visitor_id=e.visitor_id
          WHERE e.is_test=? AND COALESCE(e.partner_id, v.partner_id) IS NOT NULL GROUP BY 1""", (test,))}
    ld = {r["k"]: dict(r) for r in con.execute("""SELECT partner_id k, COUNT(*) asked_help,
          SUM(lead_type='PHONE_LEAD' AND COALESCE(phone_valid,0)=1) consented,
          SUM(qual_status='QUALIFIED') qualified, SUM(qual_status='DUPLICATE') dup,
          SUM(routing_status='DELIVERED') delivered,
          SUM(routing_status='DELIVERED' AND agent_status='SOLD') sold,
          SUM(routing_status='DELIVERED' AND agent_status='RETURNED') returned,
          SUM(routing_status='DELIVERED' AND agent_status='BAD_INFO') bad_info,
          SUM(routing_status='DELIVERED' AND agent_status='DNC') dnc,
          SUM(lead_type='EMAIL_ONLY_INQUIRY') email_only,
          COALESCE(SUM(CASE WHEN routing_status='DELIVERED' AND COALESCE(agent_status,'NEW') NOT IN ('RETURNED','BAD_INFO','DNC') THEN price END),0) revenue
          FROM leads WHERE is_test=? AND partner_id IS NOT NULL GROUP BY 1""", (test,))}
    out = []
    for p in con.execute("SELECT * FROM partners ORDER BY display_name"):
        pid = p["partner_id"]
        e, volume = ev.get(pid, {}), ld.get(pid, {})
        g = lambda k: volume.get(k) or 0  # noqa: E731
        r = dict(display_name=p["display_name"], partner_id=pid, type=p["type"], audience=p["audience"],
                 language=p["language"], status=p["status"], created_at=p["created_at"],
                 visits=e.get("visits", 0), form_starts=e.get("form_starts", 0), check_done=e.get("check_done", 0),
                 asked_help=g("asked_help"), consented=g("consented"), qualified=g("qualified"), dup=g("dup"),
                 delivered=g("delivered"), sold=g("sold"), returned=g("returned"), bad_info=g("bad_info"),
                 dnc=g("dnc"), email_only=g("email_only"), revenue=round(g("revenue"), 2),
                 cost_usd=None,    # unknown: partner-level spend is not attributable from any recorded data
                 qual_pct=round(100 * g("qualified") / g("consented"), 1) if g("consented") else None)
        out.append(r)
    out.sort(key=lambda r: (-r["qualified"], -r["consented"], -r["visits"], r["display_name"]))
    return out, gap


import io
import json
import os
import re
import secrets
from datetime import date, timedelta
from pathlib import Path

from .db import now


# ---------------------------------------------------------------- partner follow-up pipeline ----
# Stage progression uses the partner_activity APPEND-ONLY trail; the partners row keeps only the
# CURRENT position + latest outreach fields. Nothing here contacts anyone: the operator logs what they
# already did, and the queue tells them what's next. Existing free-text statuses map to stages so the
# 365 imported partners don't show as blank; a partner with no activity is a real zero — '-' means
# "the operator has not set it yet", never a fabricated state.
STAGES = ["IDENTIFIED", "READY_FOR_OUTREACH", "CONTACTED", "INTERESTED", "ACTIVATION_READY",
          "ACTIVATED", "DECLINED", "PAUSED"]
STAGE_LABEL = {"IDENTIFIED": "Identified", "READY_FOR_OUTREACH": "Ready for outreach", "CONTACTED": "Contacted",
               "INTERESTED": "Interested", "ACTIVATION_READY": "Ready for activation", "ACTIVATED": "Activated",
               "DECLINED": "Declined", "PAUSED": "Paused"}
METHODS = ["EMAIL", "PHONE", "IN_PERSON", "EVENT", "REFERRAL", "OTHER"]


def stage_of(p: dict) -> str:
    """Current stage of a partner row (free-text legacy `status` backfills until the operator sets one)."""
    s = (p["stage"] or "").strip()
    if s in STAGES:
        return s
    status = (p["status"] or "").upper()
    if "ACTIV" in status:            # ACTIVATED / ACTIVE
        return "ACTIVATED"
    if "DECLIN" in status or "NO INTEREST" in status or "NO RESPONSE" in status:
        return "DECLINED"
    if "PAUSE" in status:
        return "PAUSED"
    if "NOT CONTACTED" in status or "UNTOUCHED" in status or "PROSPECT" in status:
        return "IDENTIFIED"
    if "INTEREST" in status:
        return "INTERESTED"
    if "CONTACT" in status:          # genuinely contacted/replied phrasings only
        return "CONTACTED"
    if "READY" in status or "APPROVAL" in status or "MOD_OK" in status:
        return "READY_FOR_OUTREACH"
    return "IDENTIFIED"


def _valid_date(v: str) -> str | None:
    """ISO YYYY-MM-DD or None on garbage; past dates allowed on purpose (they land in Overdue)."""
    v = (v or "").strip()
    try:
        return date.fromisoformat(v).isoformat()
    except ValueError:
        return None


def qr_png(url: str) -> bytes:
    """Partner QR as a PNG. qrcode's default factory needs Pillow; when that library stack
    cannot load in this environment (Windows long-path DLL issue), the route answers with a
    clear 503 instead of an obfuscating 500 — the operator may build the QR externally."""
    try:
        import qrcode
        buf = io.BytesIO()
        qrcode.make(url, box_size=10, border=2).save(buf, format="PNG")
        return buf.getvalue()
    except ImportError as e:
        raise PdfUnavailable(f"QR PNG unavailable in this environment: {e}") from e


def _wrap(c, t, x, y, w, f="Helvetica", s=10.5, l=14, col=None):
    if col is None:
        col = _ink()[0]
    c.setFont(f, s); c.setFillColor(col); line = ""
    for word in t.split():
        tt = (line + " " + word).strip()
        if c.stringWidth(tt, f, s) > w:
            c.drawString(x, y, line); y -= l; line = word
        else:
            line = tt
    if line:
        c.drawString(x, y, line); y -= l
    return y


def guide_pdf(partner: dict, guide: dict, base_url: str, site: dict, fpl_rows) -> bytes:
    """Two-page co-branded guide with the partner's tracked QR (built from the live base URL)."""
    HexColor, letter, canvas, ImageReader = _pdf_libs()
    INK, ACC, MUTED = _ink()
    white = HexColor(255, 255, 255)
    es = partner.get("language") == "es"
    W, H = letter
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setTitle(guide["title"])
    c.setFillColor(INK); c.rect(0, H - 150, W, 150, fill=1, stroke=0)
    c.setFillColor(HexColor("#9fe3c9")); c.setFont("Helvetica-Bold", 10); c.drawString(48, H - 48, guide["kicker"])
    _wrap(c, guide["title"], 48, H - 80, W - 96, "Helvetica-Bold", 24, 28, white)
    if partner.get("cobrand_approved"):
        c.setFillColor(white); c.setFont("Helvetica", 10); c.drawString(48, H - 140, ("Cortesía de " if es else "Provided by ") + partner["display_name"])
    y = _wrap(c, guide["intro"], 48, H - 180, W - 96, "Helvetica", 11.5, 16) - 6
    for head, bullets in guide["sections"]:
        if y < 120:
            c.showPage(); y = H - 60
        y = _wrap(c, head, 48, y - 6, W - 96, "Helvetica-Bold", 13, 18)
        for b_ in bullets:
            c.setFillColor(ACC); c.circle(54, y + 3.5, 2.6, fill=1, stroke=0)
            y = _wrap(c, b_, 64, y, W - 112, "Helvetica", 10.5, 14) - 3
    c.showPage()
    y = _wrap(c, guide["table_title"], 48, H - 60, W - 96, "Helvetica-Bold", 13, 18)
    for k, v in fpl_rows(es):
        c.setFont("Helvetica", 11); c.setFillColor(INK); c.drawString(64, y, k); c.drawRightString(W - 64, y, v)
        c.setStrokeColor(HexColor("#e3e8ee")); c.line(64, y - 5, W - 64, y - 5); y -= 20
    y = _wrap(c, ("Basado en las guías de pobreza HHS 2026 que se usan para la cobertura 2027. La elegibilidad real la determina el Mercado." if es else
                  "Based on the 2026 HHS poverty guidelines used for 2027 coverage. Actual eligibility is determined by the Marketplace."), 64, y - 2, W - 128, "Helvetica", 8.5, 11, MUTED) - 14
    url = f"{base_url}/p/{partner['partner_id']}?src=guide"
    img = io.BytesIO(qr_png(url))
    from reportlab.lib.utils import ImageReader
    c.drawImage(ImageReader(img), 64, y - 170, 160, 160)
    yy = _wrap(c, ("Escanea: Chequeo de Seguro Médico 2027" if es else "Scan: 2027 Health Coverage Check"), 240, y - 40, W - 300, "Helvetica-Bold", 14, 18, ACC)
    yy = _wrap(c, ("Gratis, 60 segundos. Un agente con licencia te ayuda solo si tú lo pides." if es else "Free, 60 seconds. A licensed agent helps only if you ask."), 240, yy - 4, W - 300, "Helvetica", 10.5, 14)
    _wrap(c, url.replace("https://", "").replace("http://", ""), 240, yy - 6, W - 300, "Helvetica", 9, 12, MUTED)
    _wrap(c, (site.get("disclosure_es") if es else site["disclosure"]) + " " + (site.get("gov_disclaimer_es") if es else site.get("gov_disclaimer", "")),
          48, 60, W - 96, "Helvetica", 7.5, 9.5, MUTED)
    c.save()
    return buf.getvalue()


def record_action(con, pid: str, *, stage: str = "", method: str = "", outcome: str = "", next_action: str = "",
                  next_followup: str = "", note: str = "", operator: str = "admin") -> tuple[list[str], list[str]]:
    """Validated, append-only pipeline update for one partner. Returns (messages, errors); errors mean
    NOTHING was written (single transaction). Duplicate retries (identical payload within the same
    minute) are detected with dedupe_key and reported instead of creating a second record."""
    p = con.execute("SELECT * FROM partners WHERE partner_id=?", (pid,)).fetchone()
    if not p:
        return [], ["unknown partner"]
    errs, msgs = [], []
    stage = (stage or "").strip().upper()
    method = (method or "").strip().upper()
    note = (note or "").strip()[:500]
    outcome = (outcome or "").strip()[:200]
    next_action = (next_action or "").strip()[:200]
    stage_now = stage_of(dict(p))
    if stage and stage not in STAGES:
        errs.append(f"unknown stage '{stage}'")
    if method and method not in METHODS:
        errs.append(f"unknown outreach method '{method}'")
    nf = _valid_date(next_followup) if (next_followup or "").strip() else ""
    if (next_followup or "").strip() and nf is None:
        errs.append("follow-up date must be YYYY-MM-DD")
    if errs:
        return [], errs
    out_rows = []                     # rows to insert into partner_activity (append-only)
    if stage and stage != stage_now:
        out_rows.append(("STAGE", stage_now, stage, "", ""))
        msgs.append(f"stage {stage_now} -> {stage}")
    touch = {}
    if method or outcome or next_action or note:
        out_rows.append(("OUTREACH", "", "", outcome, note))
        touch["last_outreach_at"], touch["outreach_method"], touch["last_outcome"] = now(), method, outcome
        touch["next_action"] = next_action
        touch["notes"] = note
        msgs.append("outreach recorded")
    if nf:
        out_rows.append(("FOLLOWUP", "", "", "", note))
        touch["next_followup"] = nf
        msgs.append(f"next follow-up {nf}")
    if not out_rows:
        return ["nothing to record — set a stage, method, outcome, next action or follow-up date"], []
    ts = now()
    minute = ts[:16]                  # dedupe: identical retry within the same minute is a double-click
    inserted = skipped = 0
    for kind, f, t, occ, nte in out_rows:
        dk = f"v1|{pid}|{kind}|{stage_now}|{t}|{method}|{occ}|{next_action}|{nf}|{nte}|{minute}"
        cur = con.execute("SELECT 1 FROM partner_activity WHERE dedupe_key=?", (dk,)).fetchone()
        if cur:
            skipped += 1
            continue
        con.execute("INSERT INTO partner_activity (ts, partner_id, kind, stage_from, stage_to, method, next_followup, note, dedupe_key, operator) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)", (ts, pid, kind, f, t, method, nf or None, nte, dk, operator[:80]))
        inserted += 1
    if skipped == len(out_rows):      # every row was an exact retry of one just recorded
        return ["already recorded — identical update exists"], []
    if stage:
        touch["stage"], touch["status"] = stage, STAGE_LABEL[stage]   # keep the legacy Partners tab in sync
    if touch:
        cols = ",".join(f"{k}=?" for k in touch)
        con.execute(f"UPDATE partners SET {cols} WHERE partner_id=?", list(touch.values()) + [pid])
    con.commit()
    return msgs, []


def history(con, pid: str) -> list[dict]:
    """Everything the operator ever recorded for this partner, oldest first (never overwritten)."""
    return [dict(r) for r in con.execute(
        "SELECT ts, kind, stage_from, stage_to, method, next_followup, note, operator FROM partner_activity "
        "WHERE partner_id=? ORDER BY id", (pid,))]


BUCKET_ORDER = ["OVERDUE", "DUE_TODAY", "ACTIVATION_READY", "AWAITING_FIRST_CONTACT", "INTERESTED_WAITING",
                "SCHEDULED", "RECENT", "NO_FOLLOWUP"]


# ---------------------------------------------------------------- partner prioritization ----
# The queue answers "what do I owe whom when"; this answers "where does my acquisition attention go".
# It is NOT a score: every partner is placed in an evidence-labelled section with explicit reasons and
# one next action, built ONLY from data that exists — pipeline buckets, verified acquisition metrics,
# tracked-visitor recency, recorded outreach, and cost entries an operator actually typed. Labels are
# honest about holes: no traffic is INSUFFICIENT EVIDENCE (not "bad"), a new partner is never penalised
# for young history, unrecorded cost is MISSING DATA (never 0), and a stalled channel is a dated FACT,
# not a quality verdict (inactivity and lead quality are different things).
SECTION_ORDER = ["DO_NOW", "PROVEN", "ENGAGING_UNQUAL", "WARM", "STALLED", "INSUFFICIENT", "KEEPING"]
SECTION_LABEL = {"DO_NOW": "Act now — overdue / due today / ready to activate",
                 "PROVEN": "Proven performers — verified results, keep them loaded",
                 "ENGAGING_UNQUAL": "Getting traffic but no qualified leads — needs a look at the funnel",
                 "WARM": "Recorded interest — no verified outcomes yet, keep the touch cadence",
                 "STALLED": "Activity stopped — dated fact, not a quality verdict",
                 "INSUFFICIENT": "Insufficient evidence — next step is data, not judgement",
                 "KEEPING": "Running well — no follow-up owed"}
# one-line explanation handed to the template for each section
SECTION_HINT = {"DO_NOW": "follow-up promises that came due, plus partners marked ready for activation.",
                "PROVEN": "ranked by VERIFIED qualified leads and revenue attributed to their links.",
                "ENGAGING_UNQUAL": ("real visits arrive but no consented/qualified lead came out - check the "
                                    "placement, landing or audience before spending more there."),
                "WARM": "the operator recorded interest but nothing measurable yet.",
                "STALLED": "had tracked visits, then nothing changed. Ask why - flags inactivity, not quality.",
                "INSUFFICIENT": ("no attributed visits at all, so performance CANNOT be judged yet. Move the "
                                 "QR/guide kit where attribution counts; a brand-new partner is expected here."),
                "KEEPING": "activated channels with recent tracked activity."}
# how the template marks evidence tags (inline colour, no new CSS file)
EV_STYLES = {"VERIFIED": "#0b7a5a", "RECORDED": "#1a5fb4", "ACTIVITY": "#0f2238", "MISSING": "#8a6d1f",
             "INSUFFICIENT": "#8a6d1f"}


def _cutoff(today: str, days: int) -> str:
    return (date.fromisoformat(today) - timedelta(days=days)).isoformat()


def prioritize(con, *, today: str | None = None, stall_days: int = 14) -> dict:
    """Prioritisation across the WHOLE partner registry (staging-safe, test traffic excluded).
    Returns {"sections": [{key, label, hint, entries}], "channels": [...]}.
      - entries carry partner_id, name, stage, why[], evidence[(kind,text)], next action, and the core
        verified numbers for the panel
      - channels: per outreach method, how many partners were contacted by it, how many hold a
        stage beyond CONTACTED, and their verified outcomes — a snapshot of recorded data, not a
        controlled experiment
    """
    today = today or date.today().isoformat()
    cutoff = _cutoff(today, stall_days)
    queue = {r["partner_id"]: r for r in pipeline(con)}
    perf = {r["partner_id"]: r for r in performance(con, 0)[0]}
    lastact = {r["partner_id"]: (r["last_seen"] or "")[:10] for r in con.execute(
        "SELECT partner_id, MAX(last_seen) last_seen FROM visitors WHERE partner_id IS NOT NULL AND is_test=0 GROUP BY 1")}
    n_outreach = {r["partner_id"]: r["n"] for r in con.execute(
        "SELECT partner_id, COUNT(*) n FROM partner_activity WHERE kind='OUTREACH' GROUP BY 1")}
    cost = {r["partner_id"]: r["c"] for r in con.execute(
        "SELECT partner_id, SUM(cost) c FROM experiments WHERE partner_id IS NOT NULL AND COALESCE(cost,0)>0 GROUP BY 1")}
    out = {k: [] for k in SECTION_ORDER}
    for pid, q in queue.items():
        stage, bucket, nf = q["stage"], q["bucket"], (q["next_followup"] or "")
        p = perf.get(pid) or {}
        vis = int(p.get("visits") or 0)
        qual = int(p.get("qualified") or 0)
        sold = int(p.get("sold") or 0)
        rev = round(p.get("revenue") or 0, 2)
        ret = int(p.get("returned") or 0) + int(p.get("bad_info") or 0)
        la = lastact.get(pid, "")
        n_out = n_outreach.get(pid, 0)
        bucket_rank = 9                     # used only for DO_NOW ordering (overdue → due-today → activation)
        ev = []                       # (kind, text): VERIFIED | RECORDED | ACTIVITY | MISSING | INSUFFICIENT
        if qual or rev:
            ev.append(("VERIFIED", f"{qual} qualified · ${rev:,.2f} revenue" + (f" · {sold} sold" if sold else "")))
        if ret:
            ev.append(("VERIFIED", f"{ret} returned/bad-info"))
        if la:
            ev.append(("ACTIVITY", f"last tracked visitor {la}"))
        if n_out:
            ev.append(("RECORDED", f"{n_out} outreach{'es' if n_out != 1 else ''} logged"))
        ev.append(("MISSING", "cost " + (f"recorded ${cost[pid]:,.2f}" if pid in cost else "not recorded (unknown — not zero)")))
        sec, why, nxt = "", [], ""
        if bucket in ("OVERDUE", "DUE_TODAY", "ACTIVATION_READY"):
            sec = "DO_NOW"
            why = [ f"follow-up overdue (was due {nf})" if bucket == "OVERDUE" else
                    "follow-up due today" if bucket == "DUE_TODAY" else
                    "marked ready for activation — log the activation and get the kit placed"]
            nxt = "log the outcome on the partner card"
            bucket_rank = 0 if bucket == "OVERDUE" else 1 if bucket == "DUE_TODAY" else 2
        elif stage in ("DECLINED", "PAUSED"):
            continue                  # operator closed this out; only their next_followup would reopen it
        elif stage == "ACTIVATED":
            if la and la < cutoff:
                sec, why = "STALLED", [f"activated but no tracked visitor since {la}"]
                nxt = "check placement / re-promote the kit (inactivity — not a quality verdict)"
            else:
                sec, why = "KEEPING", [f"activated with recent tracked activity" if la else
                                        "activated — no tracked visits seen yet (attribution or placement?)"]
                nxt = "keep the kit placed"
        elif qual or rev or sold:
            sec = "PROVEN"
            why = [f"verified {qual} qualified / ${rev:,.2f}"]
            nxt = ("schedule the next reload touch — no follow-up on file" if not nf else
                   f"follow-up already set ({nf})")
            if ret and qual and ret >= qual:
                why.append("most outcomes were returns/bad-info — fix lead quality before reloading more")
        elif vis:
            sec = "ENGAGING_UNQUAL"
            why = [f"{vis} tracked visits but 0 qualified leads"]
            nxt = "inspect the placement/landing — traffic without qualified leads"
        elif stage == "INTERESTED" or n_out:
            sec = "WARM"
            why = [("operator-recorded interest" if stage == "INTERESTED" else
                    f"{n_out} outreach logged but no response evidence yet")]
            nxt = "schedule the next follow-up" if not nf else f"follow-up set ({nf})"
        else:
            sec = "INSUFFICIENT"
            why = ["no attributed visits or outreach recorded — cannot evaluate performance yet"]
            if not (q.get("contact_hint") or ""):
                why.append("no stored contact hint either")
            if (q.get("created_at") or "")[:10] >= cutoff:
                why.append("partner is new — zero history is expected for a brand-new partner")
            nxt = "place the QR/guide kit where attribution counts, then log first contact"
        if sec:
            out[sec].append({"partner_id": pid, "display_name": q["display_name"], "type": q["type"],
                             "stage": stage, "bucket": bucket, "bucket_rank": bucket_rank, "why": why,
                             "evidence": ev, "next": nxt,
                             "visits": vis, "qualified": qual, "revenue": rev, "last_activity": la,
                             "created_at": (q.get("created_at") or "")[:10]})
    rank = {"DO_NOW": lambda r: (r["bucket_rank"], r["qualified"] > 0, -(r["qualified"] + r["revenue"]), r["display_name"]),
            "PROVEN": lambda r: (-r["qualified"], -r["revenue"], r["display_name"]),
            "ENGAGING_UNQUAL": lambda r: (-r["visits"], r["display_name"]),
            "WARM": lambda r: (r["display_name"],),
            "STALLED": lambda r: (r["last_activity"], r["display_name"]),
            "INSUFFICIENT": lambda r: (r["created_at"], r["display_name"]),
            "KEEPING": lambda r: (r["last_activity"], r["display_name"])}
    for sec in out:
        out[sec].sort(key=rank[sec])
    sections = [{"key": k, "label": SECTION_LABEL[k], "hint": SECTION_HINT[k], "entries": out[k]} for k in SECTION_ORDER]
    # per-method outreach rollup from recorded data only (which channel produced what relative to effort)
    ch: dict = {}
    for q in queue.values():
        m = (q.get("outreach_method") or "").strip()
        if not m:
            continue
        p = perf.get(q["partner_id"]) or {}
        c = ch.setdefault(m, {"method": m, "contacted": 0, "beyond_contact": 0, "qualified": 0, "revenue": 0.0})
        c["contacted"] += 1
        if stage_of(q) in ("INTERESTED", "ACTIVATION_READY", "ACTIVATED"):
            c["beyond_contact"] += 1
        c["qualified"] += int(p.get("qualified") or 0)
        c["revenue"] += round(p.get("revenue") or 0, 2)
    channels = sorted(ch.values(), key=lambda c: (-c["qualified"], -c["revenue"], -c["contacted"], c["method"]))
    return {"sections": sections, "channels": channels}


def pipeline(con) -> list[dict]:
    """The action queue: every partner with a derived work bucket, highest priority first.
    OVERDUE / DUE_TODAY come from a real recorded next_followup date vs today (UTC); AWAITING_FIRST_CONTACT
    is exactly 'never recorded an outreach and no follow-up set'; SCHEDULED is future-dated."""
    today = date.today().isoformat()
    rows = []
    for p in con.execute("SELECT * FROM partners"):
        d = dict(p)
        stage = stage_of(d)
        nf = (d["next_followup"] or "")[:10]
        if stage in ("ACTIVATION_READY",) and not nf:
            bucket = "ACTIVATION_READY"
        elif nf and nf < today:
            bucket = "OVERDUE"
        elif nf == today:
            bucket = "DUE_TODAY"
        elif stage == "INTERESTED" and not nf:
            bucket = "INTERESTED_WAITING"
        elif not d["last_outreach_at"] and not nf:
            bucket = "AWAITING_FIRST_CONTACT" if stage in ("IDENTIFIED", "READY_FOR_OUTREACH") else "INTERESTED_WAITING"
        elif nf > today:
            bucket = "SCHEDULED"
        else:
            bucket = "RECENT"
        rows.append(dict(d, stage=stage, bucket=bucket))
    rows.sort(key=lambda r: (BUCKET_ORDER.index(r["bucket"]), r["next_followup"] or "9999-99-99", r["display_name"]))
    return rows
