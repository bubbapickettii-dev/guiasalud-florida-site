"""Referral network builder: verified ORGANIZATION contacts -> activation-ready referral partners.

Why this exists. The public-profile discovery pipeline proved that individual consumers do not
publish contact details next to a coverage need (470 posts -> 37 genuine need posts -> 0 usable
contacts). The one channel that actually moves consumers is a person they already trust handing
them a link: a clinic front desk, a WIC office, a staffing agency, a church. We already hold 800+
VERIFIED organization contacts from official registries (HRSA health-center site file, CMS NPPES).
This module turns them into the machinery of that channel:

  org record -> partner row (/p/<id>) -> printed reference card + QR -> consumer runs /check
             -> lead attributed to that partner -> partner sees aggregate counts only

Hard boundaries (all enforced here, none optional):
  * ORGANIZATIONS ONLY. Nothing in this module reads or writes the consumer prospect store, and
    nothing creates a lead. An organization phone number is never a consumer contact.
  * NO CO-BRANDING WITHOUT PERMISSION. Partners are created with cobrand_approved=0, so the
    intake page never carries an org's name until that org says yes. Creating a partner record is
    bookkeeping, not an affiliation claim.
  * NOTHING IS SENT. `kit()` only composes text an operator can copy; there is no mail, SMS,
    webhook or browser call anywhere in this module. Outreach stays a human, approved action.
  * IDEMPOTENT. Re-running never overwrites an operator-edited partner row (status, stage, notes,
    tokens survive); it reports what already existed.

Access/terms: the source registries are official public files whose published purpose is public
listing (the HRSA site file is expressly a public directory of service locations; NPPES is the
national provider registry). robots/terms for each source were captured at collection time and are
carried on the org row (`terms_note`) and copied into the partner's evidence so an operator can see
it before acting.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

from . import partners as partners_mod
from .db import now

ROOT = Path(__file__).resolve().parents[1]
ORG_DB = ROOT / "data" / "org_contacts.db"
PID_PREFIX = "net-"

# org_type (org_contacts) -> partner type (partners.TYPES). Everything here reaches individual
# people who may need coverage; none of them is a brokerage or an employer buying leads.
PARTNER_TYPE = {
    "clinic": "community_org",        # FQHC / community health center: uninsured patients daily
    "publichealth": "community_org",  # county health departments
    "nonprofit": "community_org",     # food banks, WIC-adjacent, mutual aid
    "food_bank": "community_org",     # the pantry line is where uninsured people already are
    "education": "community_org",     # ESOL / adult ed: high uninsured share
    "staffing": "employer_hr",        # job changers: the job_loss angle
    "other": "community_org",
}
# Only VERIFIED org records may become partners: the contact was taken straight from the registry of
# record (or re-checked against it). A row flagged as a duplicate of another is never promoted - it
# would double-count one referral source and split its funnel.
VERIFIED_KINDS = ("VERIFIED_PRIMARY", "VERIFIED_RECHECK", "PUBLIC_CONTACT_VERIFIED")
VERIFIED_STATUS = ("VERIFIED_PRIMARY", "VERIFIED_RECHECK")

SCHEMA = """CREATE TABLE IF NOT EXISTS network_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, org_db TEXT, state TEXT,
  records_considered INTEGER, partners_created INTEGER, partners_existing INTEGER,
  skipped_unverified INTEGER, skipped_duplicate INTEGER, skipped_no_phone INTEGER,
  skipped_not_health INTEGER, errors INTEGER, note TEXT)"""


# ------------------------------------------------------------------ helpers
def connect_orgs(path: Path | str = ORG_DB) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    return con


def ensure(con: sqlite3.Connection) -> None:
    con.execute(SCHEMA)
    con.commit()


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:38] or "org"


def partner_id_for(org: dict) -> str:
    """Deterministic id: the same organization always maps to the same partner row, so repeat
    builds can never double-count a referral source."""
    key = org.get("org_key") or ""
    return f"{PID_PREFIX}{_slug(org.get('org_name'))}-{hashlib.sha1(key.encode()).hexdigest()[:6]}"


def _phones(org: dict) -> list[str]:
    try:
        rows = json.loads(org.get("phones") or "[]")
    except ValueError:
        return []
    out = []
    for r in rows if isinstance(rows, list) else []:
        v = (r.get("value") if isinstance(r, dict) else r) or ""
        v = str(v).strip()
        if v:
            out.append(v)
    return out


def _record_ids(org: dict) -> dict:
    try:
        v = json.loads(org.get("record_ids") or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4}|\d{4})?\b")
_ADDR_FULL = re.compile(r"^(?P<street>.+?),\s*(?P<city>[^,]+),\s*(?P<state>[A-Z]{2})\b[,\s]*(?P<zip>\d{5}(?:-\d{4})?)?\s*$")


def address_parts(address: str) -> dict:
    """Registry address -> street/city/state/zip, without inventing anything.

    Two published shapes exist in the ledger: HRSA's 'street[, suite], city, ST, ZIP-4' and NPPES's
    'CITY, ST, ZIP+4' (location address, no street). Both are parsed by shape, and a record that fits
    neither keeps its city/zip only - the directory then prints what the registry actually said.
    """
    raw = (address or "").strip()
    if not raw:
        return {"street": "", "city": "", "state": "", "zip": ""}
    raw = re.sub(r"\s+", " ", raw)
    m = _ADDR_FULL.match(raw)
    if m:
        z = m.group("zip") or ""
        return {"street": _title((m.group("street") or "").strip()),
                "city": _title((m.group("city") or "").strip()),
                "state": m.group("state"), "zip": z[:5]}
    bits = [b.strip() for b in raw.split(",") if b.strip()]
    state = next((b for b in bits if re.fullmatch(r"[A-Z]{2}", b)), "")
    if not state:
        sm = re.search(r"\b(F[L]|[A-Z]{2})\b\s*\d{5}", raw)
        state = sm.group(1) if sm else ""
    zipm = ZIP_RE.search(raw)
    city = ""
    if state and state in bits:
        i = bits.index(state)
        if i:
            city = bits[i - 1]
    street = bits[0] if bits and bits[0] != city and not re.fullmatch(r"\d{5}", bits[0]) else ""
    if street and street.upper() == city.upper():
        street = ""
    return {"street": _title(street), "city": _title(city), "state": state,
            "zip": (zipm.group(1) if zipm else "")}


def _title(s: str) -> str:
    """Street/city display case that leaves ordinals alone ('45th St', not '45Th St')."""
    out = (s or "").title()
    return re.sub(r"\b(\d+)(St|Nd|Rd|Th)\b", lambda m: m.group(1) + m.group(2).lower(), out)


# ------------------------------------------------------------------ build
def build(con: sqlite3.Connection, org_db: Path | str = ORG_DB, *, state: str = "FL",
          limit: int | None = None, dry_run: bool = False) -> dict:
    """Create activation-ready partner rows from verified organization contacts.

    Reads the org ledger read-only, writes only `partners` (+ one `network_runs` measurement row).
    Returns explicit skipped counts so a thin result is never mistaken for a complete run.
    """
    ensure(con)
    stats = {"records_considered": 0, "partners_created": 0, "partners_existing": 0,
             "skipped_unverified": 0, "skipped_duplicate": 0, "skipped_no_phone": 0,
             "skipped_not_health": 0, "errors": 0}
    created: list[dict] = []
    try:
        orgs = connect_orgs(org_db)
    except sqlite3.Error as e:
        stats["errors"] += 1
        _record_run(con, org_db, state, stats, f"org ledger unreadable: {type(e).__name__}")
        return {"stats": stats, "created": created}

    try:
        rows = orgs.execute("SELECT * FROM org_contacts ORDER BY org_name").fetchall()
    except sqlite3.Error:
        rows = []
    for org in rows:
        if limit is not None and stats["records_considered"] >= limit:
            break
        stats["records_considered"] += 1
        o = dict(org)
        if (o.get("dup_status") or "UNIQUE") != "UNIQUE" or not o.get("org_key"):
            stats["skipped_duplicate"] += 1
            continue
        if ((o.get("record_kind") or "") not in VERIFIED_KINDS
                and (o.get("verify_status") or "") not in VERIFIED_STATUS):
            stats["skipped_unverified"] += 1
            continue
        parts = address_parts(o.get("address") or "")
        if state and parts["state"] and parts["state"] != state:
            stats["skipped_not_health"] += 1      # outside the served state: not our market yet
            continue
        ptype = PARTNER_TYPE.get((o.get("org_type") or "other").lower())
        if not ptype:
            stats["skipped_not_health"] += 1
            continue
        phones = _phones(o)
        if not phones:
            stats["skipped_no_phone"] += 1
            continue
        pid = partner_id_for(o)
        if con.execute("SELECT 1 FROM partners WHERE partner_id=?", (pid,)).fetchone():
            stats["partners_existing"] += 1
            continue
        ids = _record_ids(o)
        evidence = (f"{o.get('source') or 'org ledger'} official public listing; "
                    f"record ids {json.dumps(ids, sort_keys=True) if ids else 'n/a'}; "
                    f"{(o.get('terms_note') or '')[:160]}; {o.get('source_url') or ''}")[:900]
        contact_hint = (f"Published organization line {phones[0]} ({o.get('purpose_note') or 'public phone'}). "
                        "Ask for the person who handles community resources or patient navigation.")[:400]
        if dry_run:
            stats["partners_created"] += 1
            created.append({"partner_id": pid, "display_name": o.get("org_name"), "phone": phones[0],
                            "city": parts["city"], "type": ptype, "source": o.get("source")})
            continue
        try:
            name = (o.get("org_name") or pid).strip()
            if parts["city"]:
                name = f"{name} ({parts['city']})"      # a partner is a LOCATION, not a brand
            partners_mod.create(con, {
                "partner_id": pid, "display_name": name[:120],
                "type": ptype, "audience": "uninsured", "default_angle": "health_check",
                "language": "en", "status": "PROSPECT - not contacted",
                "cobrand_approved": 0,             # never co-brand without the org's yes
                "evidence": evidence, "contact_hint": contact_hint,
            })
        except Exception:                          # noqa: BLE001 - one bad row never kills a build
            stats["errors"] += 1
            continue
        stats["partners_created"] += 1
        created.append({"partner_id": pid, "display_name": o.get("org_name"), "phone": phones[0],
                        "city": parts["city"], "type": ptype, "source": o.get("source")})
    orgs.close()
    if not dry_run:
        _record_run(con, org_db, state, stats, "")
    return {"stats": stats, "created": created}


def _record_run(con, org_db, state, stats, note) -> None:
    ensure(con)
    con.execute(f"INSERT INTO network_runs (ts, org_db, state, {', '.join(stats)}, note) "
                f"VALUES (?,?,?,{','.join('?' * len(stats))}, ?)",
                (now(), str(org_db), state, *stats.values(), note))
    con.commit()


# ------------------------------------------------------------------ partner kit
CARD_EN = ("Free health coverage check\n\n"
           "{name} shares this free resource: a 2-minute check at {link} that shows your 2027 "
           "enrollment window and whether your income qualifies for help paying premiums.\n\n"
           "Free. No obligation. You can stop at any time. Nobody calls you unless you ask. "
           "We are not a government agency and not an insurance company.")
CARD_ES = ("Chequeo gratis de cobertura médica\n\n"
           "{name} comparte este recurso gratis: un chequeo de 2 minutos en {link} que muestra tu "
           "periodo de inscripción 2027 y si tu ingreso califica para ayuda con las primas.\n\n"
           "Gratis. Sin compromiso. Puedes parar cuando quieras. Nadie te llama a menos que tú lo "
           "pidas. No somos una agencia del gobierno ni una compañía de seguros.")
OUTREACH_EN = ("Subject: a free coverage-check card for your {audience_label}\n\n"
               "Hi {name} team - I run {brand}, a Florida health-coverage resource (not an insurer, "
               "not an agency). We made a free, printable card your {audience_label} can scan: it "
               "opens a 2-minute check of their 2027 enrollment window and whether their income "
               "qualifies for premium help.\n\n"
               "Two things it does NOT do: it never asks for a phone number unless the person "
               "chooses to, and it never shares anyone's details with you. Your counter just gets "
               "aggregate counts at {stats_link}.\n\n"
               "If you want it, reply and I'll send the print file with your name on it (only with "
               "your permission). If not, no follow-up.")
OUTREACH_ES = ("Asunto: una tarjeta gratis de chequeo de cobertura para su comunidad\n\n"
               "Hola, equipo de {name} - dirijo {brand}, un recurso de cobertura médica en Florida "
               "(no somos aseguradores ni agencia). Hicimos una tarjeta imprimible gratis que su "
               "comunidad puede escanear: abre un chequeo de 2 minutos del periodo de inscripción "
               "2027 y de si el ingreso califica para ayuda con las primas.\n\n"
               "Dos cosas que NO hace: no pide teléfono a menos que la persona lo decida, y no "
               "comparte los datos de nadie con ustedes. Su mostrador solo ve conteos agregados en "
               "{stats_link}.\n\n"
               "Si la quieren, respondan y les envío el archivo para imprimir con su nombre (solo "
               "con su permiso). Si no, no insistimos.")
AUDIENCE_LABEL = {"community_org": "patients and neighbors", "employer_hr": "job seekers",
                  "other": "community"}


def kit(partner: dict, base_url: str = "", brand: str = "Gia Salud") -> dict:
    """Copy-ready activation kit for ONE partner. Text only: nothing here sends, posts or calls."""
    pid = partner.get("partner_id") or partner.get("id")
    base = (base_url or "").rstrip("/")
    link = f"{base}/p/{pid}"
    name = partner.get("display_name") or pid
    label = AUDIENCE_LABEL.get(partner.get("type") or "other", "community")
    fmt = {"name": name, "link": link, "brand": brand, "audience_label": label,
           "stats_link": f"{base}/partner/{partner.get('token') or ''}"}
    return {"partner_id": pid, "display_name": name, "link": link,
            "qr_url": f"/qr.png?u=/p/{pid}", "stats_link": fmt["stats_link"],
            "card_en": CARD_EN.format(**fmt), "card_es": CARD_ES.format(**fmt),
            "outreach_en": OUTREACH_EN.format(**fmt), "outreach_es": OUTREACH_ES.format(**fmt),
            "cobrand_approved": bool(partner.get("cobrand_approved")),
            "note": "Nothing is sent by this platform. Sending is an operator action, after approval."}


# ------------------------------------------------------------------ operator work product
KIT_FIELDS = ["partner_id", "display_name", "type", "phone", "city", "link", "qr_url", "stats_link",
              "stage", "cobrand_approved", "card_en", "card_es", "outreach_en", "outreach_es", "approval"]
_PHONE_IN_HINT = re.compile(r"line\s+([^\s(]+)")
_CITY_IN_NAME = re.compile(r"\(([^()]+)\)\s*$")


def export_kits(con: sqlite3.Connection, base_url: str = "", brand: str = "Gia Salud",
                path: Path | str | None = None, limit: int | None = None,
                net_only: bool = True) -> dict:
    """One CSV the operator can work through: every partner's link, QR, card copy and outreach draft.

    This is the difference between a list of names and a channel: the file contains the finished
    artifact for each location. The `approval` column is written on EVERY row so a forwarded file can
    never be mistaken for approved outreach - sending stays a human decision, logged in
    /admin/partners.
    """
    import csv

    rows = [dict(r) for r in con.execute("SELECT * FROM partners ORDER BY display_name")]
    if net_only:
        rows = [p for p in rows if (p.get("partner_id") or "").startswith(PID_PREFIX)]
    if limit:
        rows = rows[:limit]
    out_path = Path(path) if path else ROOT / "out" / "network_kits.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=KIT_FIELDS)
        w.writeheader()
        for p in rows:
            k = kit(p, base_url, brand)
            name = p.get("display_name") or ""
            cm = _CITY_IN_NAME.search(name)
            pm = _PHONE_IN_HINT.search(p.get("contact_hint") or "")
            w.writerow({"partner_id": k["partner_id"], "display_name": name, "type": p.get("type"),
                        "phone": pm.group(1) if pm else "", "city": cm.group(1) if cm else "",
                        "link": k["link"], "qr_url": k["qr_url"], "stats_link": k["stats_link"],
                        "stage": partners_mod.stage_of(p), "cobrand_approved": int(bool(p.get("cobrand_approved"))),
                        "card_en": k["card_en"], "card_es": k["card_es"],
                        "outreach_en": k["outreach_en"], "outreach_es": k["outreach_es"],
                        "approval": "APPROVAL REQUIRED - nothing sent by the platform"})
    return {"partners": len(rows), "path": str(out_path)}


# ------------------------------------------------------------------ measurement
def report(con: sqlite3.Connection) -> dict:
    """Network activation + yield, from RECORDED rows only.

    Activation counts are partner stages; yield reuses `partners.performance` (the single existing
    attribution computation) restricted to network partners, so network numbers can never diverge
    from the acquisition page. An empty network is a real zero, not an unknown.
    """
    ensure(con)
    rows = [dict(r) for r in con.execute("SELECT * FROM partners ORDER BY display_name")]
    net = [p for p in rows if (p.get("partner_id") or "").startswith(PID_PREFIX)]
    stages = {s: 0 for s in partners_mod.STAGES}
    for p in net:
        stages[partners_mod.stage_of(p)] += 1
    perf, _gap = partners_mod.performance(con, 0)
    net_ids = {p["partner_id"] for p in net}
    perf = [r for r in perf if r["partner_id"] in net_ids]
    totals = {"visits": 0, "check_done": 0, "asked_help": 0, "consented": 0, "qualified": 0,
              "delivered": 0, "sold": 0, "dup": 0, "returned": 0, "bad_info": 0, "dnc": 0,
              "email_only": 0, "revenue": 0.0}
    for r in perf:
        for k in totals:
            totals[k] += r.get(k) or 0
    totals["revenue"] = round(totals["revenue"], 2)
    by_type: dict[str, dict] = {}
    for r in perf:
        t = by_type.setdefault(r["type"], {"partners": 0, "visits": 0, "consented": 0, "qualified": 0})
        t["partners"] += 1
        for k in ("visits", "consented", "qualified"):
            t[k] += r.get(k) or 0
    last = con.execute("SELECT * FROM network_runs ORDER BY id DESC LIMIT 1").fetchone()
    runs = con.execute("SELECT COALESCE(SUM(partners_created),0) c, COUNT(*) n FROM network_runs").fetchone()
    return {"partners_total": len(net), "all_partners": len(rows), "stages": stages,
            "activated": stages.get("ACTIVATED", 0),
            "with_activity": sum(1 for r in perf if (r.get("visits") or 0) > 0),
            "totals": totals, "by_type": sorted(
                ({"type": k, **v} for k, v in by_type.items()), key=lambda d: -d["visits"]),
            "last_run": dict(last) if last else None,
            "runs": {"count": runs["n"], "created": runs["c"]},
            "coverage": ("network partners are ORGANIZATIONS. They never become consumer leads; "
                         "their role is to hand the /check link to a real person who then submits "
                         "their own contact details voluntarily.")}


# ------------------------------------------------------------------ public directory
DIRECTORY_TYPES = ("clinic", "publichealth", "nonprofit", "food_bank", "education")


def directory(org_db: Path | str = ORG_DB, *, state: str = "FL", city: str = "",
              page: int = 1, per_page: int = 50) -> dict:
    """Read-only consumer-facing rows for the free-help directory. Published registry data only:
    name, address, phone, and the source that published it. No emails (these are switchboard
    numbers), no contact scraping, no inference, and no row that has no phone to call."""
    out = {"rows": [], "cities": [], "total": 0, "grand_total": 0, "page": max(1, page),
           "per_page": per_page, "pages": 1, "city": city, "state": state, "source_note": ""}
    try:
        orgs = connect_orgs(org_db)
    except sqlite3.Error:
        return out
    try:
        rows = [dict(r) for r in orgs.execute("SELECT * FROM org_contacts ORDER BY org_name")]
    except sqlite3.Error:
        rows = []
    finally:
        orgs.close()
    clean = []
    for o in rows:
        if (o.get("dup_status") or "UNIQUE") != "UNIQUE":
            continue
        if ((o.get("record_kind") or "") not in VERIFIED_KINDS
                and (o.get("verify_status") or "") not in VERIFIED_STATUS):
            continue
        if (o.get("org_type") or "").lower() not in DIRECTORY_TYPES:
            continue
        parts = address_parts(o.get("address") or "")
        if state and parts["state"] and parts["state"] != state:
            continue
        phones = _phones(o)
        if not phones or not parts["city"]:
            # A row you cannot call, or cannot place in a city, is not help. Several seeded
            # nonprofit rows published only a service label ('Food Pantry') with no town; listing
            # them would pad the count and waste the visitor's time.
            continue
        clean.append({"name": (o.get("org_name") or "").strip(), "city": parts["city"],
                      "street": parts["street"], "zip": parts["zip"], "phone": phones[0],
                      "type": (o.get("org_type") or "").lower(), "source": o.get("source") or "",
                      "source_url": o.get("source_url") or ""})
    cities = sorted({c["city"] for c in clean if c["city"]})
    sel = [c for c in clean if c["city"].lower() == city.strip().lower()] if city else clean
    sel.sort(key=lambda c: (c["city"], c["name"]))
    per_page = max(1, min(200, per_page))
    pages = max(1, (len(sel) + per_page - 1) // per_page)
    p = min(max(1, page), pages)
    out.update(rows=sel[(p - 1) * per_page:p * per_page], cities=cities, total=len(sel),
               grand_total=len(clean), page=p, pages=pages,
               source_note=("Locations and phone numbers come from the official HRSA health-center "
                            "site file and CMS public registries. Hours and services change - call "
                            "before you go."))
    return out
