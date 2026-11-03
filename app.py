"""Lead Engine web app (Flask).

Public:   /c/<slug>  landing + value tool + multi-step form      /go/<code>  tracked redirect (QR, bios, ads)
          /embed/<slug>.js  partner widget   /optout  /privacy  /terms   /sitemap.xml   /robots.txt
API:      /api/event  /api/route-preview  /api/lead
Admin:    /admin?token=...   (overview, leads, lead detail, intel, channels, links, CSV export)

Run:  python app.py            (http://localhost:8000)
Env:  ADMIN_TOKEN, ALLOW_INDEXING=0|1, LEADENGINE_LIVE_DELIVERY=0|1, INTEL_DB=path
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import os
import secrets
import sys
import urllib.parse
import uuid
from pathlib import Path

from flask import make_response, Flask, Response, abort, jsonify, redirect, render_template, request, url_for

from engine import intel as intel_mod
from engine import billing, funnel, lab, notify, payments, quality, qualify, route, tracking
from engine import opportunities as opp_mod
from engine import ops as ops_mod
from engine import phone as phone_mod
from engine import partners as partners_mod
from engine import buyer_pipeline as bpline
from engine import signals as signals_mod
from engine import prospect_contacts as people_mod
from engine import signal_clusters as clusters_mod
from engine import sources as sources_mod
from engine import content_intel
from engine import acq_assets
from engine import leadgen
from engine import network as network_mod
from engine import hosting
from engine import prospector
from engine import discovery
from engine import identity as identity_mod
from engine import prospects as prospects_mod
from engine import admission as admission_mod
from engine import web_extract as webx
from engine.db import connect, insert, now

BASE = Path(__file__).resolve().parent
CFG = Path(os.environ.get("LEADENGINE_CFG", BASE / "config"))
DB_PATH = Path(os.environ.get("LEADENGINE_DB", BASE / "data" / "leads.db"))
INTEL_DB = Path(os.environ.get("INTEL_DB", BASE.parent / "leadgen" / "data" / "leadgen.db"))
SIGNALS_DB = Path(os.environ.get("SIGNALS_DB", BASE / "data" / "signals.db"))
PEOPLE_DB = Path(os.environ.get("PEOPLE_DB", BASE / "data" / "prospect_people.db"))
PROSPECTS_DB = Path(os.environ.get("PROSPECTS_DB", BASE / "data" / "prospects.db"))
OUTBOX = Path(os.environ.get("LEADENGINE_OUTBOX", BASE / "outbox"))
SNAP = Path(os.environ.get("LEADENGINE_SNAP", BASE / "data" / "snapshots"))
ORG_DB = Path(os.environ.get("ORG_CONTACTS_DB", BASE / "data" / "org_contacts.db"))
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "DEVLOCAL")
ALLOW_INDEXING = os.environ.get("ALLOW_INDEXING", "0") == "1"
PRODUCTION = os.environ.get("LEADENGINE_ENV") == "production"
if PRODUCTION and (ADMIN_TOKEN in ("DEVLOCAL", "change-me", "demo", "") or len(ADMIN_TOKEN) < 16):
    raise SystemExit("Refusing to start: set a strong ADMIN_TOKEN (16+ chars) in production.")
# Payment-rail fail-safes: a wrong or half-configured Stripe rail must stop startup, not start a
# service that charges real cards or hands out credits nobody can verify. Test-mode keys (sk_test_)
# are always safe. Live keys (sk_live_) may only ever exist with LEADENGINE_ENV=production.
_sk = os.environ.get("STRIPE_SECRET_KEY", "").strip()
_wh = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
if _sk and not (_sk.startswith("sk_test_") or _sk.startswith("sk_live_")):
    raise SystemExit("Refusing to start: STRIPE_SECRET_KEY is set but is neither sk_test_… nor sk_live_… (typo/unknown key - would create sessions nobody can fulfill).")
if _sk.startswith("sk_live_") and not PRODUCTION:
    raise SystemExit("Refusing to start: sk_live_ keys are production-only. Remove the live key from this environment (use sk_test_ for staging).")
if _sk and not _wh:
    raise SystemExit("Refusing to start: STRIPE_SECRET_KEY without STRIPE_WEBHOOK_SECRET - buyers could pay but no event could ever be verified, so credits would never be granted. Configure both or neither.")
if PRODUCTION and _wh and not _sk:
    raise SystemExit("Refusing to start: STRIPE_WEBHOOK_SECRET without STRIPE_SECRET_KEY - half-configured payment rail.")
# Consumer-privacy fail-safe: the public-record prospect intel DB (names/addresses of real people
# scraped from public sources) lives on the operator's machine only. It must never be uploaded to a
# production server - production reads briefs from config/regional_briefs.json instead (see launch_check.py).
if PRODUCTION and os.environ.get("INTEL_DB", "").strip():
    raise SystemExit("Refusing to start: INTEL_DB is set in production. The public-record prospect DB stays on your machine; delete the variable (regional briefs ship as config/regional_briefs.json).")
# Storage durability (engine/hosting.py): a database the host wipes on restart cannot hold consent
# evidence (10 years), buyer ledgers or payment rows. On such a host the app runs a READ-ONLY public
# preview — it serves the real pages, but refuses submissions it could not keep instead of losing them.
#
# The mode is a DECLARATION (LEADENGINE_STORAGE), never a silent inference: a guessed path can be
# wrong either way (a mounted volume under /app, a Windows server on D:\). An undeclared path is
# still reported on /healthz, on Admin > Readiness and by launch_check.py — loudly — so nothing is
# collected on a host nobody thought about.
STORAGE = hosting.storage_mode(DB_PATH)
READONLY_PREVIEW = PRODUCTION and hosting.storage_declared() == "ephemeral"
if READONLY_PREVIEW:
    print("[launch] READ-ONLY PREVIEW: LEADENGINE_DB (" + str(DB_PATH) + ") is declared ephemeral, so this "
          "host does not keep what it stores. Public submissions are refused rather than stored and lost; "
          "leads, consent records and payment rows need a persistent disk. See docs/RENDER_DEPLOY.md.",
          file=sys.stderr)
elif PRODUCTION and STORAGE == "ephemeral":
    print("[launch] WARNING: LEADENGINE_DB (" + str(DB_PATH) + ") is not recognisably on a persistent disk and "
          "LEADENGINE_STORAGE is unset. Declare LEADENGINE_STORAGE=persistent (the path is a mounted volume) or "
          "LEADENGINE_STORAGE=ephemeral (this host keeps nothing: the app then runs a read-only public preview). "
          "launch_check.py and Admin > Readiness report this until it is declared. See docs/RENDER_DEPLOY.md.",
          file=sys.stderr)

app = Flask(__name__)
# Dev/demo reliability: template edits must appear on the next page load so a live
# demo or review can never show long-cached stale markup (the "${60.00}" incident
# fixed in the file kept rendering until the server was restarted). Production
# keeps caching ON for speed; the switch is a one-liner to verify locally.
app.config["TEMPLATES_AUTO_RELOAD"] = not PRODUCTION
# Public-write hardening: a hostile client can otherwise flood the DB (and, live, drain buyer credits).
# 2 MB is far above any real consumer payload (snapshots are capped at ~200 KB) but stops body bombs.
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
# Throttle real (non-test) lead submissions per source IP per hour. Tuned leniently: a single address
# submitting this many consented leads in an hour is a form filler / script, not a real consumer run.
IP_LEADS_PER_HOUR = int(os.environ.get("LEADENGINE_IP_LEADS_PER_HOUR", "30"))
# Same idea for analytics events per fake visitor id (a scripted flood would poison acquisition metrics).
IP_EVENTS_PER_HOUR = int(os.environ.get("LEADENGINE_IP_EVENTS_PER_HOUR", "120"))
app.jinja_env.filters["fromjson"] = lambda s: json.loads(s) if s else {}
class _ThreadLocalDB:
    """One SQLite connection per thread. A single shared connection breaks under concurrent requests
    (gunicorn threads / Flask threaded dev server) and silently drops writes. WAL mode lets them coexist."""

    def __init__(self, path):
        import threading
        self._path, self._local = path, threading.local()

    def _c(self):
        c = getattr(self._local, "c", None)
        if c is None:
            c = self._local.c = connect(self._path)
        return c

    def __getattr__(self, name):
        return getattr(self._c(), name)


con = _ThreadLocalDB(DB_PATH)


def cfg(name):
    # encoding='utf-8': on Windows the default cp1252 mangles the Spanish text (seguro médico → mojibake)
    return json.loads((CFG / f"{name}.json").read_text(encoding="utf-8"))


# ONE list of config keys the deployment may override from the environment. It is applied at import
# AND on every reload, so an admin config reload can never silently drop the host's real origin or
# identity (that used to be a trap: reload_cfg() re-read the file and lost BASE_URL).
SITE_ENV_OVERRIDES = (("base_url", "BASE_URL"), ("meta_pixel_id", "META_PIXEL_ID"), ("ga4_id", "GA4_ID"),
                      ("brand", "BRAND"), ("legal_entity", "LEGAL_ENTITY"), ("location", "LOCATION"),
                      ("founded", "FOUNDED"), ("contact_email", "CONTACT_EMAIL"),
                      ("mailing_address", "MAILING_ADDRESS"), ("support_phone", "SUPPORT_PHONE"),
                      ("support_email", "SUPPORT_EMAIL"))


def _apply_env_site(site: dict) -> dict:
    """Environment wins over config/site.json, for every key in SITE_ENV_OVERRIDES.

    The origin is then resolved ONCE, centrally (engine/hosting.py): BASE_URL > the host-assigned
    origin (Render publishes RENDER_EXTERNAL_URL = https://<service>.onrender.com) > config. A fresh
    deploy therefore serves correct canonicals, sitemap, robots and QR codes at its assigned URL before
    anyone pastes a domain in — and `_base_url_source` records where the value came from, so no page
    has to guess."""
    for _k, _env in SITE_ENV_OVERRIDES:
        _v = os.environ.get(_env)
        if _v:
            site[_k] = _v.rstrip("/") if _k == "base_url" else _v
    site["base_url"] = hosting.public_origin(site)
    site["_base_url_source"] = hosting.origin_source(site)
    return site


SITE, QUESTIONS, CONSENT = cfg("site"), cfg("questions"), cfg("consent")
_apply_env_site(SITE)
if PRODUCTION and re.match(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$", SITE.get("base_url") or "x", re.I):
    # Loud, not fatal: a first deploy often lands before DNS exists. But every canonical URL, sitemap
    # entry, QR code and prepared link would point at a machine no visitor can reach.
    print("[launch] WARNING: BASE_URL is still a loopback origin (" + str(SITE.get("base_url")) + "). "
          "Canonical URLs, the sitemap, QR codes and every prepared link will point at an unreachable "
          "host. Set BASE_URL=https://yourdomain.com before sending traffic.", file=sys.stderr)
CAMPAIGNS = {c["slug"]: c for c in cfg("campaigns")}
CAMPAIGN_BY_ID = {c["id"]: c for c in CAMPAIGNS.values()}
PARTNERS = {p["id"]: p for p in cfg("partners")} if (CFG / "partners.json").exists() else {}
# buyer marketplace: prepaid credit ledger + (configurable) credits-before-routing enforcement
MARKETPLACE = billing.load_marketplace(CFG) if (CFG / "marketplace.json").exists() else billing.defaults()


def get_marketplace():
    """Single truth for return/credit policy and buyer-money config: always reads the CURRENT global,
    so after reload_cfg() every page (for-agents, desk, billing, set_status) agrees with the engine
    instead of one request carrying a stale snapshot copy."""
    return MARKETPLACE


def _credit_enforcing() -> bool:
    """Whether prepaid credits gate ROUTING. The marketplace switch turns enforcement on deliberately
    in staging; a LIVE deployment always enforces it for real traffic, so an unfunded or overdrawn
    buyer can never receive a real consumer's consented lead (a sale nobody can pay for is a billing
    failure, not revenue). TEST traffic keeps the switch's own behaviour, so demo rehearsals and the
    fixture suite still run without credits."""
    return bool(MARKETPLACE.get("enforce_credits")) or PRODUCTION


def _blocked_buyers(buyers: list[dict], test: bool) -> frozenset:
    """Buyers excluded from the routing-preview/consent when credit enforcement is on (billing.blocked_ids, else empty)."""
    # production: enforcement applies to the REAL lane only (test/demo rehearsals stay credit-free)
    return billing.blocked_ids(con, buyers, MARKETPLACE, 1 if test else 0,
                               production=PRODUCTION and not test)


def _health() -> dict:
    """Quality-aware routing inputs: buyer_health over the configured window (marketplace.buyer_health).
    Empty when disabled — every caller computes it once per request so preview/route/explain agree."""
    return route.buyer_health(con, MARKETPLACE.get("buyer_health", {}))


def reload_cfg():
    global SITE, QUESTIONS, CONSENT, CAMPAIGNS, CAMPAIGN_BY_ID, MARKETPLACE
    SITE, QUESTIONS, CONSENT = cfg("site"), cfg("questions"), cfg("consent")
    _apply_env_site(SITE)                                  # reload keeps the deployment's origin/identity
    CAMPAIGNS = {c["slug"]: c for c in cfg("campaigns")}
    CAMPAIGN_BY_ID = {c["id"]: c for c in CAMPAIGNS.values()}
    MARKETPLACE = billing.load_marketplace(CFG) if (CFG / "marketplace.json").exists() else billing.defaults()


def _real_value(v) -> str:
    """A configured business value, or '' — never a placeholder. The bracket convention ([YOUR LLC NAME]),
    CHANGE-ME and the sample domain are all 'not supplied yet', and a public page must not print them."""
    s = (v or "").strip()
    if not s or s.startswith("[") or "CHANGE-ME" in s.upper() or "yourdomain" in s.lower():
        return ""
    return s


def business_identity() -> dict:
    """ONE reading of the real business identity, shared by the public pages, the footer and the
    readiness screen, so no surface can print a different name/address than another (or print a
    placeholder one). `missing` is what the operator still has to supply — a list, not a guess."""
    ident = {
        "brand": _real_value(SITE.get("brand")),
        "legal_entity": _real_value(SITE.get("legal_entity")),
        "location": _real_value(SITE.get("location")),
        "founded": _real_value(SITE.get("founded")),
        "mailing_address": _real_value(SITE.get("mailing_address")),
        "support_email": _real_value(SITE.get("support_email") or SITE.get("contact_email")),
        "support_phone": _real_value(SITE.get("support_phone")),
    }
    # A street address is NOT required for this website configuration (owner decision 2026-11-02):
    # the public location is enough, and no invented street address is ever printed.
    ident["missing"] = [k for k in ("legal_entity", "location", "support_email") if not ident[k]]
    ident["complete"] = not ident["missing"]
    return ident


def _contact_ref(lang="en"):
    """How the site says 'contact us': the on-site contact form when CONTACT_FORM=1 (no mailbox needed), else the email."""
    from markupsafe import Markup
    email = (business_identity()["support_email"] or SITE.get("contact_email") or "").strip()
    # A bracketed config placeholder ([support@yourdomain.com]) must never be printed on a public
    # page: fall back to the on-site form, which is real and always works.
    if os.environ.get("CONTACT_FORM") == "1" or not email or email.startswith("["):
        return Markup('<a href="/contact">' + ("formulario de contacto" if lang == "es" else "contact form") + "</a>")
    return email


def _pretty_date(iso, lang="en"):
    """'2026-10-08' -> '8 de octubre de 2026' / 'October 8, 2026'. Returns '' for a missing/invalid date (never invents one)."""
    import datetime as _dt
    try:
        d = _dt.date.fromisoformat(str(iso)[:10])
    except Exception:
        return ""
    if lang == "es":
        m = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"][d.month - 1]
        return f"{d.day} de {m} de {d.year}"
    return _pretty_day_en(d)


def _pretty_day_en(d):
    """'October 8, 2026' — no %-d (that glibc extension raises ValueError on Windows)."""
    return d.strftime("%B ") + str(d.day) + d.strftime(", %Y")


app.jinja_env.filters["pretty_date"] = _pretty_date
app.jinja_env.globals["now"] = lambda: now()   # buyer side renders 'overview corrected …' timestamps
app.jinja_env.globals["contact_ref"] = _contact_ref
app.jinja_env.filters["pphone"] = lambda v: phone_mod.pretty(phone_mod.digits(v or ""))

NO_BUYER_DISC = {
    "disclosure": "We are not an insurance company, an insurance agency or a government site, and we do not sell insurance. We publish educational information about health coverage. We do not currently connect anyone with an agent, and we do not ask for your phone number or email in the coverage check. Estimates are not quotes, eligibility decisions or offers of coverage.",
    "disclosure_es": "No somos una aseguradora, ni una agencia de seguros, ni un sitio del gobierno, y no vendemos seguros. Publicamos información educativa sobre cobertura médica. Por ahora no conectamos a nadie con un agente y el chequeo no te pide teléfono ni correo. Los cálculos no son cotizaciones, decisiones de elegibilidad ni ofertas de cobertura.",
}


# Bilingual pairs. Every public page has exactly one counterpart in the other language, so the
# canonical URL and the hreflang alternates can be derived from the request path instead of being
# hand-maintained in 70 templates.
_LANG_PAIRS = {
    "/": "/?lang=en", "/?lang=en": "/",
    "/learn": "/aprende", "/aprende": "/learn",
    "/privacy": "/privacidad", "/privacidad": "/privacy",
    "/terms": "/terminos", "/terminos": "/terms",
    "/free-help": "/ayuda-gratis", "/ayuda-gratis": "/free-help",
    "/open-enrollment-2027": "/inscripcion-abierta-2027",
    "/inscripcion-abierta-2027": "/open-enrollment-2027",
    "/layoffs": "/despidos", "/despidos": "/layoffs",
    "/for-agents": "/para-agencias", "/para-agencias": "/for-agents",
}


# The check's language lives in ?a=<angle>, so the "same page in the other language" is the twin angle.
# Only some hooks have a real translation; the rest fall back to the generic angle of that language
# rather than pretending an untranslated hook exists.
_ANGLE_TWINS = {"self_employed": "es_self_employed", "life_change": "es_life_change",
                "family": "es_family", "plan_change": "es_plan_change"}


def _angle_pair(arg: str, es: bool) -> tuple[str, str]:
    """(this page's angle, the other language's angle) for /check."""
    angles = HEALTH_FLOW["angles"]
    aid = arg if arg in angles else ("es_check" if es else "health_check")
    twin = _ANGLE_TWINS.get(aid)
    if twin is None and aid.startswith("es_"):
        twin = aid[3:] if aid[3:] in angles else None
    if twin is None:
        twin = "es_check" if not es else "health_check"
    return aid, twin


def _alt_url(path: str, es: bool) -> str:
    """The same page in the other language, or '' when the page genuinely has only one version.
    Content pages are written in one language each (config/content.json has no cross-language
    slug field), so they correctly report no alternate instead of inventing a twin URL."""
    if path == "/":
        return "/?lang=en" if es else "/"     # current ES home -> EN twin; current EN home -> ES twin
    if path in _LANG_PAIRS:
        return _LANG_PAIRS[path]
    if path.startswith("/layoffs/") or path.startswith("/despidos/"):
        slug = path.split("/", 2)[2]
        return f"{'/despidos' if path.startswith('/layoffs/') else '/layoffs'}/{slug}"
    if path.startswith(("/learn/", "/aprende/")):
        p_ = CONTENT_BY_SLUG.get(path.split("/", 2)[2])
        if p_ and p_.get("twin"):        # content.json now records the real translation pair
            return f"{'/aprende' if p_['lang'] == 'en' else '/learn'}/{p_['twin']}"
    return ""


@app.context_processor
def inject():
    site = SITE
    # Never say "we connect you with licensed agents" while no real agent is configured: that would promise a service we cannot deliver.
    if PRODUCTION and not _buyers(False):
        site = {**SITE, **NO_BUYER_DISC, "no_buyer": True}
    _lang_arg = (request.args.get("lang") or "").strip().lower()
    _angle_arg = (request.args.get("a") or "").strip().lower()
    # Bare / is rendered in the visitor's PREFERRED language (home() calls _pref_lang), so the flag
    # must come from that same decision — otherwise the header switch is built for a language the
    # page is not actually showing (an English browser at / was offered "/?lang=en", i.e. itself).
    _es_req = ((request.path == "/" and _lang_arg != "en" and _pref_lang() == "es")
               or _lang_arg == "es" or request.path.startswith("/aprende")
               or request.path in ("/privacidad", "/terminos", "/ayuda-gratis",
                                   "/inscripcion-abierta-2027", "/despidos"))
    if request.path == "/check":         # the check's language lives in ?a=<angle>, not in the path
        if _angle_arg:                    # an explicit angle decides, and it decides BEFORE the switch link is built
            _es_req = _angle_arg.startswith("es")
        elif _lang_arg in ("en", "es"):
            _es_req = _lang_arg == "es"
        else:
            _es_req = _pref_lang() == "es"  # cookie, then the visitor's own browser
    if request.path == "/" and _lang_arg != "en":
        # The switch needs ?lang= to beat the sticky le_lang cookie set by home(): an English visitor
        # at / is sent to the Spanish home, a Spanish visitor to the English twin URL.
        alt = ("/?lang=en" if _es_req else "/?lang=es") if request.method == "GET" else ""
    else:
        alt = _alt_url(request.path, _es_req) if request.method == "GET" else ""
    _origin = _site_origin()                       # no absolute origin yet => no canonical tag at all
    canonical = (_origin + request.path) if _origin else ""
    if request.path == "/check":          # two languages share one path: the angle is what tells them apart
        _aid, _twin = _angle_pair(_angle_arg, _es_req)
        alt = f"/check?a={_twin}&lang={'es' if _twin.startswith('es') else 'en'}"   # the language the link GOES TO
        canonical += "?a=" + _aid
    elif request.path == "/" and _lang_arg == "en":   # bare / is Spanish-first, so the EN twin is its own URL
        canonical += "?lang=en"
    return {"site": site, "allow_indexing": ALLOW_INDEXING, "staging": os.environ.get("STAGING") == "1",
            "biz": business_identity(),
            "canonical": canonical, "alt_url": alt, "current_path": request.path}


# ----------------------------------------------------------------- public pages
def _pref_lang() -> str:
    """Which language this visitor should start in: an explicit ?lang= wins, then the choice they made
    last time (le_lang cookie), then their own browser's Accept-Language. Spanish stays the default
    for Florida traffic that says nothing, and the header switch is on every page either way."""
    p = (request.args.get("lang") or "").strip().lower()
    if p in ("en", "es"):
        return p
    ck = (request.cookies.get("le_lang") or "").strip().lower()
    if ck in ("en", "es"):
        return ck
    return "en" if (request.headers.get("Accept-Language") or "").lower().startswith("en") else "es"


@app.get("/")
def home():
    # Spanish-first home (English at /?lang=en). Life campaigns are not promoted; the check/guides are the entry points.
    lang = _pref_lang()
    resp = make_response(render_template("index.html", lang=lang))
    resp.set_cookie("le_lang", lang, max_age=60 * 60 * 24 * 365, samesite="Lax")   # the choice sticks
    return resp


@app.get("/c/<slug>")
def landing(slug):
    c = CAMPAIGNS.get(slug) or abort(404)
    if c["product"] != "health_aca" and PRODUCTION and not _buyers(False):
        return redirect("/", code=302)   # non-health (life) forms stay paused while there is no named buyer; config is untouched
    if c["product"] == "health_aca" and not request.args.get("legacy"):   # health traffic -> branching check
        q = dict(request.args); q.setdefault("a", "es_check" if c.get("lang") == "es" else "health_check")
        return redirect(url_for("health_check", **q), code=302)
    lang = c.get("lang", "en")
    steps = []
    for sid in c["steps"]:
        q = dict(QUESTIONS[sid], id=sid)
        if lang == "es":
            q["label"] = q.get("label_es", q["label"])
            q["options"] = q.get("options_es", q.get("options"))
        steps.append(q)
    # A/B: sticky per-browser variant (cookie), forced with ?v=<id> for previews/QA
    variants = c.get("variants") or []
    vid = request.args.get("v") or request.cookies.get(f"le_v_{c['id']}")
    var = next((v for v in variants if v["id"] == vid), None) or (secrets.choice(variants) if variants else None)
    cv = dict(c)
    if var:
        cv.update({k: var[k] for k in ("headline", "subhead", "cta") if k in var})
        cv["variant"] = var["id"]
    partner = PARTNERS.get(request.args.get("partner", ""))
    resp = app.make_response(render_template("landing.html", c=cv, steps=steps, embed=request.args.get("embed") == "1",
                                             states=SITE["served_states"], consent_version=CONSENT["version"],
                                             partner=partner if partner and partner.get("cobrand_approved") else None))
    if var:
        resp.set_cookie(f"le_v_{c['id']}", var["id"], max_age=60 * 60 * 24 * 60, samesite="Lax")
    return resp


HEALTH_FLOW = json.loads((CFG / "health_flow.json").read_text(encoding="utf-8"))
BRIDGE = json.loads((CFG / "bridge.json").read_text(encoding="utf-8"))


@app.get("/i/<code>")
def bridge(code):
    """The consumer bridge: where a link placed in a public discussion actually lands.

    Why not straight into /check: a person arriving cold from a thread needs to know what this is,
    what it costs (nothing), what they will see, and what will NOT happen (nobody calls unless they
    ask) BEFORE being asked for answers. This page says that, in their language, matched to the
    situation their own post described, and hands them one CTA that carries `lc` so the visit and any
    later submission stay attributed to the exact discussion that produced it.

    It stores nothing and asks for nothing. The only data touchpoints are the analytics event the
    browser sends (no contact fields) and, if the person chooses, the Check itself.
    """
    l = con.execute("SELECT * FROM links WHERE code=?", (code,)).fetchone() or abort(404)
    want = request.args.get("lang")
    if want in ("en", "es"):
        lang = want
    else:
        lang = "es" if (l["lang"] or "") == "es" else "en"
    keys = l.keys()
    sit = (l["situation"] if "situation" in keys else "") or "coverage"
    block = BRIDGE["situations"].get(sit) or BRIDGE["situations"]["coverage"]
    s = {"situation": sit if sit in BRIDGE["situations"] else "coverage",
         "eyebrow": block["eyebrow_" + lang], "title": block["title_" + lang],
         "intro": block["intro_" + lang], "bullets": block["bullets_" + lang]}
    q = {"a": block["angle_" + lang], "lc": code}
    for k in ("utm_source", "utm_medium", "utm_campaign", "utm_content"):
        if l[k]:
            q[k] = l[k]
    if l["experiment_id"]:
        q["x"] = l["experiment_id"]
    if l["partner_id"]:
        q["partner"] = l["partner_id"]
    if request.args.get("test") == "1":
        q["test"] = "1"
    other = url_for("bridge", code=code, lang=("en" if lang == "es" else "es"),
                    **({"test": "1"} if q.get("test") else {}))
    return render_template("bridge.html", lang=lang, s=s, cta=url_for("health_check", **q),
                           other_url=other, code=code)


@app.get("/check")
def health_check():
    """The Health Coverage Check. ?a=<angle> picks the hook (EN/ES); ?x=<experiment> attributes the visit."""
    angles = HEALTH_FLOW["angles"]
    aid = request.args.get("a") or ("es_check" if _pref_lang() == "es" else "health_check")
    if aid not in angles:
        aid = "health_check"
    angle = angles[aid]
    lang = angle["lang"]
    partner = PARTNERS.get(request.args.get("partner", "")) or _partner_db(request.args.get("partner", ""))
    return render_template("check.html", angle=angle, angle_id=aid, lang=lang, flow=HEALTH_FLOW,
                           campaign_id="oep-2027-health-es" if lang == "es" else "oep-2027-health",
                           embed=request.args.get("embed") == "1", partner=partner if partner and partner.get("cobrand_approved") else None,
                           email_fallback=bool(SITE.get("email_fallback")))


@app.get("/free-help")
@app.get("/ayuda-gratis")
def help_directory():
    """Public, indexable directory of organizations that give FREE in-person enrollment help.

    Inbound acquisition, not data collection: the page lists locations their own official registry
    publishes (name, address, switchboard phone) and hands the visitor options - call one of these,
    or run the check. It asks for nothing, and it is the same verified org ledger the referral
    network is built from, so the two channels can never disagree.
    """
    es = request.path == "/ayuda-gratis" or request.args.get("lang") == "es"
    city = (request.args.get("city") or "").strip()[:60]
    try:
        page = int(request.args.get("page") or 1)
    except ValueError:
        page = 1
    states = SITE.get("served_states") or ["FL"]
    d = network_mod.directory(ORG_DB, state=states[0], city=city, page=page, per_page=40)
    items = []
    for i, r in enumerate(d["rows"]):
        if not r["phone"]:
            continue
        addr = {"@type": "PostalAddress", "addressLocality": r["city"] or "",
                "addressRegion": states[0], "addressCountry": "US"}
        if r["street"]:
            addr["streetAddress"] = r["street"]
        if r["zip"]:
            addr["postalCode"] = r["zip"]
        items.append({"@type": "ListItem", "position": i + 1,
                      "item": {"@type": "MedicalClinic", "name": r["name"],
                               "telephone": r["phone"], "address": addr}})
    ld = {"@context": "https://schema.org", "@type": "ItemList",
          "name": ("Ayuda gratis en persona para inscribirse en cobertura médica" if es else
                   "Free in-person help enrolling in health coverage"),
          "itemListElement": items}
    return render_template("help_directory.html", lang="es" if es else "en", **d,
                           jsonld=json.dumps(ld, ensure_ascii=False).replace("<", "\\u003c"))


@app.get("/privacidad")
def privacidad():
    return render_template("simple.html", page="privacidad")


@app.get("/terminos")
def terminos():
    return render_template("simple.html", page="terminos")


def _partner_db(pid):
    if not pid:
        return None
    r = con.execute("SELECT * FROM partners WHERE partner_id=?", (pid,)).fetchone()
    return dict(r, id=r["partner_id"]) if r else None


BOT_UA = re.compile(r"bot|crawl|spider|preview|facebookexternalhit|slack|whatsapp|telegram|curl|wget|python-requests|headless", re.I)


@app.get("/go/<code>")
def go(code):
    l = con.execute("SELECT * FROM links WHERE code=?", (code,)).fetchone() or abort(404)
    ua = (request.headers.get("User-Agent") or "").lower()
    qa = request.args.get("test") == "1" or request.cookies.get("le_qa") == "1"
    if not qa and not BOT_UA.search(ua):   # tests, QA browsers, link-preview bots and crawlers are not clicks
        con.execute("UPDATE links SET clicks=clicks+1 WHERE code=?", (code,))
        con.commit()
    q = {k: l[k] for k in ("utm_source", "utm_medium", "utm_campaign", "utm_content") if l[k]}
    q["lc"] = code
    if l["experiment_id"]:
        q["x"] = l["experiment_id"]
    if l["partner_id"]:
        q["partner"] = l["partner_id"]
    if l["phone_variant"]:
        q["pv"] = l["phone_variant"]
    if request.args.get("test") == "1":
        q["test"] = "1"
    # per-placement tags on one tracking link: /go/x013?c=story vs ?c=reel, /go/x015?a=es_check&c=es-adgroup
    if re.fullmatch(r"[a-z0-9_-]{1,40}", request.args.get("c", "")):
        q["utm_content"] = request.args["c"]
    angle_override = request.args.get("a") if request.args.get("a") in HEALTH_FLOW["angles"] else None
    c = CAMPAIGN_BY_ID.get(l["campaign"])
    lp = (l["landing_page"] or "").strip()
    if lp.startswith("/") and not lp.startswith("//"):          # experiment-specific landing page (e.g. a /learn article)
        path, _, extra = lp.partition("?")
        merged = dict(urllib.parse.parse_qsl(extra))
        merged.update(q)
        if path == "/check" and (angle_override or ("a" not in merged and l["angle"])):
            merged["a"] = angle_override or l["angle"]
        return redirect(path + "?" + urllib.parse.urlencode(merged), code=302)
    if l["angle"] or (c and c["product"] == "health_aca") or not c:
        q["a"] = angle_override or l["angle"] or ("es_check" if (l["lang"] == "es" or (c and c.get("lang") == "es")) else "health_check")
        return redirect(url_for("health_check", **q), code=302)
    return redirect(url_for("landing", slug=c["slug"], **q), code=302)


@app.get("/qa")
def qa_mode():
    """Mark THIS browser as a QA/test device so your own clicks on live links never count as real traffic."""
    on = request.args.get("on", "1") == "1"
    resp = make_response(render_template("simple.html", page="qa", on=on))
    if on:
        resp.set_cookie("le_qa", "1", max_age=3600 * 24 * 365, samesite="Lax")
    else:
        resp.delete_cookie("le_qa")
    return resp


@app.get("/p/<partner_id>")
def partner_entry(partner_id):
    """Partner short link (QR on guides, onboarding portals, counter cards): /p/<partner>[?c=<campaign_id>]."""
    p = _partner_db(partner_id) or abort(404)
    if request.args.get("c") and request.args.get("c") in CAMPAIGN_BY_ID and CAMPAIGN_BY_ID[request.args["c"]]["product"] != "health_aca":
        c = CAMPAIGN_BY_ID[request.args["c"]]
        q = {"utm_source": "partner", "utm_medium": p["type"], "utm_campaign": c["id"], "utm_content": request.args.get("src", "qr"), "partner": p["id"]}
        return redirect(url_for("landing", slug=c["slug"], **q), code=302)
    q = {"a": p["default_angle"], "utm_source": "partner", "utm_medium": p["type"], "utm_content": request.args.get("src", "qr"), "partner": p["id"]}
    if request.args.get("test") == "1":
        q["test"] = "1"
    return redirect(url_for("health_check", **q), code=302)


@app.get("/partner/<token>")
def partner_stats(token):
    """What a partner sees: their own counts only - never names or contact details of consumers."""
    r = con.execute("SELECT * FROM partners WHERE token=?", (token,)).fetchone() or abort(404)
    p = dict(r, id=r["partner_id"])
    st = partners_mod.stats(con, p["id"])
    stats = {"visits": st["visits"], "used_tool": st["used_check"], "requested_help": st["requested_help"]}
    return render_template("simple.html", page="partner", p=p, stats=stats, base=SITE["base_url"])


@app.get("/embed/<slug>.js")
def embed(slug):
    c = CAMPAIGNS.get(slug) or abort(404)
    partner = request.args.get("partner", "")
    src = f"{SITE['base_url']}/c/{c['slug']}?embed=1&utm_source=partner&utm_medium=embed&utm_campaign={c['id']}&partner={partner}"
    js = ("(function(){var s=document.currentScript;var f=document.createElement('iframe');"
          f"f.src={json.dumps(src)};f.style.cssText='width:100%;max-width:560px;height:780px;border:0;border-radius:16px';"
          "f.title='Coverage check';f.loading='lazy';s.parentNode.insertBefore(f,s);})();")
    return Response(js, mimetype="application/javascript")


@app.route("/optout", methods=["GET", "POST"])
def optout():
    """Record a real opt-out. A submission that identifies NOBODY must never render the success
    message: telling a person 'we recorded it, you will not be contacted again' when nothing was
    stored is a false promise about a legal request. The same identifier list gates new submissions
    (see _opted_out), so an opt-out actually stops a later sale instead of only flagging old rows."""
    done, err = False, ""
    if request.method == "POST" and READONLY_PREVIEW:
        # "We recorded your opt-out" cannot be true on a host that erases the file on restart, and
        # this deployment contacts nobody, so the honest answer is that nothing was stored.
        err = ("Nothing was recorded: this preview deployment does not store data yet, and it does not "
               "contact anyone either. / No se registró nada: esta vista previa todavía no guarda datos y "
               "tampoco contacta a nadie.")
    elif request.method == "POST":
        recorded = []
        for kind in ("email", "phone"):
            raw = (request.form.get(kind) or "").strip().lower()
            v = phone_mod.digits(raw) if kind == "phone" else raw[:160]   # any real 10-digit number (+1 or not)
            if kind == "phone" and len(v) != 10:
                v = ""
            if v:
                con.execute("INSERT OR REPLACE INTO optouts VALUES (?,?,?,?)", (v, kind, now(), "web_form"))
                con.execute(f"UPDATE leads SET opted_out=1 WHERE lower({kind})=?", (v,))
                recorded.append(kind)
        if recorded:
            con.commit()
            done = True
        else:
            err = ("Nothing was recorded: enter the email or the 10-digit phone number you used, so we know "
                   "whom to stop contacting. / No se registró nada: escribe el correo o el teléfono de 10 "
                   "dígitos que usaste.")
    return render_template("simple.html", page="optout", done=done, err=err, prefill=request.args.get("email", ""),
                           ui_lang=_pref_lang())


@app.route("/contact", methods=["GET", "POST"])
def contact():
    """A consumer's support message. Stored, notified to the configured inbox (dry-run .eml without
    SMTP) and listed on Admin ▸ Inquiries — an empty message is refused instead of being answered with
    a false 'we received it', because a support channel that silently drops requests is worse than none."""
    done, err = False, ""
    if request.method == "POST":
        msg = (request.form.get("message") or "").strip()[:2000]
        reply_to = (request.form.get("reply_to") or "").strip()[:120]
        if READONLY_PREVIEW:
            # The host wipes the database on restart: storing the message and answering "we received
            # it" would be a false promise, so say plainly that nothing was sent.
            err = ("This preview deployment does not store messages yet, so nothing was sent. "
                   "/ Esta vista previa todavía no guarda mensajes; no se envió nada.")
        elif (request.form.get("website") or "").strip():        # honeypot: answer 200, store nothing
            done = True
        elif not msg:
            err = "Please write a message before sending. / Escribe un mensaje antes de enviar."
        else:
            con.execute("CREATE TABLE IF NOT EXISTS contact_msgs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, message TEXT, reply_to TEXT)")
            con.execute("INSERT INTO contact_msgs (ts, message, reply_to) VALUES (?,?,?)", (now(), msg, reply_to))
            con.commit()
            try:
                notify.consumer_inquiry(msg, reply_to, {**SITE, **business_identity()}, OUTBOX)
            except Exception as exc:                            # noqa: BLE001 - the message is already stored
                app.logger.warning("consumer inquiry notification failed: %r", exc)
            done = True
    return render_template("simple.html", page="contact", done=done, err=err, ui_lang=_pref_lang())


@app.get("/privacy")
def privacy():
    return render_template("simple.html", page="privacy")


# ----------------------------------------------------------------- buyer acquisition (public): agency application
# The commercial funnel for the SELL side: a licensed agency lands here, applies, and the operator reviews
# the resulting draft buyer on Admin > Buyers before anything can ever route a lead to them.
@app.get("/for-agents")
@app.get("/para-agencias")
def for_agents():
    """Buyer onboarding, EN at /for-agents and ES at /para-agencias. Same handler, same fields, same
    /for-agents/apply POST - only the rendered language differs, so the two versions can never drift
    into accepting different information."""
    es = request.path == "/para-agencias"
    return render_template("para_agencias.html" if es else "for_agents.html",
                           packages=MARKETPLACE.get("packages", []),
                           returns=get_marketplace().get("returns", {}),
                           enforcing=_credit_enforcing(),
                           err=_apply_err_es(request.args.get("err", "")) if es else request.args.get("err", ""))


# The application form refuses for five specific reasons; a Spanish applicant must read the same
# reason in Spanish, not an English sentence under a Spanish heading.
_APPLY_ERR_ES = {
    "CRM webhook delivery needs an https:// URL - consumer contact data is never posted over plain http":
        "la entrega por webhook del CRM necesita una URL https:// - los datos de contacto del consumidor nunca se envian por http sin cifrar",
    "agency name is required, with no placeholder brackets - it identifies your business on every lead record":
        "el nombre legal de la agencia es obligatorio y no puede llevar corchetes de ejemplo - es exactamente lo que el consumidor acepta en el consentimiento",
    "contact name is required": "el nombre de contacto es obligatorio",
    "a reply-to email is required": "se necesita un correo electronico para responderte",
}


def _apply_err_es(why: str) -> str:
    if not why:
        return ""
    if why in _APPLY_ERR_ES:
        return _APPLY_ERR_ES[why]
    if why.startswith("licensed state(s) are required"):
        return ("se necesita al menos un estado con licencia (aquí servimos: "
                f"{', '.join(SITE['served_states'])})")
    return why


def _application_fields() -> tuple[str, str]:
    """Read + sanitize the application form. Returns (buyer_draft, contact) or ('', reason)."""
    agency = (request.form.get("agency") or "").strip()[:140]
    contact_name = (request.form.get("contact_name") or "").strip()[:120]
    cemail = (request.form.get("contact_email") or "").strip().lower()[:160]
    cphone = phone_mod.pretty(phone_mod.digits(request.form.get("contact_phone") or "")) if phone_mod.digits(request.form.get("contact_phone") or "") else ""
    license_number = (request.form.get("license_number") or "").strip()[:60]
    npn = (request.form.get("npn") or "").strip()[:20]
    states = [s.strip().upper() for s in (request.form.get("licensed_states") or "").split(",")
              if s.strip().upper() in SITE["served_states"]]
    products = [p.strip() for p in (request.form.get("products") or "").split(",") if p.strip() in _ALLOWED_PRODUCTS]
    volume = (request.form.get("expected_volume") or "").strip()[:60]
    note = (request.form.get("note") or "").strip()[:800]
    dmethod = (request.form.get("delivery_method") or "outbox").strip()
    dwebhook = (request.form.get("delivery_webhook") or "").strip()[:300]
    if dmethod not in _ALLOWED_DELIVERY:
        dmethod = "outbox"
    # consumer contact data is never posted over plain http: webhook delivery is https-only (the
    # application agreement, the admin onboarding form and the buyer portal enforce the same rule)
    if dmethod == "webhook" and not _https_url(dwebhook):
        return "", "CRM webhook delivery needs an https:// URL - consumer contact data is never posted over plain http"
    if not agency or "[" in agency or "CHANGE-ME" in agency.upper():
        return "", "agency name is required, with no placeholder brackets - it identifies your business on every lead record"
    if not contact_name:
        return "", "contact name is required"
    if "@" not in cemail or "." not in cemail:
        return "", "a reply-to email is required"
    if not states:
        return "", f"licensed state(s) are required (served here: {', '.join(SITE['served_states'])})"
    b = {"name": agency, "is_demo": False, "active": False, "priority": 2, "exclusive": True,
         "license_number": license_number, "npn": npn, "licensed_states": states,
         "products": products or ["health_aca"], "tiers": ["A", "B"],
         "price_by_tier": {"A": 0, "B": 0, "C": 0},     # operator + agency set prices at onboarding, never the browser
         "daily_cap": 0, "age_min": 18, "age_max": 100,   # set at onboarding; a draft is never in routing anyway
         "delivery": {"method": dmethod, "email": cemail, **(({"webhook_url": dwebhook}) if dmethod == "webhook" and dwebhook else {})},
         "_application": {"ts": now(), "contact_name": contact_name, "contact_email": cemail, "contact_phone": cphone,
                          "expected_volume": volume, "note": note, "licensed_states": states, "products": products or ["health_aca"],
                          "license_number": license_number, "npn": npn, "delivery_preference": dmethod,
                          "delivery_webhook": dwebhook if dmethod == "webhook" else "", "source": request.path
                          + ("?" + urllib.parse.urlencode({"x": request.args.get("x")}) if request.args.get("x") else "")}}
    return b, ""


@app.post("/for-agents/apply")
def buyer_apply():
    if (request.form.get("website") or "").strip():          # honeypot: bots fill it, humans never see it
        return jsonify(ok=True)
    es = (request.form.get("lang") or "").strip().lower() == "es"
    tpl = "para_agencias.html" if es else "for_agents.html"
    if READONLY_PREVIEW:
        # buyers.json lives on the same ephemeral filesystem: a draft would vanish on restart, and the
        # applicant would be told "we'll reply" about an application nobody will ever see.
        return render_template(tpl, packages=MARKETPLACE.get("packages", []), returns=get_marketplace().get("returns", {}),
                               enforcing=_credit_enforcing(),
                               err=("Esta vista previa no guarda solicitudes todavía; no se envió nada."
                                    if es else
                                    "This preview deployment does not store applications yet, so nothing was sent."))
    buyers = json.loads((CFG / "buyers.json").read_text(encoding="utf-8"))
    b, why = _application_fields()
    if why:
        return render_template(tpl, packages=MARKETPLACE.get("packages", []), returns=get_marketplace().get("returns", {}),
                               enforcing=_credit_enforcing(),
                               err=_apply_err_es(why) if es else why)
    base = re.sub(r"[^a-z0-9]+", "-", b["name"].lower()).strip("-")[:28].strip("-") or "agency"
    existing = next((x for x in buyers if x["id"] == base), None)
    if existing and (existing.get("is_demo") or existing.get("active")):
        b["id"] = base + "-" + secrets.token_hex(3)           # name collision with a real/demo buyer: never clobber
        buyers.append(b)
        bid = b["id"]
        msg = (f"Solicitud recibida ({bid}). Responderemos a {b['_application']['contact_email']} dentro de un día hábil."
               if es else
               f"Application received ({bid}). We'll reply to {b['_application']['contact_email']} within a business day.")
    elif existing:
        # repeat application for the same agency: keep ONE draft, append the note (a correction beats a second row)
        prev = existing.get("_application", {}).get("note", "")
        new = b["_application"]
        merged_note = ((prev + " | " if prev else "") + f"{now()[:10]} update: {new['note']}").strip(" |")[:800]
        existing["_application"] = {**new, "note": merged_note}
        existing["license_number"], existing["npn"], existing["licensed_states"], existing["products"] = \
            b["license_number"], b["npn"], b["licensed_states"], b["products"]
        new_delivery = {**existing.get("delivery", {}), "email": b["delivery"]["email"]}
        # the applicant may correct their delivery preference on a repeat application — carry it over
        if new["delivery_preference"]:
            new_delivery["method"] = new["delivery_preference"]
        if new["delivery_webhook"]:
            new_delivery["webhook_url"] = new["delivery_webhook"]
        existing["delivery"] = new_delivery
        bid = existing["id"]
        msg = (f"Solicitud actualizada ({bid}). Responderemos a {new['contact_email']} dentro de un día hábil."
               if es else
               f"Application updated on file ({bid}). We'll reply to {new['contact_email']} within a business day.")
        buyers = [x for x in buyers if x["id"] != bid] + [existing]
    else:
        b["id"] = base
        buyers.append(b)
        bid = base
        msg = (f"Solicitud recibida ({bid}). Responderemos a {b['_application']['contact_email']} dentro de un día hábil."
               if es else
               f"Application received ({bid}). We'll reply to {b['_application']['contact_email']} within a business day.")
    _save_buyers(buyers)
    notify.buyer_application(bid, b["_application"], b["name"], SITE, OUTBOX)   # dry-run email; reply-to = the applicant
    con.commit()
    return render_template(tpl, packages=MARKETPLACE.get("packages", []), returns=get_marketplace().get("returns", {}),
                           enforcing=_credit_enforcing(), done=msg, err="")


@app.get("/terms")
def terms():
    return render_template("simple.html", page="terms")


def _site_origin() -> str:
    """The absolute public origin links may be built from, or '' when config has none yet.
    One read for robots/sitemap/canonical: a value that is not an absolute http(s) origin can never
    silently become a "permanent" URL in a search engine's index."""
    u = (SITE.get("base_url") or "").strip().rstrip("/")
    return u if hosting.is_absolute_origin(u) else ""


@app.before_request
def _force_https():
    """TLS terminates at the host, which tells us so in X-Forwarded-Proto. A request that actually
    arrived over plain http must be sent to https instead of being served: the admin token, portal
    links and consent evidence would otherwise travel in clear text. Nothing happens when the public
    origin is not https, or when no proxy header is present (Render's internal health check, the test
    client, a local run) — so a health check can never be redirected into a loop."""
    proto = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower()
    if request.method not in ("GET", "HEAD") or proto != "http":
        return None
    origin = _site_origin()
    if not origin.startswith("https://"):
        return None
    qs = request.query_string.decode("utf-8", "replace")
    return redirect(origin + request.path + ("?" + qs if qs else ""), code=301)


@app.get("/robots.txt")
def robots():
    """Crawl policy. The Sitemap line is advertised ONLY when crawling is actually allowed: telling
    crawlers "Disallow: /" and "here is my sitemap" in the same file is a contradiction (Search
    Console reports it as an error) and it silently exposes a staging origin if BASE_URL is unset."""
    if not ALLOW_INDEXING:
        return Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")
    body = "User-agent: *\nAllow: /\nDisallow: /admin\nDisallow: /api\nDisallow: /agent\nDisallow: /partner\n"
    origin = _site_origin()
    if origin:
        body += f"Sitemap: {origin}/sitemap.xml\n"
    return Response(body, mimetype="text/plain")


@app.get("/sitemap.xml")
def sitemap():
    """Every indexable page in both languages, each entry declaring its own twin.

    The home page appears twice on purpose: bare / is Spanish-first and /?lang=en is the English twin,
    so omitting either would hide half the site from search. /c/<slug> campaign pages are NOT listed —
    they 302 to the check, and a redirecting URL does not belong in a sitemap.
    """
    base = _site_origin()
    if not base:                       # no public origin configured: a sitemap of relative URLs is invalid
        abort(404)
    es_prefixes = ("/aprende", "/privacidad", "/terminos", "/ayuda-gratis", "/inscripcion", "/despidos")

    def is_es(path):
        core = path.split("?")[0]
        return (core == "/" and path != "/?lang=en") or core.startswith(es_prefixes) or path == "/check?a=es_check"

    def twin(path):
        if path == "/":
            return "/?lang=en"
        if path == "/?lang=en":
            return "/"
        if path.startswith("/check"):
            return "/check?a=es_check" if not is_es(path) else "/check?a=health_check"
        return _alt_url(path.split("?")[0], is_es(path))

    roots = ["/", "/?lang=en", "/check?a=health_check", "/check?a=es_check", "/learn", "/aprende",
             "/layoffs", "/despidos", "/open-enrollment-2027", "/inscripcion-abierta-2027",
             "/free-help", "/ayuda-gratis", "/for-agents", "/para-agencias"]
    pages = json.loads((CFG / "content.json").read_text(encoding="utf-8"))["pages"]
    roots += [("/aprende/" if p_["lang"] == "es" else "/learn/") + p_["slug"] for p_ in pages
              if p_.get("category") != "life" and "-life-" not in p_["slug"] and "seguro-de-vida" not in p_["slug"]]
    evs = json.loads((CFG / "layoffs.json").read_text(encoding="utf-8"))["events"]
    roots += [f"/layoffs/{e_['slug']}" for e_ in evs if e_.get("status") == "ready"]
    roots += [f"/despidos/{e_['slug']}" for e_ in evs if e_.get("status") == "ready"]

    def esc(u):
        return u.replace("&", "&amp;")

    out = []
    for path in roots:
        loc, me = base + path, ("es" if is_es(path) else "en")
        links = f'<xhtml:link rel="alternate" hreflang="{me}" href="{esc(loc)}"/>'
        other = twin(path)
        if other:
            links += f'<xhtml:link rel="alternate" hreflang="{"en" if me == "es" else "es"}" href="{esc(base + other)}"/>'
        out.append(f"<url><loc>{esc(loc)}</loc>{links}</url>")
    return Response('<?xml version="1.0" encoding="UTF-8"?>'
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
                    'xmlns:xhtml="http://www.w3.org/1999/xhtml">' + "".join(out) + "</urlset>",
                    mimetype="application/xml")


# ----------------------------------------------------------------- API
def _independently_verified(b: dict) -> bool:
    """The operator RECORDED both external checks (FL license + NPN lookup) for this buyer. Supplied ≠
    verified: a licence number typed into a form is a claim until someone looked it up."""
    v = b.get("_verification") or {}
    return bool(v.get("license") and v.get("npn"))


def _buyers(test=False):
    """Who may be named in a consumer's consent and receive a lead.

    Demo buyers exist so the funnel can be tested end to end: in production they are only ever offered
    to TEST traffic, so a real consumer can never see (or consent to) a demo company. On a live
    deployment a REAL lead additionally requires a buyer whose licence and NPN checks are recorded —
    an unverified agency cannot buy consumer data. Both gates are production-only, so staging keeps its
    labelled rehearsals and the test suite keeps its fixtures."""
    bs = route.load_buyers(CFG)
    if test or not PRODUCTION:
        return bs
    return [b for b in bs if not b.get("is_demo") and _independently_verified(b)]


def _is_test(d):
    """TEST if the URL says so or this browser is marked as a QA device (cookie from /qa?on=1).
    TEST-ness is decided SERVER-side: in production no client hint is trusted — a real consumer's payload
    claiming is_test=1 could otherwise be sold to a demo buyer and booked into the TEST ledger. In staging
    the payload flag is honored so the QA 'test=1 persists for the session' flow keeps working."""
    if PRODUCTION:
        return 0
    return 1 if (d.get("is_test") or request.args.get("test") == "1" or request.cookies.get("le_qa") == "1") else 0


def _page_experiment(entry_page):
    """Experiments that own a set of pages (SEO / AI-citation tests) get credit for untagged arrivals on those pages."""
    if not entry_page:
        return None
    for r in con.execute("SELECT experiment_id, pages FROM experiments WHERE pages IS NOT NULL AND pages != '' AND status NOT IN ('KILLED')"):
        for pat in (x.strip() for x in r["pages"].split(",")):
            if pat and (entry_page == pat or (pat.endswith("*") and entry_page.startswith(pat[:-1]))):
                return r["experiment_id"]
    return None


def _pv(v):
    return v if isinstance(v, str) and re.fullmatch(r"[A-Z]", v or "") else None


def _entry(v):
    return (v or "")[:120] or None


@app.post("/api/event")
def api_event():
    d = request.get_json(force=True, silent=True) or {}
    if not d.get("experiment_id") and d.get("entry_page"):
        d["experiment_id"] = _page_experiment(d.get("entry_page"))
    vid = (d.get("visitor_id") or "")[:64] or uuid.uuid4().hex
    t = _is_test(d)
    v = con.execute("SELECT stage FROM visitors WHERE visitor_id=?", (vid,)).fetchone()
    if not v:
        insert(con, "visitors", {"visitor_id": vid, "first_seen": now(), "last_seen": now(), "first_campaign": d.get("campaign"),
                                 "first_utm_source": d.get("utm_source"), "first_utm_medium": d.get("utm_medium"),
                                 "first_utm_campaign": d.get("utm_campaign"), "first_utm_content": d.get("utm_content"),
                                 "first_link_code": d.get("link_code"), "partner_id": d.get("partner_id"),
                                 "referrer": d.get("referrer"), "landing_url": d.get("page_url"), "stage": "VISITOR", "is_test": t,
                                 "experiment_id": d.get("experiment_id"), "angle": d.get("angle"), "lang": d.get("lang"),
                                 "phone_variant": _pv(d.get("phone_variant")), "entry_page": _entry(d.get("entry_page"))})
    ev = d.get("event", "")
    if re.search(r"//[^/]+/(admin|agent|partner|qa)(/|\?|$)", str(d.get("page_url") or "")):
        return jsonify(ok=True, ignored="internal page")          # owner/agent/partner screens are not traffic
    # Crawlers, link-preview fetchers and headless agents are not human visits. They are dropped before
    # they become a visitor, so a source report never counts a bot as a person. A TEST browser (QA
    # cookie / ?test=1) is exempt: that traffic is labelled TEST instead of thrown away.
    if BOT_UA.search(request.headers.get("User-Agent") or "") and not t:
        return jsonify(ok=True, ignored="bot user agent")
    # analytics never carry contact data: drop phone/email-like keys and digit runs from event payloads
    data = {k: v_ for k, v_ in (d.get("data") or {}).items() if not re.search(r"phone|email|tel|mail", k, re.I)} \
        if isinstance(d.get("data"), dict) else {}
    data = {k: re.sub(r"\d{7,}", "[redacted]", v_) if isinstance(v_, str) else v_ for k, v_ in data.items()}
    step = re.sub(r"\d{7,}", "[redacted]", str(d.get("step") or ""))[:120] or None
    if ev in ("tool_start", "tool_complete", "step", "form_start") and (not v or v["stage"] == "VISITOR"):
        con.execute("UPDATE visitors SET stage='RESPONDER' WHERE visitor_id=?", (vid,))
    peer = (request.remote_addr or "").split(",")[0].strip()
    if peer not in ("127.0.0.1", "::1") and con.execute(
            "SELECT COUNT(*) FROM events WHERE visitor_id=? AND substr(ts,1,13)=?",
            (vid, now()[:13])).fetchone()[0] >= IP_EVENTS_PER_HOUR:
        return jsonify(ok=True, ignored="rate_limited", visitor_id=vid)   # acknowledged, NOT recorded
    con.execute("UPDATE visitors SET last_seen=? WHERE visitor_id=?", (now(), vid))
    insert(con, "events", {"ts": now(), "visitor_id": vid, "session_id": d.get("session_id"), "campaign": d.get("campaign"),
                           "event": ev, "step": step, "utm_source": d.get("utm_source"), "utm_medium": d.get("utm_medium"),
                           "utm_campaign": d.get("utm_campaign"), "utm_content": d.get("utm_content"),
                           "link_code": d.get("link_code"), "partner_id": d.get("partner_id"), "page_url": d.get("page_url"),
                           "data": json.dumps(data), "is_test": t, "variant": d.get("variant"),
                           "experiment_id": d.get("experiment_id"), "angle": d.get("angle"), "lang": d.get("lang"),
                           "phone_variant": _pv(d.get("phone_variant")), "entry_page": _entry(d.get("entry_page"))})
    con.commit()
    return jsonify(ok=True, visitor_id=vid)


@app.post("/api/phone-check")
def api_phone_check():
    """Inline validation at the contact step. Nothing is stored: a phone is only kept once the consumer
    checks the consent box and submits."""
    d = request.get_json(force=True, silent=True) or {}
    r = phone_mod.validate(str(d.get("phone") or "")[:40], d.get("test") in (1, "1", True))
    return jsonify(valid=r["valid"], reason=r["reason"], pretty=phone_mod.pretty(r["national"]), region=r["area_region"])


def _platform_seller(lang="en"):
    """The seller the consumer actually consents to: the PLATFORM (GS Allude) connecting them with a
    licensed agent — never a pre-chosen agency the consumer could have steered or expected to pick."""
    return SITE["brand"] + (" y su agente asociado con licencia" if lang == "es" else " and its licensed agent partner")


def _consent_text(seller, product, lang="en", flow="legacy"):
    es = lang == "es"
    cta = CONSENT.get("cta", {}).get(flow if flow in ("check", "legacy") else "legacy", {}).get(lang, "Get my options")
    label = CONSENT.get("product_labels_es" if es else "product_labels", {}).get(product, "seguro" if es else "insurance")
    return CONSENT["text_es" if es else "text"].replace("{seller}", seller).replace("{cta}", cta).replace("{product_label}", label)


def _email_text(lang="en"):
    return CONSENT["email_only_text_es" if lang == "es" else "email_only_text"].replace("{brand}", SITE["brand"])


@app.get("/api/route-preview")
def api_route_preview():
    """Availability + the CURRENT consent text only. It NEVER names or returns a buyer: which licensed
    agent fulfills a request is assigned by the platform at submit time, never chosen by the consumer
    (and never relayed by the browser)."""
    c = CAMPAIGN_BY_ID.get(request.args.get("campaign", "")) or abort(404)
    _t = (request.args.get("test") == "1" or request.cookies.get("le_qa") == "1") and not PRODUCTION
    buyers = _buyers(_t)
    b = route.preview(con, buyers, c["product"], request.args.get("state", ""), request.args.get("age_range"),
                      blocked_ids=_blocked_buyers(buyers, _t), health=_health())
    lang = c.get("lang", "en")
    text = _consent_text(_platform_seller(lang), c["product"], lang, request.args.get("flow", "legacy"))
    return jsonify(available=bool(b), consent_text=text, consent_version=CONSENT["version"],
                   consent_email_only=_email_text(lang))


def _sell_lead(lead: dict, buyers: list[dict]) -> None:
    """Deliver + charge a SOLD lead (the platform assigned exactly this buyer at routing time; the consumer consented to the platform, on-screen) as ONE committed unit, then notify.

    Failure guarantees:
      - if deliver or charge raises, everything rolls back and the lead stays stranded in ROUTED — it is
        NEVER lost and NEVER re-sold (the consumer consented to this buyer only); the buyer's desk picks
        it up on the next load and retries once everything is healthy again
      - a notification failure after the sale commits can only lose the notification, never the sale
    """
    b = next((x for x in buyers if x["id"] == lead["buyer_id"]), None)
    if not b:
        return                      # no live buyer = nothing to sell to; the lead is NOT re-routed (consent)
    try:
        route.deliver(con, buyers, lead, OUTBOX)
        billing.charge(con, b["id"], lead, buyer_demo=bool(b.get("is_demo")))
        con.commit()
    except Exception as exc:        # stranded sale: heal the DB state, tell the buyer's desk to retry
        con.rollback()
        try:
            con.execute("INSERT INTO lead_outcomes (lead_id,buyer_id,ts,outcome,note) VALUES (?,?,?,?,?)",
                        (lead["lead_id"], b["id"], now(), "DELIVERY_RETRY", ("sale committed but delivery failed: " + repr(exc))[:450]))
            con.commit()
        except Exception:
            con.rollback()
        return
    try:
        notify.buyer_new_lead(con, b, lead, SITE, OUTBOX)
        con.commit()
    except Exception:
        con.rollback()              # notification is disposable; the sale is already committed


def _client_ip():
    return (request.headers.get("X-Forwarded-For", request.remote_addr) or "?").split(",")[0].strip()


def _opted_out(phone: str, email: str) -> bool:
    """True when this phone/email is on the opt-out list (/optout, or an agent's DNC action).
    Kept as ONE lookup over the same `optouts` keys the opt-out form and the DNC flow write, so
    'stop contacting me' cannot be undone by a later form fill, on any path."""
    keys = [k for k in ((phone or "").strip(), (email or "").strip().lower()) if k]
    return any(con.execute("SELECT 1 FROM optouts WHERE key=?", (k,)).fetchone() for k in keys)


def _lead_throttle_exceeded(con, ip_, is_test, *, remote_addr=None):
    """True when this source IP has already sent its hour's quota of REAL (non-test) consented leads.
    The socket peer (never a spoofable header) on loopback is exempt so local QA can never lock itself
    out; in production real visitors arrive from outside, so the throttle only ever faces real traffic.
    TEST-flagged submissions are excluded from the count as well (staging convenience, server-decided)."""
    peer = remote_addr if remote_addr is not None else request.remote_addr
    if (peer or "").split(",")[0].strip() in ("127.0.0.1", "::1"):
        return False
    if is_test:
        return False
    n = con.execute("""SELECT COUNT(*) FROM consents c JOIN leads l ON l.lead_id=c.lead_id
                       WHERE c.ip=? AND l.is_test=0 AND substr(c.ts,1,13)=?""",
                    (ip_, now()[:13])).fetchone()[0]
    return n >= IP_LEADS_PER_HOUR


@app.post("/api/lead")
def api_lead():
    d = request.get_json(force=True, silent=True) or {}
    if READONLY_PREVIEW:
        # Ephemeral host: a stored lead, consent record or payment row would be erased by the next
        # restart, so the submission is refused up front rather than accepted and lost.
        return jsonify(ok=False, error="preview_read_only",
                       detail="This preview deployment stores no consumer data; use the persistent-disk deployment."), 503
    if PRODUCTION and not _buyers(False):
        # No-buyer mode: nobody is named to receive a request, so we collect nothing (same behavior as the hosted Worker).
        return jsonify(ok=False, error="not_accepting_requests"), 503
    c = CAMPAIGN_BY_ID.get(d.get("campaign", "")) or abort(400)
    trap = (d.get("hpw") or "").strip() or (d.get("website") or "").strip()
    if trap:  # honeypot field - bots fill it, humans never see it
        # HONEYPOT QUARANTINE (never silent): return 200 to the browser (bot stealth — do not
        # confirm/leak that we noticed), but PERSIST enough of the submission for an operator to
        # recover a genuine consumer whose profile-autofill tripped the trap (Admin > Discards).
        # No qualification, no routing, no contact: recovery is always an operator decision made
        # through the existing Admin > New-lead flow.
        try:
            insert(con, "discards", {"ts": now(), "reason": "honeypot", "trap_value": trap[:200],
               "campaign": c["id"], "first_name": (d.get("first_name") or "")[:60],
               "last_name": (d.get("last_name") or "")[:60], "email": (d.get("email") or "")[:120],
               "phone": (d.get("phone") or "")[:20], "zip": (d.get("zip") or "")[:12],
               "consent_checked": 1 if d.get("consent_checked") else 0,
               "ip": request.headers.get("X-Forwarded-For", request.remote_addr),
               "user_agent": request.headers.get("User-Agent", "")[:200],
               "page_url": (d.get("page_url") or "")[:200]})
            con.commit()
        except Exception as _e:                 # noqa: BLE001 - quarantine must not break stealth
            app.logger.warning("honeypot quarantine write failed: %r", _e)
        return jsonify(ok=True)
    form = {k: (d.get(k) or "").strip() if isinstance(d.get(k), str) else d.get(k) for k in d}
    buyers = _buyers(_is_test(d))
    if _lead_throttle_exceeded(con, _client_ip(), _is_test(d)):
        return jsonify(ok=False, error="too_many_requests"), 429
    # PLATFORM-CONTROLLED DISTRIBUTION: any buyer id in the payload is ignored; the consumer consented
    # to the platform (see the on-screen text), and the buyer is assigned by route.route() below.
    full = bool(d.get("consent_checked"))
    form["contact_scope"] = "PHONE_AND_EMAIL" if full else "EMAIL_ONLY"
    if not full and not d.get("email_consent_checked"):
        return jsonify(ok=False, error="consent_required"), 400
    vid = d.get("visitor_id") or ""
    events = [dict(r) for r in con.execute("SELECT event FROM events WHERE visitor_id=?", (vid,))]
    match = intel_mod.match_lead(INTEL_DB, form.get("first_name", ""), form.get("last_name", ""), form.get("zip", ""))
    t = _is_test(d)
    form["is_test"] = t
    # WRITE-LATCH: dedup-check (a SELECT) followed by the lead INSERT is a read-then-write seam, so two
    # all-submitted consumers racing the same phone could BOTH pass is_duplicate() before either commits
    # (30-day dedup can't be expressed as a unique index, so the DB alone can't enforce it), and under WAL
    # the losing writer can additionally die with a stale-snapshot 'database is locked'. Taking
    # BEGIN IMMEDIATE here serializes submission writers: the loser waits, and when it proceeds its
    # dedup read happens AFTER the winner's commit -> it records a DUPLICATE, never a second sale.
    if con.in_transaction:
        con.commit()                                    # never nest inside an open implicit transaction
    con.execute("BEGIN IMMEDIATE")
    q = qualify.qualify(con, form, c, SITE, events, match)
    # HONOR AN EXISTING OPT-OUT: a previous STOP / web opt-out is not erased by a new form fill. The
    # submission is still recorded for the operator (nothing hidden), but it is never qualified, routed,
    # sold or contacted — the page promised we would stop, and this is where that promise is enforced.
    opted_out = _opted_out(q.get("phone") or "", form.get("email") or "")
    if opted_out:
        q["qual_status"], q["qual_reasons"] = "UNQUALIFIED", ["consumer_opted_out"]
    lead_id = "L" + now()[:10].replace("-", "") + "-" + secrets.token_hex(4)
    t = _is_test(d)
    lead = {"lead_id": lead_id, "created_at": now(), "visitor_id": vid, "is_test": t, "campaign": c["id"], "product": c["product"],
            "landing_url": d.get("page_url"), "utm_source": d.get("utm_source"), "utm_medium": d.get("utm_medium"),
            "utm_campaign": d.get("utm_campaign"), "utm_content": d.get("utm_content"), "utm_term": d.get("utm_term"),
            "link_code": d.get("link_code"), "partner_id": d.get("partner_id"), "gclid": d.get("gclid"), "fbclid": d.get("fbclid"),
            "referrer": d.get("referrer"), "variant": d.get("variant"), "experiment_id": d.get("experiment_id"),
            "angle": d.get("angle"), "lang": d.get("lang") or c.get("lang", "en"), "need_path": d.get("need_path"),
            "household_size": d.get("household_size"), "income_band": d.get("income_band"),
            "enrollment_situation": d.get("enrollment_situation"), "urgency": d.get("urgency"),
            "product_category": d.get("product_category"), "flags": d.get("flags") or [], "first_name": form.get("first_name"), "last_name": form.get("last_name"),
            "email": (form.get("email") or "").lower(), "phone": q["phone"], "zip": form.get("zip"), "state": form.get("state"),
            "age_range": form.get("age_range"), "coverage_for": form.get("coverage_for"),
            "coverage_amount": form.get("coverage_amount") or form.get("fe_amount") or form.get("mortgage_balance"),
            "tobacco": form.get("tobacco"), "health_rating": form.get("health_rating"), "timeframe": form.get("timeframe"),
            "preferred_contact": form.get("preferred_contact"), "best_time": form.get("best_time"),
            "answers": d.get("answers") or {}, "tool_result": d.get("tool_result") or {}, "contact_scope": form["contact_scope"],
            "qual_status": q["qual_status"], "qual_reasons": q["qual_reasons"], "score": q["score"], "tier": q["tier"],
            "score_breakdown": q["score_breakdown"], "intel_match": match or {}, "routing_status": "PENDING",
            # the product: the consumer's own phone + provenance (never a public-record number; DB trigger enforces it)
            "lead_type": q["lead_type"], "phone_e164": q["phone_check"]["e164"] or None,
            "phone_valid": 1 if q["phone_check"]["valid"] else 0, "phone_check": q["phone_check"]["reason"],
            "phone_area_region": q["phone_check"]["area_region"] or None, "phone_line_type": q["phone_check"]["line_type"] if q["phone"] else None,
            "phone_line_type_source": q["phone_check"]["line_type_source"] if q["phone"] else None,
            "phone_source": phone_mod.PHONE_SOURCE if q["phone"] else None,
            "tracking_id": d.get("link_code") or d.get("experiment_id") or d.get("gclid") or d.get("fbclid") or None}
    # consent record - text is re-rendered SERVER-side from the versioned template (never trusted from the browser)
    if full:
        ctext = _consent_text(_platform_seller(c.get("lang", "en")), c["product"], c.get("lang", "en"), d.get("flow") or "legacy")
    else:
        ctext = _email_text(c.get("lang", "en"))
    snap = (d.get("form_snapshot") or "")[:200000]
    sha = hashlib.sha256((snap + "|" + ctext).encode("utf-8")).hexdigest()
    SNAP.mkdir(parents=True, exist_ok=True)
    # EXACT utf-8 bytes, never the platform default codec: the consent copy carries non-ASCII glyphs
    # (arrows, Spanish accents) and a cp1252 text write would 500 the whole submission on Windows.
    (SNAP / f"{lead_id}.html").write_bytes(snap.encode("utf-8"))
    cts = now()
    insert(con, "consents", {"lead_id": lead_id, "ts": cts, "consent_version": CONSENT["version"], "consent_text": ctext,
                             "seller_named": _platform_seller(c.get("lang", "en")) if full else SITE["brand"] + " (email only)",
                             "checkbox_checked": 1 if full else 0, "scope": form["contact_scope"],
                             "ip": request.headers.get("X-Forwarded-For", request.remote_addr),
                             "user_agent": request.headers.get("User-Agent", "")[:400], "page_url": d.get("page_url"),
                             "form_snapshot_sha256": sha, "phone": q["phone"], "email": lead["email"]})
    lead["consent_ts"], lead["consent_version"] = cts, CONSENT["version"]
    lead["phone_variant"], lead["entry_page"] = _pv(d.get("phone_variant")), _entry(d.get("entry_page"))
    v_ = con.execute("SELECT * FROM visitors WHERE visitor_id=?", (vid,)).fetchone() if vid else None
    if v_:   # attribution fallback: a returning visitor keeps the source that first brought them
        for lk, vk in (("experiment_id", "experiment_id"), ("utm_source", "first_utm_source"), ("utm_medium", "first_utm_medium"),
                       ("utm_campaign", "first_utm_campaign"), ("utm_content", "first_utm_content"), ("link_code", "first_link_code"),
                       ("partner_id", "partner_id"), ("entry_page", "entry_page"), ("phone_variant", "phone_variant")):
            if not lead.get(lk) and v_[vk]:
                lead[lk] = v_[vk]
        if not lead.get("tracking_id"):
            lead["tracking_id"] = lead.get("link_code") or lead.get("experiment_id")
    r = route.route(con, buyers, lead, blocked_ids=_blocked_buyers(buyers, t), health=_health())
    # Belt and braces on top of _buyers(): on a LIVE deployment a REAL lead may never be sold to a demo
    # fixture, even if some future caller passes an unfiltered buyer list. Withholding it (NO_BUYER, reason
    # recorded below) is the safe failure mode; delivering it would hand a consumer's phone to a fake
    # company. Staging keeps the old behaviour: its demo desks are local rehearsals and are labelled as
    # such everywhere they appear.
    if PRODUCTION and r.get("routing_status") == "ROUTED" and not t:
        _won = next((x for x in buyers if x["id"] == r.get("buyer_id")), None)
        if _won and _won.get("is_demo"):
            r = {"routing_status": "NO_BUYER", "buyer_id": None, "price": None, "sale_type": ""}
    lead.update(r)
    # WHY-a-lead-flowed audit trail (routing_decisions): matched, withheld or unroutable — always the reason
    if r["routing_status"] == "ROUTED":
        decision, bid_, reason = "ASSIGNED", r["buyer_id"], \
            f"eligible buyer available -> assigned EXCLUSIVE at tier {lead.get('tier')} @ ${r['price']} (contracted criteria, daily cap, prepaid credits; priority + round-robin)"
    elif r["routing_status"] == "NO_BUYER":
        reason, n_cands = route.explain(con, buyers, lead, _blocked_buyers(buyers, t), health=_health())
        decision, bid_ = "NO_BUYER", None
    else:
        decision, bid_ = "NOT_ROUTABLE", None
        reason = (f"not a receivable phone lead (lead_type={lead.get('lead_type')}, phone_valid={lead.get('phone_valid')}, "
                  f"qual={lead.get('qual_status')}, reasons {lead.get('qual_reasons')})")[:450]
    insert(con, "routing_decisions", {"ts": now(), "lead_id": lead_id, "decision": decision, "buyer_id": bid_, "reason": reason, "is_test": t})
    insert(con, "leads", lead)
    con.execute("UPDATE visitors SET stage='SUBMITTED' WHERE visitor_id=?", (vid,))
    con.commit()
    # Discovery attribution: if this submission arrived through a /go link that was issued for a
    # public prospect thread, connect the two. It only succeeds when this very lead has a consent row
    # (prospector.link_lead re-reads `leads` + `consents`), so a public post alone can never turn into
    # a sellable lead. Best-effort: a discovery bookkeeping failure must never fail a consumer's request.
    try:
        prospector.attribute_lead(con, lead_id)
    except Exception:                                            # noqa: BLE001
        pass
    if lead["routing_status"] == "ROUTED":
        _sell_lead(lead, buyers)                             # atomic deliver+charge; notify can't undo a sale
    if lead["product"] == "health_aca" and lead["qual_status"] in ("QUALIFIED", "NURTURE") and lead.get("email"):
        assigned = next((x["name"] for x in buyers if (full and x["id"] == lead["buyer_id"] and lead["routing_status"] == "DELIVERED")), None)
        notify.consumer_summary(con, lead, SITE, OUTBOX, assigned)
        con.commit()
    if lead["qual_status"] in ("QUALIFIED", "NURTURE") and not t:
        tracking.meta_capi_lead(lead, request, SITE, OUTBOX)  # server-side conversion (dry-run without token)
    return jsonify(ok=True, lead_id=lead_id, status=lead["qual_status"], routed=lead["routing_status"],
                   opted_out=opted_out)


# ----------------------------------------------------------------- buyer (agent) portal = feedback loop
# ----------------------------------------------------------------- admin
def _admin():
    if request.args.get("token") != ADMIN_TOKEN:
        if not PRODUCTION and ADMIN_TOKEN == "DEVLOCAL":
            # dev-only: point the operator at the token instead of a bare 403
            # (revealing the DEFAULT dev token can never leak a deliberately set secret)
            abort(403, description="wrong admin token. Local dev token: ?token=DEVLOCAL "
                                   "(set ADMIN_TOKEN env to override; see docs/USER_WALKTHROUGH.md)")
        abort(403)


def _where(test):
    return "" if test == "all" else ("WHERE is_test=1" if test == "only" else "WHERE is_test=0")


def _buyer_state_rows():
    """Recorded-state view rows for EVERY configured buyer (active + inactive + demo + drafts):
    blockers (WHY routing excludes them from NEW leads, same criteria as preview()), the
    operator's next concrete step, balances, delivery config, and the readiness LADDER
    (configured / verified / active / rehearsed / capable) used by /admin AND /admin/readiness.
    Read-only; derived only from config + recorded tables."""
    buyers_all = json.loads((CFG / "buyers.json").read_text(encoding="utf-8"))
    buyers_all.sort(key=lambda b_: (not b_.get("active"), not b_.get("is_demo"), b_["id"]))
    # WHY a configured buyer is or is not receiving new leads: the SAME criteria our routing
    # (preview()) filters by — daily/monthly caps, prepaid credit enforcement, quality-aware
    # exclusion, and a buyer with no positive per-lead price. Computed from the live tables only.
    _h = route.buyer_health(con, MARKETPLACE.get("buyer_health", {}))
    state_today = {b["id"]: route.delivered_today(con, b["id"]) for b in buyers_all}
    state_month = {b["id"]: route.delivered_in_month(con, b["id"]) for b in buyers_all}
    for b in buyers_all:
        defs = []
        if not b.get("active"):
            defs.append("not activated")
        if not any((b.get("price_by_tier") or {}).get(t) for t in (b.get("tiers") or [])):
            defs.append("no positive per-lead price configured")
        if b.get("active"):
            if state_today[b["id"]] >= b["daily_cap"]:
                defs.append(f"daily cap {b['daily_cap']} reached")
            mc = b.get("monthly_cap")
            if mc and state_month[b["id"]] >= mc:
                defs.append(f"monthly purchase limit {mc} reached")
            if _credit_enforcing() and billing.balance(con, b["id"], 0) <= float(MARKETPLACE.get("min_balance_usd", 0) or 0):
                defs.append("no prepaid credits (enforcement ON)")
            if _h.get(b["id"], {}).get("rank") == 2:
                defs.append("quality-blocked by recent delivered outcomes")
            # production-only gate (see _buyers): a live deployment refuses real leads to a buyer whose
            # licence/NPN checks were never recorded. Without this line the row would look perfectly
            # active while silently receiving nothing — the hardest kind of operational surprise.
            if PRODUCTION and not b.get("is_demo") and not _independently_verified(b):
                defs.append("licence/NPN checks not recorded (production refuses real leads until both are on file)")
        b["_blockers"] = defs
        # the operator's NEXT concrete step for this buyer, derived only from recorded state
        # (what was supplied, what was verified, what was rehearsed) — no invented requirements.
        if not b.get("is_demo"):
            ver = b.get("_verification") or {}
            licensed = ver.get("license") and ver.get("npn")
            if not b.get("active"):
                if not (b.get("name") and b.get("license_number") and b.get("npn")):
                    b["_next"] = "configure: save their legal name, FL license and NPN on the onboarding form"
                elif b.get("delivery", {}).get("method") == "webhook" and not ver.get("delivery_tested"):
                    b["_next"] = "test their webhook delivery (the signed ping) — nothing here activates them"
                elif not licensed:
                    b["_next"] = "verify independently: record your FL license + NPN checks on the row below"
                else:
                    b["_next"] = "activate (their verification + pricing are on record)"
            else:
                if PRODUCTION and not _independently_verified(b):
                    b["_next"] = "verify independently: record your FL license + NPN checks — real leads are not routed until both are on file"
                td = con.execute("SELECT COUNT(*) FROM leads WHERE buyer_id=? AND routing_status='DELIVERED' AND is_test=1", (b["id"],)).fetchone()[0]
                rd = con.execute("SELECT COUNT(*) FROM leads WHERE buyer_id=? AND routing_status='DELIVERED' AND is_test=0", (b["id"],)).fetchone()[0]
                if td == 0:
                    b["_next"] = "rehearse: send a TEST funnel lead through their desk before any real traffic"
                elif rd == 0 and billing.balance(con, b["id"], 0) == 0:
                    b["_next"] = "ready for the pilot: record their first credit pack once funds are received"
                else:
                    b["_next"] = ""
        else:
            b["_next"] = ""
    def _count(bid: str, istest: int) -> int:
        return con.execute("SELECT COUNT(*) FROM leads WHERE buyer_id=? AND routing_status='DELIVERED' AND is_test=?",
                           (bid, istest)).fetchone()[0]
    bstat = []
    for b in buyers_all:
        ver = b.get("_verification") or {}
        td, rd = _count(b["id"], 1), _count(b["id"], 0)
        bstat.append({"name": b["name"], "cap": b.get("daily_cap", "— not set"), "today": state_today[b["id"]],
                      "month": state_month[b["id"]], "monthly_cap": b.get("monthly_cap") or "",
                      "delivered": con.execute("SELECT COUNT(*) FROM leads WHERE buyer_id=? AND routing_status='DELIVERED'", (b["id"],)).fetchone()[0],
                      "next": b["_next"],
                      "balance_real": billing.balance(con, b["id"], 0), "balance_test": billing.balance(con, b["id"], 1),
                      "blockers": b["_blockers"],
                      "id": b["id"], "is_test_banner": bool(b.get("is_demo")), "active": bool(b.get("active")),
                      "portal_token": b.get("portal_token", ""),
                      "applied": b.get("_application") or ({} if b.get("active") else None),
                      "contact": b.get("contact") or {},
                      "verification": ver,
                      "converted_from_prospect": b.get("converted_from_prospect") or None,
                      "webhook_secret": b.get("delivery", {}).get("webhook_secret", ""),
                      "delivery": b.get("delivery", {}),
                      # readiness LADDER, computed once and shared by both surfaces:
                      "ladder": {"configured": bool(b.get("name") and b.get("license_number") and b.get("npn") and
                                                   (b.get("tiers") or []) and any((b.get("price_by_tier") or {}).get(t) for t in (b.get("tiers") or []))),
                                 "verified": bool(ver.get("license") and ver.get("npn")),
                                 "active": bool(b.get("active")),
                                 "rehearsed": td > 0,
                                 # an INACTIVE buyer is intentionally NOT receiving leads, not 'blocked':
                                 "capable": bool(b.get("active")) and not b["_blockers"],
                                 "delivered_real": rd > 0}})
    return bstat, buyers_all


@app.get("/admin/inquiries")
def admin_inquiries():
    """Consumer messages sent through the public contact form — the support channel itself. Nothing here
    is a lead: no routing, no sale, no list. Reply only where a reply was invited (reply_to), and the
    required business identity is shown at the top so an answer can carry the real entity details."""
    _admin()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT id, ts, message, reply_to FROM contact_msgs ORDER BY id DESC LIMIT 200")]
    except Exception:                        # noqa: BLE001 - table is created on the first message
        rows = []
    return render_template("admin_inquiries.html", rows=rows, token=ADMIN_TOKEN, tab="inquiries",
                           biz=business_identity())


@app.get("/admin/discards")
def admin_discards():
    """Quarantined consumer submissions: a honeypot-filled form got a 200 from the stealth
    response, but it is recorded here instead of vanishing. NOTHING in this list was ever
    qualified, routed, or contacted. Recovery is manual by operator decision only — enter a
    genuine inquiry through Admin ▸ New lead, or (if it was your own walk-through) resubmit it
    through the real /check form now that the trap field's name no longer autofill-trips."""
    _admin()
    try:
        rows = con.execute("SELECT * FROM discards ORDER BY id DESC LIMIT 200").fetchall()
    except Exception:                        # noqa: BLE001 - pre-migration DB
        rows = []
    return render_template("admin_discards.html", rows=rows, token=ADMIN_TOKEN, tab="walkthrough")


@app.get("/admin/walkthrough")
def admin_walkthrough():
    """OWNER WALKTHROUGH — a guided index of the REAL routes this app actually serves, built
    only from pages that exist (no invented destinations): consumer journey, AI/collection,
    opportunities/prospects, buyer onboarding, marketplace/delivery, administration. Demo
    buyers drive the buyer-portal links; every state change in the stories is local-only
    (dry-run delivery to outbox/, no email/SMS/webhook, no charge)."""
    _admin()
    # Demo desks are LOCAL fixtures: on a production deployment their tokens are refused by _buyer(), so the
    # walkthrough must not offer links that cannot work (and must not imply a demo account is a customer).
    demos = {} if PRODUCTION else {b["id"]: b for b in json.loads((CFG / "buyers.json").read_text(encoding="utf-8"))
                                   if b.get("is_demo") and b.get("portal_token")}
    leads_test = con.execute("SELECT COUNT(*) FROM leads WHERE is_test=1").fetchone()[0]
    leads_real = con.execute("SELECT COUNT(*) FROM leads WHERE is_test=0").fetchone()[0]
    return render_template("admin_walkthrough.html", token=ADMIN_TOKEN, tab="walkthrough",
                           demos=demos, leads_test=leads_test, leads_real=leads_real,
                           signals_db=SIGNALS_DB.exists(),
                           live_delivery=os.environ.get("LEADENGINE_LIVE_DELIVERY", "0") == "1")


@app.get("/admin")
def admin():
    _admin()
    swept, swept_left = _auto_reoffer()             # timed re-offer sweep on the unmatched queue (operator-visible, audited)
    test = request.args.get("test", "all")
    w = _where(test)
    wv = w.replace("is_test", "v.is_test")
    try:                                      # honeypot-quarantine count (table ships in db.SCHEMA)
        discards_n = con.execute("SELECT COUNT(*) FROM discards").fetchone()[0]
    except Exception:                        # noqa: BLE001 - pre-migration DB opened read-mostly
        discards_n = 0
    k = {}
    k["visitors"] = con.execute(f"SELECT COUNT(*) FROM visitors {w}").fetchone()[0]
    k["responders"] = con.execute(f"SELECT COUNT(*) FROM visitors {w} {'AND' if w else 'WHERE'} stage IN ('RESPONDER','SUBMITTED')").fetchone()[0]
    for s in ("QUALIFIED", "NURTURE", "DUPLICATE", "UNQUALIFIED"):
        k[s] = con.execute(f"SELECT COUNT(*) FROM leads {w} {'AND' if w else 'WHERE'} qual_status=?", (s,)).fetchone()[0]
    k["submitted"] = con.execute(f"SELECT COUNT(*) FROM leads {w}").fetchone()[0]
    k["delivered"] = con.execute(f"SELECT COUNT(*) FROM leads {w} {'AND' if w else 'WHERE'} routing_status='DELIVERED'").fetchone()[0]
    k["revenue"] = con.execute(f"SELECT COALESCE(SUM(price),0) FROM leads {w} {'AND' if w else 'WHERE'} routing_status='DELIVERED'").fetchone()[0]
    funnel = [dict(r) for r in con.execute(f"""
      SELECT v.first_campaign campaign, COALESCE(v.first_utm_source,'(direct)') source, COALESCE(v.first_utm_medium,'') medium,
        COUNT(*) visitors, SUM(v.stage IN ('RESPONDER','SUBMITTED')) responders, SUM(v.stage='SUBMITTED') submitted,
        (SELECT COUNT(*) FROM leads l WHERE l.visitor_id IN (SELECT visitor_id FROM visitors v2 WHERE v2.first_campaign=v.first_campaign
           AND COALESCE(v2.first_utm_source,'(direct)')=COALESCE(v.first_utm_source,'(direct)')) AND l.qual_status='QUALIFIED') qualified
      FROM visitors v {wv} GROUP BY 1,2,3 ORDER BY visitors DESC""")]
    # leads inventory: same TEST scope, plus optional desk filters (qualification, routing state,
    # campaign) — allow-listed so nothing else reaches the SQL. Empty values mean no filter.
    qual_f = request.args.get("qual", "").upper()
    rout_f = request.args.get("rout", "").upper()
    camp_f = request.args.get("camp", "")
    ALLOWED_QUAL = ("QUALIFIED", "NURTURE", "DUPLICATE", "UNQUALIFIED")
    ALLOWED_ROUT = ("PENDING", "ROUTED", "DELIVERED", "NO_BUYER", "NOT_ROUTABLE")
    lf_clauses, lf_args = [], []
    if qual_f in ALLOWED_QUAL:
        lf_clauses.append("qual_status=?"); lf_args.append(qual_f)
    if rout_f in ALLOWED_ROUT:
        lf_clauses.append("routing_status=?"); lf_args.append(rout_f)
    if re.fullmatch(r"[a-z0-9_-]{2,40}", camp_f):
        lf_clauses.append("campaign=?"); lf_args.append(camp_f)
    lf = (["is_test=1"] if test == "only" else (["is_test=0"] if test != "all" else [])) + lf_clauses
    try:
        limit_f = min(max(int(request.args.get("limit", 200) or 200), 10), 1000)
    except ValueError:
        limit_f = 200
    lead_sql = "SELECT * FROM leads" + (" WHERE " + " AND ".join(lf) if lf else "") + f" ORDER BY created_at DESC LIMIT {limit_f}"
    leads = [dict(r) for r in con.execute(lead_sql, lf_args)]
    bstat, buyers_all = _buyer_state_rows()
    # Demand-vs-capacity strip (workstream: commercial readiness): what the ACTIVE buyers can still
    # absorb today, derived ONLY from recorded state — remaining daily caps of active, non-demo,
    # no-blocker buyers, and the prepaid credit value sitting behind those caps for real leads.
    # Unknowns stay unknown: a buyer with no daily cap grows the potential capacity but not the sum.
    _act = [b for b in bstat if b["active"] and not b["is_test_banner"] and not b["blockers"]]
    _act_all = [b for b in bstat if b["active"] and not b["is_test_banner"]]
    _blocked = len(_act_all) - len(_act)
    def _cap_int(v):
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            return 0          # unknown/absent cap: don't count phantom capacity, the buyer stays 'open'
    capacity = {"open": len(_act_all),
                "blocked": _blocked,
                "remaining_leads": sum(_cap_int(b["cap"]) - int(b["today"] or 0) for b in _act if _cap_int(b["cap"]) > 0),
                "uncapped": any(_cap_int(b["cap"]) == 0 for b in _act),
                "credit_value": round(max(0.0, sum(float(b["balance_real"]) for b in _act)), 2),
                "deliverers": len([b for b in _act if (b.get("delivery") or {}).get("method") in ("webhook", "email")]),
                "portal_only": len([b for b in _act if (b.get("delivery") or {}).get("method", "outbox") == "outbox"])}
    prefill = next((dict(b) for b in buyers_all if b["id"] == request.args.get("f")), None)   # onboarding form prefill
    var = [dict(r) for r in con.execute(f"""
      SELECT e.campaign, e.variant, COUNT(DISTINCT e.visitor_id) visitors,
        COUNT(DISTINCT CASE WHEN e.event IN ('tool_start','tool_complete','form_start') THEN e.visitor_id END) responders,
        COUNT(DISTINCT CASE WHEN e.event='tool_complete' THEN e.visitor_id END) tool_done,
        (SELECT COUNT(*) FROM leads l WHERE l.campaign=e.campaign AND l.variant=e.variant {('AND l.is_test=' + ('1' if test=='only' else '0')) if test!='all' else ''}) submitted,
        (SELECT COUNT(*) FROM leads l WHERE l.campaign=e.campaign AND l.variant=e.variant AND l.qual_status='QUALIFIED' {('AND l.is_test=' + ('1' if test=='only' else '0')) if test!='all' else ''}) qualified
      FROM events e WHERE e.variant IS NOT NULL AND e.variant != '' {('AND e.is_test=' + ('1' if test=='only' else '0')) if test!='all' else ''}
      GROUP BY 1,2 ORDER BY 1,2""")]
    outc = [dict(r) for r in con.execute(f"""SELECT l.campaign, COALESCE(l.utm_source,'(direct)') source, COUNT(DISTINCT l.lead_id) delivered,
        SUM(o.outcome='CONTACTED' OR o.outcome IN ('APPOINTMENT','QUOTED','SOLD')) contacted, SUM(o.outcome='APPOINTMENT') appts,
        SUM(o.outcome='SOLD') sold, SUM(o.outcome='BAD_INFO') bad,
        SUM(o.outcome='RETURNED') returned, SUM(o.outcome='DNC') dnc, SUM(l.agent_status IS NULL OR l.agent_status='NEW' OR l.agent_status='VIEWED') untouched
        FROM leads l LEFT JOIN lead_outcomes o ON o.lead_id=l.lead_id WHERE l.routing_status='DELIVERED' {('AND l.is_test=' + ('1' if test=='only' else '0')) if test!='all' else ''}
        GROUP BY 1,2 ORDER BY delivered DESC""")]
    # sale-integrity alerts: sold-but-stranded sales, exhausted webhook retries, negative REAL balances,
    # stuck payments, unbilled deliveries — one kernel shared with the CLI daily run (tools/daily_run.py)
    alerts = ops_mod.sale_alerts(con)
    decisions = [dict(r) for r in con.execute("""
        SELECT decision, COUNT(*) n FROM routing_decisions GROUP BY decision""")]
    withheld = [dict(r) for r in con.execute(
        """SELECT ts, lead_id, decision, buyer_id, reason, candidates FROM routing_decisions
            WHERE decision != 'ASSIGNED' ORDER BY id DESC LIMIT 15""")]
    # unmatched queue: qualified phone leads the consumer consented for, with no eligible buyer at the
    # time. They are never re-sold blindly — each re-offer here re-runs the SAME platform assignment
    # (consent named the platform, not a particular agency) and is audited in routing_decisions.
    no_buyer = [dict(r) for r in con.execute(
        f"SELECT lead_id, created_at, state, zip, product, tier, campaign, age_range FROM leads {w} "
        f"{'AND' if w else 'WHERE'} routing_status='NO_BUYER' AND qual_status='QUALIFIED' ORDER BY created_at DESC LIMIT 20")]
    return render_template("admin.html", k=k, funnel=funnel, leads=leads, bstat=bstat, token=ADMIN_TOKEN, test=test,
                           discards_n=discards_n,
                           prefill_prospect=(prefill or {}).get("converted_from_prospect") if prefill else None,
                           campaigns=CAMPAIGN_BY_ID, tab="overview", var=var, outc=outc, marketplace=MARKETPLACE,
                           qual_f=qual_f, rout_f=rout_f, camp_f=camp_f, limit_f=limit_f,
                           alerts=alerts, base=SITE["base_url"], prefill=prefill, no_buyer=no_buyer, swept=swept,
                           swept_left=swept_left,
                           rdec={d["decision"]: d["n"] for d in decisions}, withheld=withheld,
                           capacity=capacity,
                           AGENT_STATUSES=AGENT_STATUSES + [None])  # None = lead the desk never touched yet


@app.get("/admin/lead/<lead_id>")
def admin_lead(lead_id):
    _admin()
    l = con.execute("SELECT * FROM leads WHERE lead_id=?", (lead_id,)).fetchone() or abort(404)
    c = con.execute("SELECT * FROM consents WHERE lead_id=?", (lead_id,)).fetchone()
    ev = [dict(r) for r in con.execute("SELECT ts,event,step,campaign,utm_source,data FROM events WHERE visitor_id=? ORDER BY id", (l["visitor_id"],))]
    dl = [dict(r) for r in con.execute("SELECT * FROM deliveries WHERE lead_id=?", (lead_id,))]
    rd = [dict(r) for r in con.execute("SELECT ts, decision, buyer_id, reason FROM routing_decisions WHERE lead_id=? ORDER BY id", (lead_id,))]
    # the operator's WHOLE lead story in one page: WHAT THE BUYER DID with the lead (lead_outcomes
    # rows — desk feedback, returns, credit decisions) and WHAT THE LEDGER recorded against it
    # (deliveries' charges/credits/adjustments that reference this lead). Always real stored rows,
    # never reconstructed; the pending-return flag comes from the SAME queue the Credit tab uses.
    outc = [dict(r) for r in con.execute("SELECT id, ts, buyer_id, outcome, note FROM lead_outcomes WHERE lead_id=? ORDER BY id", (lead_id,))]
    money = [dict(r) for r in con.execute("SELECT ts, buyer_id, kind, amount, balance_after, note FROM buyer_ledger WHERE lead_id=? ORDER BY id", (lead_id,))]
    pending_return = any(r["lead_id"] == lead_id for r in billing.pending_credit_requests(con))
    return render_template("admin_lead.html", l=dict(l), c=dict(c) if c else {}, ev=ev, dl=dl, rd=rd, token=ADMIN_TOKEN, tab="leads",
                           j=lambda s: json.dumps(json.loads(s or "{}"), indent=1), outc=outc, money=money,
                           pending_return=pending_return, resend=request.args.get("resend", ""))


@app.post("/admin/lead/<lead_id>/resend")
def admin_lead_resend(lead_id):
    """Operator escalation for a webhook delivery that exhausted its automatic retries: re-posts the
    ORIGINAL recorded payload to the buyer's CURRENT webhook URL, appends one deliveries row, and never
    re-sells, re-charges, or changes routing state. Admin token is the credential here."""
    _admin()
    l = con.execute("SELECT * FROM leads WHERE lead_id=?", (lead_id,)).fetchone() or abort(404)
    if l["routing_status"] != "DELIVERED":
        why = ("re-post refused: only a delivered lead has a recorded payload to resend (this one is " + l["routing_status"] + ")")
        return redirect(url_for("admin_lead", lead_id=lead_id, token=ADMIN_TOKEN, resend=why), code=302)
    result = route.operator_resend(con, route.load_buyers(CFG), dict(l))
    con.commit()
    spoke = {"RESENT": "re-posted to the buyer's webhook successfully",
             "FAILED": "re-post FAILED — newest deliveries row has the response; contact the buyer",
             "NO_WEBHOOK": "this buyer is not on webhook delivery (method/URL unset) — nothing posted",
             "NO_PAYLOAD": "no recorded payload file exists for this lead — nothing posted",
             "LIVE_DELIVERY_OFF": "LEADENGINE_LIVE_DELIVERY is off (staging): nothing was posted"}.get(result, result)
    return redirect(url_for("admin_lead", lead_id=lead_id, token=ADMIN_TOKEN, resend=spoke), code=302)


@app.route("/admin/leads/new", methods=["GET", "POST"])
def admin_lead_new():
    """Operator manual lead entry (secure admin form). RULES IT NEVER BREAKS:
    - a consumer phone WITHOUT recorded consent evidence cannot enter as a sellable lead:
      no attestation + timestamp + actual consent text -> EMAIL_ONLY -> NURTURE, never routed;
    - the SAME qualify.validate() rules as a consumer submission (name, zip, served state, phone);
    - duplicates are detected by the SAME 30-day phone/email check and recorded as DUPLICATE;
    - the consumer-phone DB trigger still forbids any non-CONSUMER_SUBMITTED number;
    - routing, when a qualified phone lead does clear qualification, is the SAME route.route()
      platform assignment (never a chosen buyer), audited in routing_decisions like every lead.
    Provenance is explicit: the operator states WHERE the inquiry came from, and the console
    pastes the consent evidence it actually holds. Nothing is invented."""
    _admin()
    cids = sorted(CAMPAIGN_BY_ID)
    ages = sorted(qualify.AGE_LOW)
    if not request.form:
        return render_template("admin_lead_new.html", token=ADMIN_TOKEN, campaigns=cids,
                               states=SITE["served_states"], ages=ages, consent_version=CONSENT["version"],
                               d={}, errs=[], tab="overview")
    errs, d = [], request.form
    cid = (d.get("campaign") or "").strip()
    camp = CAMPAIGN_BY_ID.get(cid)
    if not camp:
        errs.append("Choose the campaign this inquiry belongs to.")
    first = (d.get("first_name") or "").strip()
    last = (d.get("last_name") or "").strip()
    email = (d.get("email") or "").strip().lower()
    raw_phone = (d.get("phone") or "").strip()
    src = (d.get("source") or "").strip()
    note = (d.get("note") or "").strip()
    # consent evidence the operator must actually hold (who consented, when, to what wording)
    att = bool(d.get("consent_attested"))
    cts = (d.get("consent_ts") or "").strip()
    ctext = (d.get("consent_text") or "").strip()
    full = att and bool(cts) and bool(ctext)
    if not first or not last:
        errs.append("First and last name are required.")
    if not src:
        errs.append("State where this inquiry came from (provenance is required).")
    if not full and not email:
        errs.append("Without full consent evidence (attestation + timestamp + the consent wording) "
                    "a phone cannot be recorded as callable — enter the email instead, or supply the evidence.")
    if errs:
        return render_template("admin_lead_new.html", token=ADMIN_TOKEN, campaigns=cids,
                               states=SITE["served_states"], ages=ages, consent_version=CONSENT["version"],
                               d=d, errs=errs, tab="overview"), 422
    ist = 1 if d.get("test") else 0
    form = {"first_name": first, "last_name": last, "email": email, "phone": raw_phone,
            "contact_scope": "PHONE_AND_EMAIL" if full else "EMAIL_ONLY",
            "zip": (d.get("zip") or "").strip(), "state": (d.get("state") or "").strip(),
            "age_range": (d.get("age_range") or "").strip(), "coverage_for": (d.get("coverage_for") or "").strip(),
            "timeframe": (d.get("timeframe") or "").strip(), "urgency": (d.get("urgency") or "").strip(),
            "household_size": (d.get("household_size") or "").strip(), "income_band": (d.get("income_band") or "").strip(),
            "enrollment_situation": (d.get("enrollment_situation") or "").strip(),
            "preferred_contact": (d.get("preferred_contact") or "").strip(), "is_test": ist}
    q = qualify.qualify(con, form, camp, SITE, [], None)
    lead_id = "L" + now()[:10].replace("-", "") + "-" + secrets.token_hex(4)
    lead = {"lead_id": lead_id, "created_at": now(), "visitor_id": "", "is_test": ist,
            "campaign": camp["id"], "product": camp["product"], "landing_url": "",
            "utm_source": "manual_entry", "utm_medium": "operator", "utm_campaign": "",
            "first_name": first, "last_name": last, "email": email, "phone": q["phone"],
            "zip": form["zip"], "state": form["state"], "age_range": form["age_range"],
            "coverage_for": form["coverage_for"], "timeframe": form["timeframe"],
            "preferred_contact": form["preferred_contact"],
            "answers": {"manual_entry": {"source": src, "note": note, "by": "admin", "ts": now()},
                        "urgency": form["urgency"], "enrollment_situation": form["enrollment_situation"]},
            "tool_result": {}, "contact_scope": form["contact_scope"],
            "qual_status": q["qual_status"], "qual_reasons": q["qual_reasons"], "score": q["score"],
            "tier": q["tier"], "score_breakdown": q["score_breakdown"], "intel_match": {},
            "routing_status": "PENDING",
            "lead_type": q["lead_type"], "phone_e164": q["phone_check"]["e164"] or None,
            "phone_valid": 1 if q["phone_check"]["valid"] else 0, "phone_check": q["phone_check"]["reason"],
            "phone_area_region": q["phone_check"]["area_region"] or None,
            "phone_line_type": q["phone_check"]["line_type"] if q["phone"] else None,
            "phone_line_type_source": q["phone_check"]["line_type_source"] if q["phone"] else None,
            "phone_source": phone_mod.PHONE_SOURCE if q["phone"] else None}
    if full:   # the operator-recorded consent: stored like any web consent but honestly marked
        ctext_final = f"[operator-recorded consent · source: {src}]\n{ctext}"
        insert(con, "consents", {"lead_id": lead_id, "ts": cts, "consent_version": (d.get("consent_version") or CONSENT["version"]).strip() or CONSENT["version"],
                                 "consent_text": ctext_final, "seller_named": _platform_seller(camp.get("lang", "en")),
                                 "checkbox_checked": 1, "scope": "PHONE_AND_EMAIL",
                                 "ip": (d.get("consent_ip") or "").strip() or "operator-recorded",
                                 "user_agent": "manual-entry", "page_url": (d.get("consent_page_url") or "").strip() or "",
                                 "form_snapshot_sha256": "", "phone": q["phone"], "email": email})
        lead["consent_ts"], lead["consent_version"] = cts, CONSENT["version"]
    # THE SAME routing control as a consumer submission — a qualified, consented phone lead is
    # offered to the platform (never a chosen buyer); anything else is NOT_ROUTABLE and stays put.
    buyers = _buyers(bool(ist))
    if q["qual_status"] == "QUALIFIED" and q["lead_type"] == "PHONE_LEAD" and q["phone"]:
        r = route.route(con, buyers, lead, blocked_ids=_blocked_buyers(buyers, ist), health=_health())
        lead.update(r)
        if r["routing_status"] == "ROUTED":
            decision, bid_ = "ASSIGNED", r["buyer_id"]
            reason = (f"manual entry ({src}): qualified consented phone lead -> assigned EXCLUSIVE at "
                      f"tier {lead.get('tier')} @ ${r['price']} (same platform assignment as a consumer submission)")
        else:
            decision, bid_ = "NO_BUYER", None
            reason, _ = route.explain(con, buyers, lead, _blocked_buyers(buyers, ist), health=_health())
            reason = f"manual entry ({src}): {reason}"
    else:
        decision, bid_ = "NOT_ROUTABLE", None
        reason = (f"manual entry ({src}): not a routable phone lead (qual={q['qual_status']}, "
                  f"lead_type={q['lead_type']}, scope={form['contact_scope']}, reasons {q['qual_reasons']})")[:450]
    insert(con, "routing_decisions", {"ts": now(), "lead_id": lead_id, "decision": decision,
                                      "buyer_id": bid_, "reason": reason, "is_test": ist})
    insert(con, "leads", lead)
    con.commit()
    if lead.get("routing_status") == "ROUTED":
        _sell_lead(lead, buyers)
    spoken = {"QUALIFIED": "lead recorded; routing ran the normal platform assignment",
              "DUPLICATE": "recorded as DUPLICATE (same phone/email already inside 30 days) — never sold",
              "NURTURE": "email-only inquiry recorded — not sold, not routed",
              "UNQUALIFIED": "recorded but did not pass qualification — never routed"}[q["qual_status"]]
    return redirect(url_for("admin_lead", lead_id=lead_id, token=ADMIN_TOKEN, resend=spoken), code=302)


@app.post("/admin/lead/<lead_id>/deliver")
def admin_lead_deliver(lead_id):
    """Operator 'deliver now' for a STRANDED sale: the consumer consented and the platform assigned a
    buyer (ROUTED), but delivery + charge never finished — a crash mid-sale, or the same self-heal that
    normally runs when the buyer's desk loads (which never happens while the buyer is deactivated).
    Re-runs the SAME atomic deliver+charge for the SAME assigned buyer: deliver() refuses an already
    DELIVERED lead and the ledger dedupe makes a second charge impossible, so this can never re-sell
    or double-bill. Never re-routes: the buyer was fixed at routing time (the consent named the platform,
    not a switchable buyer). Uses the full buyers file — an inactive buyer who was ALREADY assigned and
    sold this lead still receives it; deactivating a buyer stops NEW routing, not an existing sale."""
    _admin()
    row = con.execute("SELECT * FROM leads WHERE lead_id=?", (lead_id,)).fetchone() or abort(404)
    l = dict(row)
    if l["routing_status"] != "ROUTED" or not l.get("buyer_id"):
        why = ("the lead is already DELIVERED (nothing stranded to complete)" if l["routing_status"] == "DELIVERED"
               else "only an assigned-but-undelivered (ROUTED) sale can be completed; routing is never re-decided here")
        return redirect(url_for("admin_lead", lead_id=lead_id, token=ADMIN_TOKEN, resend=why), code=302)
    _sell_lead(l, json.loads((CFG / "buyers.json").read_text(encoding="utf-8")))
    msg = "delivery re-attempted — the lead is DELIVERED (and charged exactly once) or still ROUTED with the failure noted in lead outcomes"
    return redirect(url_for("admin_lead", lead_id=lead_id, token=ADMIN_TOKEN, resend=msg), code=302)


def _reoffer_attempt(l: dict, buyers: list[dict], t: int, src: str) -> str:
    """ONE re-offer attempt for an UNMATCHED lead: the SAME platform-controlled assignment (the consumer
    consented to the platform, never to a pre-chosen agency), audited either way in routing_decisions.
    Shared by the operator button on /admin/lead and the timed sweep (_auto_reoffer)."""
    r = route.route(con, buyers, l, blocked_ids=_blocked_buyers(buyers, t), health=_health())
    if r["routing_status"] == "ROUTED":
        l.update(r)
        con.execute("UPDATE leads SET routing_status='ROUTED', buyer_id=?, price=?, routed_at=?, sale_type=? WHERE lead_id=?",
                    (r["buyer_id"], r["price"], r["routed_at"], r["sale_type"], l["lead_id"]))
        insert(con, "routing_decisions", {"ts": now(), "lead_id": l["lead_id"], "decision": "RE_ASSIGNED", "buyer_id": r["buyer_id"],
                                          "reason": f"{src}: unmatched lead assigned EXCLUSIVE at tier {l.get('tier')} "
                                                    f"@ ${r['price']} (same platform consent)", "is_test": t})
        con.commit()                        # the assignment is committed BEFORE delivery: no stranded-rewrite
        _sell_lead(l, buyers)               # atomic deliver+charge (never re-routes; may strand -> desk retries)
        return f"re-offered and assigned to {r['buyer_id']} at ${r['price']} — delivery follows (see the lead's deliveries)"
    reason, _ = route.explain(con, buyers, l, _blocked_buyers(buyers, t), health=_health())
    insert(con, "routing_decisions", {"ts": now(), "lead_id": l["lead_id"], "decision": "NO_BUYER", "buyer_id": None,
                                      "reason": f"{src}: " + reason, "is_test": t})
    con.commit()
    return "still no eligible buyer — nothing was sold or assigned (reason updated below)"


def _auto_reoffer(force: bool = False) -> tuple[int, int]:
    """Sweep over the unmatched-queue: every NO_BUYER lead whose last assignment attempt is older than
    marketplace.reoffer.after_hours re-runs the ONE platform assignment. Consent-valid (the consent
    names the platform, never a chosen agency) and audited per attempt; the previous attempt's
    timestamp suppresses repeats inside the window. Guards as always: QUALIFIED phone leads only,
    never opted-out, never a delivered sale (exclusivity). In production the AUTOMATIC (on-page-load)
    sweep stays OFF until marketplace.reoffer.auto_in_live is truthy — but force=True is the operator's
    explicit POST /admin/sweep click, which IS that decision, so only the usual safety guards apply.
    Returns (re_offered, still_unmatched)."""
    ro = MARKETPLACE.get("reoffer", {}) or {}
    if not ro.get("enabled", True):
        return 0, 0
    if not force and PRODUCTION and not ro.get("auto_in_live"):
        return 0, 0
    import datetime as _dt
    batch = int(ro.get("batch", 10) or 0)
    cutoff = "9999" if force else (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=float(ro.get("after_hours", 48) or 0))).strftime("%Y-%m-%dT%H:%M:%S")
    re_offered = still_unmatched = 0
    for row in con.execute("""SELECT * FROM leads WHERE routing_status='NO_BUYER' AND qual_status='QUALIFIED'
        AND lead_type='PHONE_LEAD' AND COALESCE(phone,'') != '' AND NOT COALESCE(opted_out,0)
        AND COALESCE((SELECT d.ts FROM routing_decisions d WHERE d.lead_id=leads.lead_id
             ORDER BY d.id DESC LIMIT 1), '') <= ? ORDER BY created_at LIMIT ?""", (cutoff, batch)).fetchall():
        try:
            l = dict(row)
            tt = 1 if l.get("is_test") else 0
            _reoffer_attempt(l, _buyers(bool(tt)), tt, "operator sweep" if force else f"auto re-offer ({ro.get('after_hours', 48)}h sweep)")
            if con.execute("SELECT routing_status FROM leads WHERE lead_id=?", (l["lead_id"],)).fetchone()[0] in ("ROUTED", "DELIVERED"):
                re_offered += 1
            else:
                still_unmatched += 1
        except Exception:
            con.rollback()      # one bad row never blocks the load; it stays queued for the next sweep
            still_unmatched += 1
    return re_offered, still_unmatched


@app.post("/admin/lead/<lead_id>/reroute")
def admin_lead_reroute(lead_id):
    """Operator re-offer for an UNMATCHED lead: re-runs the one platform assignment for a QUALIFIED phone
    lead that had no eligible buyer (cap hit, credits drained, schedule gap). Consent-valid by design:
    the consumer consented to the PLATFORM connecting them with a licensed agent — never to a
    pre-chosen agency — so assigning them later (when a buyer is able to take new leads) fulfills the
    same consent. Guards: only a NO_BUYER lead (a lead is never re-sold once delivered); no opted-out
    consumer; same billing enforcement (blocked buyers); every attempt is audited in routing_decisions.
    No eligible buyer → the lead simply stays unmatched; nothing is sold, charged or re-routed."""
    _admin()
    row = con.execute("SELECT * FROM leads WHERE lead_id=?", (lead_id,)).fetchone() or abort(404)
    if row["routing_status"] != "NO_BUYER":
        why = ("nothing to re-offer — this lead was already " + row["routing_status"] +
               (" (a delivered sale is never re-routed or re-sold)" if row["routing_status"] == "DELIVERED" else ""))
        return redirect(url_for("admin_lead", lead_id=lead_id, token=ADMIN_TOKEN, resend=why), code=302)
    l = dict(row)
    if l["qual_status"] != "QUALIFIED" or l.get("lead_type", "PHONE_LEAD") != "PHONE_LEAD" or not l.get("phone"):
        return redirect(url_for("admin_lead", lead_id=lead_id, token=ADMIN_TOKEN,
                                resend="re-offer refused: only a QUALIFIED PHONE lead is routable — this one is not"), code=302)
    if l.get("opted_out"):
        return redirect(url_for("admin_lead", lead_id=lead_id, token=ADMIN_TOKEN,
                                resend="re-offer refused: the consumer opted out — never contacted again"), code=302)
    t = 1 if l.get("is_test") else 0
    buyers = _buyers(bool(t))
    msg = _reoffer_attempt(l, buyers, t, "operator re-offer")
    return redirect(url_for("admin_lead", lead_id=lead_id, token=ADMIN_TOKEN, resend=msg), code=302)


@app.post("/admin/sweep")
def admin_sweep():
    """Run the unmatched re-offer sweep NOW as an explicit operator action. Same ONE platform assignment,
    same audits — but force=True means the operator made the decision to attempt every current queue
    row regardless of when its last attempt ran, instead of waiting for the after_hours timed sweep.
    Never re-offers a delivered sale, respects opt-outs, caps and prepaid credits."""
    _admin()
    try:
        n, left = _auto_reoffer(force=True)
    except Exception:
        con.rollback()
        n, left = 0, 0
    msg = (f"sweep: re-offered {n} unmatched lead(s), {left} still waiting for an eligible buyer — "
           "every attempt is audited in the distribution decisions")
    return redirect(url_for("admin", token=ADMIN_TOKEN, saved=msg), code=302)


@app.post("/admin/payments/<payment_rowid>/mark")
def admin_payment_mark(payment_rowid):
    """Operator resolution for a STUCK self-serve payment (PENDING >24h, webhook never confirmed it).
    The operator verifies the real status on Stripe's dashboard first; marking here records the finding
    and closes the row. This NEVER grants credits: fulfillment moves only PENDING → PAID → CREDITED,
    so a row marked CLOSED can never be double-credited by a late webhook — if money did reach the
    account, the operator records it explicitly with the top-up form plus the verification note."""
    _admin()
    try:
        rid = int(payment_rowid)
    except ValueError:
        abort(404)
    row = con.execute("SELECT rowid AS rid, buyer_id, status FROM buyer_payments WHERE rowid=?", (rid,)).fetchone() or abort(404)
    note = (request.form.get("note") or "").strip()
    if not note:
        return redirect(url_for("admin", token=ADMIN_TOKEN, err="a resolution note is required — what Stripe showed (session id / paid / refunded)"), code=302)
    if row["status"] != "PENDING":
        return redirect(url_for("admin", token=ADMIN_TOKEN, err=f"payment row {payment_rowid} is already {row['status']} — only a PENDING row can be closed"), code=302)
    con.execute("UPDATE buyer_payments SET status='CLOSED' WHERE rowid=?", (rid,))
    old_note = con.execute("SELECT note FROM buyer_payments WHERE rowid=?", (rid,)).fetchone()[0] or ""
    con.execute("UPDATE buyer_payments SET note=? WHERE rowid=?", ((old_note + " | operator: " + note)[:300], rid))
    con.commit()
    return redirect(url_for("admin", token=ADMIN_TOKEN, saved=f"stuck payment closed for {row['buyer_id']} — recorded: {note[:120]}"), code=302)


@app.post("/admin/buyer/<buyer_id>/test_webhook")
def admin_buyer_test_webhook(buyer_id):
    """Fire a SIGNED, consumer-data-free test ping at the buyer's current webhook URL so the operator can
    actually perform the 'delivery fan-out test' verification (the UI records the check with a reference;
    until now nothing could produce one). The payload proves to the CRM WEBHOOK side that their
    verification pipeline handles our signature header; it contains no consumer data whatsoever, so it
    can never sell a lead or leak one. Staging default: nothing posts (LEADENGINE_LIVE_DELIVERY off)."""
    _admin()
    b = next((x for x in json.loads((CFG / "buyers.json").read_text(encoding="utf-8")) if x["id"] == buyer_id), None) or abort(404)
    d = b.get("delivery", {})
    if d.get("method") != "webhook" or not d.get("webhook_url"):
        return redirect(url_for("admin", token=ADMIN_TOKEN, err=f"{buyer_id} is not on webhook delivery — set the method + URL first"), code=302)
    if not d.get("webhook_secret"):
        return redirect(url_for("admin", token=ADMIN_TOKEN, err=f"{buyer_id} has webhook delivery but no signing secret yet — activate/re-save with the secret issued"), code=302)
    if os.environ.get("LEADENGINE_LIVE_DELIVERY") != "1":
        return redirect(url_for("admin", token=ADMIN_TOKEN, err="staging: nothing was posted (LEADENGINE_LIVE_DELIVERY is off) — flip it on for a real fan-out test"), code=302)
    body = json.dumps({"type": "test", "brand": SITE["brand"], "sent_at": now(),
                       "message": "Connectivity + signature test from the GS Allude lead engine. No consumer data — discard after verifying."}).encode("utf-8")
    try:
        ok, resp = route.post_signed(d["webhook_url"], d["webhook_secret"], body)
        msg = ("test ping accepted (" + resp + ") — the buyer's CRM signed in! Record the 'delivery fan-out test' "
               "verification with this response and the date") if ok \
              else ("test ping REJECTED (" + resp + ") — check the webhook URL/CRM receiver with the buyer first")
    except Exception as exc:
        msg = "test ping FAILED: " + repr(exc)[:200]
    return redirect(url_for("admin", token=ADMIN_TOKEN, saved=msg), code=302)


@app.get("/admin/credits")
def admin_credits():
    """Return-credit dispute queue: buyer requests that got NO automatic credit, each with the evidence
    that produced the refusal (window, contact state, price, history). Nothing here moves money on its
    own; the operator's explicit decision does — grant credits the SAME SQL-guarded ledger path (one
    credit per lead+buyer, never twice), deny records the reasoning. Every decision is appended in
    lead_outcomes as a CREDIT_DECISION row the buyer can see on their lead page too."""
    _admin()
    rows = billing.pending_credit_requests(con)
    trail = billing.decided_credit_requests(con)
    return render_template("admin_credits.html", rows=rows, trail=trail,
                           token=ADMIN_TOKEN, tab="credits")


@app.post("/admin/lead/<lead_id>/credit_decision")
def admin_credit_decision(lead_id):
    """Operator resolution for a refused buyer return-credit. GRANT: reuses billing.credit_return — the
    SQL-guarded, once-per-lead+buyer credit (if a credit already exists, e.g. the desk heal raced in,
    nothing more is written and the decision note says so) — never an unscoped ADJUSTMENT. DENY:
    records the reasoning only; the ledger is untouched. The note is required either way: an operator
    money decision always carries its why."""
    _admin()
    decision = request.form.get("decision", "")
    note = (request.form.get("note") or "").strip()
    if decision not in ("grant", "deny"):
        abort(400)
    if not note:
        return redirect(url_for("admin_credits", token=ADMIN_TOKEN, err="a decision note is required — state what the buyer showed / what policy applies"), code=302)
    l = con.execute("SELECT * FROM leads WHERE lead_id=?", (lead_id,)).fetchone() or abort(404)
    buyer_id = l["buyer_id"]
    already = con.execute("SELECT 1 FROM buyer_ledger WHERE buyer_id=? AND lead_id=? AND kind='LEAD_CREDIT'",
                          (buyer_id, lead_id)).fetchone()
    if decision == "grant" and l["routing_status"] != "DELIVERED":
        return redirect(url_for("admin_credits", token=ADMIN_TOKEN, err=f"{lead_id} is not a delivered lead — nothing to credit"), code=302)
    if decision == "grant" and not already:
        billing.credit_return(con, buyer_id, dict(l))   # guarded: writes at most once per lead+buyer
        spoke = "credited (via the same once-only return-credit ledger path)"
    elif decision == "grant" and already:
        spoke = "credited (the ledger already carried exactly one credit for this lead — nothing more written)"
    else:
        spoke = "denied — the ledger is untouched"
    con.execute("INSERT INTO lead_outcomes (lead_id,buyer_id,ts,outcome,note) VALUES (?,?,?,?,?)",
                (lead_id, buyer_id, now(), billing.CREDIT_DECISION, f"operator {decision}: {note} ({spoke})"[:500]))
    con.commit()
    return redirect(url_for("admin_credits", token=ADMIN_TOKEN, saved=f"credit request for {lead_id}: {spoke} (decision recorded on the lead's history)"), code=302)


@app.get("/admin/intel")
def admin_intel():
    _admin()
    aud = intel_mod.audience(INTEL_DB, list(CAMPAIGNS.values()))
    pt = intel_mod.partner_targets(INTEL_DB)
    return render_template("admin_intel.html", aud=aud, pt=pt, token=ADMIN_TOKEN, tab="intel")


@app.get("/admin/intel/signals")
def admin_signals():
    """Review queue for PUBLIC intent signals captured by tools/signals_watch.py / offline ingest.
    Read-only review loop over data/signals.db (a separate DB from leads.db): NO route, delivery,
    contact, or purchase action exists on this page by design. The only transitions are review
    statuses on a signal row, and IRRELEVANT/ACTIONED require an evidence note (which page/test
    the finding served, or why it is not our need) — the same evidence standard the Scout ladder uses.
    Filters: platform (source), language, topic taxonomy, relevance floor, and age window — the
    unified discovery console the operator brief asked for."""
    _admin()
    scon = signals_mod.connect(SIGNALS_DB)
    try:
        status = request.args.get("status", "NEW")
        if status not in ("NEW", "REVIEWED", "ACTIONED", "IRRELEVANT"):
            status = "NEW"
        try:
            min_intent = float(request.args.get("min_intent", "0") or 0)
        except ValueError:
            min_intent = 0.0
        try:
            days = int(request.args.get("days", "0") or 0)
        except ValueError:
            days = 0
        filters = {
            "source": request.args.get("source", "").strip()[:40],
            "lang": request.args.get("lang", "").strip(),
            "topic": request.args.get("topic", "").strip()[:40],
            "min_intent": max(0.0, min(min_intent, 10.0)),
            "days": max(0, min(days, 365)),
            "kind": request.args.get("kind", "").strip()[:10],
            "intent_cat": request.args.get("intent_cat", "").strip()[:12],
        }
        rows = signals_mod.queue_filtered(scon, status, limit=100, **filters)
        return render_template("admin_signals.html", rows=rows,
                               c=signals_mod.counts(scon), status=status, token=ADMIN_TOKEN, tab="intel",
                               facets=signals_mod.facets(scon), f=filters,
                               kk=signals_mod.kind_counts(scon),
                               msg=request.args.get("msg", ""))
    finally:
        scon.close()


@app.get("/admin/intel/signals.csv")
def admin_signals_csv():
    """Export AGGREGATE insight rows (source x language x topic): counts, average relevance,
    and how many findings were actioned vs irrelevant. Deliberately NOT a row dump: a signal
    row has no consumer data anyway (no usernames, no contact fields), and the operating value
    is the pattern, not the individual post. Evidence lives in the review queue UI."""
    _admin()
    scon = signals_mod.connect(SIGNALS_DB)
    try:
        rows = signals_mod.aggregate(scon)
    finally:
        scon.close()
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["kind", "source", "lang", "topic", "n", "avg_intent", "actioned", "irrelevant"])
    w.writeheader()
    w.writerows(rows)
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=signal_insights.csv"})


@app.get("/admin/intel/signals/<int:sid>/campaign-link")
def admin_signal_campaign_link(sid):
    """Turn a reviewed INTEREST signal into a TRACKED acquisition link: a /go/<code> row in
    the EXISTING leads.db link table pointing at the real consumer /check journey, attributed
    as utm_source=signal_<platform>, utm_campaign=intent_<topic>_<lang>, utm_content=signal_<id>.
    This is the ONLY bridge discovery ever builds to the consumer side: the operator still
    publishes the link themselves (a content page, a reply NOT sent by us, a bio). No contact,
    no export of the post author, no automatic lead — a lead only exists when that person
    voluntarily submits through the Check with consent."""
    _admin()
    scon = signals_mod.connect(SIGNALS_DB)
    try:
        s = scon.execute("SELECT * FROM signals WHERE id=?", (sid,)).fetchone()
    finally:
        scon.close()
    if not s:
        abort(404)
    code = f"s{sid}"
    exists = con.execute("SELECT code FROM links WHERE code=?", (code,)).fetchone()
    if not exists:
        con.execute("INSERT OR IGNORE INTO links (code, campaign, channel, utm_source, utm_medium, "
                    "utm_campaign, utm_content, partner_id, note, created_at, clicks) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,0)",
                    (code, CAMPAIGN_BY_ID.get("health_aca", {}).get("id", "health_aca"), "seo",
                     f"signal_{s['source'][:20]}", "organic",
                     f"intent_{(s['topic'] or 'none')[:24]}_{s['lang'] or 'en'}",
                     f"signal_{sid}", None,
                     f"signal #{sid} {s['url'][:200]}", now()))
        con.commit()
    msg = f"/{code} → /check?utm_source=signal_{s['source']}&utm_campaign=intent_{(s['topic'] or 'none')}_{s['lang'] or 'en'}&utm_content=signal_{sid}&a=health_check"
    return redirect(url_for("admin_signals", token=ADMIN_TOKEN, status=s["status"], msg=msg), code=302)


@app.post("/admin/intel/signals/<int:sid>/status")
def admin_signals_status(sid):
    """Record a signal-review decision. Evidence rule enforced server-side: IRRELEVANT and
    ACTIONED refuse to save without a note (what the finding answered, or why it is not ours).
    There is deliberately no 'contact the author' action anywhere in this loop."""
    _admin()
    status = request.form.get("status", "").strip()
    note = (request.form.get("note") or "").strip()
    if status not in ("REVIEWED", "IRRELEVANT", "ACTIONED"):
        return redirect(url_for("admin_signals", token=ADMIN_TOKEN,
                                msg=f"unknown status '{status}'"), code=302)
    if status != "REVIEWED" and not note:
        return redirect(url_for("admin_signals", token=ADMIN_TOKEN,
                                msg=f"{status} requires an evidence note (which page/test it served, or why not ours)"), code=302)
    scon = signals_mod.connect(SIGNALS_DB)
    try:
        ok = signals_mod.review(scon, sid, status, note)
    finally:
        scon.close()
    return redirect(url_for("admin_signals", token=ADMIN_TOKEN,
                            msg=f"signal #{sid}: {status} recorded" if ok else f"signal #{sid} not found"), code=302)


# ------------------------------------------------------------------ consumer prospects people registry
# The evidence-bearing path: public signals -> prospect rows with EXPLICITLY PUBLISHED
# contact surfaces. A row can never become a lead here: the consumer must submit through
# the existing /check journey themselves. PERMISSION_VERIFIED / QUALIFIED are operator
# actions with recorded notes — nothing in the pipeline is ever automatic past CONTACT_FOUND.
@app.get("/admin/people")
def admin_people():
    """Consumer-prospect registry fed by the sweep's contact miner. Distinguishes:
    relevant posts with no contact (CONTACT_NOT_AVAILABLE), prospects with published
    contact but no verified permission (CONTACT_FOUND, permission NONE), prospects whose
    permission an operator verified by opening the source URL (PERMISSION_VERIFIED), and
    operator-decided QUALIFIED rows ready to be handed an OFFICIAL /check link (the person
    still has to submit themselves). No contact/export/send action exists on this page."""
    _admin()
    filters = {
        "status": request.args.get("status", "").strip()[:40],
        "source": request.args.get("source", "").strip()[:60],
        "contact": request.args.get("contact", "").strip()[:40],
        "dup": request.args.get("dup", "").strip()[:12],
    }
    filters["match"] = request.args.get("match", "").strip()[:12]
    rows = prospector.list_rows(con, limit=300, **filters)
    c = prospector.counts(con)
    return render_template("admin_people.html", rows=rows, c=c, irep=identity_mod.report(con),
                           yields=prospector.source_yield(con), token=ADMIN_TOKEN, tab="prospects",
                           link_url=request.args.get("link_url", ""), link_pid=request.args.get("link_pid", ""),
                           facets={"sources": [r["source"] for r in c["by_source"]]}, f=filters,
                           stages=prospector.STATUSES, action=prospector.NEXT_ACTION,
                           msg=request.args.get("msg", ""))


@app.post("/admin/people/<int:pid>/verify")
def admin_people_verify(pid):
    """VERIFIED: an operator opened the source URL and confirmed a real person with a current need."""
    _admin()
    note = (request.form.get("note") or "").strip()
    ok = prospector.verify(con, pid, note)
    msg = (f"prospect #{pid}: VERIFIED" if ok else
           f"prospect #{pid}: not recorded (needs a 10+ character note about what you checked)")
    return redirect(url_for("admin_people", token=ADMIN_TOKEN, msg=msg), code=302)


@app.post("/admin/people/<int:pid>/link")
def admin_people_link(pid):
    """Create the tracked /check link for a QUALIFIED prospect's public thread.

    This is the missing conversion step, not a contact action: it produces a URL an operator may
    place in the same public thread where the person asked. Nothing is emailed, texted or DMed, and
    the person still has to submit the check themselves for a lead to exist.
    """
    _admin()
    link = prospector.issue_link(con, pid, SITE["base_url"])
    if not link:
        msg = (f"prospect #{pid}: no link created. A link can only be issued for a QUALIFIED row "
               f"(verify the thread, then qualify it with a note).")
        return redirect(url_for("admin_people", token=ADMIN_TOKEN, msg=msg), code=302)
    return redirect(url_for("admin_people", token=ADMIN_TOKEN, link_url=link["url"], link_pid=pid,
                            msg=f"prospect #{pid}: tracked check link {'already existed' if link['existing'] else 'created'}"),
                    code=302)


@app.post("/admin/people/<int:pid>/status")
def admin_people_status(pid):
    """Operator transitions on a public prospect. Every one requires evidence, and no adapter can
    reach any of them: CONSENTED needs the person's own authorization quoted, QUALIFIED needs a
    verified row with a published contact, ROUTING_ELIGIBLE is not offered here at all (it is set
    only when that same person submits /check themselves)."""
    _admin()
    status = (request.form.get("status") or "").strip().upper()
    note = (request.form.get("note") or "").strip()
    if status == "VERIFIED":
        ok, need = prospector.verify(con, pid, note), "a 10+ character note about what you checked"
    elif status == "CONSENTED":
        ok, need = prospector.record_consent(con, pid, note), "the person's own authorization, quoted"
    elif status == "QUALIFIED":
        ok, need = prospector.qualify(con, pid, note), "VERIFIED or CONSENTED, a published contact, and a reason"
    elif status == "REJECTED":
        ok, need = prospector.reject(con, pid, note), "a reason (8+ characters)"
    elif status == "DUPLICATE":
        ok, need = prospector.mark_duplicate(con, pid, note), "the earlier prospect key"
    else:
        ok, need = False, "VERIFIED | CONSENTED | QUALIFIED | REJECTED | DUPLICATE"
    msg = (f"prospect #{pid}: {status} recorded" if ok else
           f"prospect #{pid}: {status or 'status'} refused - needs {need}")
    return redirect(url_for("admin_people", token=ADMIN_TOKEN, msg=msg), code=302)


@app.post("/admin/people/<int:pid>/resolve")
def admin_people_resolve(pid):
    """Operator-triggered identity resolution for ONE prospect.

    The one network action on this page: it reads that same public account's other public posts and
    its platform profile, then records the name/phone it can prove. It refuses nothing on the basis
    of "no contact" - an unproven row simply stays UNRESOLVED with the reason kept on the row. It
    contacts nobody, and a resolved phone is still not permission (marketing stays blocked).
    """
    _admin()
    try:
        r = identity_mod.resolve(con, pid, probes=(request.form.get("probes") == "1"))
    except Exception as e:                          # noqa: BLE001 - surfaced to the operator
        return redirect(url_for("admin_people", token=ADMIN_TOKEN,
                                msg=f"prospect #{pid}: resolution failed ({type(e).__name__}: {str(e)[:140]})"),
                        code=302)
    if r.get("status") == "missing":
        return redirect(url_for("admin_people", token=ADMIN_TOKEN,
                                msg=f"no prospect #{pid}"), code=302)
    msg = (f"prospect #{pid}: name {r['name_status']} ({r['name'] or 'none'}), phone "
           f"{r['phone_status']} ({r['phone'] or 'none'}), match {r['match_status']}, "
           f"permission {r['permission']}, marketing {r['eligibility']}")
    return redirect(url_for("admin_people", token=ADMIN_TOKEN, msg=msg), code=302)


# ------------------------------------------------------- imported contacts (third-party records)
# The admission store: records we HOLD from datasets/exports, kept with their provenance and
# stored as evidence — never as submissions. Nothing on this page can dial, email or sell a
# record: a row becomes routable only when that same person submits /check and the row is
# linked to their own consented lead (engine/admission.promote).
@app.get("/admin/contacts")
def admin_contacts():
    """Imported-contact registry: what we hold, where it came from, and exactly why each row may
    not be routed or sold yet."""
    _admin()
    f = {"eligibility": request.args.get("eligibility", "").strip()[:32],
         "source": request.args.get("source", "").strip()[:60],
         "type": request.args.get("type", "").strip()[:24],
         "data_status": request.args.get("data_status", "").strip()[:24],
         "contact": request.args.get("contact", "").strip()[:24],
         "dup": request.args.get("dup", "UNIQUE").strip()[:12],
         "q": request.args.get("q", "").strip()[:80]}
    rows = admission_mod.list_rows(con, limit=300, **{
        "eligibility": f["eligibility"], "source": f["source"], "record_type": f["type"],
        "data_status": f["data_status"], "contact": f["contact"], "dup": f["dup"], "q": f["q"]})
    return render_template("admin_contacts.html", rows=rows, rep=admission_mod.report(con),
                           token=ADMIN_TOKEN, tab="contacts", f=f, msg=request.args.get("msg", ""),
                           eligibility=admission_mod.ELIGIBILITY, why=admission_mod.WHY,
                           types=admission_mod.RECORD_TYPES, statuses=admission_mod.DATA_STATUSES)


@app.post("/admin/contacts/import")
def admin_contacts_import():
    """Admit a pasted CSV export. Every row lands in imported_contacts as NOT_ELIGIBLE with its
    source, licence label and collection date; the import can never create a lead or a consent."""
    _admin()
    source = {"id": (request.form.get("source_id") or "").strip()[:60],
              "name": (request.form.get("source_name") or "").strip(),
              "url": (request.form.get("source_url") or "").strip(),
              "license": (request.form.get("license") or "").strip(),
              "license_note": (request.form.get("license_note") or "").strip()}
    if not source["id"]:
        return redirect(url_for("admin_contacts", token=ADMIN_TOKEN,
                                msg="import refused: the source id is required (an unattributed record is not admissible)"),
                        code=302)
    try:
        r = admission_mod.import_csv_text(
            con, source, request.form.get("csv") or "",
            record_type=(request.form.get("record_type") or "UNKNOWN").strip().upper(),
            batch=(request.form.get("batch") or "").strip()[:40],
            is_test=1 if request.form.get("test") == "1" else 0)
    except Exception as e:                          # noqa: BLE001 - surfaced to the operator
        return redirect(url_for("admin_contacts", token=ADMIN_TOKEN,
                                msg=f"import failed ({type(e).__name__}: {str(e)[:160]})"), code=302)
    return redirect(url_for("admin_contacts", token=ADMIN_TOKEN,
                            msg=(f"batch {r['batch']}: {r['received']} row(s) read · {r['imported']} imported · "
                                 f"{r['duplicates']} duplicate · {r['rejected']} rejected · "
                                 f"{r['missing_contact']} without any phone/email · "
                                 f"all NOT_ELIGIBLE (nothing here is consent)")), code=302)


@app.post("/admin/contacts/<int:cid>/attest")
def admin_contacts_attest(cid):
    """Operator judgement that the record is a real person/organisation.

    Deliberately cannot grant marketing permission: authenticity and permission are separate facts,
    and a note about "this is a real person" says nothing about whether they may be called.
    """
    _admin()
    r = admission_mod.attest(con, cid, request.form.get("status") or "",
                             request.form.get("note") or "")
    msg = (f"contact #{cid}: {r['reason']} (marketing permission unchanged)" if r["ok"]
           else f"contact #{cid}: refused - {r['reason']}")
    return redirect(url_for("admin_contacts", token=ADMIN_TOKEN, msg=msg), code=302)


@app.post("/admin/contacts/<int:cid>/use")
def admin_contacts_use(cid):
    """Record what the source's licence/terms permit for ONE record. This cannot make the record
    consented or sellable — a permitted use is not marketing permission."""
    _admin()
    r = admission_mod.record_use(con, cid, request.form.get("uses") or "",
                                 request.form.get("note") or "")
    msg = (f"contact #{cid}: use recorded (USE_LIMITED, still not sellable)" if r["ok"]
           else f"contact #{cid}: refused - {r['reason']}")
    return redirect(url_for("admin_contacts", token=ADMIN_TOKEN, msg=msg), code=302)


@app.post("/admin/contacts/<int:cid>/promote")
def admin_contacts_promote(cid):
    """Link an imported record to the SAME person's own consented submission.

    The only legitimate path from "we hold this contact" to "this contact is eligible": the person
    submitted /check themselves (a lead with a consent row exists) and their own submitted contact
    matches this record. Nothing is created here — no lead, no consent, no edit to the lead.
    """
    _admin()
    r = admission_mod.promote(con, cid, (request.form.get("lead_id") or "").strip(),
                              request.form.get("note") or "")
    msg = (f"contact #{cid}: {r['reason']}" if r["ok"] else f"contact #{cid}: refused - {r['reason']}")
    return redirect(url_for("admin_contacts", token=ADMIN_TOKEN, msg=msg), code=302)


@app.get("/admin/intel/clusters")
def admin_clusters():
    """Opportunity-discovery view over collected PUBLIC signals: deduplicated topic+lang clusters
    distilled by the LOCAL Ollama model. Structure enforces facts vs inference — counts, mean
    relevance and cited post titles are recorded facts; the model's title/why text is stored as
    labeled inference only. Every cluster links to the EXISTING /admin/opportunities theses the
    topic map grants (e.g. OPP-08), so this refines the ladder instead of parallel-tracking it.
    Build is operator-triggered (collect.py clusters-build, or the button here): never scheduled,
    never a live sweep, never a contact. Dismissing requires a note like any evidence review."""
    _admin()
    status = request.args.get("status", "")
    if status not in ("", "NEW", "REVIEWED", "DISMISSED"):
        status = ""
    scon = signals_mod.connect(SIGNALS_DB)   # clusters live in the same signals.db (one discovery store)
    try:
        rows = clusters_mod.list_clusters(scon, status)
        c = clusters_mod.counts(scon)
    finally:
        scon.close()
    return render_template("admin_clusters.html", rows=rows, c=c, status=status,
                           token=ADMIN_TOKEN, tab="intel", msg=request.args.get("msg", ""))


@app.post("/admin/intel/clusters/<int:cid>/status")
def admin_cluster_status(cid):
    """Record a cluster decision. DISMISS requires a note (why the pattern is not our need),
    matching the evidence standard the signal queue enforces."""
    _admin()
    status = request.form.get("status", "").strip()
    note = (request.form.get("note") or "").strip()
    if status not in ("REVIEWED", "DISMISSED"):
        return redirect(url_for("admin_clusters", token=ADMIN_TOKEN,
                                msg=f"unknown status '{status}'"), code=302)
    if status == "DISMISSED" and not note:
        return redirect(url_for("admin_clusters", token=ADMIN_TOKEN,
                                msg="DISMISSED requires a note — say why this pattern is not our need"), code=302)
    scon = signals_mod.connect(SIGNALS_DB)
    try:
        clusters_mod.review(scon, cid, status, note)
    finally:
        scon.close()
    return redirect(url_for("admin_clusters", token=ADMIN_TOKEN,
                            msg=f"cluster #{cid}: {status} recorded"), code=302)


@app.get("/admin/intel/supply")
def admin_supply():
    """The researched lead-supply registry (config/lead_supply.json): what the Oct 2026 research
    determined about external supply paths, what each integrates like, what consent and exclusivity
    come with, and — critically — THAT RESELLING IS NOT AUTHORIZED BY DEFAULT anywhere. Read-only:
    this page sells nothing and activates nothing; every external row stays supply-disabled until
    a signed agreement lands and the operator records it in buyer verification."""
    _admin()
    return render_template("admin_supply.html", d=cfg("lead_supply"), token=ADMIN_TOKEN, tab="intel")


@app.get("/admin/integrations")
def admin_integrations():
    """One honest page about every connection: the outbound SINKS (where consumer records,
    notifications and money bookkeeping go), the inbound signal SOURCES (what the discovery
    layer can actually collect: COLLECTS / NEEDS_SETUP / NEEDS_KEY / NEEDS_CREDENTIALS /
    TOS_BLOCKED / AGREEMENT_REQUIRED), the webreader's AI-extraction capability state, and
    the buyer-prospect acquisition paths. Status is derived at render time from the same
    env vars and config the code itself uses, so this page cannot claim an integration is
    live when it is not. Read-only: no activation button exists."""
    _admin()
    scon = signals_mod.connect(SIGNALS_DB)
    try:
        sig_counts = signals_mod.per_source_counts(scon)
        try:
            recent_fetches = [dict(r) for r in scon.execute(
                "SELECT ts, source, query, fetched, accepted, note FROM fetch_log "
                "ORDER BY id DESC LIMIT 10")]
        except Exception:                       # noqa: BLE001 - fresh DB: fetch_log exists, be safe
            recent_fetches = []
    finally:
        scon.close()
    pcon = prospects_mod.connect(PROSPECTS_DB)
    try:
        try:
            recent_prospects = prospects_mod.collect_log_recent(pcon, 6)
        except Exception:                       # noqa: BLE001 - pre-2026-10-27 DB without collect_log
            recent_prospects = []
    finally:
        pcon.close()
    buyers = route.load_buyers(CFG)
    # Local-AI block: real env facts + the persisted health/smoke result written by
    # `python tools/collect.py ai-health` / `ai-smoke` (out/ai_state.json).
    import importlib.metadata as _md
    import sys as _sys
    try:
        pip_ver = _md.version("pip")
    except Exception:                       # noqa: BLE001
        pip_ver = "?"
    ai_state = None
    try:
        ai_state = json.loads((BASE / "out" / "ai_state.json").read_text(encoding="utf-8"))
    except Exception:                       # noqa: BLE001 - not run yet
        ai_state = None
    return render_template("admin_integrations.html",
                           sinks=sources_mod.sink_registry(con, cfg("marketplace"), buyers),
                           sources=sources_mod.sources_catalog(),
                           prospect_sources=prospects_mod.prospect_sources(),
                           webreader=webx.sga_status(),
                           sig_counts=sig_counts, recent_fetches=recent_fetches,
                           recent_prospects=recent_prospects,
                           py_version=f"{_sys.version.split()[0]}", py_path=_sys.executable,
                           pip_version=pip_ver, ai_state=ai_state,
                           token=ADMIN_TOKEN, tab="integrations")


# ----------------------------------------------------------------- buyer prospects
@app.get("/admin/prospects")
def admin_prospects():
    """BUSINESS-buyer acquisition prospects (agencies, brokerages, partners) — a separate
    registry in data/prospects.db, deliberately disconnected from consumer leads and from
    the consumer-signal loop. A prospect is never a lead and never an activated buyer:
    activation only ever happens through the operator's buyer onboarding/verification flow.
    There is no contact/send action on this page by design; operator outreach is manual."""
    _admin()
    pcon = prospects_mod.connect(PROSPECTS_DB)
    try:
        decision = request.args.get("decision", "").strip()[:30]
        f = {k: request.args.get(k, "").strip()[:80]
             for k in ("source", "category", "state", "q", "verify_state", "segment")}
        try:
            prio = int(request.args.get("priority", "") or 0)
        except ValueError:
            prio = 0
        stage = request.args.get("stage", "").strip()[:30]
        sort = request.args.get("sort", "priority").strip()[:20]
        due_only = request.args.get("due", "") == "1"
        att_only = request.args.get("attention", "") == "1"
        verify_only = request.args.get("needs_verify", "") == "1"
        rows = prospects_mod.queue(pcon, decision=decision, priority=prio, limit=200,
                                   order=("priority" if sort == "priority" else "updated"), **f)
        if stage:
            rows = [r for r in rows if (r.get("stage") or "NEW") == stage]
        today = now()[:10]
        reasons = prospects_mod.decision_reasons(pcon)
        sorder = {s: i for i, s in enumerate(prospects_mod.STAGES)}
        for r in rows:                                   # review gaps computed server-side,
            r["_gaps"] = prospects_mod.needs_attention(r)  # same source of truth as detail page
            nd = (r.get("next_action_date") or "")[:10]
            r["_due"] = bool(nd and nd <= today)          # follow-up action is due/overdue
            r["_reason"] = reasons.get(r["id"], "")        # recorded why of the last decision
            r["_ready"] = bool((r.get("stage") or "") in ("QUALIFIED", "READY_FOR_ONBOARDING")
                               and not prospects_mod.handoff_gate(r))
            r["_verify_pending"] = (r.get("verify_state") or "UNVERIFIED") in ("UNVERIFIED", "PUBLIC_DETAILS_CHECKED")
        if verify_only:
            rows = [r for r in rows if r["_verify_pending"]]
        if att_only:
            rows = [r for r in rows if r["_gaps"]]
        if due_only:
            rows = [r for r in rows if r["_due"]]
        if sort == "priority":
            # the imported worklist: best-evidenced prospects first, then A→Z
            rows.sort(key=lambda r: (r.get("priority") or 99, (r.get("name") or "").lower()))
        elif sort == "name":
            rows.sort(key=lambda r: (r.get("name") or "").lower())
        elif sort == "updated":
            rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
        elif sort == "stage":
            rows.sort(key=lambda r: (sorder.get(r.get("stage") or "NEW", 99), r.get("updated_at") or ""))
        else:   # 'next': most recently touched first, then stable-ordered: due follow-ups first,
            rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)  # then most-qualified
            rows.sort(key=lambda r: (not r["_due"], sorder.get(r.get("stage") or "NEW", 99)))
        counts = prospects_mod.counts(pcon)
        stages = prospects_mod.stage_counts(pcon)
        facets = prospects_mod.facets(pcon)
        psrc = prospects_mod.prospect_sources()
        # the segmentation an operator works from: every row lands in exactly one bucket
        wl = prospects_mod.worklist_stats_cached(pcon)
    finally:
        pcon.close()
    return render_template("admin_prospects.html", rows=rows, counts=counts, stage_counts=stages,
                           stage_label=prospects_mod.STAGE_LABEL, stage=stage, sort=sort,
                           due_only=due_only, att_only=att_only, verify_only=verify_only,
                           wl=wl, seg_owner=prospects_mod.SEG_OWNER, seg_phone=prospects_mod.SEG_PHONE,
                           seg_email=prospects_mod.SEG_EMAIL, prio=prio,
                           facets=facets, f={"decision": decision, **f}, psrc=psrc,
                           token=ADMIN_TOKEN, tab="prospects",
                           msg=request.args.get("msg", ""))


@app.post("/admin/prospects/<int:pid>/decision")
def admin_prospect_decision(pid):
    """Record a review decision on a BUSINESS prospect. A note is required to move off NEW
    (the prospect log keeps the why). Nothing is contacted and nothing is activated."""
    _admin()
    decision = request.form.get("decision", "").strip()
    note = (request.form.get("note") or "").strip()
    if decision not in prospects_mod.DECISIONS:
        return redirect(url_for("admin_prospects", token=ADMIN_TOKEN,
                                msg=f"unknown decision '{decision}'"), code=302)
    if decision != "NEW" and not note:
        return redirect(url_for("admin_prospects", token=ADMIN_TOKEN,
                                msg="a decision note is required — why this classification"), code=302)
    if decision == "CONTACTS_AGREEMENT_DRAFTED" and "agreement" not in note.lower():
        return redirect(url_for("admin_prospects", token=ADMIN_TOKEN,
                                msg="agreement-drafted requires the agreement reference in the note"), code=302)
    pcon = prospects_mod.connect(PROSPECTS_DB)
    try:
        ok = prospects_mod.decide(pcon, pid, decision, note)
    finally:
        pcon.close()
    return redirect(url_for("admin_prospects", token=ADMIN_TOKEN,
                            msg=f"prospect #{pid}: {decision} recorded" if ok
                            else f"prospect #{pid} not found"), code=302)


@app.post("/admin/prospects/<int:pid>/verify")
def admin_prospect_verify(pid):
    """An explicit operator VERIFICATION record (named source of the public check). Same
    discipline as buyer verification: manual, evidence-bearing, never auto-derived, and
    never an activation. The note names WHERE the public details were checked."""
    _admin()
    verify_state = request.form.get("verify_state", "").strip()
    note = (request.form.get("note") or "").strip()
    if verify_state not in prospects_mod.VERIFY_STATES:
        return redirect(url_for("admin_prospects", token=ADMIN_TOKEN,
                                msg=f"unknown verification state '{verify_state}'"), code=302)
    if verify_state != "UNVERIFIED" and not note:
        return redirect(url_for("admin_prospects", token=ADMIN_TOKEN,
                                msg="a verification note is required — where you checked"), code=302)
    pcon = prospects_mod.connect(PROSPECTS_DB)
    try:
        ok = prospects_mod.verify(pcon, pid, verify_state, note)
    finally:
        pcon.close()
    return redirect(url_for("admin_prospects", token=ADMIN_TOKEN,
                            msg=f"prospect #{pid}: {verify_state} recorded (never an activation)"
                            if ok else f"prospect #{pid} not found"), code=302)


@app.get("/admin/prospects.csv")
def admin_prospects_csv():
    """Prospect CSV: PUBLIC BUSINESS FIELDS ONLY (name, category, public contact as
    provided by the source, location, source, verification state). Aggregate-minded
    operator file for the outreach pipeline; no consumer fields exist in this DB."""
    _admin()
    seg = request.args.get("segment", "").strip()[:40]
    aud = request.args.get("audience", "").strip()[:40]
    pcon = prospects_mod.connect(PROSPECTS_DB)
    try:
        if seg or aud:          # a WORKING segment: same classification the CLI exports, no cap
            wl = prospects_mod.worklist(pcon)
            rows = [r for r in wl["rows"]
                    if (not seg or r["segment"] == seg)
                    and (not aud or r["audience"] == aud)]
            rows.sort(key=lambda r: (r["priority"], r["id"]))
            cols = [(k, h) for k, h in prospects_mod.WORKLIST_COLUMNS]
        else:
            rows = prospects_mod.queue(pcon, limit=1000)
            cols = [(k, k) for k in ("id", "name", "category", "public_phone", "website",
                                     "address", "city", "state", "zip5", "source",
                                     "evidence_url", "decision", "verify_state",
                                     "created_at", "updated_at")]
    finally:
        pcon.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([h for _, h in cols])
    for r in rows:
        w.writerow([(r.get(k) if r.get(k) is not None else "") for k, _ in cols])
    name = f"prospects_{seg or 'registry'}{('_' + aud) if aud else ''}.csv"
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


def _draft_checklist(buyer: dict | None) -> list[dict]:
    """The BUYER-draft half of the prospect's onboarding-readiness checklist — derived ONLY
    from existing application requirements (_onboard_errors + the four independent
    `_verification` records + explicit activation). No invented legal rules. Each item
    states where the answer lives and whether it is operator-provided info or an
    independent verification record."""
    if not buyer:
        return []
    items = [{"label": "Buyer draft created via gated handoff (INACTIVE — cannot receive leads)",
              "status": "complete", "kind": "provided",
              "source": "handoff (linked to the prospect record)",
              "detail": buyer.get("_onboarding_origin") or f"id {buyer.get('id')}"}]
    # the EXISTING activation requirements, verbatim as the routing/onboarding engine states them
    for e in _onboard_errors(buyer):
        items.append({"label": e, "status": "missing", "kind": "provided",
                      "source": "existing onboarding requirement (the same check /admin/buyers runs before activation)",
                      "detail": ""})
    ver = buyer.get("_verification") or {}
    for k, label in _VERIFIABLE.items():
        if k in ver:
            items.append({"label": f"Independently verified: {label}", "status": "complete",
                          "kind": "verified", "source": "independent check record (/admin/buyer/<id>/verify)",
                          "detail": f"{ver[k].get('ref', '')} recorded {ver[k].get('ts', '')[:16]}"})
    items.append({"label": "EXPLICIT operator activation through the real onboarding gate",
                  "status": "complete" if buyer.get("active") else "missing",
                  "kind": "provided", "source": "Admin → Buyers — activation button (gate-checked)",
                  "detail": "buyer is ACTIVE" if buyer.get("active") else "awaiting explicit operator approval"})
    return items


@app.get("/admin/prospects/<int:pid>")
def admin_prospect(pid):
    """One business prospect, end to end: the full record, the operator's decision/verification
    history, the commercial-qualification fields (every unknown stays explicitly unknown), the
    next action, and the handoff-to-onboarding gate with exactly what is still missing. Read
    only about money: stated budgets are CLAIMS to verify with the person, never commitments."""
    _admin()
    pcon = prospects_mod.connect(PROSPECTS_DB)
    try:
        row = prospects_mod.detail(pcon, pid)
        if not row:
            abort(404)
        history = prospects_mod.log(pcon, pid, limit=100)
        gaps = prospects_mod.needs_attention(row)
        stage_counts = prospects_mod.stage_counts(pcon)
    finally:
        pcon.close()
    buyers = json.loads((CFG / "buyers.json").read_text(encoding="utf-8"))
    buyer = next((b for b in buyers if b.get("id") == (row.get("buyer_id") or "\x00")), None)
    missing = prospects_mod.handoff_gate(row)
    dup = prospects_mod.duplicate_buyer(row, [b for b in buyers if not b.get("is_demo")])
    # ACTIVATED mirror: a converted prospect with an ACTIVE buyer is genuinely activated.
    stage = row.get("stage") or "NEW"
    if buyer and buyer.get("active") and stage in ("ONBOARDING_STARTED", "ONBOARDED"):
        stage = "ACTIVATED"
    # readiness checklist = prospect-side gate items + (when a draft exists) the DRAFT-side
    # items, all derived from the EXISTING requirements; operator-provided vs independently
    # verified information is labeled per item.
    checklist = prospects_mod.gate_item(row)["items"] + _draft_checklist(buyer)
    return render_template("admin_prospect.html", r=row, history=history, gaps=gaps,
                           stage=stage, stage_label=prospects_mod.STAGE_LABEL,
                           manual_stages=prospects_mod.MANUAL_STAGES,
                           biz_fields=prospects_mod.BIZ_FIELDS, missing=missing, dup=dup,
                           checklist=checklist, buyer=buyer, stage_counts=stage_counts,
                           token=ADMIN_TOKEN, tab="prospects",
                           msg=request.args.get("msg", ""), err=request.args.get("err", ""))


@app.post("/admin/prospects/<int:pid>/update")
def admin_prospect_update(pid):
    """Record business/commercial facts an operator actually obtained. ONLY supplied fields
    move (blank = unknown, never a default); every change is logged before/after so the
    history shows what was edited. No verification, decision or activation happens here."""
    _admin()
    fields = {k: (request.form.get(k) or "") for k in prospects_mod.BIZ_FIELDS if k in request.form}
    if not fields:
        return redirect(url_for("admin_prospect", pid=pid, token=ADMIN_TOKEN,
                                err="nothing to record"), code=302)
    pcon = prospects_mod.connect(PROSPECTS_DB)
    try:
        try:
            changed = prospects_mod.update_fields(pcon, pid, fields)
            err = None
        except ValueError as exc:
            changed, err = [], str(exc)
    finally:
        pcon.close()
    if err:
        return redirect(url_for("admin_prospect", pid=pid, token=ADMIN_TOKEN, err=err), code=302)
    return redirect(url_for("admin_prospect", pid=pid, token=ADMIN_TOKEN,
                            msg="recorded: " + ", ".join(changed) if changed else "no change"), code=302)


@app.post("/admin/prospects/<int:pid>/stage")
def admin_prospect_stage(pid):
    """Move a prospect along the lifecycle ladder. QUALIFIED / DISQUALIFIED / READY_FOR_ONBOARDING
    demand the reason in the note; ONBOARDED/ACTIVATED are never hand-set (the handoff and
    activation flows record them)."""
    _admin()
    stage = (request.form.get("stage") or "").strip()
    note = (request.form.get("note") or "").strip()
    pcon = prospects_mod.connect(PROSPECTS_DB)
    try:
        err = prospects_mod.set_stage_checked(pcon, pid, stage, note)
    finally:
        pcon.close()
    if err or stage not in prospects_mod.STAGES:
        return redirect(url_for("admin_prospect", pid=pid, token=ADMIN_TOKEN,
                                err=err or f"unknown stage {stage!r}"), code=302)
    return redirect(url_for("admin_prospect", pid=pid, token=ADMIN_TOKEN,
                            msg=f"stage set to {stage}"), code=302)


@app.post("/admin/prospects/<int:pid>/handoff")
def admin_prospect_handoff(pid):
    """THE CONNECTION: move a qualified + operator-verified prospect into the EXISTING buyer
    onboarding flow as an INACTIVE, portal-less draft (/admin?f=<bid> prefills the form).
    Refuses unless the prospects.handoff_gate passes (QUALIFIED decision + OPERATOR_VERIFIED
    + stage + territory) and no buyer with the same name/website exists. The draft is never
    active: activation still runs the full _onboard_errors() + explicit operator approval.
    Requires the typed confirmation action 'convert' in the form."""
    _admin()
    if (request.form.get("confirm") or "").strip().lower() != "convert":
        return redirect(url_for("admin_prospect", pid=pid, token=ADMIN_TOKEN,
                                err="handoff needs the explicit confirmation action (type convert)"), code=302)
    pcon = prospects_mod.connect(PROSPECTS_DB)
    try:
        row = prospects_mod.detail(pcon, pid)
    finally:
        pcon.close()
    if not row:
        return redirect(url_for("admin_prospects", token=ADMIN_TOKEN,
                                msg=f"prospect #{pid} not found"), code=302)
    buyers = json.loads((CFG / "buyers.json").read_text(encoding="utf-8"))
    # RESUME path: a linked draft already exists — never create a second account, just
    # return the operator to ITS onboarding form (field-level values are preserved there).
    if (row.get("buyer_id") or "").strip() and any(x.get("id") == row["buyer_id"] for x in buyers):
        return redirect(url_for("admin", token=ADMIN_TOKEN, f=row["buyer_id"],
                                saved=f"Resuming the EXISTING onboarding draft '{row['buyer_id']}' for "
                                      f"prospect #{pid} — no new buyer account was created."), code=302)
    try:
        draft = prospects_mod.handoff_buyer_draft(row, buyers)
    except ValueError as exc:
        return redirect(url_for("admin_prospect", pid=pid, token=ADMIN_TOKEN, err=str(exc)), code=302)
    bid = draft["id"]
    draft["_application"] = {
        "ts": now(), "contact_name": (row.get("contact_person") or "").strip()[:120],
        "contact_email": (row.get("biz_email") or "").strip().lower()[:160],
        "contact_phone": (row.get("public_phone") or "").strip()[:40],
        "expected_volume": (row.get("stated_weekly_volume") or "").strip()[:60],
        "note": "",
        "licensed_states": draft["licensed_states"], "products": draft["products"],
        "license_number": "", "npn": "", "delivery_preference": draft["delivery"]["method"],
        "delivery_webhook": "", "source": f"prospect:#{pid} ({row.get('source') or 'manual'})"}
    draft["_onboarding_origin"] = f"prospect #{pid} — confirmed {now()[:10]}"
    buyers = [x for x in buyers if x.get("id") != bid] + [draft]
    _save_buyers(buyers)
    pcon = prospects_mod.connect(PROSPECTS_DB)
    try:
        prospects_mod.set_stage_checked(pcon, pid, "ONBOARDING_STARTED",
                                        note=f"buyer draft {bid} created via explicit handoff (inactive)",
                                        allow_terminal=True)
        prospects_mod.set_buyer_link(pcon, pid, bid)
        err = None
    except ValueError as exc:
        err = str(exc)
    finally:
        pcon.close()
    bstat, _ = _buyer_state_rows()
    pipe_msgs, pipe_errs = bpline.record_action(con, bid, buyers, stage="APPLIED", method="OTHER",
                                                note=f"created from prospect #{pid} by operator handoff")
    save_note = "buyer pipeline trail: " + "; ".join(pipe_msgs or pipe_errs)
    if err:
        return redirect(url_for("admin", token=ADMIN_TOKEN, f=bid,
                                err=f"draft {bid} created, but the prospect trail failed: {err}"), code=302)
    return redirect(url_for("admin", token=ADMIN_TOKEN, f=bid,
                            saved=f"Prospect #{pid} handed off as buyer draft '{bid}' (INACTIVE). {save_note}. "
                                  "Complete onboarding below — activation still needs the full "
                                  "verification and an explicit operator approval."), code=302)


@app.get("/admin/channels")
def admin_channels():
    _admin()
    links = [dict(r) for r in con.execute("SELECT * FROM links ORDER BY created_at DESC")]
    made = con.execute("SELECT * FROM links WHERE code=?", (request.args.get("made") or "\x00",)).fetchone()
    return render_template("admin_channels.html", ch=cfg("channels"), links=links, token=ADMIN_TOKEN, tab="channels",
                           base=SITE["base_url"], made=dict(made) if made else None,
                           err=request.args.get("err"), angles=sorted(HEALTH_FLOW["angles"]))


@app.post("/admin/channels/new")
def admin_channels_new():
    """Operator-created tracked link: one code per place a real person will see it — a subreddit answer,
    a library flyer QR, an org newsletter, a Spanish-language Facebook group.

    Why this exists: traffic that arrives without a code is indistinguishable from 'direct' in Admin >
    Sources, so a channel that actually works looks like no channel at all. The link stamps
    utm_source/medium/campaign/content + the link code on the visitor, the events and the lead, which is
    exactly what the arrival report groups by. It never touches consent, routing or delivery.
    """
    _admin()
    def back(**kw):
        return redirect(url_for("admin_channels", token=ADMIN_TOKEN, **kw), code=302)

    label = (request.form.get("label") or "").strip()[:140]
    plain = lambda v, n=40: re.sub(r"[^a-z0-9_-]", "", (v or "").strip().lower())[:n]
    src, med = plain(request.form.get("utm_source")), plain(request.form.get("utm_medium"))
    campaign = (request.form.get("utm_campaign") or "").strip()[:60] or "oep-2027-health"
    content = plain(request.form.get("utm_content")) or None
    partner = (request.form.get("partner_id") or "").strip() or None
    landing = (request.form.get("landing") or "").strip()[:200]
    angle = (request.form.get("angle") or "").strip() or None
    lang = request.form.get("lang") if request.form.get("lang") in ("en", "es") else "en"

    if not label or not src or not med:
        return back(err="A label, a source and a medium are required — they are what the report groups by.")
    if landing and (not landing.startswith("/") or landing.startswith("//")):
        return back(err="The landing page must be a path on this site, starting with a single /.")
    if angle and angle not in HEALTH_FLOW["angles"]:
        return back(err="Unknown check angle. Pick one from the list.")
    if partner and not con.execute("SELECT 1 FROM partners WHERE partner_id=?", (partner,)).fetchone():
        return back(err=f"No partner '{partner}' is registered. Register the partner first.")

    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:20] or "link"
    code = f"{src[:10]}-{slug}-{secrets.token_hex(2)}"
    for _ in range(5):                                   # collision is unlikely, silent overwrite is not acceptable
        if not con.execute("SELECT 1 FROM links WHERE code=?", (code,)).fetchone():
            break
        code = f"{src[:10]}-{slug}-{secrets.token_hex(2)}"
    con.execute("""INSERT INTO links (code, campaign, channel, utm_source, utm_medium, utm_campaign, utm_content,
                   partner_id, note, created_at, clicks, angle, lang, landing_page) VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?,?)""",
                (code, campaign, request.form.get("channel") or src, src, med, campaign, content,
                 partner, label, now(), angle, lang, landing or None))
    con.commit()
    return back(made=code)


@app.get("/admin/leadgen")
def admin_leadgen():
    """The eight-stage consumer acquisition view: what happened, where people stopped, and which
    dependency is blocking the next stage today. Read-only; nothing here routes or sells."""
    _admin()
    t = 1 if request.args.get("test") == "1" else 0
    rep = leadgen.report(con, CFG, t)
    st = rep["stages"]
    return render_template("admin_leadgen.html", rep=rep, st=st,
                           stages=[(k, label, st.get(k), desc) for k, label, desc in leadgen.STAGES],
                           steps=rep["steps"],
                           visitors=st.get("visitors") or 0, blockers=rep["blockers"],
                           blocking=rep["blocking"], by_channel=rep["by_channel"],
                           gate=rep["gate"], network=rep["network"], test=t,
                           token=ADMIN_TOKEN, tab="leadgen")


@app.get("/admin/leadgen.csv")
def admin_leadgen_csv():
    _admin()
    rep = leadgen.report(con, CFG, 1 if request.args.get("test") == "1" else 0)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["stage", "label", "count", "pct_of_visitors", "definition"])
    w.writeheader()
    w.writerows(leadgen.csv_rows(rep))
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=acquisition-stages.csv"})


@app.get("/admin/export.csv")
def admin_export():
    _admin()
    rows = [dict(r) for r in con.execute("SELECT * FROM leads ORDER BY created_at DESC")]
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=leads.csv"})


@app.get("/admin/consents.csv")
def admin_consents_csv():
    """Every consent record as one CSV — the compliance artifact behind the 'keep it 10 years' promise:
    consent version, exact on-screen wording, timestamp, IP, page, scope, form snapshot hash, and the
    lead it belongs to. Includes TEST rows only with an is_test flag column so they can be excluded.
    Read-only reporting; nothing here changes a sale or a routing decision."""
    _admin()
    rows = [dict(r) for r in con.execute(
        """SELECT c.lead_id, c.ts, c.consent_version, c.seller_named, c.checkbox_checked, c.scope,
                  c.consent_text, c.ip, c.page_url, c.form_snapshot_sha256, l.is_test, l.campaign, l.product, l.lang
           FROM consents c LEFT JOIN leads l ON l.lead_id=c.lead_id ORDER BY c.ts, c.lead_id""")]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["lead_id", "ts", "consent_version", "seller_named", "checkbox_checked", "scope",
                                        "consent_text", "ip", "page_url", "form_snapshot_sha256", "is_test", "campaign", "product", "lang"])
    w.writeheader()
    w.writerows(rows)
    name = f"consents-{now()[:10]}.csv"
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={name}"})



# ----------------------------------------------------------------- startup data
partners_mod.import_json(con, CFG / "partners.json")
# Public prospects: migrate the standalone prospect_people.db into the lead database ONCE so the
# operator reviews discovery rows in the same store as everything else (idempotent, never upgrades).
prospector.ensure(con)
try:
    _legacy = people_mod.connect(PEOPLE_DB)
    try:
        _migrated = prospector.migrate_legacy(con, _legacy)
    finally:
        _legacy.close()
    if _migrated:
        print(f"prospector: migrated {_migrated} legacy prospect row(s) into leads.db")
except Exception as _e:                      # noqa: BLE001 - a missing legacy DB is normal
    print("prospector: legacy migration skipped:", type(_e).__name__)
if not con.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] and (CFG / "experiments_seed.json").exists():
    for _e in json.loads((CFG / "experiments_seed.json").read_text(encoding="utf-8")):
        lab.create(con, _e)
for _wave in ("experiments_wave1.json", "experiments_wave2.json", "experiments_wave3.json"):
    for _e in (json.loads((CFG / _wave).read_text(encoding="utf-8")) if (CFG / _wave).exists() else []):
        if not con.execute("SELECT 1 FROM experiments WHERE experiment_id=?", (_e["experiment_id"],)).fetchone():
            lab.create(con, _e)
# Engine 1: every organic/partner placement gets its own tracked link (config/distribution.json)
for _pl in (json.loads((CFG / "distribution.json").read_text(encoding="utf-8"))["placements"] if (CFG / "distribution.json").exists() else []):
    if _pl.get("kind") != "setup":
        lab.ensure_placement_link(con, _pl)
con.commit()
# operator decisions (kill / redirect / merge) recorded in config and applied idempotently on every start
for _d in (json.loads((CFG / "experiment_decisions.json").read_text(encoding="utf-8")) if (CFG / "experiment_decisions.json").exists() else []):
    lab.apply_decision(con, _d)
CONTENT = json.loads((CFG / "content.json").read_text(encoding="utf-8")) if (CFG / "content.json").exists() else {"pages": []}
CONTENT_BY_SLUG = {p_["slug"]: p_ for p_ in CONTENT["pages"]}


# ----------------------------------------------------------------- content engine (SEO / short-form source)
@app.get("/learn")
@app.get("/aprende")
def learn_index():
    lang = "es" if request.path.startswith("/aprende") else "en"
    return render_template("learn_index.html", pages=[p_ for p_ in CONTENT["pages"] if p_["lang"] == lang], lang=lang)


@app.get("/learn/<slug>")
@app.get("/aprende/<slug>")
def learn_page(slug):
    p_ = CONTENT_BY_SLUG.get(slug) or abort(404)
    # A page exists in ONE language only, so /learn/<es-slug> and /aprende/<en-slug> used to serve
    # the wrong-language article under the other route (English copy on a Spanish URL). Send the
    # visitor to the route that matches the page's own language instead of duplicating it.
    want = "es" if request.path.startswith("/aprende/") else "en"
    if p_["lang"] != want:
        dest = f"{'/aprende' if p_['lang'] == 'es' else '/learn'}/{slug}"   # the page's own language
        qs = request.query_string.decode("utf-8", "replace")
        return redirect(dest + (("?" + qs) if qs else ""), code=301)
    others = [o for o in CONTENT["pages"] if o["lang"] == p_["lang"] and o["slug"] != slug][:4]
    return render_template("learn.html", p=p_, lang=p_["lang"], others=others, flow=HEALTH_FLOW)


# ----------------------------------------------------------------- job-loss engine (WARN-triggered employer pages)
def _layoffs(include_drafts=False):
    from datetime import date as _d, timedelta as _td
    data = json.loads((CFG / "layoffs.json").read_text(encoding="utf-8")) if (CFG / "layoffs.json").exists() else {"events": []}
    today = _today()
    out = []
    for e in data["events"]:
        if e.get("status") != "ready" and not include_drafts:
            continue
        eff = _d.fromisoformat(e["effective_date"])
        e = dict(e, effective_pretty=_pretty_day_en(eff), window_end_pretty=_pretty_day_en(eff + _td(days=60)),
                 effective_pretty_es=_pretty_date(eff.isoformat(), "es"),
                 window_end_pretty_es=_pretty_date((eff + _td(days=60)).isoformat(), "es"),
                 in_window=today <= eff + _td(days=60))
        out.append(e)
    return sorted(out, key=lambda x: x["effective_date"], reverse=True)


def _today():
    from datetime import date as _d
    t = request.args.get("today") if (request and _is_test({})) else None
    try:
        return _d.fromisoformat(t) if t else _d.today()
    except ValueError:
        return _d.today()


@app.get("/layoffs")
def layoffs_hub():
    return render_template("layoffs_hub.html", events=_layoffs(), hub=CONTENT_BY_SLUG.get("laid-off-florida-health-insurance", {"a": []}), lang="en")


@app.get("/layoffs/<slug>")
@app.get("/despidos/<slug>")
def layoff_page(slug):
    es = request.path.startswith("/despidos/")
    e = next((x for x in _layoffs(include_drafts=request.args.get("token") == ADMIN_TOKEN) if x["slug"] == slug), None) or abort(404)
    return render_template("layoff_es.html" if es else "layoff.html", e=e, reviewed="2026-10-05", lang="es" if es else "en")


def _oep_state(today, es):
    from datetime import date as _d
    f = HEALTH_FLOW["facts"]
    start, d15, end = _d.fromisoformat(f["oep_start"]), _d.fromisoformat(f["dec15"]), _d.fromisoformat(f["oep_end"])
    T = (lambda en, sp: sp if es else en)
    if today < start:
        return {"title": T("When does 2027 Open Enrollment start in Florida?", "¿Cuándo empieza la Inscripción Abierta 2027 en Florida?"),
                "answer": T("It opens November 1, 2026.", "Abre el 1 de noviembre de 2026."), "days": (start - today).days,
                "days_label": T("days until it opens", "días para que abra"),
                "detail": T("Start now: check whether your income is in the range for help, and if your 2027 plan is ending or going up.",
                            "Empieza ya: revisa si tu ingreso está en el rango de ayuda y si tu plan 2027 se termina o sube de precio.")}
    if today <= d15:
        return {"title": T("Is 2027 Open Enrollment open in Florida?", "¿Está abierta la Inscripción Abierta 2027 en Florida?"),
                "answer": T("Yes. Enroll by December 15 for coverage on January 1.", "Sí. Inscríbete antes del 15 de diciembre para tener cobertura el 1 de enero."),
                "days": (d15 - today).days, "days_label": T("days left for January 1 coverage", "días para tener cobertura el 1 de enero"),
                "detail": T("After December 15 you can still enroll until January 15, but coverage starts February 1.",
                            "Después del 15 de diciembre todavía puedes inscribirte hasta el 15 de enero, pero la cobertura empieza el 1 de febrero.")}
    if today <= end:
        return {"title": T("Did I miss Open Enrollment? (Florida, 2027)", "¿Se me pasó la Inscripción Abierta? (Florida, 2027)"),
                "answer": T("No. It's still open until January 15. Coverage starts February 1.", "No. Sigue abierta hasta el 15 de enero. La cobertura empieza el 1 de febrero."),
                "days": (end - today).days, "days_label": T("days left to enroll", "días para inscribirte"),
                "detail": T("December 15 was only the deadline for January 1 coverage. If you enroll by January 15, your plan starts February 1.",
                            "El 15 de diciembre era solo la fecha para tener cobertura el 1 de enero. Si te inscribes antes del 15 de enero, tu plan empieza el 1 de febrero.")}
    return {"title": T("Is Open Enrollment over?", "¿Ya terminó la Inscripción Abierta?"),
            "answer": T("Yes. 2027 Open Enrollment ended January 15, 2027.", "Sí. La Inscripción Abierta 2027 terminó el 15 de enero de 2027."), "days": None, "days_label": "",
            "detail": T("You can still enroll if you have a qualifying life event, like losing other coverage, moving, getting married or having a baby, usually within 60 days.",
                        "Todavía puedes inscribirte si tienes un evento de vida que califique, como perder otra cobertura, mudarte, casarte o tener un bebé, normalmente dentro de 60 días.")}


@app.get("/open-enrollment-2027")
@app.get("/inscripcion-abierta-2027")
def oep_status():
    es = request.path.startswith("/inscripcion")
    t = _today()
    meses = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
    pretty = f"{t.day} de {meses[t.month - 1]} de {t.year}" if es else _pretty_day_en(t)
    return render_template("oep_status.html", s=_oep_state(t, es), lang="es" if es else "en", today_iso=t.isoformat(), today_pretty=pretty)


@app.get("/despidos")
def despidos_hub():
    """Spanish layoffs hub - the same WARN-driven employer list as /layoffs, in Spanish. It used to
    302 into one article, which left Spanish readers without the employer list their English
    neighbours get."""
    return render_template("layoffs_hub_es.html", events=_layoffs(),
                           hub=CONTENT_BY_SLUG.get("me-despidieron-seguro-medico-florida", {"a": []}), lang="es")


# ----------------------------------------------------------------- QR for any internal path (live base URL)
@app.get("/qr.png")
def qr():
    path = request.args.get("u", "/check")
    if not path.startswith("/"):
        abort(400)
    return Response(partners_mod.qr_png(SITE["base_url"] + path), mimetype="image/png")


@app.get("/healthz")
def healthz():
    con.execute("SELECT 1").fetchone()
    # `environment` + `indexing` + `credit_enforcement` make it impossible to mistake a staging origin
    # for the live one when checking a deployment: the PUBLIC origin, the crawl policy and the real
    # money lane are all named here instead of inferred.
    return jsonify(ok=True, base_url=SITE["base_url"], public_origin=_site_origin() or None,
                   base_url_source=SITE.get("_base_url_source"),
                   environment="production" if PRODUCTION else "staging",
                   # storage + accepts_submissions say whether this host can KEEP what it collects:
                   # "ephemeral" means a recorded lead/consent/payment row dies on the next restart.
                   storage=STORAGE, accepts_submissions=not READONLY_PREVIEW,
                   read_only_preview=READONLY_PREVIEW,
                   indexing=ALLOW_INDEXING, credit_enforcement=_credit_enforcing(),
                   live_delivery=os.environ.get("LEADENGINE_LIVE_DELIVERY") == "1")


@app.errorhandler(404)
def e404(e):
    return render_template("simple.html", page="404"), 404


@app.errorhandler(500)
def e500(e):
    app.logger.exception("server error")
    return render_template("simple.html", page="500"), 500


# ----------------------------------------------------------------- agent desk (buyer side + feedback engine)
AGENT_STATUSES = ["NEW", "VIEWED", "CONTACTED", "APPOINTMENT", "SOLD", "NOT_QUALIFIED", "RETURNED", "BAD_INFO", "DNC", "OTHER"]


def _buyer(token):
    """A buyer desk, by portal token. Two gates beyond load_buyers' `active` filter, so a demo identity
    can never be presented to a real buyer on a live deployment:

      * `active` is required, so a draft row (its token is literally CHANGE-ME-…) has no desk at all;
      * on LEADENGINE_ENV=production a DEMO/test fixture's token is refused, because the demo desks are
        local fixtures whose tokens are documented in docs/USER_WALKTHROUGH.md and must not be a working
        login once real buyers exist. Tests run with LEADENGINE_ENV unset, so the fixtures stay usable.
    """
    b = next((x for x in route.load_buyers(CFG) if x.get("portal_token") and x.get("portal_token") == token), None)
    if not b or (PRODUCTION and b.get("is_demo")):
        abort(404)
    return b


def _return_hint(l: dict, buyer: dict = None) -> str:
    """What the RETURN button will do for THIS lead, shown beside the action (policy text, never a
    promise). Derived from the SAME _returns_allowed() rules that _set_status applies at submit, so
    guidance and decision can never drift apart — only the moment of evaluation differs (the recorded
    outcome at click time wins over this pre-submit view)."""
    ok, why = _returns_allowed(l, buyer)
    if ok:
        h = _effective_window_hours(buyer)
        extra = "" if get_marketplace().get("returns", {}).get("allow_after_contact", False) else ", before any contact is recorded"
        return (f"credit back into balance — a return is credited within {h:g}h of delivery{extra}; "
                "the recorded rules apply at the moment you click")
    return "no credit: " + why


def _effective_window_hours(buyer: dict = None) -> float:
    """Per-deal override from the buyer row (`return_window_hours`) beats the marketplace default;
    an operator can make a deal tighter or more generous, hours only — never the other rules."""
    pol = get_marketplace().get("returns", {})
    try:
        ov = float(buyer.get("return_window_hours")) if buyer and buyer.get("return_window_hours") is not None else None
    except (TypeError, ValueError):
        ov = None
    base = float(pol.get("window_hours", 24))
    return ov if (ov is not None and ov > 0) else base


def _age_hours(iso):
    """Hours since an ISO timestamp, or None when missing/unparseable (never invents an age)."""
    import datetime as _dt
    try:
        d = _dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except Exception:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return round((_dt.datetime.now(_dt.timezone.utc) - d).total_seconds() / 3600, 2)


def _returns_allowed(l: dict, buyer: dict = None) -> tuple[bool, str]:
    """The ONLY automatic return-credit rules, straight from config/marketplace.json 'returns'.
    A buyer row may carry its own `return_window_hours` (operator-set per deal in the admin buyer
    editor); everything else comes from the one global policy. Anything outside the rules needs an
    operator ADJUSTMENT — no refund is invented here."""
    pol = get_marketplace().get("returns", {})
    if not pol.get("enabled", True):
        return False, "returns are disabled by the operator"
    if l["routing_status"] != "DELIVERED" or l["agent_status"] == "ACCEPTED":
        return False, "only a delivered, not-yet-accepted lead can be returned"
    if float(l["price"] or 0) <= 0:
        return False, "no recorded price to credit"
    if l["agent_status"] in ("CONTACTED", "APPOINTMENT", "SOLD") and not pol.get("allow_after_contact", False):
        return False, "lead was already contacted; returns close at first contact"
    age_h = _age_hours(l["delivered_at"])
    if age_h is None or age_h > _effective_window_hours(buyer):
        return False, "outside the configured return window"
    return True, ""


def _pol():
    """The one return/credit policy: config/marketplace.json 'returns' — the buyer UI, the note
    recording and the admin pages all read the same file, so the wording can never drift."""
    return get_marketplace().get("returns", {})


def _set_status(b, lid, st, note=""):
    """One buyer-side disposition. RETURNED additionally credits the ledger back, but only inside the
    configured rules (_returns_allowed); the refusal reason is always recorded so the buyer sees WHY."""
    l = con.execute("SELECT * FROM leads WHERE lead_id=? AND buyer_id=?", (lid, b["id"])).fetchone()
    if st not in AGENT_STATUSES or not l:
        return False
    prev = (l["agent_status"] or "")
    if st == "RETURNED":
        pol = _pol()
        ldict = dict(l)
        ok, why = _returns_allowed(ldict, b)
        if not ok:
            con.execute("UPDATE leads SET agent_status=? WHERE lead_id=?", (st, lid))
            con.execute("INSERT INTO lead_outcomes (lead_id,buyer_id,ts,outcome,note) VALUES (?,?,?,?,?)",
                        (lid, b["id"], now(), st,
                         (f"no credit: {why} - queued for operator review")[:500]))   # stable prefix drives the /admin/credits queue
            con.commit()
            return True
        if (request.form.get("reason") or "").strip() in pol.get("reasons", []):
            why = request.form["reason"].strip()
        elif not why:
            # a confident return whose reason box was never filled is still recorded with the
            # policy's configured default — never a blank evidence line
            why = pol.get("default_reason", "other")
        con.execute("UPDATE leads SET agent_status=? WHERE lead_id=?", (st, lid))
        con.execute("INSERT INTO lead_outcomes (lead_id,buyer_id,ts,outcome,note) VALUES (?,?,?,?,?)",
                    (lid, b["id"], now(), st, ("returned: " + why + (" | " + note if note else ""))[:500]))
        if prev != "RETURNED" and l["routing_status"] == "DELIVERED":
            billing.credit_return(con, b["id"], ldict, buyer_demo=bool(b.get("is_demo")))   # ledger credit (SQL-guarded: at most once per lead+buyer)
    else:
        con.execute("UPDATE leads SET agent_status=? WHERE lead_id=?", (st, lid))
        con.execute("INSERT INTO lead_outcomes (lead_id,buyer_id,ts,outcome,note) VALUES (?,?,?,?,?)", (lid, b["id"], now(), st, note[:500]))
    if st == "DNC":   # do-not-contact propagates to the whole system immediately
        l = con.execute("SELECT phone,email FROM leads WHERE lead_id=?", (lid,)).fetchone()
        for kind, v in (("phone", l["phone"]), ("email", (l["email"] or "").lower())):
            if v:
                con.execute("INSERT OR REPLACE INTO optouts VALUES (?,?,?,?)", (v, kind, now(), f"agent:{b['id']}"))
        con.execute("UPDATE leads SET opted_out=1 WHERE phone=? OR lower(email)=?", (l["phone"] or "~", (l["email"] or "~").lower()))
    con.commit()
    return True


@app.route("/agent/<token>", methods=["GET", "POST"])
def agent_desk(token):
    b = _buyer(token)
    if request.method == "POST":
        ok = _set_status(b, request.form.get("lead_id"), request.form.get("status"), request.form.get("note") or "")
        tail = {"msg": "saved"} if ok else {"err": "notapplied"}   # the desk shows WHY nothing changed
        if request.form.get("back"):
            return redirect(url_for("agent_lead", token=token, lead_id=request.form.get("lead_id"), **tail))
        return redirect(url_for("agent_desk", token=token, f=request.args.get("f", ""), **tail))
    # self-healing billing: any delivered-but-uncharged lead (e.g. a crash between delivery and charge)
    # is charged exactly once here; the SQL-guarded dedupe makes repeated loads no-ops.
    for lr in con.execute("SELECT * FROM leads WHERE buyer_id=? AND routing_status='DELIVERED' AND price>0",
                          (b["id"],)):
        billing.reconcile_lead(con, {**dict(lr), "_buyer_demo": bool(b.get("is_demo"))})
    # self-healing delivery: a sale committed as ROUTED but stranded before delivery (crash / webhook error)
    # is retried here; deliver + charge are SQL-guarded so a retry can never double-sell or double-charge.
    buyers_live = route.load_buyers(CFG)
    for lr in con.execute("SELECT * FROM leads WHERE buyer_id=? AND routing_status='ROUTED'", (b["id"],)):
        _sell_lead(dict(lr), buyers_live)
    # self-healing webhook delivery: a FAILED webhook attempt for a delivered lead is re-posted (same payload
    # file, budgeted retries) so the buyer's CRM eventually receives it without a new sale ever being created.
    if os.environ.get("LEADENGINE_LIVE_DELIVERY") == "1":
        for lr in con.execute("SELECT * FROM leads WHERE buyer_id=? AND routing_status='DELIVERED'", (b["id"],)):
            route.retry_webhook(con, buyers_live, dict(lr))
    ist = 1 if b.get("is_demo") else 0
    bal = billing.balance(con, b["id"], ist)
    prices = [(t, b["price_by_tier"].get(t, 0)) for t in (b.get("tiers") or [])]
    min_price = min((p for _, p in prices if p > 0), default=0)
    # say WHY leads stop: with enforcement ON a buyer whose balance cannot cover their cheapest tier is
    # excluded from the routing preview BEFORE any consumer consent — from the buyer's side this shows
    # up only as 'no new leads', so the desk must name it and offer the fix (add credits).
    flow = "on"
    if _credit_enforcing():
        floor = float(MARKETPLACE.get("min_balance_usd", 0) or 0)
        flow = "blocked" if bal <= floor else ("low" if bal < min_price else "on")
    elif bal < 0:
        flow = "overdraft"
    # caps are a SECOND independent stop reason (the engine's preview excludes an at-cap buyer exactly
    # like an unaffordable one), so the desk must name them too: a capped buyer otherwise sees only
    # 'everything is fine' and cannot tell why leads stopped. Credit problems outrank caps in the
    # message because a blocked balance never heals by waiting for the next day.
    cap_hit = ""
    daily_used = route.delivered_today(con, b["id"])
    monthly_used = route.delivered_in_month(con, b["id"])
    if b.get("daily_cap") and daily_used >= int(b["daily_cap"]):
        cap_hit = "daily"
    elif b.get("monthly_cap") and monthly_used >= int(b["monthly_cap"]):
        cap_hit = "monthly"
    if cap_hit and flow == "on":
        flow = "cap_" + cap_hit
    meta = {"balance": bal,
            "is_demo": bool(b.get("is_demo")),
            "enforcing": _credit_enforcing(),
            "prices": prices,
            "states": b.get("licensed_states") or [],
            "daily_cap": b.get("daily_cap"),
            "today": daily_used,
            "cap_hit": cap_hit,
            "month": monthly_used,
            "monthly_cap": b.get("monthly_cap")}
    leads = [dict(r) for r in con.execute("""SELECT * FROM leads WHERE buyer_id=? AND routing_status='DELIVERED'
        ORDER BY created_at DESC LIMIT 500""", (b["id"],))]
    for l in leads:
        l["agent_status"] = l["agent_status"] or "NEW"
    counts = {s_: sum(1 for l in leads if l["agent_status"] == s_) for s_ in AGENT_STATUSES}
    counts["ALL"] = len(leads)
    # the desk's status tab ("?f=SOLD") filters the same list; a filter that matches nothing is a
    # filter result, NOT an empty desk — the template must say exactly that.
    f = request.args.get("f", "")
    desk_rows = [dict(l) for l in ([l for l in leads if (l["agent_status"] or "NEW") == f]
                                   if (f and f in AGENT_STATUSES) else leads[:8])]
    for l in desk_rows:
        l["agent_status"] = l["agent_status"] or "NEW"
    desk_hints = {l["lead_id"]: _return_hint(dict(l), b) for l in desk_rows}
    # the provenance strip the old desk carried: the consumer's phone is the product, so every desk
    # row says where the number came from, how it validated and when consent was recorded.
    def _prov(l):
        bits = ["consumer-submitted", "valid format" if l.get("phone_valid") else "NOT valid"]
        if l.get("phone_valid"):
            bits.append(f"{l.get('phone_area_region') or '—'} area code")
        bits.append(f"consented {(l.get('consent_ts') or '—')[5:16].replace('T', ' ')}")
        return " · ".join(str(x) for x in bits)
    desk_prov = {l["lead_id"]: _prov(l) for l in desk_rows}
    # inline preview: the situation a buyer needs before calling, without leaving the desk
    def _tr(l):
        try:
            return json.loads(l.get("tool_result") or "{}") if not isinstance(l.get("tool_result"), dict) else l["tool_result"]
        except Exception:
            return {}
    desk_prev = {l["lead_id"]: _tr(l) for l in desk_rows}
    # working-tracker widget: leads whose next follow-up date has arrived (recorded by the buyer on
    # each lead) — reading recorded rows only, never a consumer signal.
    due_n = con.execute("""SELECT COUNT(*) FROM leads WHERE buyer_id=? AND routing_status='DELIVERED'
        AND followup_at IS NOT NULL AND followup_at <= date('now')""", (b["id"],)).fetchone()[0]
    # CRM/delivery health (recorded rows only): the LATEST delivery row per lead tells whether the
    # buyer's system actually accepted the payload — a FAILED latest attempt is what the buyer can
    # act on (the lead page's Retry re-posts the SAME recorded payload; automatic retries are
    # budgeted first, and an operator can re-post too). Nothing is extrapolated.
    _latest_dl = {}
    for r_ in con.execute("SELECT lead_id, ts, status, response FROM deliveries WHERE buyer_id=? ORDER BY id",
                          (b["id"],)):
        _latest_dl[r_["lead_id"]] = {"ts": r_["ts"], "status": r_["status"],
                                     "response": (r_["response"] or "").replace("\n", " ")[:120]}
    dl_failed = [{"lead_id": k, **v} for k, v in _latest_dl.items() if v["status"] == "FAILED"]
    dl_health = {"sent": sum(1 for v in _latest_dl.values() if v["status"] in ("SENT", "RESENT", "POSTED")),
                 "failed": dl_failed, "method": (b.get("delivery") or {}).get("method", "outbox")}
    own = route.buyer_health(con, MARKETPLACE.get("buyer_health", {})).get(b["id"])
    # rehearsal visibility: outside production EVERY desk (demo and real) also sees the other lane's
    # rows — badged per row and never counted as real activity. This is what keeps a demo buyer's
    # cards coherent when routing legitimately hands it a real-channel lead (real leads fill demo
    # desks last): the sale, its ledger row and its stats then all appear in the same sandbox view.
    # In production each lane sees only its own channel.
    chn = [ist] if PRODUCTION else [0, 1]
    ch = "(%s)" % ",".join("?" * len(chn))
    # ------------------------------------------------ home overview (dashboard-pattern):
    # every card is a RECORDED figure from this buyer's own delivered leads / ledger — never a forecast.
    def _win(days_from, days_to=None):
        lo = f"delivered_at >= datetime('now','-{abs(days_from)} days')"
        hi = f" AND delivered_at < datetime('now','-{abs(days_to)} days')" if days_to is not None else ""
        row = con.execute(f"""SELECT COUNT(*) n, COALESCE(SUM(price),0) usd FROM leads
            WHERE buyer_id=? AND routing_status='DELIVERED' AND is_test IN {ch} AND {lo}{hi}""", (b["id"], *chn)).fetchone()
        n, usd = int(row["n"]), float(row["usd"])
        return {"n": n, "usd": round(usd, 2), "avg": round(usd / n, 2) if n else None}

    w = _win(7); pw = _win(14, 7); mo = _win(30)

    def _delta(a, b_):
        # None means "no delivered leads in that window" (avg is None at n=0). Treating it as a
        # number raised TypeError and turned the whole desk into a 500 for any buyer with leads
        # 8-14 days ago but none in the last 7.
        if a in (None, 0) and b_ in (None, 0):
            return None
        if not b_:
            return 100.0 if a else None
        if a is None:
            return -100.0
        return round((a - b_) / b_ * 100.0, 1)

    active_camps = con.execute(f"""SELECT COUNT(DISTINCT campaign) FROM leads WHERE buyer_id=? AND is_test IN {ch}
        AND routing_status='DELIVERED' AND delivered_at >= datetime('now','-14 days')""", (b["id"], *chn)).fetchone()[0]
    rowp = con.execute("""SELECT COUNT(*) n, COALESCE(SUM(amount_usd),0) usd FROM buyer_payments
        WHERE buyer_id=? AND is_test=? AND status='PENDING'""", (b["id"], ist)).fetchone()
    todayspend = round(float(con.execute("""SELECT COALESCE(SUM(amount),0) FROM buyer_ledger
        WHERE buyer_id=? AND is_test=? AND kind='LEAD_CHARGE' AND ts >= datetime('now','start of day')""",
        (b["id"], ist)).fetchone()[0] or 0), 2)
    txns = [dict(r) for r in con.execute(f"""SELECT ts, kind, amount, lead_id, package_id, balance_after, note, is_test FROM buyer_ledger
        WHERE buyer_id=? AND is_test IN {ch}
        AND (lead_id IS NULL OR lead_id IN (SELECT lead_id FROM leads WHERE buyer_id=? AND routing_status='DELIVERED'))
        ORDER BY id DESC LIMIT 12""", [b["id"], *chn, b["id"]])]
    recent = [dict(r) for r in con.execute("""SELECT lead_id, first_name, last_name, tier, price, created_at, delivered_at,
        campaign, agent_status, pinned, is_test FROM leads
        WHERE buyer_id=? AND routing_status='DELIVERED'
        ORDER BY COALESCE(delivered_at, created_at) DESC LIMIT 8""", (b["id"],))]
    for l in recent:
        l["agent_status"] = l["agent_status"] or "NEW"
    # highlights: each bullet is a recorded fact on file, not a goal or a promise
    unworked = con.execute(f"""SELECT COUNT(*) n FROM leads WHERE buyer_id=? AND is_test IN {ch} AND routing_status='DELIVERED'
        AND (agent_status IS NULL OR agent_status IN ('NEW','VIEWED')) AND created_at < datetime('now','-3 days')""",
        (b["id"], *chn)).fetchone()[0]
    rets7d = con.execute("""SELECT COUNT(*) n FROM lead_outcomes WHERE buyer_id=? AND outcome='RETURNED'
        AND ts >= datetime('now','-7 days')""", (b["id"],)).fetchone()[0]
    sold30 = con.execute(f"""SELECT COUNT(*) n FROM leads WHERE buyer_id=? AND is_test IN {ch} AND routing_status='DELIVERED'
        AND agent_status='SOLD' AND created_at >= datetime('now','-30 days')""", (b["id"], *chn)).fetchone()[0]
    today_n = con.execute(f"""SELECT COUNT(*) FROM leads WHERE buyer_id=? AND is_test IN {ch}
        AND routing_status='DELIVERED' AND delivered_at >= datetime('now','start of day')""",
        (b["id"], *chn)).fetchone()[0]
    return render_template("agent_home.html", b=b, token=token, meta=meta, flow=flow, own=own,
                           stat_week=w, stat_prev=pw, stat_month=mo, f=f,
                           leads=leads, desk_rows=desk_rows, desk_hints=desk_hints, desk_prov=desk_prov, desk_counts=counts,
                           desk_prev=desk_prev, due_n=due_n,
                           call_rules=route.CONTACT_RULES,
                           stat_today={"n": today_n},
                           stat_delta={"week": _delta(w["n"], pw["n"]), "avg": _delta(w["avg"], pw["avg"])},
                           active_camps=active_camps, pend={"n": rowp["n"], "usd": float(rowp["usd"])},
                           dl_health=dl_health,
                           todayspend=todayspend,
                           txns=txns, recent=recent, unworked=unworked, rets7d=rets7d, sold30=sold30,
                           nav="home",
                           desk_msg=request.args.get("msg", ""), desk_err=request.args.get("err", ""),
                           ret_reasons=(_pol().get("reasons") or []), ret_default=_pol().get("default_reason", "other"))


# ------------------------------------------------------------ lead LIST (search / filter / sort / pagination)
@app.get("/agent/<token>/leads")
def agent_leads(token):
    """The working lead list: search, date window, status/pinned filters, sortable columns and
    pagination over THIS buyer's delivered leads only. Nothing here can route, sell, or touch
    other buyers' data — every query is scoped by the portal token's buyer id."""
    b = _buyer(token)
    ist = 1 if b.get("is_demo") else 0
    # rehearsal visibility: outside production EVERY desk (demo and real) also sees the other lane's
    # rows — badged per row and never counted as real activity. This is what keeps a demo buyer's
    # cards coherent when routing legitimately hands it a real-channel lead (real leads fill demo
    # desks last): the sale, its ledger row and its stats then all appear in the same sandbox view.
    # In production each lane sees only its own channel.
    chn = [ist] if PRODUCTION else [0, 1]
    ch = "(%s)" % ",".join("?" * len(chn))
    q = (request.args.get("q") or "").strip()[:80]
    f = request.args.get("f", "")
    days = request.args.get("days", "7")
    days = int(days) if days.isdigit() and int(days) in (0, 7, 14, 30, 90) else 7
    sort = request.args.get("sort", "date")
    dir_ = "asc" if request.args.get("dir") == "asc" else "desc"
    page = max(1, int(request.args.get("page", "1") or 1) if request.args.get("page", "1").isdigit() else 1)
    PER = 25
    SORTS = {"date": "COALESCE(delivered_at, created_at)", "name": "lower(first_name||' '||last_name)",
             "campaign": "lower(campaign)", "tier": "tier", "price": "price", "status": "agent_status"}
    order = (SORTS.get(sort) or SORTS["date"]) + (" ASC" if dir_ == "asc" else " DESC")
    where, args = ["buyer_id=?", "routing_status='DELIVERED'", f"is_test IN {ch}"], [b["id"], *chn]
    if q:
        like = f"%{q.lower()}%"
        where.append("(lower(first_name||' '||last_name) LIKE ? OR lower(email) LIKE ? OR lower(phone) LIKE ? "
                     "OR lower(lead_id) LIKE ? OR lower(campaign) LIKE ?)")
        args += [like, like, like, like, like]
    if f and f in AGENT_STATUSES:
        where.append("COALESCE(agent_status,'NEW')=?")
        args.append(f)
    if request.args.get("pinned") == "1":
        where.append("COALESCE(pinned,0)=1")
    if request.args.get("due") == "1":
        where.append("(followup_at IS NOT NULL AND followup_at <= date('now'))")
    # a due lead is urgent regardless of when it was delivered: the due view must not be narrowed by the
    # received-date window (default 7 days), or overdue outreach rows silently disappear from the desk.
    if days and request.args.get("due") != "1":
        where.append("COALESCE(delivered_at, created_at) >= datetime('now','-%d days')" % days)
    ww = " AND ".join(where)
    total = con.execute(f"SELECT COUNT(*) FROM leads WHERE {ww}", args).fetchone()[0]
    pages = max(1, -(-total // PER))
    page = min(page, pages)
    rows = [dict(r) for r in con.execute(
        f"""SELECT lead_id, first_name, last_name, phone, email, zip, state, campaign, tier, price, created_at,
        delivered_at, agent_status, pinned, is_test, est_av, contact_at, followup_at, preferred_contact, need_path
        FROM leads WHERE {ww} ORDER BY COALESCE(pinned,0) DESC, {order} LIMIT ? OFFSET ?""",
        args + [PER, (page - 1) * PER])]
    for l in rows:
        l["agent_status"] = l["agent_status"] or "NEW"
    pmin = min((b["price_by_tier"].get(t, 0) for t in (b.get("tiers") or [])), default=0)
    return render_template("agent_leads.html", b=b, token=token, rows=rows, q=q, f=f, days=days, sort=sort, dir_=dir_,
                           page=page, pages=pages, total=total, per=PER, statuses=AGENT_STATUSES,
                           nav="leads", pmin=pmin, due=(request.args.get("due") == "1"),
                           desk_msg=request.args.get("msg", ""), desk_err=request.args.get("err", ""))


# ----------------------------------------------------------------- outcomes tracker (inline record fields)
@app.get("/agent/<token>/outcomes")
def agent_outcomes(token):
    """Working tracker for the leads already delivered: first-contact date, estimated submitted AV,
    next follow-up and current status at a glance. Same scope rules as the lead list."""
    b = _buyer(token)
    ist = 1 if b.get("is_demo") else 0
    # rehearsal visibility: outside production EVERY desk (demo and real) also sees the other lane's
    # rows — badged per row and never counted as real activity. This is what keeps a demo buyer's
    # cards coherent when routing legitimately hands it a real-channel lead (real leads fill demo
    # desks last): the sale, its ledger row and its stats then all appear in the same sandbox view.
    # In production each lane sees only its own channel.
    chn = [ist] if PRODUCTION else [0, 1]
    ch = "(%s)" % ",".join("?" * len(chn))
    q = (request.args.get("q") or "").strip()[:80]
    week = request.args.get("week", "4")
    week = int(week) if week.isdigit() and int(week) in (1, 4, 12, 0) else 4
    where, args = ["buyer_id=?", "routing_status='DELIVERED'", f"is_test IN {ch}"], [b["id"], *chn]
    if q:
        like = f"%{q.lower()}%"
        where.append("(lower(first_name||' '||last_name) LIKE ? OR lower(email) LIKE ? OR lower(phone) LIKE ?)")
        args += [like, like, like]
    if week:
        where.append("COALESCE(created_at, delivered_at) >= datetime('now','-%d days')" % week)
    rows = [dict(r) for r in con.execute(
        """SELECT lead_id, first_name, last_name, phone, email, campaign, tier, price, created_at, delivered_at,
        agent_status, household_size, contact_at, est_av, followup_at, pinned FROM leads
        WHERE """ + " AND ".join(where) + " ORDER BY COALESCE(contact_at, followup_at, COALESCE(delivered_at, created_at)) DESC LIMIT 100",
        args)]
    return render_template("agent_outcomes.html", b=b, token=token, rows=rows, q=q, week=week,
                           statuses=AGENT_STATUSES, nav="leads", desk_msg=request.args.get("msg", ""),
                           desk_err=request.args.get("err", ""))


# ------------------------------------------------------------ buyer-desk lead WORKSPACE actions (own leads only)
@app.post("/agent/<token>/lead/<lead_id>/pin")
def agent_lead_pin(token, lead_id):
    """Toggle this buyer's pin on one of their own delivered leads (a working-tracker flag, never a
    routing/pricing input). Redirects back through the SAME list query so filters survive."""
    b = _buyer(token)
    l = con.execute("SELECT pinned FROM leads WHERE lead_id=? AND buyer_id=? AND routing_status='DELIVERED'",
                    (lead_id, b["id"])).fetchone() or abort(404)
    con.execute("UPDATE leads SET pinned=? WHERE lead_id=?", (0 if (l["pinned"] or 0) else 1, lead_id))
    con.commit()
    return redirect(_back_to_list(token), code=302)


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@app.post("/agent/<token>/lead/<lead_id>/fields")
def agent_lead_fields(token, lead_id):
    """Record the buyer-side working fields on their OWN lead: first-contact date, estimated submitted
    AV, next follow-up. Dates must be YYYY-MM-DD or blank; AV must be a non-negative number or blank.
    A rejected field never changes the lead and the redirect carries exactly which field was refused."""
    b = _buyer(token)
    l = con.execute("SELECT contact_at, est_av, followup_at FROM leads WHERE lead_id=? AND buyer_id=? AND routing_status='DELIVERED'",
                    (lead_id, b["id"])).fetchone() or abort(404)
    bad = ""
    vals = {}
    for key in ("contact_at", "followup_at"):
        v = (request.form.get(key) or "").strip()
        if v and not _DATE_RE.match(v):
            bad = key
        vals[key] = v or None
    av = (request.form.get("est_av") or "").strip()
    if av and not re.fullmatch(r"\d{1,7}(\.\d{1,2})?", av):
        bad = "est_av"
    vals["est_av"] = round(float(av), 2) if av else None
    if bad:
        return redirect(_back_to_list(token, err=f"field:{bad}"), code=302)
    con.execute("UPDATE leads SET contact_at=?, est_av=?, followup_at=? WHERE lead_id=? AND buyer_id=?",
                (vals["contact_at"], vals["est_av"], vals["followup_at"], lead_id, b["id"]))
    con.commit()
    return redirect(_back_to_list(token, msg="fields"), code=302)


def _back_to_list(token, **tail):
    """Come back to the referring buyer-desk page (list or tracker) preserving the exact filter query,
    so the buyer sees the same rows after an action."""
    ret = request.form.get("ret") or request.args.get("ret") or ""
    if not ret.startswith("/agent/"):
        ret = url_for("agent_leads", token=token)
    seps = "&" if "?" in ret else "?"
    return ret + (seps + "&".join(f"{k}={v}" for k, v in tail.items()) if tail else "")


@app.get("/agent/<token>/lead/<lead_id>")
def agent_lead(token, lead_id):
    b = _buyer(token)
    l = con.execute("SELECT * FROM leads WHERE lead_id=? AND buyer_id=?", (lead_id, b["id"])).fetchone() or abort(404)
    if not l["agent_status"] or l["agent_status"] == "NEW":
        con.execute("UPDATE leads SET agent_status='VIEWED' WHERE lead_id=?", (lead_id,))
        con.execute("INSERT INTO lead_views VALUES (?,?,?)", (lead_id, b["id"], now()))
        con.commit()
    c_ = con.execute("SELECT * FROM consents WHERE lead_id=?", (lead_id,)).fetchone()
    hist = [dict(r) for r in con.execute("SELECT ts,outcome,note FROM lead_outcomes WHERE lead_id=? ORDER BY id", (lead_id,))]
    # latest CRM delivery attempt for THIS buyer's lead (webhook buyers only): the buyer sees the same
    # truth the operator sees — and can trigger the same budgeted re-post themselves.
    crm = con.execute("""SELECT status, ts, response FROM deliveries WHERE lead_id=? AND buyer_id=? AND method='webhook'
        ORDER BY id DESC LIMIT 1""", (lead_id, b["id"])).fetchone()
    # the RETURN button carries the same policy hint the desk tooltip shows: derived from
    # _returns_allowed (the exact rules that decide at submit), never a promise of approval
    return render_template("agent_lead.html", b=b, l=dict(l), c=dict(c_) if c_ else {}, hist=hist, statuses=AGENT_STATUSES, token=token,
                           j=lambda s_: json.loads(s_ or "{}"), crm=dict(crm) if crm else None, nav="leads",
                           meta={"balance": billing.balance(con, b["id"], 1 if b.get("is_demo") else 0)},
                           return_hint=_return_hint(dict(l), b),
                           redeliver=request.args.get("redeliver", ""),
                           desk_msg=request.args.get("msg", ""), desk_err=request.args.get("err", ""),
                           ret_reasons=(_pol().get("reasons") or []), ret_default=_pol().get("default_reason", "other"))


# ----------------------------------------------------------------- markets/client list (campaigns delivering to you)
@app.get("/agent/<token>/markets")
def agent_markets(token):
    """The buyer's campaign list (the reference desk's Campaigns view, kept honest): one row per
    campaign that has actually delivered leads to THIS buyer, with recorded volume, spend, avg price
    and status mix — the same figures the home stat cards use, never a projection. Empty state names
    the true position (no campaigns delivering to you yet), not an error."""
    b = _buyer(token)
    ist = 1 if b.get("is_demo") else 0
    rows = [dict(r) for r in con.execute(
        f"""SELECT campaign, COUNT(*) n, COALESCE(SUM(price),0) usd, ROUND(AVG(price),2) avg,
                  MAX(COALESCE(delivered_at, created_at)) last_delivery,
                  SUM(agent_status='SOLD') sold, SUM(COALESCE(agent_status,'NEW')='NEW') unworked,
                  SUM(is_test) test_n
           FROM leads WHERE buyer_id=? AND is_test=? AND routing_status='DELIVERED'
           GROUP BY campaign ORDER BY n DESC""", (b["id"], ist))]
    for r in rows:
        r["usd"] = round(float(r["usd"] or 0), 2)
        r["avg"] = round(float(r["avg"]), 2) if r["avg"] is not None else None
        r["sold"] = int(r["sold"] or 0); r["unworked"] = int(r["unworked"] or 0); r["test_n"] = int(r["test_n"] or 0)
    total_n = sum(r["n"] for r in rows); total_usd = round(sum(r["usd"] for r in rows), 2)
    return render_template("agent_markets.html", b=b, token=token, rows=rows, total_n=total_n, total_usd=total_usd,
                           nav="markets")


# ----------------------------------------------------------------- buyer billing page (prepaid credits + Stripe rail)
@app.route("/agent/<token>/billing", methods=["GET"])
def agent_billing(token):
    """Buyer account view: balance, purchases, pending payments, delivered leads, ledger. This page moves no
    money; a purchase starts a server-created Checkout session (test mode) and credits land ONLY via the
    signature-verified webhook."""
    b = _buyer(token)
    ist = 1 if b.get("is_demo") else 0
    s = billing.buyer_summary(con, b["id"], ist)
    s["last_7d_charged"] = billing.per_week(con, b["id"], 7, ist)
    s["last_28d_charged"] = billing.per_week(con, b["id"], 28, ist)
    enforcing = _credit_enforcing()
    ret = get_marketplace().get("returns", {})
    rail = payments.mode()                                      # off | test | live
    # purchase history: the account's own lane rows PLUS clearly-labeled SANDBOX rehearsal rows
    # (a sandbox purchase is always is_test=1, so a real buyer would otherwise never see it here)
    purch = [dict(r) for r in con.execute(
        "SELECT id, ts, package_id, amount_usd, status, provider, session_id, session_url FROM buyer_payments "
        "WHERE buyer_id=? AND (is_test=? OR provider='sandbox') ORDER BY id DESC LIMIT 25", (b["id"], ist))]
    for lr in con.execute("SELECT * FROM leads WHERE buyer_id=? AND routing_status='DELIVERED' AND price>0 AND is_test=?",
                          (b["id"], ist)):
        billing.reconcile_lead(con, {**dict(lr), "_buyer_demo": bool(b.get("is_demo"))})
    # last CRM webhook attempt per delivered lead, so a failing CRM is visible on THIS page too — the
    # same truth the operator alerts see; the buyer can trigger the same budgeted re-post from the lead.
    crm = {r["lead_id"]: dict(r) for r in con.execute(
        """SELECT d.* FROM deliveries d WHERE d.lead_id IN (SELECT lead_id FROM leads WHERE buyer_id=? AND is_test=? AND routing_status='DELIVERED')
            AND d.method='webhook' AND d.id = (SELECT MAX(x.id) FROM deliveries x WHERE x.lead_id=d.lead_id)""", (b["id"], ist))}
    poll = [dict(r) for r in con.execute(
        "SELECT lead_id, delivered_at, tier, routing_status, agent_status, price, sale_type FROM leads "
        "WHERE buyer_id=? AND routing_status='DELIVERED' AND is_test=? ORDER BY delivered_at DESC LIMIT 25", (b["id"], ist))]
    # a RETURNED status alone says NOTHING about money: the page must agree with the ledger, so the
    # per-lead credit is looked up here and 'returned & credited' is only shown when a LEAD_CREDIT row
    # exists (a refused return shows as refused — never as money the buyer did not get).
    credits = {r[0]: r[1] for r in con.execute(
        "SELECT lead_id, amount FROM buyer_ledger WHERE buyer_id=? AND is_test=? AND kind=?", (b["id"], ist, billing.LEAD_CREDIT))}
    eff_ret = _effective_window_hours(b)
    eff_ret = int(eff_ret) if float(eff_ret).is_integer() else eff_ret   # 2h, not 2.0h
    rh = billing.return_history(con, b["id"], ist)   # the buyer's OWN return/credit story (scoped)
    # a non-demo buyer's page is scoped to the REAL ledger, so deliberate TEST rehearsal activity
    # (TEST packs + TEST lead charges) would otherwise be invisible here and a rehearsed pilot looks
    # like "$0.00" in silence. The sandbox numbers only ever appear clearly labeled as TEST — the
    # real line above stays the authoritative balance; manipulating the sandbox is impossible from here.
    test_s = None
    if not ist:
        tb = billing.balance(con, b["id"], 1)
        tt = [dict(r) for r in con.execute(
            "SELECT ts, kind, amount, note FROM buyer_ledger WHERE buyer_id=? AND is_test=? ORDER BY id DESC LIMIT 10",
            (b["id"], 1))]
        if tb or tt:
            test_s = {"balance": tb, "txns": tt}
    return render_template("agent_billing.html", b=b, token=token, s=s, ist=ist, packages=MARKETPLACE.get("packages", []), nav="billing",
                           enforcing=enforcing, rail=rail, purch=purch, lead_rows=poll, crm=crm, rh=rh, credits=credits,
                           sandbox_ok=payments.sandbox_allowed(),
                           test_s=test_s,
                           return_window=eff_ret if ret.get("enabled", True) else None,
                           allow_after_contact=bool(ret.get("allow_after_contact")),
                           pay_msg=request.args.get("payment", ""), kinds={
                               "PACKAGE_PURCHASE": "Credit pack", "LEAD_CHARGE": "Lead delivered",
                               "LEAD_CREDIT": "Lead returned (credited back)", "ADJUSTMENT": "Adjustment",
                               "PENDING": "Pending payment", "PAID": "Paid (awaiting credit)",
                               "CREDITED": "Paid & credited", "FAILED": "Failed/expired"})


@app.post("/agent/<token>/billing/package")
def agent_buy_package(token):
    """Buyer starts a package purchase. Server-side only: prices come from config, never the browser.
    Credits NOTHING here — the webhook does, after Stripe's signature is verified."""
    b = _buyer(token)
    pkg = next((p for p in MARKETPLACE.get("packages", []) if p.get("id") == request.form.get("package")), None)
    if not pkg:
        abort(400)
    base = (SITE.get("base_url") or request.url_root).rstrip("/")
    is_test = 1 if (b.get("is_demo") or request.form.get("test") == "1") else 0
    chk = payments.create_checkout(con, b, pkg, base, MARKETPLACE, is_test=is_test)
    if not chk:
        # no Stripe rail: outside production the buyer can still REHEARSE the purchase on the
        # sandbox — a clearly TEST-only checkout that credits the TEST lane and moves no money
        if payments.sandbox_allowed():
            rowid = payments.create_sandbox_checkout(con, b, pkg)
            return redirect(url_for("agent_sandbox_checkout", token=token, rowid=rowid), code=302)
        return redirect(url_for("agent_billing", token=token, payment="rail_off"), code=302)
    return redirect(chk["url"], code=302)


@app.get("/agent/<token>/billing/sandbox/<int:rowid>")
def agent_sandbox_checkout(token, rowid):
    """Sandbox TEST checkout confirmation page: prints exactly what confirming will record
    (a PACKAGE_PURCHASE row on the TEST lane for this buyer, nothing else) and offers
    Confirm / Cancel. Rows other than this buyer's own are not visible (404)."""
    b = _buyer(token)
    row = payments.sandbox_row(con, rowid, b["id"])
    if not row:
        abort(404)
    pkg = next((p for p in MARKETPLACE.get("packages", []) if p.get("id") == row["package_id"]),
               {"id": row["package_id"], "label": row["package_id"], "amount_usd": row["amount_usd"]})
    ist = 1 if b.get("is_demo") else 0
    return render_template("agent_sandbox_checkout.html", b=b, token=token, row=row, pkg=pkg,
                           ist=ist, nav="billing",
                           testing_balance=billing.balance(con, b["id"], 1))


@app.post("/agent/<token>/billing/sandbox/<int:rowid>")
def agent_sandbox_confirm(token, rowid):
    """Resolve the sandbox purchase: cancel (PENDING -> FAILED, nothing ledgered) or confirm
    (one-way PENDING -> CREDITED, ONE TEST-lane package row). Idempotent: confirming twice
    redirects with a duplicate notice, never a second credit. Another buyer's row 404s."""
    b = _buyer(token)
    if request.form.get("action") == "cancel":
        payments.cancel_sandbox(con, rowid, b["id"])
        return redirect(url_for("agent_billing", token=token, payment="sandbox_cancelled"), code=302)
    res = payments.fulfill_sandbox(con, rowid, b["id"])
    if res["state"] == "not_found":
        abort(404)
    amt = f"${res['amount_usd']:.2f}"
    pid = res.get("package_id", "")
    msgs = {
        "credited": ("sandbox_credited",
                     f"SANDBOX TEST purchase recorded: {pid} deposited {amt} as TEST credits "
                     "(TEST lane only) — no money moved and your real-lane balance is untouched."),
        "duplicate": ("sandbox_duplicate",
                      f"This sandbox purchase was already resolved — nothing new was credited (it has stayed {amt} once, no matter how often you confirm)."),
        "refused": ("sandbox_refused", f"The sandbox purchase was refused ({res.get('reason', 'invalid')}) and no credits were recorded."),
    }
    msg, text = msgs.get(res["state"], ("sandbox_refused", "Unexpected sandbox resolution."))
    return redirect(url_for("agent_billing", token=token, payment=msg, sandbox_msg=text), code=302)


@app.post("/agent/<token>/lead/<lead_id>/redeliver")
def agent_lead_redeliver(token, lead_id):
    """Buyer-initiated re-post of a FAILED CRM webhook delivery: the SAME budgeted retry the desk runs
    on load — the EXACT recorded payload file (never regenerated data), signed with the buyer's secret,
    one new deliveries row, and never a new sale, re-route or re-charge. Returns status spokes:
    RESENT | FAILED | '' (live delivery off / budget exhausted / nothing failed)."""
    b = _buyer(token)
    l = con.execute("SELECT * FROM leads WHERE lead_id=? AND buyer_id=?", (lead_id, b["id"])).fetchone() or abort(404)
    if l["routing_status"] != "DELIVERED":
        abort(400)   # only a sold lead has a recorded payload; nothing here can re-route or re-sell
    result = route.retry_webhook(con, route.load_buyers(CFG), dict(l))
    spoke = {
        "RESENT": "CRM delivery re-posted successfully — the newest deliveries row has the response",
        "FAILED": "still failing — the newest deliveries row has the CRM's response; contact the operator if it persists",
        "": "nothing re-posted: no failed attempt is waiting (or the retry budget of 5 is exhausted — an operator can always resend from the lead page)",
    }.get(result, result)
    return redirect(url_for("agent_lead", token=token, lead_id=lead_id, redeliver=spoke), code=302)


@app.get("/agent/<token>/leads.csv")
def agent_leads_csv(token):
    """Download the lead list as CSV (the desk's export/backup affordance). Honors the exact same
    filters as the on-screen list (search, status, date window, pinned) and the same scope: only this
    buyer's delivered leads in this account's TEST/real lane. Columns stay stable for CRM imports.
    Nothing here routes, re-sells, or creates a billing event — it reads recorded rows."""
    b = _buyer(token)
    ist = 1 if b.get("is_demo") else 0
    # rehearsal visibility: outside production EVERY desk (demo and real) also sees the other lane's
    # rows — badged per row and never counted as real activity. This is what keeps a demo buyer's
    # cards coherent when routing legitimately hands it a real-channel lead (real leads fill demo
    # desks last): the sale, its ledger row and its stats then all appear in the same sandbox view.
    # In production each lane sees only its own channel.
    chn = [ist] if PRODUCTION else [0, 1]
    ch = "(%s)" % ",".join("?" * len(chn))
    q = (request.args.get("q") or "").strip()[:80]
    f = request.args.get("f", "")
    days = request.args.get("days", "7")
    days = int(days) if days.isdigit() and int(days) in (0, 7, 14, 30, 90) else 7
    where, args = ["buyer_id=?", "routing_status='DELIVERED'", f"is_test IN {ch}"], [b["id"], *chn]
    if q:
        like = f"%{q.lower()}%"
        where.append("(lower(first_name||' '||last_name) LIKE ? OR lower(email) LIKE ? OR lower(phone) LIKE ? "
                     "OR lower(lead_id) LIKE ? OR lower(campaign) LIKE ?)")
        args += [like, like, like, like, like]
    if f and f in AGENT_STATUSES:
        where.append("COALESCE(agent_status,'NEW')=?")
        args.append(f)
    if request.args.get("pinned") == "1":
        where.append("COALESCE(pinned,0)=1")
    if request.args.get("due") == "1":
        where.append("(followup_at IS NOT NULL AND followup_at <= date('now'))")
    # a due lead is urgent regardless of when it was delivered: the due view must not be narrowed by the
    # received-date window (default 7 days), or overdue outreach rows silently disappear from the desk.
    if days and request.args.get("due") != "1":
        where.append("COALESCE(delivered_at, created_at) >= datetime('now','-%d days')" % days)
    ordering = (" ORDER BY lower(first_name||' '||last_name) ASC" if request.args.get("sort") == "name"
                else " ORDER BY COALESCE(delivered_at, created_at) ASC")
    rows = [dict(r) for r in con.execute(
        f"""SELECT lead_id, first_name, last_name, phone, email, zip, state, campaign, tier, price,
        created_at, delivered_at, agent_status, is_test, contact_at, est_av, followup_at FROM leads WHERE {" AND ".join(where)}{ordering} LIMIT 2000""",
        args)]
    # mirror the screen wording: NEW when the desk hasn't recorded a status yet
    for l in rows:
        l["agent_status"] = l["agent_status"] or "NEW"
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["lead_id", "first_name", "last_name", "phone", "email", "zip", "state",
                                        "campaign", "tier", "price", "received_utc", "agent_status", "is_test",
                                        "contact_at", "est_av", "followup_at"])
    w.writeheader()
    for l in rows:
        w.writerow({"lead_id": l["lead_id"], "first_name": l["first_name"], "last_name": l["last_name"],
                    "phone": l["phone"], "email": l["email"] or "", "zip": l["zip"], "state": l["state"],
                    "campaign": l["campaign"], "tier": l["tier"], "price": l["price"],
                    "received_utc": l["delivered_at"] or l["created_at"], "agent_status": l["agent_status"],
                    "is_test": l["is_test"], "contact_at": l["contact_at"] or "",
                    "est_av": l["est_av"] if l["est_av"] is not None else "", "followup_at": l["followup_at"] or ""})
    name = f"leads-{b['id']}-{now()[:10]}.csv"
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={name}"})


@app.get("/agent/<token>/billing/statement.csv")
def agent_statement(token):
    """The buyer's ledger as a CSV for their own accounting/reconciliation. Same rows, same order,
    same TEST scope as the on-screen ledger — nothing here is a new billing decision."""
    b = _buyer(token)
    ist = 1 if b.get("is_demo") else 0
    rows = [dict(r) for r in con.execute(
        "SELECT ts, kind, amount, balance_after, lead_id, package_id, note FROM buyer_ledger "
        "WHERE buyer_id=? AND is_test=? ORDER BY id", (b["id"], ist))]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["ts", "kind", "amount", "balance_after", "lead_id", "package_id", "note"])
    w.writeheader()
    w.writerows(rows)
    name = f"ledger-{b['id']}-{now()[:10]}.csv"
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={name}"})


@app.get("/agent/<token>/outcomes.csv")
def agent_outcomes_csv(token):
    """Download the outcome tracker as CSV — the same rows, filters and ordering as the on-screen
    tracker (/agent/<token>/outcomes), so a buyer's CRM import mirrors exactly what their desk shows.
    Columns: lead identity, status, the buyer's own recorded working fields (first contact, est AV,
    next follow-up) and the TEST badge. No contact detail beyond what the buyer already sees
    on their desk: scope is this token's leads only, same TEST/real lane rules.
    """
    b = _buyer(token)
    ist = 1 if b.get("is_demo") else 0
    chn = [ist] if (ist or PRODUCTION) else [0, 1]
    ch = "(%s)" % ",".join("?" * len(chn))
    q = (request.args.get("q") or "").strip()[:80]
    week = request.args.get("week", "4")
    week = int(week) if week.isdigit() and int(week) in (1, 4, 12, 0) else 4
    where, args = ["buyer_id=?", "routing_status='DELIVERED'", f"is_test IN {ch}"], [b["id"], *chn]
    if q:
        like = f"%{q.lower()}%"
        where.append("(lower(first_name||' '||last_name) LIKE ? OR lower(email) LIKE ? OR lower(phone) LIKE ?)")
        args += [like, like, like]
    if week:
        where.append("COALESCE(created_at, delivered_at) >= datetime('now','-%d days')" % week)
    rows = [dict(r) for r in con.execute(
        """SELECT lead_id, first_name, last_name, phone, email, campaign, tier, price, created_at, delivered_at,
        agent_status, is_test, contact_at, est_av, followup_at FROM leads
        WHERE """ + " AND ".join(where) + """ ORDER BY COALESCE(contact_at, followup_at, COALESCE(delivered_at, created_at)) DESC LIMIT 2000""",
        args)]
    for l in rows:                              # mirror the screen: NEW until the desk records a status
        l["agent_status"] = l["agent_status"] or "NEW"
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["lead_id", "first_name", "last_name", "phone", "email", "campaign", "tier",
                                        "price", "received_utc", "agent_status", "is_test", "contact_at", "est_av",
                                        "followup_at"])
    w.writeheader()
    for l in rows:
        w.writerow({"lead_id": l["lead_id"], "first_name": l["first_name"], "last_name": l["last_name"],
                    "phone": l["phone"], "email": l["email"] or "", "campaign": l["campaign"], "tier": l["tier"],
                    "price": l["price"], "received_utc": l["delivered_at"] or l["created_at"],
                    "agent_status": l["agent_status"], "is_test": l["is_test"], "contact_at": l["contact_at"] or "",
                    "est_av": l["est_av"] if l["est_av"] is not None else "", "followup_at": l["followup_at"] or ""})
    name = f"outcomes-{b['id']}-{now()[:10]}.csv"
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={name}"})


@app.post("/agent/<token>/delivery")
def agent_delivery(token):
    """Buyer self-serve delivery endpoint: where their sold leads go (portal/outbox by default, their CRM
    webhook, or email). The portal token is already the credential, so no new auth surface. The buyer can
    only change THEIR row; nothing here can activate, cap or price a buyer — that stays with the operator."""
    b = _buyer(token)
    buyers = json.loads((CFG / "buyers.json").read_text(encoding="utf-8"))
    row = next((x for x in buyers if x["id"] == b["id"]), None) or abort(404)
    method = request.form.get("method")
    if method not in _ALLOWED_DELIVERY:
        abort(400)
    email = (request.form.get("email") or "").strip().lower()[:160]
    url_ = (request.form.get("webhook_url") or "").strip()[:400]
    d = {**row.get("delivery", {})}
    if method == "webhook":
        if not url_ and not d.get("webhook_url"):
            return redirect(url_for("agent_billing", token=token, payment="delivery_url_missing"), code=302)
        if url_ and not re.fullmatch(r"https://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+", url_):
            return redirect(url_for("agent_billing", token=token, payment="delivery_url_bad"), code=302)
        if url_:
            d["webhook_url"] = url_
    if method == "email" and "@" not in email:
        return redirect(url_for("agent_billing", token=token, payment="delivery_email_bad"), code=302)
    d["method"] = method
    if email:
        d["email"] = email
    row["delivery"] = d
    # a self-serve webhook switch-on needs a signing secret too: every lead POST is HMAC-SHA256 signed
    # with it (x-ticket-signature header). Shown on this private page (the portal token IS the credential).
    if method == "webhook" and not d.get("webhook_secret"):
        d["webhook_secret"] = secrets.token_urlsafe(32)
        row["delivery"] = d
    _save_buyers(buyers)
    return redirect(url_for("agent_billing", token=token, payment="delivery_saved"), code=302)


@app.post("/api/webhooks/stripe")
def stripe_webhook():
    """THE ONLY place credits are granted for a self-serve purchase. Stripe's signature must verify
    (Stripe-Signature header vs STRIPE_WEBHOOK_SECRET) before any DB write; anything else is 400/503."""
    res = payments.handle_webhook(con, request.get_data(cache=False), request.headers.get("Stripe-Signature", ""), MARKETPLACE)
    if not res["ok"]:
        return jsonify(webhook="rejected", reason=res["reason"]), res["http"]
    return jsonify(webhook="ok", **(res["result"] or {})), res["http"]


@app.route("/admin/buyer/<buyer_id>/topup", methods=["POST"])
def admin_buyer_topup(buyer_id):
    """Operator records a prepaid credit pack AFTER confirming funds received. Idempotent by note+package+day
    is not required — this is manual bookkeeping; every submission writes exactly one ledger row with a note."""
    _admin()
    # the REAL buyers file: an onboarded-but-not-yet-active buyer can already have paid — their credits
    # are recorded now and apply the moment they are activated (load_buyers() would only see active rows).
    b = next((x for x in json.loads((CFG / "buyers.json").read_text(encoding="utf-8")) if x["id"] == buyer_id), None)
    if not b:
        abort(404)
    pkg_id = request.form.get("package", "")
    pkg = next((p for p in MARKETPLACE.get("packages", []) if p.get("id") == pkg_id), None)
    note = request.form.get("note", "")
    testmode = request.form.get("test") == "1"
    if testmode and not b.get("is_demo"):
        abort(400)  # TEST-mode ledger rows only apply to demo buyers
    pkg_use = pkg or {"id": pkg_id or "custom", "amount_usd": request.form.get("amount_usd", "")}
    try:
        row = billing.record_package(con, b["id"], pkg_use, note=note or "recorded by operator after funds received",
                                     is_test=1 if testmode else 0)
    except (ValueError, TypeError):
        abort(400)  # bad amount / empty package / non-positive value
    # once the operator has recorded the pack here, any still-PENDING self-serve Checkout session for the
    # SAME package can never credit again: a late webhook would otherwise grant the pack a SECOND time.
    # Closing the row keeps the documented guarantee — fulfillment moves only PENDING -> PAID -> CREDITED.
    if pkg:
        amt = round(float(pkg.get("amount_usd") or 0), 2)
        for r_ in con.execute("SELECT rowid AS rid FROM buyer_payments WHERE buyer_id=? AND package_id=? "
                              "AND amount_usd=? AND status='PENDING'", (b["id"], pkg.get("id", ""), amt)).fetchall():
            con.execute("UPDATE buyer_payments SET status='CLOSED', "
                        "note=COALESCE(note,'') || ' | closed by the operator top-up - the pack is in the ledger exactly once' WHERE rowid=?",
                        (r_["rid"],))
        con.commit()
    return redirect(url_for("agent_billing", token=b.get("portal_token", "")))  # never send the admin token into a buyer-facing URL


@app.post("/admin/buyer/<buyer_id>/adjust")
def admin_buyer_adjust(buyer_id):
    """Operator ledger ADJUSTMENT (credit or debit with a mandatory note) — the escape hatch the return
    policy points to for off-policy returns, goodwill credits or corrections. Bookkeeping only."""
    _admin()
    b = next((x for x in json.loads((CFG / "buyers.json").read_text(encoding="utf-8")) if x["id"] == buyer_id), None)
    if not b:
        abort(404)
    if request.form.get("test") == "1" and not b.get("is_demo"):
        abort(400)  # TEST-mode ledger rows only apply to demo buyers
    try:
        amount = round(float(request.form.get("amount") or 0), 2)
    except ValueError:
        abort(400)
    note = (request.form.get("note") or "").strip()
    if amount == 0 or not note:
        abort(400)  # an adjustment is always a specific amount AND a reason
    billing.apply_adjustment(con, b["id"], amount, note=f"operator: {note}"[:500], is_test=1 if b.get("is_demo") else 0)
    con.commit()
    return redirect(url_for("admin", token=ADMIN_TOKEN, saved=f"adjustment recorded for {buyer_id}"))


# ------------------------------------------- application review: what the applicant SUPPLIED vs what was VERIFIED
# Verification is always an operator attestation of an EXTERNAL check they performed (FL DFS licensee search,
# NPN lookup, a delivery fan-out test, reaching the named contact) — it is never derived from the application,
# and recording it requires naming the source/date it was checked against. Nothing here activates a buyer,
# changes routing, or touches the ledger.
_VERIFIABLE = {"license": "FL DFS license", "npn": "NPN lookup", "delivery_tested": "delivery fan-out test",
               "contact_reached": "contact reached"}


@app.post("/admin/buyer/<buyer_id>/verify")
def admin_buyer_verify(buyer_id):
    _admin()
    buyers = json.loads((CFG / "buyers.json").read_text(encoding="utf-8"))
    b = next((x for x in buyers if x["id"] == buyer_id), None)
    if not b:
        abort(404)
    what = request.form.get("what", "")
    if what not in _VERIFIABLE:
        abort(400)
    if request.form.get("action") == "forget":          # withdraw a wrongly-recorded verification explicitly
        v = b.pop("_verification", {})
        v.pop(what, None)
        if v:
            b["_verification"] = v
        _save_buyers(buyers)
        return redirect(url_for("admin", token=ADMIN_TOKEN, saved=f"verification withdrawn for {buyer_id}: {_VERIFIABLE[what]}"))
    ref = (request.form.get("ref") or "").strip()[:300]
    if not ref:
        return redirect(url_for("admin", token=ADMIN_TOKEN,
                                err="a verification reference is required (what was checked + where/when, e.g. 'FL DFS licensee search 2026-10-22, active')"))
    b.setdefault("_verification", {})[what] = {"ts": now(), "ref": ref}
    _save_buyers(buyers)
    return redirect(url_for("admin", token=ADMIN_TOKEN, saved=f"verification recorded for {buyer_id}: {_VERIFIABLE[what]}"))


# ----------------------------------------------------------------- admin: buyer onboarding (config is validated, never hand-untouched JSON)
_ALLOWED_PRODUCTS = ("health_aca", "term_life", "final_expense", "mortgage_protection", "key_person")


def _https_url(u: str) -> bool:
    """Consumer contact data may only be posted to an https:// CRM webhook. The buyer-portal
    delivery form already enforces this; the application form and the admin onboarding form
    must not become the http loophole. (Local dev fixtures are tested at engine level.)"""
    from urllib.parse import urlparse as _p
    try:
        return _p(u).scheme == "https" and bool(_p(u).netloc)
    except Exception:
        return False
_ALLOWED_TIERS = ("A", "B", "C")
_ALLOWED_DELIVERY = ("outbox", "email", "webhook")


def _placeholder(s) -> bool:
    return not s or "[" in s or "CHANGE-ME" in str(s).upper()


def _onboard_errors(b: dict) -> list[str]:
    """Every reason this buyer must NOT sell real leads yet. Empty list = activation is safe."""
    errs = []
    if _placeholder(b.get("name")) or len(str(b.get("name") or "")) < 4:
        errs.append("the seller name is what consumers consent to - set the EXACT legal name, no placeholder brackets")
    if not str(b.get("license_number") or "").strip():
        errs.append("license_number is empty - verify the license at the FL DFS licensee search before activation")
    if not str(b.get("npn") or "").strip():
        errs.append("npn is empty")
    if not b.get("portal_token") or _placeholder(b["portal_token"]):
        errs.append("portal_token missing/placeholder - portal login gives full contact data")
    states = [s for s in (b.get("licensed_states") or []) if s]
    bad_states = [s for s in states if s not in SITE["served_states"]]
    if not states:
        errs.append("licensed_states is empty")
    elif bad_states:
        errs.append(f"licensed state(s) not served by this site: {', '.join(bad_states)}")
    prods = [p for p in (b.get("products") or []) if p]
    if not prods:
        errs.append("products is empty")
    elif any(p not in _ALLOWED_PRODUCTS for p in prods):
        errs.append(f"unknown product(s): {[p for p in prods if p not in _ALLOWED_PRODUCTS]}")
    tiers = [x for x in (b.get("tiers") or []) if x]
    if not tiers or any(t not in _ALLOWED_TIERS for t in tiers):
        errs.append("tiers must be a non-empty subset of A/B/C")
    prices = b.get("price_by_tier") or {}
    bad_prices = [t for t in tiers if not (prices.get(t) or 0) > 0]
    if bad_prices:
        errs.append(f"price_by_tier must set a POSITIVE price for every tier sold: missing/<=0 for {', '.join(bad_prices)}")
    if (b.get("daily_cap") or 0) < 1:
        errs.append("daily_cap must be at least 1")
    if not (18 <= int(b.get("age_min") or 0) <= int(b.get("age_max") or 0) <= 100):
        errs.append("age band invalid (18 <= age_min <= age_max <= 100)")
    method = b.get("delivery", {}).get("method", "outbox")
    if method not in _ALLOWED_DELIVERY:
        errs.append(f"delivery method must be one of {', '.join(_ALLOWED_DELIVERY)}")
    if method in ("email", "webhook") and not (b.get("delivery", {}).get("email") or b.get("delivery", {}).get("webhook_url")):
        errs.append(f"delivery method {method} needs the {'email' if method == 'email' else 'webhook_url'} filled in")
    if method == "webhook" and (b.get("delivery", {}).get("webhook_url") or "") \
            and not re.match(r"^https?://[a-z0-9.][^\s]{3,}$", b["delivery"]["webhook_url"], re.IGNORECASE):
        errs.append("webhook_url must be a URL starting with http(s):// — a CRM endpoint, not an email address")
    return errs


def _save_buyers(buyers: list[dict]) -> None:
    (CFG / "buyers.json").write_text(json.dumps(buyers, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    # Prospect↔buyer traceability: when a handoff-created buyer is actually ACTIVATED
    # (only ever by the operator through the real onboarding gates above), the originating
    # prospect records ACTIVATED. Best-effort bookkeeping — a registry failure never
    # breaks the buyer save, and a paused buyer never records a downgrade.
    for b in buyers:
        src = b.get("converted_from_prospect") or {} if isinstance(b, dict) else {}
        if src.get("prospect_id") and b.get("active"):
            try:
                pcon = prospects_mod.connect(PROSPECTS_DB)
                try:
                    row = prospects_mod.detail(pcon, src["prospect_id"])
                    if row and row.get("stage") != "ACTIVATED":
                        prospects_mod.set_stage_checked(
                            pcon, src["prospect_id"], "ACTIVATED", allow_terminal=True,
                            note=f"buyer {b['id']} activated through the real onboarding gates")
                        prospects_mod.set_buyer_link(pcon, src["prospect_id"], b["id"])
                finally:
                    pcon.close()
            except Exception:                          # noqa: BLE001 - bookkeeping must not break saves
                pass


@app.post("/admin/buyers")
def admin_buyers_onboard():
    """Add or update a buyer in config/buyers.json. ACTIVATION is refused while _onboard_errors() is
    non-empty: a lead sold to an invalid buyer is a consent-compliance failure, not a config typo.
    Demo buyers are read-only here: they are placeholders, never a real seller in the consumer consent."""
    _admin()
    buyers = json.loads((CFG / "buyers.json").read_text(encoding="utf-8"))   # raw file: includes inactive + demo rows
    bid = (request.form.get("buyer_id") or "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,40}", bid):
        return redirect(url_for("admin", token=ADMIN_TOKEN, err="buyer id: 3-40 chars of a-z, 0-9, _ or - (starts with a letter)"))
    b = next((x for x in buyers if x["id"] == bid), None)
    if b and b.get("is_demo"):
        return redirect(url_for("admin", token=ADMIN_TOKEN, err=f"{bid} is a demo placeholder - demo buyers are managed in the file only"))
    isNew = b is None
    b = dict(b) if b else {"id": bid, "is_demo": False, "priority": 1, "exclusive": True,
                           "price_by_tier": {"A": 0, "B": 0, "C": 0}, "delivery": {"method": "outbox", "email": ""}}
    action = request.form.get("action", "save")
    if action == "rotate_token":
        # issue a FRESH portal token; the buyer's old link stops working immediately (stolen-link control).
        # Never flips active state and never changes any other field beyond what this form carries.
        b["portal_token"] = secrets.token_urlsafe(24)
        buyers = [x for x in buyers if x["id"] != bid] + [b]
        _save_buyers(buyers)
        return redirect(url_for("admin", token=ADMIN_TOKEN, saved=f"portal token rotated for {bid} - copy the new link to the buyer"))
    # editable fields (never overwriting id / tokens on update unless explicitly supplied)
    b["name"] = (request.form.get("name") or b.get("name") or "").strip()
    b["license_number"] = (request.form.get("license_number") or b.get("license_number") or "").strip()
    b["npn"] = (request.form.get("npn") or b.get("npn") or "").strip()
    # buyer CONTACT (who the operator actually reaches): one small dict beside the application snapshot,
    # so an operator-created buyer is exactly as contactable as one who applied. Blank input keeps the
    # saved value — the standalone Activate/Deactivate buttons post no fields at all.
    _c = dict(b.get("contact") or {})
    for _k, _f in (("name", "contact_name"), ("email", "contact_email"), ("phone", "contact_phone")):
        _v = (request.form.get(_f) or "").strip()
        if _v:
            _c[_k] = _v[:160]
    if _c.get("email") and ("@" not in _c["email"] or "." not in _c["email"]):
        return redirect(url_for("admin", token=ADMIN_TOKEN, err="the contact email is not an email address", f=bid))
    if _c:
        b["contact"] = _c
    # "leave unknown fields blank to keep current values": a field the operator did not touch
    # (e.g. the standalone Activate/Deactivate button, which posts only buyer_id+action) must NEVER
    # silently reset a configured buyer to the new-draft defaults — a re-activated buyer would start
    # selling with whatever products/states the form defaulted to instead of the ones recorded.
    def _csv(field, cur, allowed, fallback):
        sent = request.form.get(field)
        cell = sent if (sent is not None and sent.strip() != "") else ",".join(cur or [])
        vals = [x.strip() for x in cell.split(",") if x.strip() in allowed] if cell else []
        return vals or (cur if cur else fallback)
    _states_sent = [s.strip().upper() for s in (request.form.get("licensed_states") or "").split(",") if s.strip()]
    b["licensed_states"] = _states_sent or (
        [s.strip().upper() for s in ",".join(b.get("licensed_states") or []).split(",") if s.strip()] or b.get("licensed_states") or [])
    b["products"] = _csv("products", b.get("products"), _ALLOWED_PRODUCTS,
                         b.get("products") or ["health_aca"])
    b["tiers"] = _csv("tiers", b.get("tiers"), _ALLOWED_TIERS, ["A", "B"])
    for t in _ALLOWED_TIERS:
        v = request.form.get(f"price_{t}")
        if v is not None and v.strip() != "":
            try:
                b["price_by_tier"][t] = round(float(v), 2)
            except ValueError:
                pass
    for k, cast in (("daily_cap", int), ("monthly_cap", int), ("age_min", int), ("age_max", int), ("return_window_hours", float)):
        if request.form.get(k, "").strip():
            try:
                v_ = cast(request.form[k])
                if v_ > 0:
                    b[k] = v_
            except ValueError:
                pass
    if not b.get("portal_token") or _placeholder(b.get("portal_token")):
        b["portal_token"] = secrets.token_urlsafe(24)          # issued once, here - never emailed/system-logged
    if request.form.get("delivery_method") in _ALLOWED_DELIVERY:
        b["delivery"]["method"] = request.form["delivery_method"]
    if request.form.get("delivery_email", "").strip():
        b["delivery"]["email"] = request.form["delivery_email"].strip()
    if request.form.get("delivery_webhook", "").strip():
        _u = request.form["delivery_webhook"].strip()
        if not re.match(r"^https?://[a-z0-9.][^\s]{3,}$", _u, re.IGNORECASE) or not _https_url(_u):
            return redirect(url_for("admin", token=ADMIN_TOKEN, err="webhook_url must be a valid https:// URL - consumer contact data is never posted over plain http"), code=302)
        b["delivery"]["webhook_url"] = _u
    if action == "activate":
        errs = _onboard_errors(b)
        if errs:
            return redirect(url_for("admin", token=ADMIN_TOKEN, err=", ".join(errs), f=bid))
        b["active"] = True
        # webhook buyers get a signing secret ONCE, at activation: every lead POST is HMAC-SHA256
        # signed with it (x-ticket-signature header) so the CRM can prove the POST is authentic and
        # the payload unmodified. Rotating = delete the field here and activate again.
        if b["delivery"].get("method") == "webhook" and not b["delivery"].get("webhook_secret"):
            b["delivery"]["webhook_secret"] = secrets.token_urlsafe(32)
        b.pop("_activate_when", None)
        # commercial trail: activation is a recorded stage, so the buyer pipeline answers "when did
        # this prospect come live" from the SAME append-only history (never a separate field).
        try:
            bpline.record_action(con, bid, buyers, stage="ACTIVATED")
        except Exception:
            pass                                   # the pipeline trail must never block the onboarding action
    elif action == "deactivate":
        b["active"] = False
        if b.get("is_demo"):
            b["active"] = True  # demo buyers stay active so the funnel can be tested end to end
    else:
        # draft save: an INVALID draft stays inactive rather than silently going live
        if isNew or not b.get("active"):
            b["active"] = False
    buyers = [x for x in buyers if x["id"] != bid] + [b]    # keep file order = activation order at the bottom
    _save_buyers(buyers)
    return redirect(url_for("admin", token=ADMIN_TOKEN, saved=f"{action} {bid}"))


# ----------------------------------------------------------------- admin: distribution lab / quality / partners / content
@app.route("/admin/lab", methods=["GET", "POST"])
def admin_lab():
    _admin()
    if request.method == "POST":
        if request.form.get("update"):
            eid = request.form["update"]
            con.execute("UPDATE experiments SET status=?, cost=?, learning=? WHERE experiment_id=?",
                        (request.form.get("status"), float(request.form.get("cost") or 0), request.form.get("learning", ""), eid))
            con.commit()
        else:
            d_ = request.form.to_dict()
            seg = next((x for x in PLAYBOOK.get("segments", []) if x["segment"] == d_.get("audience")), None)
            if seg and not d_.get("demand_trigger"):
                d_["demand_trigger"] = seg["circumstance"].split(".")[0]
            lab.create(con, d_)
        return redirect(url_for("admin_lab", token=ADMIN_TOKEN))
    rows = lab.all_metrics(con)
    return render_template("admin_lab.html", rows=rows, token=ADMIN_TOKEN, tab="lab", angles=HEALTH_FLOW["angles"],
                           segments=[x["segment"] for x in PLAYBOOK.get("segments", [])], phone_copy=HEALTH_FLOW.get("phone_copy", {}),
                           partners=[dict(r) for r in con.execute("SELECT partner_id, display_name FROM partners ORDER BY display_name")],
                           channels=json.loads((CFG / "channels.json").read_text(encoding="utf-8")), statuses=lab.STATUSES, base=SITE["base_url"])


@app.get("/admin/funnel")
def admin_funnel():
    _admin()
    dim, test = request.args.get("dim", "all"), request.args.get("test", "real")
    rows = funnel.by(con, dim, test)
    tot = funnel.stage_totals(rows)
    # the acquisition split sits ABOVE this funnel:MARKET rows are market intelligence (news,
    # official datasets, published pages); INTEREST rows are person-shaped public posts that
    # show WHERE the demand conversation lives. Neither is ever a lead — the funnel below
    # counts only the funnel's own stages, which start when a person voluntarily visits US.
    _scon = signals_mod.connect(SIGNALS_DB)
    try:
        sigk = signals_mod.kind_counts(_scon)
        interest_rows = signals_mod.queue_filtered(_scon, "NEW", kind="INTEREST", limit=5)
        # per-source supply split for the SAME panel: fresh = observed within 30 days, and the
        # intent categories inside the fresh INTEREST layer (shopping vs question vs news...)
        sigsrc = [dict(r) for r in _scon.execute(
            "SELECT source, COUNT(*) n, SUM(status='NEW') new, "
            "SUM(CASE WHEN COALESCE(observed_at,'') >= datetime('now','-30 day') THEN 1 ELSE 0 END) fresh "
            "FROM signals GROUP BY source ORDER BY n DESC")]
        sigcats = [dict(r) for r in _scon.execute(
            "SELECT intent_cat, COUNT(*) n FROM signals WHERE kind='INTEREST' AND "
            "COALESCE(observed_at,'') >= datetime('now','-30 day') AND intent_cat != '' GROUP BY 1 ORDER BY n DESC")]
    finally:
        _scon.close()
    # content layer (status counts from the same leads.db content_assets table): produced =
    # drafted from reviewed signals; published = flipped DEPLOYED by hand after real posting.
    try:
        content_stats = {r[0]: r[1] for r in con.execute(
            "SELECT status, COUNT(*) FROM content_assets GROUP BY 1")}
    except Exception:                                   # noqa: BLE001 - table may not exist yet
        content_stats = {}
    return render_template("admin_funnel.html", rows=rows, dim=dim, dims=list(funnel.EV_DIM), test=test,
                           stages=funnel.STAGES, labels=funnel.LABELS, ev=lambda r: funnel.evidence(r, test), token=ADMIN_TOKEN, tab="funnel",
                           tot=tot, drops=funnel.stop_points(tot), sigk=sigk, interest=interest_rows,
                           sigsrc=sigsrc, sigcats=sigcats, content_stats=content_stats)


PLAYBOOK = json.loads((CFG / "audience_playbook.json").read_text(encoding="utf-8")) if (CFG / "audience_playbook.json").exists() else {"segments": []}
SIGNAL_COUNTS = json.loads((CFG / "audience_signal_counts.json").read_text(encoding="utf-8")) if (CFG / "audience_signal_counts.json").exists() else {}


@app.get("/print/<name>.pdf")
def printable(name):
    """Public printables for partners (CareerSource handouts, pharmacy counter card)."""
    if name not in ("handout_job_en", "handout_job_es", "handout_separating_en", "counter_jan_es"):
        abort(404)
    f = BASE / "assets" / "print" / f"{name}.pdf"
    return Response(f.read_bytes(), mimetype="application/pdf") if f.exists() else abort(404)


@app.route("/admin/organic", methods=["GET", "POST"])
def admin_organic():
    """Engine 1 queue: every organic/partner action, its status, effort and its own REAL results."""
    _admin()
    if request.method == "POST":
        f_ = request.form
        ops_mod.log(con, f_["item_id"], f_.get("action", "NOTE"), f_.get("url", ""), f_.get("minutes") or 0, f_.get("cost") or 0, f_.get("note", ""))
        return redirect(url_for("admin_organic", token=ADMIN_TOKEN, test=request.args.get("test", "0")) + "#" + f_["item_id"])
    t = 1 if request.args.get("test") == "1" else 0
    rows = ops_mod.board(con, CFG, SITE["base_url"], t)
    tot = {k: round(sum(r[k] for r in rows), 2) for k in ("minutes", "cost_usd", "visits", "leads", "accepted")}
    return render_template("admin_organic.html", rows=rows, tot=tot, actions=ops_mod.ACTIONS, token=ADMIN_TOKEN, tab="organic", test=t,
                           hourly=json.loads((CFG / "engines.json").read_text(encoding="utf-8"))["decision_rules"].get("hourly_value_usd", 20))


@app.get("/admin/organic.csv")
def admin_organic_csv():
    _admin()
    rows = ops_mod.board(con, CFG, SITE["base_url"], 0)
    buf = io.StringIO()
    cols = ["id", "engine", "type", "where", "category", "priority", "status", "post_url", "minutes", "cost_usd", "visits", "check_done", "leads", "accepted", "returned", "link"]
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=organic_queue.csv"})


@app.route("/admin/opportunities", methods=["GET", "POST"])
def admin_opportunities():
    _admin()
    if request.method == "POST":
        st, res = request.form.get("status"), (request.form.get("result") or "").strip()
        if st == "DISPROVEN" and not res:
            abort(400)
        opp_mod.set_status(con, request.form["opp_id"], st, res)
        return redirect(url_for("admin_opportunities", token=ADMIN_TOKEN))
    f = request.args.get("f", "all")
    rows = opp_mod.evaluate(con, CFG)
    rows = [o for o in rows if f == "all" or (f == "hidden") == bool(o.get("hidden"))]
    return render_template("admin_opportunities.html", rows=rows, f=f, as_of=opp_mod.load(CFG).get("as_of"), token=ADMIN_TOKEN, tab="opportunities")


@app.get("/admin/audiences")
def admin_audiences():
    _admin()
    return render_template("admin_audiences.html", rows=lab.audience_status(con, PLAYBOOK, SIGNAL_COUNTS), note=PLAYBOOK.get("note", ""),
                           token=ADMIN_TOKEN, tab="audiences")


@app.get("/admin/lab/<eid>")
def admin_lab_detail(eid):
    _admin()
    if not con.execute("SELECT 1 FROM experiments WHERE experiment_id=?", (eid,)).fetchone():
        abort(404)
    test = request.args.get("test", "real")
    m = lab.metrics(con, eid)
    splits = {d: funnel.by(con, d, test, experiment=eid) for d in ("phone_variant", "landing", "language", "angle", "source")}
    leads = [dict(r) for r in con.execute("""SELECT lead_id, created_at, lang, need_path, lead_type, phone_valid, phone_variant, entry_page, utm_source,
        qual_status, routing_status, agent_status FROM leads WHERE experiment_id=? AND is_test=? ORDER BY created_at DESC LIMIT 200""",
                                          (eid, 0 if test == "real" else 1))]
    return render_template("admin_lab_detail.html", m=m, e=m["experiment"], splits=splits, leads=leads, test=test, stages=funnel.STAGES,
                           labels=funnel.LABELS, base=SITE["base_url"], token=ADMIN_TOKEN, tab="lab",
                           phone_copy=HEALTH_FLOW.get("phone_copy", {}))


@app.get("/admin/lab.csv")
def admin_lab_csv():
    _admin()
    rows = lab.all_metrics(con)
    buf = io.StringIO()
    cols = ["experiment_id", "name", "status", "evidence", "audience", "demand_trigger", "channel", "source", "medium", "content_concept", "creative",
            "offer_cta", "landing_page", "angle", "language", "campaign", "tracking_id", "phone_variant", "start_date", "end_date", "partner_id",
            "cost", "clicks", "landing_page_visits", "form_starts", "check_completions", "phone_step_views", "phones_submitted", "valid_phones", "consented_phone_leads", "form_completions", "qualified_leads", "invalid_leads",
            "agent_feedback", "appointments", "sales", "cost_per_lead", "cost_per_phone_lead", "cost_per_qualified_lead", "lead_value", "revenue_per_lead", "roi", "tracking_link", "hypothesis", "learning"]
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        e = r["experiment"]
        w.writerow({**e, **r["real"], "evidence": r["evidence"], "tracking_link": f"{SITE['base_url']}/go/{e['experiment_id'].lower()}"})
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=distribution_lab.csv"})


@app.get("/admin/quality")
def admin_quality():
    _admin()
    dim = request.args.get("dim", "source")
    test = request.args.get("test", "real")
    return render_template("admin_quality.html", rows=quality.by(con, dim, test), dim=dim, dims=list(quality.DIMENSIONS), test=test,
                           token=ADMIN_TOKEN, tab="quality")


@app.get("/admin/acquisition")
def admin_acquisition():
    """Partner & placement acquisition performance — existing attribution only, no invented numbers."""
    _admin()
    t = 1 if request.args.get("test") == "1" else 0
    prs, gap = partners_mod.performance(con, t)
    items, untracked = ops_mod.performance(con, CFG, SITE["base_url"], t)
    return render_template("admin_acquisition.html", partners=prs, gap=gap, items=items, untracked=untracked,
                           token=ADMIN_TOKEN, tab="acquisition", test=t)


@app.get("/admin/sources")
def admin_sources():
    """Arrival report per acquisition source: visitors → check started → completed → contact submitted
    → valid contact → consent+phone → qualified → routing-eligible → delivered. Real and TEST traffic
    are separate tabs because a TEST row says nothing about consumer behaviour; internal screens and
    bot user-agents never reach this table at all (they are dropped at ingest)."""
    _admin()
    t = 1 if request.args.get("test") == "1" else 0
    rows = funnel.source_table(con, "test" if t else "real")
    by_channel = funnel.source_table(con, "test" if t else "real", dim="channel")
    by_campaign = funnel.source_table(con, "test" if t else "real", dim="campaign")
    split = funnel.traffic_split(con)
    tot = funnel.stage_totals(rows)
    return render_template("admin_sources.html", rows=rows, split=split, tot=tot, by_channel=by_channel,
                           by_campaign=by_campaign, stages=funnel.ARRIVAL_STAGES, token=ADMIN_TOKEN,
                           tab="acquisition", test=t)


@app.get("/admin/acquisition.csv")
def admin_acquisition_csv():
    _admin()
    kind = request.args.get("kind", "placements")
    if kind == "partners":
        cols = ["partner_id", "display_name", "type", "audience", "language", "status", "visits", "form_starts",
                "check_done", "asked_help", "consented", "qual_pct", "qualified", "dup", "delivered", "sold",
                "returned", "bad_info", "dnc", "email_only", "revenue"]
        rows, _gap = partners_mod.performance(con, 0)
        fname = "partner_performance.csv"
    else:
        cols = ["id", "category", "where", "visits", "form_starts", "check_done", "consented", "qual_pct",
                "qualified", "dup", "delivered", "sold", "returned", "bad_info", "dnc", "revenue",
                "minutes", "cost_usd", "cost_per_qualified"]
        rows = ops_mod.performance(con, CFG, SITE["base_url"], 0)[0]
        fname = "placement_performance.csv"
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: "" if r.get(k) is None else r[k] for k in cols})
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={fname}"})


@app.get("/admin/pipeline")
def admin_pipeline():
    """Partner follow-up action queue: overdue / due-today / awaiting-first-contact / activation-ready first,
    with each partner's existing acquisition metrics inline so outreach effort follows performance."""
    _admin()
    rows = partners_mod.pipeline(con)
    metrics = {r["partner_id"]: r for r in partners_mod.performance(con, 0)[0]}
    counts = {}
    for r in rows:
        counts[r["bucket"]] = counts.get(r["bucket"], 0) + 1
    pri = partners_mod.prioritize(con)
    return render_template("admin_pipeline.html", rows=rows, counts=counts, metrics=metrics,
                           pri=pri, EV=partners_mod.EV_STYLES,
                           stages=partners_mod.STAGES, stg_label=partners_mod.STAGE_LABEL, methods=partners_mod.METHODS,
                           today=partners_mod.date.today().isoformat(), token=ADMIN_TOKEN, tab="pipeline")


@app.get("/admin/pipeline/<pid>")
def admin_pipeline_partner(pid):
    """One partner's pipeline card: stage, all recorded outreach history (append-only), the QR/guide/stats
    assets that already exist, and the partner's real acquisition metrics."""
    _admin()
    p = _partner_db(pid) or abort(404)
    h = partners_mod.history(con, pid)
    stage = partners_mod.stage_of(p)
    metrics = next((x for x in partners_mod.performance(con, 0)[0] if x["partner_id"] == pid), None)
    return render_template("admin_pipeline_detail.html", p=p, stage=stage, hist=h, metrics=metrics,
                           stages=partners_mod.STAGES, stg_label=partners_mod.STAGE_LABEL, methods=partners_mod.METHODS,
                           base=SITE["base_url"], token=ADMIN_TOKEN, tab="pipeline")


@app.post("/admin/pipeline/<pid>/action")
def admin_pipeline_action(pid):
    """Record what the operator ALREADY did: a stage change, logged outreach, or follow-up schedule.
    Nothing is sent to anyone. Validation errors abort 400; identical duplicate submissions are detected
    and reported without writing a second record."""
    _admin()
    _partner_db(pid) or abort(404)
    f = request.form
    msgs, errs = partners_mod.record_action(con, pid, stage=f.get("stage", ""), method=f.get("method", ""),
                                            outcome=f.get("outcome", ""), next_action=f.get("next_action", ""),
                                            next_followup=f.get("next_followup", ""), note=f.get("note", ""))
    if errs:
        abort(400, description="; ".join(errs))
    back = url_for("admin_pipeline_partner", pid=pid, token=ADMIN_TOKEN, saved=" · ".join(msgs) or "?")
    if "return" in f:
        back = url_for("admin_pipeline", token=ADMIN_TOKEN, saved=f"{pid}: " + (" · ".join(msgs) or "updated"))
    return redirect(back, 302)


# --------------------------------------------------- admin: prospective-BUYER pipeline (commercial ops)
# The selling side mirrors the partner pipeline: applications arrive at /for-agents, and everything
# the operator actually does between application and activation is recorded APPEND-ONLY. Nothing here
# contacts anyone or activates anything — activation stays on /admin/buyers with its own gates.
@app.get("/admin/buyer-pipeline")
def admin_buyer_pipeline():
    """Prospect-buyer follow-up queue: OVERDUE / DUE_TODAY / PILOT_READY / awaiting-first-step first,
    each row showing the same eligibility blockers the routing engine computes (activation, pricing,
    verification) so the pipeline never marks a prospect commercially ready that the engine still refuses."""
    _admin()
    buyers = json.loads((CFG / "buyers.json").read_text(encoding="utf-8"))
    bstat, _ = _buyer_state_rows()
    bstat_by_id = {x["id"]: x for x in bstat}
    rows = bpline.rows(con, buyers)
    for r in rows:                    # the SAME blocker lines routing computes — never a pipeline-only opinion
        r["blocked_line"] = "; ".join(bstat_by_id.get(r["buyer"]["id"], {}).get("blockers") or [])
        r["next_hint"] = (bstat_by_id.get(r["buyer"]["id"], {}) or {}).get("next") or ""
    counts = bpline.buckets(rows)
    # the commercial funnel in ONE place: the prospect ladder (pre-onboarding) and the live
    # buyer ladder (onboarding → active → paused/ineligible), both from RECORDED state only.
    pcon = prospects_mod.connect(PROSPECTS_DB)
    try:
        pcounts = prospects_mod.stage_counts(pcon)
    finally:
        pcon.close()
    inactive_real = sum(1 for b in buyers if not b.get("is_demo") and not b.get("active"))
    active_real = sum(1 for b in buyers if not b.get("is_demo") and b.get("active"))
    return render_template("admin_buyer_pipeline.html", rows=rows, counts=counts, funnel=bpline.funnel(con, buyers),
                           pcounts=pcounts, inactive_real=inactive_real, active_real=active_real,
                           stages=bpline.STAGES, stg_label=bpline.STAGE_LABEL, methods=bpline.METHODS,
                           token=ADMIN_TOKEN, tab="bpipeline",
                           saved=request.args.get("saved", ""), errs=request.args.getlist("err"))


@app.get("/admin/buyer-pipeline.csv")
def admin_buyer_pipeline_csv():
    """The prospect queue as CSV — same rows, buckets and blockers as the on-screen queue (the SAME
    routing-engine blocker lines, and demo placeholders still excluded). Stable columns for spreadsheets;
    nothing here is a coaching, contact or activation action."""
    _admin()
    buyers = json.loads((CFG / "buyers.json").read_text(encoding="utf-8"))
    bstat, _ = _buyer_state_rows()
    bstat_by_id = {x["id"]: x for x in bstat}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["buyer_id", "name", "stage", "bucket", "next_followup", "last_outreach_at", "outreach_method",
                "last_note", "recorded_blockers", "next_concrete_step"])
    for r in bpline.rows(con, buyers):
        st = bstat_by_id.get(r["buyer"]["id"], {})
        w.writerow([r["buyer"]["id"], r["buyer"].get("name") or "", r["stage"], r["bucket"], r["next_followup"],
                    r["last_outreach_at"], r["outreach_method"], (r["last_note"] or "").replace("\n", " ").replace("\r", " ")[:500],
                    "; ".join(st.get("blockers") or []), st.get("next") or ""])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=buyer-pipeline-{now()[:10]}.csv"})


@app.post("/admin/buyer-pipeline/<bid>/action")
def admin_buyer_pipeline_action(bid):
    """Record what the operator ALREADY did with a prospect buyer: a stage change, logged outreach
    method+note, or a follow-up date. Nothing is sent. Validation errors abort 400 so bad input is
    loud; identical resubmits within the same minute are detected and never become a second record."""
    _admin()
    buyers = json.loads((CFG / "buyers.json").read_text(encoding="utf-8"))
    if not next((b for b in buyers if b["id"] == bid), None):
        abort(404)
    f = request.form
    msgs, errs = bpline.record_action(con, bid, buyers, stage=f.get("stage", ""), method=f.get("method", ""),
                                      next_followup=f.get("next_followup", ""), note=f.get("note", ""))
    if errs:
        abort(400, description="; ".join(errs))
    saved = f"{bid}: " + (" · ".join(msgs) or "updated")
    return redirect(url_for("admin_buyer_pipeline", token=ADMIN_TOKEN, saved=saved), 302)


@app.get("/admin/buyer-pipeline/<bid>")
def admin_buyer_pipeline_detail(bid):
    """One prospect's card: full append-only history, the recorded onboarding state from buyers.json,
    and the routing blockers (if any) — the same truth the queue row shows, without duplicated logic."""
    _admin()
    buyers = json.loads((CFG / "buyers.json").read_text(encoding="utf-8"))
    b = next((x for x in buyers if x["id"] == bid and not x.get("is_demo")), None) or abort(404)
    bstat, _ = _buyer_state_rows()
    st = next((x for x in bstat if x["id"] == bid), None) or {}
    stage = bpline.stage_of(dict(b), con)
    return render_template("admin_buyer_pipeline_detail.html", b=b, st=st, stage=stage,
                           stg_label=bpline.STAGE_LABEL, hist=bpline.history(con, bid),
                           stages=bpline.STAGES, methods=bpline.METHODS, token=ADMIN_TOKEN, tab="bpipeline")


@app.get("/admin/readiness")
def admin_readiness():
    """ONE read-only screen that answers the operator question 'what actually stands between us
    and activation?'. Three sources, deliberately kept separate so nothing is overstated:

      1. LAUNCH BLOCKERS — live recomputation of the same checks launch_check.py prints on the
         CLI, rendered here against THIS app's config copy. The CLI and this page can never drift.
      2. BUYER READINESS LADDER — per buyer: configured → verified → active → rehearsed → capable,
         from recorded state only (the same helper the overview table uses).
      3. RECORDED ACTIVITY + DEMAND — TEST vs real counts, unmatched queue and WHY (routing
         reasons), ledger split. Recorded facts, never projections.

    No action buttons here: everything actionable lives on the pages that own it (buyers, credits,
    pipeline). This page just names the blocker and links to the right page — so a "ready" claim
    is impossible to fake and a blocker impossible to miss."""
    _admin()
    import launch_check
    lblock, lcat, lbase = launch_check.collect(cfg_dir=CFG)
    bstat, buyers_all = _buyer_state_rows()
    real = [b for b in bstat if not b["is_test_banner"]]
    split = {}
    for istest in (1, 0):
        rows = [dict(r) for r in con.execute(
            "SELECT COUNT(*) n, SUM(CASE WHEN qual_status='QUALIFIED' THEN 1 ELSE 0 END) q, "
            "SUM(CASE WHEN routing_status='DELIVERED' THEN 1 ELSE 0 END) d, "
            "SUM(CASE WHEN routing_status='RETURNED' THEN 1 ELSE 0 END) r FROM leads WHERE is_test=?", (istest,))][0]
        sold = con.execute("SELECT COUNT(*) FROM lead_outcomes o JOIN leads l ON l.lead_id=o.lead_id "
                           "WHERE l.is_test=? AND o.outcome='SOLD'", (istest,)).fetchone()[0]
        split[istest] = {"submitted": rows["n"], "qualified": rows["q"] or 0, "delivered": rows["d"] or 0,
                         "returned": rows["r"] or 0, "sold": sold,
                         "ledger": round(con.execute("SELECT COALESCE(SUM(amount),0) FROM buyer_ledger WHERE is_test=?",
                                                     (istest,)).fetchone()[0] or 0.0, 2)}
    # unmatched demand right now, counted the SAME way the overview's Unmatched section counts it:
    # qualified, consented REAL leads that had no eligible buyer at their last routing attempt.
    unmatched_now = con.execute(
        "SELECT COUNT(*) FROM leads WHERE is_test=0 AND routing_status='NO_BUYER' AND qual_status='QUALIFIED'").fetchone()[0]
    reasons = [dict(r) for r in con.execute("""
        SELECT decision, reason, COUNT(*) n FROM routing_decisions
        WHERE decision != 'ASSIGNED' GROUP BY decision, reason ORDER BY n DESC""")]
    no_buyer_q = [dict(r) for r in con.execute(
        "SELECT lead_id, created_at, campaign, state, price FROM leads WHERE routing_status='NO_BUYER' "
        "AND qual_status='QUALIFIED' ORDER BY created_at DESC LIMIT 20")]
    import engine.payments as _pay
    modes = {"rail": _pay.mode(),
             "env": os.environ.get("LEADENGINE_ENV", "staging"),
             "live_delivery": os.environ.get("LEADENGINE_LIVE_DELIVERY") == "1",
             "indexing": os.environ.get("ALLOW_INDEXING") == "1",
             "enforce_credits": _credit_enforcing(),
             # deployment facts from engine/hosting.py: can this host keep what it collects, and where
             # did the public origin come from (BASE_URL, the host's assigned URL, or config)?
             "storage": STORAGE, "read_only_preview": READONLY_PREVIEW,
             "base_url_source": SITE.get("_base_url_source") or "unset"}
    return render_template("admin_readiness.html", real=real, lblock=lblock, lcat=lcat, lbase=lbase,
                           token=ADMIN_TOKEN, tab="readiness", split={1: split[1], 0: split[0]},
                           unmatched_now=unmatched_now, reasons=reasons, no_buyer_q=no_buyer_q,
                           modes=modes, base=SITE["base_url"])


@app.get("/admin/economics")
def admin_economics():
    """One money screen, two unmistakably different sections:
      (1) RECORDED — every number below comes from the buyer_ledger, the spend imports and the
          leads table, split TEST vs real. Cross-checks (charges ledger vs delivered leads,
          credits vs returned leads) expose billing drift ON the page instead of in a diff.
      (2) SCENARIO — a calculator over EDITABLE assumption fields. Clearly labeled: it answers
          'what would this pricing do' — it is never a result, projection or forecast.
    No external contact, no activation, no purchase of anything: read-only + arithmetic.
    """
    _admin()
    def _kinds(istest: int) -> list[dict]:
        return [dict(r) for r in con.execute("SELECT kind, COUNT(*) n, COALESCE(SUM(amount),0) amt "
                                             "FROM buyer_ledger WHERE is_test=? GROUP BY kind ORDER BY kind", (istest,))]
    money = {}
    for istest in (1, 0):
        k = _kinds(istest)
        def _get(kind):
            row = next((x for x in k if x["kind"] == kind), None)
            return (row["n"], round(row["amt"], 2)) if row else (0, 0.0)
        delivered = con.execute("SELECT COUNT(*) FROM leads WHERE is_test=? AND routing_status='DELIVERED' AND price > 0", (istest,)).fetchone()[0]
        # a "returned" lead is a buyer-submitted RETURNED outcome (credit_requests' population), NOT
        # a routing status: returned leads can be re-offered, so status alone undercounts/overcounts.
        returned = con.execute("SELECT COUNT(DISTINCT l.lead_id) FROM leads l JOIN lead_outcomes x ON x.lead_id = l.lead_id "
                               "WHERE l.is_test=? AND x.outcome='RETURNED'", (istest,)).fetchone()[0]
        ch_n, ch_amt = _get("LEAD_CHARGE"); cr_n, cr_amt = _get("LEAD_CREDIT")
        money[istest] = {"kinds": k, "delivered": delivered, "returned": returned,
                         "charges": (ch_n, ch_amt), "credits": (cr_n, cr_amt),
                         "packages": _get("PACKAGE_PURCHASE"), "adjustments": _get("ADJUSTMENT"),
                         # reconcile: the ledger vs the leads table must tell the SAME story
                         "recon_charge": ("OK" if ch_n == delivered else f"{delivered} delivered-with-price vs {ch_n} ledger rows"),
                         "recon_credit": ("OK" if cr_n == returned else f"{returned} returned leads vs {cr_n} ledger credits"),
                         "rev_per_delivered": round(-ch_amt / delivered, 2) if delivered else None}
    spend_rows = con.execute("SELECT COUNT(*) n, COALESCE(SUM(amount),0) amt FROM spend").fetchone()
    buyers_priced = [{"id": b["id"], "active": bool(b.get("active")), "tiers": b.get("tiers") or [],
                      "prices": {t: (b.get("price_by_tier") or {}).get(t) for t in (b.get("tiers") or [])}}
                     for b in json.loads((CFG / "buyers.json").read_text(encoding="utf-8")) if not b.get("is_demo")]
    outcomes = [dict(r) for r in con.execute("SELECT o.outcome, COUNT(*) n FROM lead_outcomes o "
                                             "GROUP BY o.outcome ORDER BY n DESC")]
    # ---- SCENARIO calculator (GET form; defaults come from recorded config, nothing else) ----
    pos_prices = sorted({p for b in buyers_priced for p in b["prices"].values()
                         if isinstance(p, (int, float)) and p > 0})
    def _f(name, default):
        try:
            return float(request.args.get(name) or default)
        except (TypeError, ValueError):
            return default
    scn = {"leads": max(0.0, _f("leads", 2.0)),
           "price": max(0.0, _f("price", pos_prices[0] if pos_prices else 0.0)),
           "return_pct": min(100.0, max(0.0, _f("return_pct", 10.0)))}
    gross = scn["leads"] * scn["price"]
    refunds = gross * scn["return_pct"] / 100.0
    scn["daily_gross"], scn["daily_refunds"], scn["daily_net"] = round(gross, 2), round(refunds, 2), round(gross - refunds, 2)
    scn["weekly_net"] = round(scn["daily_net"] * 7, 2)
    scn["packs"] = [{**p, "days": round(p["amount_usd"] / scn["daily_gross"], 1) if scn["daily_gross"] else None}
                    for p in MARKETPLACE.get("packages", [])]
    return render_template("admin_economics.html", money=money, spend=(spend_rows["n"], round(spend_rows["amt"], 2)),
                           buyers=buyers_priced, outcomes=outcomes, scn=scn, packages=MARKETPLACE.get("packages", []),
                           token=ADMIN_TOKEN, tab="econ")


@app.route("/admin/partners", methods=["GET", "POST"])
def admin_partners():
    _admin()
    if request.method == "POST":
        d = request.form.to_dict()
        d["cobrand_approved"] = bool(d.get("cobrand_approved"))
        partners_mod.create(con, d)
        return redirect(url_for("admin_partners", token=ADMIN_TOKEN))
    # Two separate queues, because they are two different jobs: the referral network (837 registry
    # locations, worked in bulk from out/network_kits.csv) and hand-built partners. Showing the
    # network inside the outreach queue buried the partners an operator actually chose.
    scope = request.args.get("scope") or "outreach"
    where = "WHERE partner_id LIKE ?" if scope == "network" else "WHERE partner_id NOT LIKE ?"
    rows = []
    for r in con.execute(f"SELECT * FROM partners {where} ORDER BY created_at DESC", ("net-%",)):
        d = dict(r)
        d.update(partners_mod.stats(con, d["partner_id"]))
        rows.append(d)
    counts = {"outreach": con.execute("SELECT COUNT(*) FROM partners WHERE partner_id NOT LIKE 'net-%'").fetchone()[0],
              "network": con.execute("SELECT COUNT(*) FROM partners WHERE partner_id LIKE 'net-%'").fetchone()[0]}
    return render_template("admin_partners.html", rows=rows, token=ADMIN_TOKEN, tab="partners", types=partners_mod.TYPES,
                           angles=HEALTH_FLOW["angles"], base=SITE["base_url"], scope=scope, counts=counts)


@app.get("/admin/partners/<pid>/guide.pdf")
def admin_partner_guide(pid):
    _admin()
    p_ = _partner_db(pid) or abort(404)
    import build_partner_kit as bpk
    guide = bpk.GUIDE_ES if p_["language"] == "es" else bpk.GUIDES.get(p_["audience"], bpk.GUIDES["business"])
    try:
        pdf = partners_mod.guide_pdf(p_, guide, SITE["base_url"], SITE, bpk.fpl_rows)
    except partners_mod.PdfUnavailable as e:
        # Optional surface: the operator must see the real cause and a fix, not a 500.
        # Known cause on Windows: running from a path so long that Pillow's DLL check fails.
        msg = (str(e) + " — Fix: run the app from a shorter directory path; "
               "the HTML guide and the QR still work.")
        return redirect(url_for("admin_partners", token=ADMIN_TOKEN, err=msg))
    return Response(pdf, mimetype="application/pdf", headers={"Content-Disposition": f"inline; filename=guide_{pid}.pdf"})


@app.post("/admin/partners/<pid>/status")
def admin_partner_status(pid):
    _admin()
    con.execute("UPDATE partners SET status=?, cobrand_approved=? WHERE partner_id=?",
                (request.form.get("status"), 1 if request.form.get("cobrand_approved") else 0, pid))
    con.commit()
    return redirect(url_for("admin_partners", token=ADMIN_TOKEN))


@app.get("/admin/assets")
def admin_assets():
    """ACQUISITION ASSET QUEUE: reviewed intent signals become publishable EN/ES content drafts,
    each carrying ONE tracked /go link into the real Check. The queue itself only ever PREPARES
    and stores drafts locally: approving flips the copy into the operator's launch list, and
    DEPLOYED is a status the operator sets AFTER they have themselves posted the copy on their
    own channel — nothing here publishes, posts, replies or contacts anyone."""
    _admin()
    scon = signals_mod.connect(SIGNALS_DB)
    try:
        ready = acq_assets.candidate_counts(scon)
        # strongest un-drafted candidates (reviewed first, then fresh NEW picks)
        cands = [dict(r) for r in scon.execute(
            "SELECT id, source, lang, topic, intent_cat, intent, status, observed_at, title, url "
            "FROM signals WHERE kind='INTEREST' AND intent_cat IN ('shopping','question') "
            "AND intent >= 2.0 "
            "ORDER BY (CASE WHEN status='REVIEWED' THEN 0 ELSE 1 END), intent DESC LIMIT 25").fetchall()]
    finally:
        scon.close()
    lcon = con
    acq_assets.ensure_schema(lcon)
    have = {r["signal_id"] for r in lcon.execute("SELECT signal_id FROM content_assets WHERE signal_id IS NOT NULL")}
    cands = [c for c in cands if c["id"] not in have]
    assets = acq_assets.list_assets(lcon)
    return render_template("admin_assets.html", assets=assets, cands=cands, ready=ready,
                           token=ADMIN_TOKEN, tab="assets", base=SITE["base_url"],
                           msg=request.args.get("msg", ""))


@app.post("/admin/assets/prepare")
def admin_assets_prepare():
    """Draft ONE asset from ONE reviewed candidate (id=<sid>), or ALL currently reviewed
    candidates (id=all). Draft-only: creates the tracked /go link row and the stored copy.
    No network action, no publish."""
    _admin()
    scon = signals_mod.connect(SIGNALS_DB)
    try:
        sid = request.form.get("id", "all")
        if sid != "all":
            r = scon.execute("SELECT id, source, lang, topic, title, url, intent, status FROM signals WHERE id=?", (sid,)).fetchone()
            rows = [dict(r)] if r else []
        else:
            rows = [dict(r) for r in scon.execute(
                'SELECT id, source, lang, topic, title, url, intent, status FROM signals '
                "WHERE status IN ('REVIEWED','ACTIONED') AND kind='INTEREST' "
                "AND intent_cat IN ('shopping','question') AND intent >= 2.0 LIMIT 40").fetchall()]
    finally:
        scon.close()
    made = 0
    acq_assets.ensure_schema(con)
    for s in rows:
        if con.execute("SELECT 1 FROM content_assets WHERE signal_id=?", (s["id"],)).fetchone():
            continue
        acq_assets.draft_asset(con, s, base_url=SITE["base_url"])
        made += 1
    if request.form.get("lang") == "es" and request.form.get("topic"):
        acq_assets.draft_topic_asset(con, request.form["topic"], "es", base_url=SITE["base_url"])
        made += 1
    con.commit()
    return redirect(url_for("admin_assets", token=ADMIN_TOKEN, msg=f"prepared {made} asset(s)"), code=302)


@app.post("/admin/assets/<aid>/decision")
def admin_assets_decision(aid):
    """Operator decision on a stored draft: APPROVED (ready-to-paste launch list), SKIPPED
    (reason required), or DEPLOYED (operator states WHERE they published it — the audit trail
    for that /go link)."""
    _admin()
    status = request.form.get("status", "")
    note = (request.form.get("note") or "").strip()
    if status not in acq_assets.STATUSES:
        return redirect(url_for("admin_assets", token=ADMIN_TOKEN, msg=f"unknown status '{status}'"), code=302)
    if status in ("SKIPPED", "DEPLOYED") and not note:
        return redirect(url_for("admin_assets", token=ADMIN_TOKEN,
                                msg=f"{status} requires a note" + (" (where did you post it?)" if status == "DEPLOYED" else "")), code=302)
    acq_assets.ensure_schema(con)
    ok = acq_assets.set_status(con, aid, status, note)
    con.commit()
    return redirect(url_for("admin_assets", token=ADMIN_TOKEN,
                            msg=f"{aid}: {status}" if ok else f"{aid}: cannot move (DEPLOYED is final)"), code=302)


@app.get("/admin/content")
def admin_content():
    _admin()
    return render_template("admin_content.html", pages=CONTENT["pages"], guardrail=CONTENT.get("guardrail", ""), token=ADMIN_TOKEN,
                           tab="content", base=SITE["base_url"])


@app.get("/admin/content-ideas")
def admin_content_ideas():
    """Content intelligence: recurring REVIEWED/ACTIONED intent signals become DRAFT EN/ES
    content briefs (title, hook, 3-beat script, CTA) that point interested consumers at the
    Check. Human-edited drafts only — nothing here can publish, and it never proposes outreach
    to a person (signals are demand evidence, never prospects)."""
    _admin()
    scon = signals_mod.connect(SIGNALS_DB)
    try:
        ideas = content_intel.ideas(scon)
    finally:
        scon.close()
    return render_template("admin_content_ideas.html", ideas=ideas, token=ADMIN_TOKEN, tab="content-ideas")


@app.get("/admin/intel/briefs")
def admin_briefs():
    _admin()
    return render_template("admin_briefs.html", briefs=intel_mod.regional_briefs(INTEL_DB) or (json.loads((CFG / "regional_briefs.json").read_text(encoding="utf-8")) if (CFG / "regional_briefs.json").exists() else []), token=ADMIN_TOKEN, tab="briefs")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
