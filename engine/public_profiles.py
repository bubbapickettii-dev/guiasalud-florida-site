"""PUBLIC PROFILE CONTACT SURFACES — a second, independent route to a consumer contact.

WHY THIS EXISTS
    The post-text miner (engine/prospect_contacts.py) only reads what a person wrote IN the
    post. Measured result: 55 discovery rows, ZERO published contacts, because anonymous
    forum/AppView posts do not carry contact details. People DO publish contact details in
    their own PUBLIC profile (bio / custom fields) far more often than in a post body.
    This adapter joins the two facts we can verify WITHOUT any identity inference:
      * the SAME public account published a first-person health-insurance need (post URL,
        post date, exact wording), and
      * that SAME public account itself published a contact surface on its own profile page
        (profile URL, exact phrase).
    That is a public contact discovered + a need supported by evidence. It is still NOT
    consent, NOT a qualified lead, and NOTHING here ever contacts anyone.

ACCESS DISCIPLINE
    Only credential-free public endpoints of platforms whose public profile pages are
    server-rendered for anonymous readers:
      bluesky  -> app.bsky.feed.searchPosts + app.bsky.actor.getProfile (public AppView)
      mastodon -> /api/v1/timelines/tag/<tag>  + /api/v1/accounts/<id>
      lemmy    -> /api/v3/search?type_=Comments + /api/v3/user
    No login, no cookies, no key, no proxy, no bypass. One polite GET at a time, pacing
    from config. A 401/403/429 is recorded as an access restriction and the source stops
    for that run — it is never retried harder.

HARD GUARDRAILS (mirrored from prospect_contacts, then tightened)
    * email/phone only — a profile WEBSITE link alone is not a contact and is not stored.
    * .gov/.edu/.mil domains and system mailboxes are refused (extract_contacts).
    * org-shaped surfaces (agency/broker/llc/"we offer"/agencia/corredor...) are counted as
      ORGANISATION contacts and never inserted into the consumer prospect store.
    * one row per PUBLIC ACCOUNT (person_key = profile|platform|account id), not per post.
    * a contact value already published by another account is stored as DUPLICATE.
    * profile contact does NOT advance the row past CONTACT_FOUND, and CONTACT_FOUND is only
      reached when the bio/post itself invites inquiries.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import engine.signals as signals                 # public-endpoint helpers + taxonomy only
import engine.prospect_contacts as pcon         # extraction rules + store
import engine.web_extract as webx               # robots gate + visible-text reader

ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "config" / "sources.json"

ORIGINS = ("post", "profile", "profile_link")

# Need-shaped queries. Deliberately the ACTIVE-NEED shapes (decision points), because a
# profile contact is only useful when the same person is currently deciding.
DEFAULT_QUERIES = {
    "bluesky": [
        {"q": '"need health insurance"'},
        {"q": '"losing my health insurance"'},
        {"q": '"lost my health insurance"'},
        {"q": '"cannot afford health insurance"'},
        {"q": '"self employed" "health insurance"'},
        {"q": '"necesito seguro medico"'},
        {"q": '"perdi mi seguro"'},
    ],
    "mastodon": ["healthinsurance", "cobra", "obamacare", "openenrollment", "medicaid"],
    "lemmy": ["need health insurance", "losing health insurance", "cobra", "medicaid",
              "seguro medico"],
}

# surfaces that mean the account is selling rather than seeking
_ORG_MARKERS = (
    "insurance agency", "agency", "broker", "brokers", "llc", "inc.", " inc ", "corp",
    "consulting", "consultants", "we offer", "our team", "our agency", "book a call",
    "free quote", "get a quote", "licensed agent", "enrolled agent", "company",
    "empresa", "agencia", "corredor", "correduría", "correduria", "consultores",
    "te asesoramos", "ofrecemos", "cotiza con nosotros", "llámanos hoy",
)
_ACCESS_STOP = {401, 403, 429}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def profile_key(platform: str, account_id: str) -> str:
    """Stable dedupe key for a PUBLIC ACCOUNT (not a post): the same person posting twice
    collapses to one prospect row, which is what 'unique individuals identified' means."""
    return hashlib.sha1(f"profile|{platform}|{account_id}".strip().lower().encode()).hexdigest()[:24]


def sources_cfg() -> dict:
    """The full sources.json (platform instances/tags live there; this module reads only
    public endpoint settings, never paid or credential-gated blocks)."""
    try:
        return json.loads(CFG_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}


def load_cfg() -> dict:
    try:
        return (json.loads(CFG_PATH.read_text(encoding="utf-8")) or {}).get("public_profiles") or {}
    except (OSError, ValueError):
        return {}


# A NEED, not merely a mention. Live-verified false positives from the first scan: a
# game-dev thread saying studios "don't need health insurance" and a reporter explaining she
# "has options" both matched keyword+first-person. Direction of the statement matters.
_COVERAGE_TERM = re.compile(
    r"\b(insurance|health coverage|coverage|medicaid|medicare|cobra|marketplace|obamacare|"
    r"premium|premiums|subsid(?:y|ies)|open enrollment|deductible|aseguranza|seguro|"
    r"cobertura|subsidio|p[oó]liza)\b", re.IGNORECASE)
_FIRST_PERSON = re.compile(r"\b(i|i'?m|i'?ve|my|me|we|our|necesito|mi|mis|perd[ií]|busco)\b",
                           re.IGNORECASE)
_SEEK = re.compile(
    r"\b(need|needs|needed|losing|lost|lose|can'?t afford|cannot afford|afford|"
    r"looking for|shopping for|searching for|options for|help (?:me )?(?:get|find)|"
    r"apply for|enroll|sign up|qualify|qualified|necesito|busco|perd[ií]|no tengo|"
    r"me qued[eé] sin)\b", re.IGNORECASE)
_NEGATED = re.compile(
    r"\b(don'?t need|do not need|no need|didn'?t need|never needed|not saying|already have|"
    r"i have (?:health )?(?:insurance|coverage)|have options|no necesito|ya tengo|"
    r"no me hace falta)\b", re.IGNORECASE)


def need_shaped(text: str) -> bool:
    """A CURRENT, first-person need for coverage — and not its negation. Requires all of:
    a coverage term, a first-person marker, an active seek verb, no negating/already-covered
    phrasing, and a shopping/question intent category. Commenting on the system, reporting on
    it, or explaining that you are covered is not a prospect."""
    text = text or ""
    if not (_COVERAGE_TERM.search(text) and _FIRST_PERSON.search(text) and _SEEK.search(text)):
        return False
    if _NEGATED.search(text):
        return False
    return signals.intent_category(text, "") in ("shopping", "question")


def org_shaped(*parts: str) -> bool:
    """A selling surface (agency/broker/agency-language) is an ORGANISATION contact: real,
    worth outreach, but a different store and a different promise. Never a consumer."""
    low = " " + re.sub(r"\s+", " ", " ".join(p for p in parts if p).lower()) + " "
    return any(m in low for m in _ORG_MARKERS)


def evidence_phrase(text: str, value: str, width: int = 70) -> str:
    """The exact bio/post fragment that carries the published contact — what an operator
    reads when verifying. Empty when the value cannot be located (never invented)."""
    i = (text or "").lower().find((value or "").lower())
    if i < 0:
        return ""
    return re.sub(r"\s+", " ", text[max(0, i - width):i + len(value) + width]).strip()


def _blank(platform: str) -> dict:
    return {"source": f"{platform}_profile", "platform": platform,
            "posts_fetched": 0, "need_posts": 0, "profiles_read": 0,
            "accepted": 0, "unique_individuals": 0,
            "with_email": 0, "with_phone": 0, "duplicates": 0,
            "org_skipped": 0, "no_bio": 0, "no_contact": 0, "skipped_no_need": 0,
            "stale_skipped": 0, "access_restricted": 0, "errors": 0,
            "page_hops": 0, "page_contacts": 0, "page_robots_blocked": 0,
            "page_org_skipped": 0,
            "error_notes": [], "org_examples": []}


def _contact_index(ppl_con: sqlite3.Connection) -> dict[str, str]:
    idx: dict[str, str] = {}
    for h in ppl_con.execute("SELECT person_key, contact_methods FROM prospect_people"):
        try:
            methods = json.loads(h[1] or "[]")
        except ValueError:
            methods = []
        for m in methods:
            k = (m.get("value") or "").strip().lower()
            if k and m.get("kind") in ("email", "phone") and k not in idx:
                idx[k] = h[0]
    return idx


def _record(ppl_con: sqlite3.Connection, s: dict, *, platform: str, account_id: str,
            profile_url: str, bio: str, post_text: str, post_url: str,
            observed_at: str, community: str, lang: str, cat: str,
            contact_idx: dict[str, str], origin: str = "profile",
            evidence_prefix: str = "") -> bool:
    """Insert ONE prospect row for a public account whose own profile publishes a contact.
    Returns True when a row was stored."""
    ev = pcon.extract_contacts(bio)
    if not (ev["emails"] or ev["phones"]):
        s["no_contact"] += 1
        return False
    if org_shaped(bio):
        s["org_skipped"] += 1
        if len(s["org_examples"]) < 5:
            s["org_examples"].append({"profile_url": profile_url,
                                      "published": [e for e in ev["emails"][:2]]})
        return False
    key = profile_key(platform, account_id)
    if ppl_con.execute("SELECT 1 FROM prospect_people WHERE person_key=?", (key,)).fetchone():
        s["duplicates"] += 1
        return False
    methods = []
    for e in ev["emails"]:
        methods.append({"kind": "email", "value": e,
                        "evidence": evidence_prefix + evidence_phrase(bio, e)})
    for p in ev["phones"]:
        methods.append({"kind": "phone", "value": p,
                        "evidence": evidence_prefix + evidence_phrase(bio, p)})
    if not methods:                                  # defensive: extract_contacts changed shape
        s["no_contact"] += 1
        return False
    dup_of, dup_status = "", "UNIQUE"
    for m in methods:
        ck = m["value"].strip().lower()
        if ck in contact_idx and contact_idx[ck] != key:
            dup_of, dup_status = contact_idx[ck], "DUPLICATE"
            break
    invite_bio = pcon.permission_evidence(bio)
    invite_post = pcon.permission_evidence(post_text)
    invite = invite_bio or invite_post
    # A profile contact with an explicit invitation reaches CONTACT_FOUND; without one it
    # stays a DISCOVERED row - discovered, not invited.
    stage = "CONTACT_FOUND" if (invite and dup_status == "UNIQUE") else "DISCOVERED"
    conf = pcon.confidence(methods, cat)
    rank = round(conf + pcon.recency_weight(observed_at) + pcon.need_strength(post_text), 3)
    now = _now()
    ppl_con.execute(
        "INSERT INTO prospect_people (person_key, source, community, source_url, signal_id, "
        "stated_need, intent_cat, lang, observed_at, discovered_at, geo, contact_methods, "
        "contact_status, contact_evidence, permission_status, permission_evidence, "
        "confidence, rank_score, dup_status, dup_of, stage, next_action, profile_url, "
        "contact_origin, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (key, f"{platform}_profile", (community or "")[:80], post_url, None,
         (post_text or "")[:400], cat, lang or "?", observed_at or "", now, "",
         json.dumps(methods), "CONTACT_AVAILABLE",
         (methods[0].get("evidence") or "")[:400],
         ("INVITED_INQUIRIES" if invite else "SELF_PUBLISHED"),
         (invite or "self-published on the account's own public profile")[:400],
         conf, rank, dup_status, dup_of, stage, pcon.NEXT_ACTION[stage],
         profile_url, origin, now, now))
    for m in methods:
        contact_idx[m["value"].strip().lower()] = key
    s["accepted"] += 1
    s["unique_individuals"] += 1 if dup_status == "UNIQUE" else 0
    s["duplicates"] += 1 if dup_status == "DUPLICATE" else 0
    if ev["emails"]:
        s["with_email"] += 1
    if ev["phones"]:
        s["with_phone"] += 1
    return True


def _stale(observed_at: str) -> bool:
    try:
        ts = datetime.fromisoformat(str(observed_at or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return False
    return ts < (datetime.now(timezone.utc).timestamp() - pcon.MAX_PROSPECT_AGE_DAYS * 86400)


_LINK_HOST_SKIP = ("bsky.app", "mastodon", "twitter.com", "x.com", "instagram.com",
                   "facebook.com", "tiktok.com", "linkedin.com", "youtube.com", "t.me",
                   "discord.gg", "threads.net", "reddit.com")
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def linked_urls(text: str, limit: int = 2) -> list[str]:
    """Public URLs the account itself published in its bio, minus the platform's own hosts.
    A url shortener/link hub is allowed — the page it lands on is still public and the
    contact there is still the account's own publication."""
    out = []
    for m in _URL_RE.finditer(text or ""):
        u = m.group(0).rstrip(").,;")
        host = u.split("//", 1)[-1].split("/", 1)[0].lower()
        if any(host == h or host.endswith("." + h) for h in _LINK_HOST_SKIP):
            continue
        if u not in out:
            out.append(u)
        if len(out) >= limit:
            break
    return out


def page_contact(url: str, fetch_text, s: dict) -> str:
    """ONE hop to a page the account linked from its own profile: robots-checked, single
    GET, html -> visible text, then the SAME extraction rules. Returns the page text when it
    publishes a contact, else ''. Org-shaped pages are refused (an agency site is not this
    person's own contact promise)."""
    try:
        if not webx._robots_allowed(url, fetch_text):
            s["page_robots_blocked"] += 1
            return ""
        raw = fetch_text(url)
    except Exception as e:                       # noqa: BLE001 - reported, never fatal
        _note_error(s, e)
        return ""
    page = webx.read_page(raw, url)
    text = (page.get("text") or "")[:6000]
    if not text.strip():
        return ""
    if org_shaped(text[:2500]):
        s["page_org_skipped"] += 1
        return ""
    ev = pcon.extract_contacts(text)
    if not (ev["emails"] or ev["phones"]):
        return ""
    s["page_contacts"] += 1
    return text


def _with_linked_page(bio: str, fetch_text, s: dict, budget: dict):
    """Enrich a contact-less bio by ONE hop to a page the account itself linked. Returns
    (bio_text, contact_origin, evidence_prefix). Budget is per RUN (not per profile), so a
    noisy query can never turn into a site crawl."""
    if fetch_text is None or budget.get("left", 0) <= 0:
        return bio, "profile", ""
    ev = pcon.extract_contacts(bio)
    if ev["emails"] or ev["phones"]:
        return bio, "profile", ""
    for url in linked_urls(bio):
        if budget["left"] <= 0:
            break
        budget["left"] -= 1
        s["page_hops"] += 1
        text = page_contact(url, fetch_text, s)
        if text:
            return (bio + " | linked page " + url + ": " + text[:2000], "profile_link",
                    f"linked page {url}: ")
    return bio, "profile", ""


def _note_error(s: dict, exc: Exception) -> None:
    code = getattr(exc, "code", None)
    if code in _ACCESS_STOP:
        s["access_restricted"] += 1
    s["errors"] += 1
    if len(s["error_notes"]) < 6:
        s["error_notes"].append(f"{type(exc).__name__}: {str(exc)[:160]}")


# ------------------------------------------------------------------ adapters
def scan_bluesky(ppl_con, cfg=None, get_json=None, sleeper=time.sleep, limit=50,
                 contact_idx=None, fetch_text=None) -> dict:
    cfg = cfg if isinstance(cfg, dict) else load_cfg()
    get_json = get_json or signals._http_json
    contact_idx = contact_idx if contact_idx is not None else _contact_index(ppl_con)
    base = str(((sources_cfg().get("bluesky") or {}).get("instance")
                or "https://api.bsky.app")).rstrip("/")
    queries = cfg.get("queries", {}).get("bluesky") or DEFAULT_QUERIES["bluesky"]
    s = _blank("bluesky")
    budget = {"left": int(cfg.get("page_hops", 0) or 0)}
    for q in queries:
        params = {"q": str(q.get("q") if isinstance(q, dict) else q).strip(),
                  "sort": "latest", "limit": min(int(limit), 100),
                  "since": signals._iso_date()}
        if not params["q"]:
            continue
        if isinstance(q, dict) and q.get("lang"):
            params["lang"] = q["lang"]
        search = f"{base}/xrpc/app.bsky.feed.searchPosts?" + urllib.parse.urlencode(params)
        try:
            res = get_json(search)
        except Exception as e:                       # noqa: BLE001 - reported, not swallowed
            _note_error(s, e)
            if getattr(e, "code", None) in _ACCESS_STOP:
                break
            continue
        for post in (res or {}).get("posts") or []:
            s["posts_fetched"] += 1
            rec = post.get("record") or {}
            text = (rec.get("text") or "").strip()
            uri = (post.get("uri") or "")
            author = post.get("author") or {}
            parts = uri.split("/")
            if not text or len(parts) < 5 or not author.get("did"):
                s["skipped_no_need"] += 1
                continue
            post_url = f"https://bsky.app/profile/{parts[2]}/post/{parts[-1]}"
            if not need_shaped(text):
                s["skipped_no_need"] += 1
                continue
            if _stale((rec.get("created_at") or "")[:19]):
                s["stale_skipped"] += 1
                continue
            s["need_posts"] += 1
            if ppl_con.execute("SELECT 1 FROM prospect_people WHERE person_key=?",
                               (profile_key("bluesky", author["did"]),)).fetchone():
                s["duplicates"] += 1
                continue
            prof_url = f"{base}/xrpc/app.bsky.actor.getProfile?" + urllib.parse.urlencode(
                {"actor": author["did"]})
            try:
                prof = get_json(prof_url) or {}
            except Exception as e:                   # noqa: BLE001
                _note_error(s, e)
                if getattr(e, "code", None) in _ACCESS_STOP:
                    return s
                continue
            s["profiles_read"] += 1
            bio = (prof.get("description") or "").strip()
            if not bio:
                s["no_bio"] += 1
                continue
            cat = signals.intent_category(text, "")
            bio, origin, prefix = _with_linked_page(bio, fetch_text, s, budget)
            _record(ppl_con, s, platform="bluesky", account_id=author["did"],
                    profile_url=f"https://bsky.app/profile/{author.get('handle') or author['did']}",
                    bio=bio, post_text=text, post_url=post_url,
                    observed_at=(rec.get("created_at") or "")[:19], community="bsky.app",
                    lang="es" if any(w in text.lower() for w in ("seguro", "necesito", "perd"))
                    else "en", cat=cat, contact_idx=contact_idx,
                    origin=origin, evidence_prefix=prefix)
            sleeper(float(cfg.get("pause_seconds", 1.0)))
        sleeper(float(cfg.get("pause_seconds", 1.0)))
    ppl_con.commit()
    return s


def scan_mastodon(ppl_con, cfg=None, get_json=None, sleeper=time.sleep, limit=40,
                  contact_idx=None, fetch_text=None) -> dict:
    cfg = cfg if isinstance(cfg, dict) else load_cfg()
    get_json = get_json or signals._http_json
    contact_idx = contact_idx if contact_idx is not None else _contact_index(ppl_con)
    mcfg = sources_cfg().get("mastodon") or {}
    instance = str(mcfg.get("instance") or "https://mastodon.social").rstrip("/")
    tags = cfg.get("queries", {}).get("mastodon") or mcfg.get("tags") or DEFAULT_QUERIES["mastodon"]
    s = _blank("mastodon")
    budget = {"left": int(cfg.get("page_hops", 0) or 0)}
    for tag in tags:
        tag = str(tag).lstrip("#").strip()
        if not tag:
            continue
        url = f"{instance}/api/v1/timelines/tag/{urllib.parse.quote(tag)}?" + \
              urllib.parse.urlencode({"limit": min(int(limit), 40)})
        try:
            res = get_json(url)
        except Exception as e:                       # noqa: BLE001
            _note_error(s, e)
            if getattr(e, "code", None) in _ACCESS_STOP:
                break
            continue
        for st in (res or []) if isinstance(res, list) else []:
            s["posts_fetched"] += 1
            text = signals._strip_html(st.get("content") or "").strip()
            acct = st.get("account") or {}
            if not text or not acct.get("id"):
                s["skipped_no_need"] += 1
                continue
            if not need_shaped(text):
                s["skipped_no_need"] += 1
                continue
            if _stale((st.get("created_at") or "")[:19]):
                s["stale_skipped"] += 1
                continue
            s["need_posts"] += 1
            if ppl_con.execute("SELECT 1 FROM prospect_people WHERE person_key=?",
                               (profile_key("mastodon", str(acct["id"])),)).fetchone():
                s["duplicates"] += 1
                continue
            try:
                prof = get_json(f"{instance}/api/v1/accounts/{acct['id']}") or {}
            except Exception as e:                   # noqa: BLE001
                _note_error(s, e)
                if getattr(e, "code", None) in _ACCESS_STOP:
                    return s
                continue
            s["profiles_read"] += 1
            fields = " | ".join(
                f"{signals._strip_html(f.get('name') or '')}: {signals._strip_html(f.get('value') or '')}"
                for f in (prof.get("fields") or []) if isinstance(f, dict))
            bio = (signals._strip_html(prof.get("note") or "") + (" | " + fields if fields else "")).strip()
            if not bio:
                s["no_bio"] += 1
                continue
            cat = signals.intent_category(text, "")
            bio, origin, prefix = _with_linked_page(bio, fetch_text, s, budget)
            _record(ppl_con, s, platform="mastodon",
                    account_id=str(acct["id"]),
                    profile_url=(prof.get("url") or st.get("url") or ""),
                    bio=bio, post_text=text, post_url=(st.get("url") or ""),
                    observed_at=(st.get("created_at") or "")[:19],
                    community=instance.split("//")[-1],
                    lang="es" if any(w in text.lower() for w in ("seguro", "necesito", "perd"))
                    else "en", cat=cat, contact_idx=contact_idx,
                    origin=origin, evidence_prefix=prefix)
            sleeper(float(cfg.get("pause_seconds", 1.0)))
        sleeper(float(cfg.get("pause_seconds", 1.0)))
    ppl_con.commit()
    return s


def scan_lemmy(ppl_con, cfg=None, get_json=None, sleeper=time.sleep, limit=40,
               contact_idx=None, fetch_text=None) -> dict:
    cfg = cfg if isinstance(cfg, dict) else load_cfg()
    get_json = get_json or signals._http_json
    contact_idx = contact_idx if contact_idx is not None else _contact_index(ppl_con)
    instances = [str(i).rstrip("/") for i in
                 (sources_cfg().get("lemmy") or {}).get("instances") or ["https://lemmy.world"]]
    queries = cfg.get("queries", {}).get("lemmy") or DEFAULT_QUERIES["lemmy"]
    s = _blank("lemmy")
    budget = {"left": int(cfg.get("page_hops", 0) or 0)}
    for inst in instances:
        for q in queries:
            q = str(q).strip()
            if not q:
                continue
            url = f"{inst}/api/v3/search?" + urllib.parse.urlencode(
                {"q": q, "type_": "Comments", "sort": "New", "listing_type": "All",
                 "limit": min(int(limit), 50)})
            try:
                res = get_json(url)
            except Exception as e:                   # noqa: BLE001
                _note_error(s, e)
                if getattr(e, "code", None) in _ACCESS_STOP:
                    return s
                continue
            for view in ((res or {}).get("comments") or []) if isinstance(res, dict) else []:
                s["posts_fetched"] += 1
                c = view.get("comment") or {}
                creator = view.get("creator") or {}
                text = signals._strip_html(c.get("content") or "").strip()
                cid = c.get("creator_id") or creator.get("id")
                if not text or not cid:
                    s["skipped_no_need"] += 1
                    continue
                if not need_shaped(text):
                    s["skipped_no_need"] += 1
                    continue
                if _stale((c.get("published") or "")[:19]):
                    s["stale_skipped"] += 1
                    continue
                s["need_posts"] += 1
                if ppl_con.execute("SELECT 1 FROM prospect_people WHERE person_key=?",
                                   (profile_key("lemmy", f"{inst}|{cid}"),)).fetchone():
                    s["duplicates"] += 1
                    continue
                try:
                    # /api/v3/user takes person_id and answers {person_view: {person: ...}}
                    # (live-verified Oct 2026; ?id= is a 400). Fallback keeps older/other
                    # Lemmy builds working without a second request.
                    prof = get_json(f"{inst}/api/v3/user?person_id={cid}") or {}
                except Exception as e:               # noqa: BLE001
                    _note_error(s, e)
                    if getattr(e, "code", None) in _ACCESS_STOP:
                        return s
                    continue
                s["profiles_read"] += 1
                person = ((prof.get("person_view") or {}).get("person")
                          or prof.get("person") or {})
                bio = signals._strip_html(person.get("bio") or "").strip()
                if not bio:
                    s["no_bio"] += 1
                    continue
                cat = signals.intent_category(text, "")
                bio, origin, prefix = _with_linked_page(bio, fetch_text, s, budget)
                _record(ppl_con, s, platform="lemmy", account_id=f"{inst}|{cid}",
                        profile_url=(person.get("actor_id")
                                     or f"{inst}/u/{person.get('name') or cid}"),
                        bio=bio, post_text=text, post_url=(c.get("ap_id") or ""),
                        observed_at=(c.get("published") or "")[:19],
                        community=inst.split("//")[-1],
                        lang="es" if any(w in text.lower() for w in ("seguro", "necesito", "perd"))
                        else "en", cat=cat, contact_idx=contact_idx,
                        origin=origin, evidence_prefix=prefix)
                sleeper(float(cfg.get("pause_seconds", 1.0)))
            sleeper(float(cfg.get("pause_seconds", 1.0)))
    ppl_con.commit()
    return s


ADAPTERS = {"bluesky": scan_bluesky, "mastodon": scan_mastodon, "lemmy": scan_lemmy}


def scan_all(ppl_con: sqlite3.Connection, sources=("bluesky", "mastodon", "lemmy"),
             cfg: dict | None = None, get_json=None, sleeper=time.sleep,
             limit: int = 40, fetch_text=None) -> list[dict]:
    """Run the selected profile adapters with ONE shared contact index so a value published
    by two accounts is marked DUPLICATE across platforms, not just within one."""
    cfg = cfg if isinstance(cfg, dict) else load_cfg()
    if cfg.get("enabled") is False:
        return [{"source": f"{p}_profile", "skipped": "disabled by config (public_profiles.enabled=false)"}
                for p in sources]
    idx = _contact_index(ppl_con)
    out = []
    for name in sources:
        fn = ADAPTERS.get(name)
        if not fn:
            out.append({"source": f"{name}_profile", "error": "unknown source"})
            continue
        before = len(idx)
        stats = fn(ppl_con, cfg=cfg, get_json=get_json, sleeper=sleeper, limit=limit,
                   contact_idx=idx, fetch_text=fetch_text)
        stats["contact_values_indexed"] = len(idx) - before
        out.append(stats)
    return out


def yield_summary(ppl_con: sqlite3.Connection) -> dict:
    """Per-source stored yield from the people store — the numbers a report can be held to."""
    rows = [dict(r) for r in ppl_con.execute(
        "SELECT source, contact_origin, COUNT(*) n, "
        "SUM(contact_status='CONTACT_AVAILABLE') with_contact, "
        "SUM(dup_status='UNIQUE') uniques, "
        "SUM(permission_status='SELF_PUBLISHED') self_published, "
        "SUM(permission_status='INVITED_INQUIRIES') invited, "
        "SUM(permission_status='OPERATOR_VERIFIED') verified, "
        "SUM(rank_score >= ?) promising "
        "FROM prospect_people GROUP BY source, contact_origin ORDER BY n DESC",
        (pcon.LOW_PROMISING,))]
    prof = [r for r in rows if r["contact_origin"] in ("profile", "profile_link")
            and r["with_contact"]]
    return {"by_source": rows,
            "profile_contacts_total": sum(r["with_contact"] or 0 for r in prof),
            "profile_contacts_unique": sum(r["uniques"] or 0 for r in prof),
            "profile_contacts_verified": sum(r["verified"] or 0 for r in prof)}
