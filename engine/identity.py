"""IDENTITY RESOLUTION + CONTACT MATCHING — who is behind a consumer-intent public post.

WHY THIS EXISTS
    Discovery found the people, but almost every row stopped at a Reddit handle: measured on the
    stored `public_prospects` table, 370 rows carry ZERO resolved names and ZERO phone numbers. A
    handle is not a person and a discussion URL is not a contact, so nothing downstream could use
    the work. This module is the missing stage between "someone publicly described a coverage need"
    and "we know who they are, with evidence".

THE CHAIN (each hop writes its own evidence; nothing is inferred without a source URL)
    1. AUTHOR DOSSIER  — the same public account's OTHER public posts/comments (permitted Reddit
       archive API, credential-free). People state their name, city and sometimes a number there
       even when the insurance post itself carries none.
    2. PLATFORM PROFILE — where the source published one: Discourse `/u/<name>.json`, Mastodon
       `accounts/lookup`, Lemmy `/api/v3/user`, or the `profile_url` already on the row.
    3. HANDLE PROBE (opt-in) — a SHORT allowlist of public profile surfaces keyed by the same
       handle (GitHub API, Linktree, about.me, Keybase, Bluesky). No search-engine scraping.
    4. LINKED PAGE HOP — one robots-checked fetch of a page the account itself linked from its
       profile (a personal site or link hub), which may publish a phone.

MATCHING RULES (the point of the module - false matches are worse than no match)
    * A name is only read from an EXPLICIT self-identification ("my name is X", "me llamo X",
      "call me X", "I go by X", or "I'm <common first name>"). "I'm tired" is not a name.
    * A name must be corroborated by a second independent source to be VERIFIED; one source makes
      it PROBABLE (shown, never presented as usable). Two DIFFERENT names is CONFLICT: nothing is
      chosen, both are kept on the row as evidence.
    * A phone must be published by the same identity with an ownership verb, must be a real US
      10-digit number, and is REFUSED for toll-free/premium ranges, business/agency/broker context,
      quoted third-party numbers ("the number that called me"), or any number whose owner has no
      established name.
    * Contact permission and marketing eligibility are SEPARATE columns: publishing a number is
      never consent, so marketing eligibility is BLOCKED_NO_CONSENT until that person's own
      /check submission exists. Nothing here contacts anyone.

Everything is injectable (`fetch_json`, `fetch_text`, `sleeper`) so the whole chain tests offline;
`--live` is required on the CLI because the default path is a real network read.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import prospect_contacts as pcon          # extraction rules + third-party fences

ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "config" / "sources.json"
ARCHIVE_API = "https://arctic-shift.photon-reddit.com/api"
UA = ("GSAlludeIdentityResolver/1.0 (public-profile identity resolution for our own prospect "
      "research; robots respected; no login, no contact)")

NAME_STATUSES = ("UNRESOLVED", "PROBABLE", "VERIFIED", "CONFLICT", "REJECTED")
PHONE_STATUSES = ("UNRESOLVED", "PROBABLE", "VERIFIED", "CONFLICT", "REJECTED")
MATCH_STATUSES = ("UNRESOLVED", "PARTIAL", "MATCHED", "REJECTED")
PERMISSIONS = ("NONE", "SELF_PUBLISHED", "INVITED", "CONSENTED")
ELIGIBILITY = ("BLOCKED_NO_CONSENT", "BLOCKED_UNVERIFIED", "ELIGIBLE_CONSENTED")

# Columns this stage adds to public_prospects. They are additive: the original discovery evidence
# (`evidence`, `stated_need`, `source_url`, `author_handle`, `contact_methods`) is never rewritten.
PROSPECT_COLUMNS = (
    ("identity_name", "TEXT DEFAULT ''"),
    ("identity_name_status", "TEXT DEFAULT 'UNRESOLVED'"),
    ("identity_name_source", "TEXT DEFAULT ''"),
    ("identity_name_confidence", "REAL DEFAULT 0"),
    ("identity_phone", "TEXT DEFAULT ''"),
    ("identity_phone_status", "TEXT DEFAULT 'UNRESOLVED'"),
    ("identity_phone_source", "TEXT DEFAULT ''"),
    ("identity_phone_confidence", "REAL DEFAULT 0"),
    ("location_resolved", "TEXT DEFAULT ''"),
    ("match_status", "TEXT DEFAULT 'UNRESOLVED'"),
    ("match_reason", "TEXT DEFAULT ''"),
    ("contact_permission", "TEXT DEFAULT 'NONE'"),
    ("marketing_eligibility", "TEXT DEFAULT 'BLOCKED_NO_CONSENT'"),
    ("identity_evidence", "TEXT DEFAULT '{}'"),
    ("resolved_at", "TEXT DEFAULT ''"),
    ("resolve_attempts", "INTEGER DEFAULT 0"),
    ("resolve_last_error", "TEXT DEFAULT ''"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS identity_claims (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  prospect_id INTEGER NOT NULL,
  claim_kind TEXT NOT NULL,          -- NAME | PHONE | EMAIL | LOCATION | PROFILE
  value TEXT NOT NULL,               -- as published (raw)
  normalized TEXT DEFAULT '',        -- canonical form used for matching/dedupe
  source_url TEXT DEFAULT '',        -- WHERE it was read (the auditable part)
  source_kind TEXT DEFAULT '',       -- archive_post | archive_comment | platform_profile |
                                     -- handle_probe | linked_page
  method TEXT DEFAULT '',            -- which rule accepted it ('my name is', 'contact verb', ...)
  evidence TEXT DEFAULT '',          -- the exact fragment around the value
  strength REAL DEFAULT 0,           -- 0..1 deterministic, from the rule that matched
  status TEXT DEFAULT 'ACCEPTED',    -- ACCEPTED | REJECTED
  reject_reason TEXT DEFAULT '',
  is_test INTEGER DEFAULT 0,
  created_at TEXT, updated_at TEXT,
  UNIQUE(prospect_id, claim_kind, normalized, source_url)
);
CREATE INDEX IF NOT EXISTS ix_claim_pid ON identity_claims(prospect_id, claim_kind, status);
CREATE TABLE IF NOT EXISTS identity_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT,
  rows_scanned INTEGER DEFAULT 0, rows_resolved INTEGER DEFAULT 0,
  names_found INTEGER DEFAULT 0, names_verified INTEGER DEFAULT 0,
  phones_found INTEGER DEFAULT 0, phones_verified INTEGER DEFAULT 0,
  claims_rejected INTEGER DEFAULT 0, errors INTEGER DEFAULT 0, note TEXT DEFAULT ''
);
"""


def connect_ok(con: sqlite3.Connection) -> None:
    """Create the identity tables + widen public_prospects. Idempotent; called from prospector.ensure."""
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(public_prospects)")}
    for col, typ in PROSPECT_COLUMNS:
        if col not in have:
            con.execute(f"ALTER TABLE public_prospects ADD COLUMN {col} {typ}")
    con.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_cfg() -> dict:
    try:
        return (json.loads(CFG_PATH.read_text(encoding="utf-8")) or {}).get("identity") or {}
    except (OSError, ValueError):
        return {}


# ------------------------------------------------------------------ network (injectable)
def _default_json(url: str, timeout: int = 20) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:      # noqa: S310 - public API
            return json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        try:
            detail = e.read()[:160].decode("utf-8", "replace")
        except Exception:                       # noqa: BLE001 - body already consumed/closed
            detail = ""
        err = RuntimeError(f"HTTP {e.code} for {url[:120]} {detail}".strip())
        err.code = e.code                                               # type: ignore[attr-defined]
        raise err from e


def _default_text(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:      # noqa: S310 - public page
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        err = RuntimeError(f"HTTP {e.code} for {url[:120]}")
        err.code = e.code                                               # type: ignore[attr-defined]
        raise err from e


# ------------------------------------------------------------------ vocabulary
# Common US first names. The list exists to make "I'm <Name>" safe: without it the pattern accepts
# "I'm tired", "I'm moving", "I'm looking" as identities. Explicit markers ("my name is") do not
# need the list.
_FIRST_NAMES = frozenset("""
james mary robert patricia john jennifer michael linda david elizabeth william barbara richard susan
joseph jessica thomas sarah charles karen christopher nancy daniel lisa matthew betty anthony margaret
mark sandra donald ashley steven kimberly paul emily andrew donna joshua michelle kenneth dorothy
kevin carol brian amanda george melissa timothy deborah ronald stephanie edward rebecca jason sharon
jeffrey laura ryan cynthia jacob kathleen gary amy nicholas shirley eric angela jonathan helen stephen
anna larry brenda justin pamela scott nicole brandon emma benjamin samantha samuel katherine gregory
christine alexander debra frank rachel patrick carolyn raymond janet jack catherine dennis maria jerry
heather tyler diane aaron ruth jose julie adam olivia henry joyce nathan virginia douglas victoria
zachary kelly peter lauren kyle christina walter joan ethan evelyn jeremy judith harold megan keith
cheryl christian andrea roger hannah noah martha gerald jacqueline carl frances terry gloria sean ann
austin teresa arthur kathryn lawrence sara jesse janice dylan jean bryan alice joe madison jordan
doris billy abigail bruce julia albert judy willie grace gabriel denise logan amber alan marilyn juan
beverly wayne danielle roy theresa ralph sophia randy marie eugene diana vincent brittany russell
natalie elijah isabella louis charlotte bobby rose philip alexis johnny kayla bradley jose maria
miguel angel luis carlos ana sofia diego javier fernando gabriela alejandro ricardo pedro manuel
roberto eduardo carmen lucia rafael daniela valeria martin andres jorge alberto francisco
""".split())

# Words that look like names but are not. Kept tight: only things that actually collide.
_NOT_NAMES = frozenset("""
tired sorry here there moving new not very looking trying going done good fine sure sick scared
worried lost confused hoping planning thinking working living staying currently finally sober
married divorced single retired unemployed self employed insured uninsured healthy pregnant
moderator mod admin owner staff guest friend buddies newbie lurker brother sister mom dad mother father
girl boy dude guy sir madam user account member people person someone anyone somebody
parent parents wondering neckbeard rider track driver teacher nurse student neighbor landlord tenant
boss coworker customer client patient resident citizen veteran traveler gamer player fan reader
writer author artist singer dancer cook chef mechanic engineer developer designer manager worker
employee stranger human kid child baby son daughter wife husband boyfriend girlfriend partner uncle
aunt cousin nephew niece grandma grandpa grandparent household family man woman
alabama alaska arizona arkansas california colorado connecticut delaware florida georgia hawaii idaho
illinois indiana iowa kansas kentucky louisiana maine maryland massachusetts michigan minnesota
mississippi missouri montana nebraska nevada ohio oklahoma oregon pennsylvania tennessee texas utah
vermont virginia washington wisconsin wyoming america usa
the a an this that these those my your our their his her its it he she they we you i and or not no
yes just only also very really still even
""".split())

# Trade/business nouns. A display name like "Random Jeweler" is a SHOP, not a consumer: the first
# live resolution pass produced exactly that as a 'name', so the list is explicit rather than clever.
_ORG_WORDS = frozenset("""
agency broker brokerage insurance group llc inc corp company consulting associates clinic hospital
foundation nonprofit team services service solutions financial realty salon studio shop
jeweler jewelry barber nails tattoo landscaping lawn contractor construction electric electrical
plumbing roofing painting photography coach coaching wellness boutique cafe restaurant bakery
fitness gym studio design designs marketing media productions entertainment transport logistics
auto motors tires detailing cleaning repair repairs handyman moving movers catering events travel
agency insurance advisors advisor consulting law legal accounting tax notary church ministry
""".split())

_NAME_TOKEN = re.compile(r"^[A-ZÁÉÍÓÚÑ][a-záéíóúñ'\-]{1,18}$")

# The MARKER is case-insensitive (a sentence starts "My name is"), the captured NAME is not: only a
# capitalised token sequence is a name, which is what keeps "my name is not the issue" out.
_NAME_PATTERNS = (
    (re.compile(r"\b(?i:my name is)\s+([A-Z][a-zA-Z'\-]{1,19}(?:\s+[A-Z][a-zA-Z'\-]{1,19})?)"),
     0.9, "'my name is'"),
    (re.compile(r"\b(?i:name'?s)\s+([A-Z][a-zA-Z'\-]{1,19})"), 0.72, "\"name's\""),
    (re.compile(r"\b(?i:call me)\s+([A-Z][a-zA-Z'\-]{1,19})"), 0.8, "'call me'"),
    (re.compile(r"\b(?i:i go by)\s+([A-Z][a-zA-Z'\-]{1,19})"), 0.8, "'i go by'"),
    (re.compile(r"\b(?i:this is)\s+([A-Z][a-zA-Z'\-]{1,19}(?:\s+[A-Z][a-zA-Z'\-]{1,19})?)\s*,?\s*"
                r"(?i:i'm|i am|from|in|and)\b"), 0.62, "'this is X, ...'"),
    (re.compile(r"\b(?i:me llamo)\s+([A-ZÁÉÍÓÚÑ][a-zA-Záéíóúñ'\-]{1,19}"
                r"(?:\s+[A-ZÁÉÍÓÚÑ][a-zA-Záéíóúñ'\-]{1,19})?)"), 0.9, "'me llamo'"),
    (re.compile(r"\b(?i:soy)\s+([A-ZÁÉÍÓÚÑ][a-zA-Záéíóúñ'\-]{1,19})\s*,?\s*(?i:y\s+)?"
                r"(?i:tengo|vivo|busco|necesito|estoy)\b"), 0.7, "'soy X y ...'"),
)
# "I'm John" — the FIRST NAME LIST is the guard: "I'm tired", "I'm moving", "I'm looking" are
# adjectives/verbs, and no common-first-name entry matches them, so the pattern cannot fire.
_FIRSTNAME_SELF = re.compile(r"\b(?i:i'm|i am)\s+([A-Z][a-z]{1,15})\b")

# Cities: a Florida city named about the SELF is a location fact. Every entry maps to FL, so a city
# mention can never imply another state (the same rule prospector.extract_geo applies).
_FL_CITIES = {
    "miami": "Miami", "hialeah": "Hialeah", "tampa": "Tampa", "orlando": "Orlando",
    "jacksonville": "Jacksonville", "tallahassee": "Tallahassee",
    "fort lauderdale": "Fort Lauderdale", "st petersburg": "St. Petersburg",
    "saint petersburg": "St. Petersburg", "clearwater": "Clearwater",
    "gainesville": "Gainesville", "naples": "Naples", "sarasota": "Sarasota",
    "daytona beach": "Daytona Beach", "daytona": "Daytona Beach",
    "west palm beach": "West Palm Beach", "palm beach": "Palm Beach", "boca raton": "Boca Raton",
    "pembroke pines": "Pembroke Pines", "miramar": "Miramar", "kissimmee": "Kissimmee",
    "lakeland": "Lakeland", "ocala": "Ocala", "cape coral": "Cape Coral",
    "fort myers": "Fort Myers", "port st lucie": "Port St. Lucie", "port saint lucie": "Port St. Lucie",
    "pensacola": "Pensacola", "melbourne": "Melbourne", "bradenton": "Bradenton",
    "pompano beach": "Pompano Beach", "coral springs": "Coral Springs", "doral": "Doral",
    "fort pierce": "Fort Pierce", "panama city": "Panama City", "brandon": "Brandon",
    "lithia": "Lithia", "riverview": "Riverview", "valrico": "Valrico", "plant city": "Plant City",
    "wesley chapel": "Wesley Chapel", "lutz": "Lutz", "apollo beach": "Apollo Beach",
    "sun city center": "Sun City Center", "winter haven": "Winter Haven", "clermont": "Clermont",
    "sanford": "Sanford", "oviedo": "Oviedo", "altamonte springs": "Altamonte Springs",
    "deltona": "Deltona", "palm coast": "Palm Coast", "new port richey": "New Port Richey",
    "spring hill": "Spring Hill", "leesburg": "Leesburg", "the villages": "The Villages",
    "boynton beach": "Boynton Beach", "delray beach": "Delray Beach", "jupiter": "Jupiter",
    "stuart": "Stuart", "titusville": "Titusville", "merritt island": "Merritt Island",
    "crestview": "Crestview", "niceville": "Niceville", "marianna": "Marianna",
    "homestead": "Homestead", "kendall": "Kendall", "coral gables": "Coral Gables",
    "aventura": "Aventura", "sunrise": "Sunrise", "davie": "Davie", "weston": "Weston",
    "plantation": "Plantation", "hallandale beach": "Hallandale Beach", "osceola": "Osceola",
    "hillsborough": "Hillsborough", "pinellas": "Pinellas", "orange county": "Orange County",
}
# NOT a city: county names are regions, not a location a person "lives in" - dropped from the
# self-location map (kept separate so a county mention cannot masquerade as a city).
_REGIONS = {"hillsborough", "pinellas", "orange county", "osceola"}

_SELF_PLACE = re.compile(
    r"\b(?:i(?:'m| am)?\s+(?:currently\s+)?(?:living|live|based|located|staying)\s+in|"
    r"i\s+live\s+in|living\s+in|based\s+in|i(?:'m| am)\s+in|"
    r"(?:just\s+)?moved\s+to|moving\s+to|relocat\w*\s+to|here\s+in|located\s+in|"
    r"vivo\s+en|me\s+mud[eé]\s+a)\s+"
    r"([A-Za-z][A-Za-z .'\-]{1,28}?)(?=[,.;!?)]|\s+(?:and|but|so|which|for|with|since|"
    r"because|after|now|my|i|is|was|area|county)\b|$)", re.IGNORECASE)

_TOLLFREE = ("800", "833", "844", "855", "866", "877", "888", "900")
_BUSINESS_PHONE = re.compile(
    r"\b(licensed agent|insurance agency|agency|broker|brokerage|our office|call our|"
    r"business line|for business|front desk|customer service|appointment line|quote line|"
    r"office hours|llc|inc\.?|realty|salon|clinic|detailing|handyman|towing|"
    r"agencia|corredor|oficina|negocio)\b", re.IGNORECASE)
# The person must be offering the number for themselves, not describing a number they dealt with.
_OWNERSHIP = re.compile(
    r"\b(my (?:cell|number|phone|n[uú]mero|tel[eé]fono)|text me|call me|reach me|"
    r"contact me at|dm me|whatsapp me|ll[aá]mame|escr[ií]beme|mi (?:celular|tel[eé]fono))\b",
    re.IGNORECASE)
# A profile custom field labelled "Phone: ..." is the account's OWN published contact surface: the
# label is the ownership statement (this is how Mastodon/Lemmy bios carry a number), so it counts the
# same as "text me at". The business/agency fence still applies afterwards.
_LABELED_CONTACT = re.compile(
    r"(?:^|[|;•\n])\s*(?:phone|tel|telephone|mobile|cell|whatsapp|contact|contacto|tel[eé]fono|"
    r"celular)\s*(?:number|n[uú]mero)?\s*[:\-–]\s*\+?1?[\s(]*\d{3}", re.IGNORECASE)

_ANON_HANDLE = re.compile(r"^(?:\[?deleted\]?|automoderator|auto_?mod|moderator|removed|"
                          r"throwaway\w*|anon\w*|op|unknown)$", re.IGNORECASE)


# ------------------------------------------------------------------ name / phone / place rules
def valid_name(raw: str) -> str:
    """A name we are willing to store, or ''. Rejects handles, cities, org words, adjectives."""
    raw = re.sub(r"\s+", " ", (raw or "").strip().strip(".,;:!?'\""))
    # a profile/API value may arrive lowercase; normalise the capitalisation BEFORE the shape check
    # (the reject lists below are all lowercase, so this cannot let an adjective through)
    toks = [(t[:1].upper() + t[1:]) for t in raw.split(" ") if t]
    # 1-2 tokens only: a self-identification is a name or First+Last. Three-token strings are
    # display names ("Air Fryer Aristocrat"), and allowing them let pseudonyms through.
    if not 1 <= len(toks) <= 2:
        return ""
    for t in toks:
        if not _NAME_TOKEN.match(t):
            return ""
        low = t.lower()
        if low in _NOT_NAMES or low in _ORG_WORDS or low in _FL_CITIES or low in _REGIONS:
            return ""
    return " ".join(toks)


def name_claims(text: str, *, source_url: str, source_kind: str) -> list[dict]:
    """Names the author states about THEMSELVES, each with the fragment that proves it."""
    text = re.sub(r"\s+", " ", text or "")
    out: list[dict] = []
    for pat, strength, method in _NAME_PATTERNS:
        for m in pat.finditer(text):
            raw = m.group(1)
            name = valid_name(raw)
            if not name:
                continue
            out.append({"kind": "NAME", "value": raw, "normalized": name,
                        "source_url": source_url, "source_kind": source_kind,
                        "method": method, "strength": strength,
                        "evidence": _excerpt(text, m.start(), m.end())})
    for m in _FIRSTNAME_SELF.finditer(text):
        first = m.group(1)
        if first.lower() not in _FIRST_NAMES:
            continue
        name = valid_name(first)
        if not name:
            continue
        out.append({"kind": "NAME", "value": first, "normalized": name,
                    "source_url": source_url, "source_kind": source_kind,
                    "method": "\"I'm <common first name>\"", "strength": 0.55,
                    "evidence": _excerpt(text, m.start(), m.end())})
    return out


def _excerpt(text: str, start: int, end: int, width: int = 80) -> str:
    return re.sub(r"\s+", " ", text[max(0, start - width):end + width]).strip()[:400]


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


def phone_claims(text: str, *, source_url: str, source_kind: str) -> tuple[list[dict], list[dict]]:
    """(accepted, rejected). Refusals are returned WITH the reason so the row shows why a number a
    person quoted (an agency line, a toll-free customised ad number) was not stored as theirs."""
    text = re.sub(r"\s+", " ", text or "")
    ok: list[dict] = []
    bad: list[dict] = []
    ev = pcon.extract_contacts(text)          # verb-proximity + third-party fences already applied
    for raw in ev["phones"]:
        digits = normalize_phone(raw)
        base = {"kind": "PHONE", "value": raw, "normalized": digits or raw,
                "source_url": source_url, "source_kind": source_kind}
        if not digits:
            bad.append({"status": "REJECTED", **base, "method": "digit shape", "strength": 0.0,
                        "reject_reason": "not a 10-digit US number"})
            continue
        if digits[:3] in _TOLLFREE:
            bad.append({"status": "REJECTED", **base, "method": "number range", "strength": 0.0,
                        "reject_reason": f"toll-free/premium range {digits[:3]}"})
            continue
        i = text.find(raw)
        near = text[max(0, i - 160):i + len(raw) + 160] if i >= 0 else text[:400]
        if _BUSINESS_PHONE.search(near):
            bad.append({"status": "REJECTED", **base, "method": "business context", "strength": 0.0,
                        "reject_reason": "business/agency context, not a consumer contact"})
            continue
        labeled = bool(_LABELED_CONTACT.search(near))
        if not (_OWNERSHIP.search(near) or labeled):
            bad.append({"status": "REJECTED", **base, "method": "ownership verb", "strength": 0.0,
                        "reject_reason": "no ownership wording ('my number', 'text me') nearby"})
            continue
        ok.append({**base,
                   "method": ("self-published in a labelled profile field" if labeled else
                              "self-published with ownership wording"),
                   "strength": 0.8 if labeled else 0.85,
                   "evidence": _excerpt(text, max(0, i), max(0, i) + len(raw))})
    return ok, bad


def place_claims(text: str, *, source_url: str, source_kind: str) -> list[dict]:
    """A Florida city the person names about themselves ("I live in Brandon", "moved to Orlando")."""
    text = re.sub(r"\s+", " ", text or "")
    out: list[dict] = []
    for m in _SELF_PLACE.finditer(text):
        raw = (m.group(1) or "").strip().strip(".-").lower()
        for key, city in _FL_CITIES.items():
            if key in _REGIONS:
                continue
            if raw == key or raw.startswith(key + " ") or raw.endswith(" " + key):
                out.append({"kind": "LOCATION", "value": m.group(0)[:120],
                            "normalized": f"{city}, FL", "source_url": source_url,
                            "source_kind": source_kind, "method": "'I live in <city>'",
                            "strength": 0.8,
                            "evidence": _excerpt(text, m.start(), m.end())})
                break
    return out


def org_shaped(text: str) -> bool:
    low = " " + re.sub(r"\s+", " ", (text or "").lower()) + " "
    return any(w in low for w in (" insurance agency", " licensed agent", " broker", " llc",
                                  " our agency", " we offer", " free quote", " agencia"))


# ------------------------------------------------------------------ source reads
def _archive(handle: str, kind: str, fetch_json, cfg: dict) -> tuple[list[dict], list[str]]:
    """The author's own public posts/comments. `kind` = posts|comments."""
    limit = int(cfg.get("dossier_limit", 50) or 50)
    # `permalink` is NOT an accepted archive field (it answers 400) — the canonical URL is built
    # from the post id instead, which is what the API does return.
    if kind == "posts":
        fields = "id,subreddit,title,selftext,created_utc,author,author_flair_text"
        ep = "/posts/search"
    else:
        fields = "id,subreddit,body,created_utc,author,author_flair_text"
        ep = "/comments/search"
    url = ARCHIVE_API + ep + "?" + urllib.parse.urlencode(
        {"author": handle, "limit": limit, "sort": "desc", "fields": fields})
    try:
        data = fetch_json(url) or {}
    except Exception as e:                    # noqa: BLE001 - one source never kills a row
        return [], [f"{type(e).__name__}: {str(e)[:140]}"]
    rows = data.get("data") if isinstance(data, dict) else None
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        body = (r.get("selftext") or r.get("body") or "").strip()
        if body in ("[removed]", "[deleted]"):
            continue
        u = f"https://www.reddit.com/comments/{r['id']}" if r.get("id") else ""
        out.append({"text": " ".join(x for x in (r.get("title"), body) if x),
                    "url": u, "sub": r.get("subreddit") or "",
                    "flair": r.get("author_flair_text") or ""})
    return out, []


def _coerce_url(value) -> str:
    if isinstance(value, dict):
        return str(value.get("href") or value.get("url") or "")
    return str(value or "")


def actor_for(row: dict) -> str:
    """The public account key for this row: the stored handle, or the account embedded in the post
    URL. Bluesky posts carry a DID at /profile/<did>/post/<id> and many legacy rows stored no
    handle at all - without this fallback 65 stored rows read as 'no identity source'."""
    h = (row.get("author_handle") or "").strip().lstrip("@")
    if h:
        return h
    for u in (_coerce_url(row.get("profile_url")), row.get("source_url") or ""):
        m = re.search(r"bsky\.app/profile/([^/?#]+)", u)
        if m:
            return urllib.parse.unquote(m.group(1))
        m = re.search(r"mastodon[^/]*/@([^/?#]+)", u) or re.search(r"/@([A-Za-z0-9_]+)/\d+", u)
        if m:
            return m.group(1)
    return ""


def linked_url(text: str) -> str:
    """The FIRST non-platform URL an account published in its own bio (a personal site or link hub).
    That page is the person's own publication, so a number on it is theirs to publish."""
    skip = ("bsky.app", "mastodon", "twitter.com", "x.com", "instagram.com", "facebook.com",
            "tiktok.com", "linkedin.com", "youtube.com", "t.me", "discord.gg", "threads.net",
            "reddit.com", "linktr.ee", "linkin.bio")
    for m in re.finditer(r"https?://[^\s<>\"')\]]+", text or ""):
        u = m.group(0).rstrip(".,;")
        host = u.split("//", 1)[-1].split("/", 1)[0].lower()
        if any(host == s or host.endswith("." + s) for s in skip):
            continue
        return u
    return ""


def _linked_page(url: str, fetch_text, cfg: dict) -> tuple[list[dict], list[str]]:
    """ONE robots-checked hop to the page the account linked. Org-shaped pages are refused: an
    agency site is a business contact, not this person's own promise."""
    if not url or not cfg.get("page_hop", True):
        return [], []
    from . import web_extract as webx
    try:
        if not webx._robots_allowed(url, fetch_text):
            return [], [f"linked page blocked by robots: {url[:80]}"]
        page = webx.read_page(fetch_text(url), url)
    except Exception as e:                      # noqa: BLE001 - unreachable page is a normal outcome
        return [], [f"linked page {url[:60]}: {type(e).__name__}"]
    text = (page.get("text") or "")[:6000]
    if not text.strip():
        return [], []
    if org_shaped(text[:1500]):
        return [], [f"linked page is org-shaped, refused: {url[:70]}"]
    claims = name_claims(text, source_url=url, source_kind="linked_page")
    claims += place_claims(text, source_url=url, source_kind="linked_page")
    okp, badp = phone_claims(text, source_url=url, source_kind="linked_page")
    return claims + okp + badp, []


def _profile_facts(row: dict, fetch_json, fetch_text) -> tuple[dict, list[str]]:
    """Name/location/bio from the platform profile the row already points at. Each platform uses its
    own PUBLIC endpoint; an unreachable/absent profile is reported, never guessed."""
    out = {"name": "", "name_method": "", "bio": "", "url": "", "source_kind": "platform_profile"}
    notes: list[str] = []
    handle = actor_for(row)
    platform = (row.get("platform") or row.get("source") or "").lower()
    profile_url = _coerce_url(row.get("profile_url"))
    try:
        if "discourse" in platform or "discourse" in profile_url:
            origin = profile_url.split("/u/")[0] if "/u/" in profile_url else ""
            if origin and handle:
                d = fetch_json(f"{origin}/u/{urllib.parse.quote(handle)}.json") or {}
                u = (d.get("user") or {})
                out["name"] = (u.get("name") or "").strip()
                out["name_method"] = "Discourse profile name"
                out["bio"] = " ".join(x for x in (u.get("bio_raw"), u.get("location")) if x)
                out["url"] = profile_url
        elif "mastodon" in platform and handle:
            host = "https://mastodon.social"
            if profile_url.startswith("http"):
                host = "/".join(profile_url.split("/")[:3])
            d = fetch_json(f"{host}/api/v1/accounts/lookup?" +
                           urllib.parse.urlencode({"acct": handle})) or {}
            out["name"] = (d.get("display_name") or "").strip()
            out["name_method"] = "Mastodon display name"
            fields = " | ".join(f"{f.get('name')}: {f.get('value')}"
                                for f in (d.get("fields") or []) if isinstance(f, dict))
            out["bio"] = re.sub(r"<[^>]+>", " ", (d.get("note") or "")) + " " + fields
            out["url"] = d.get("url") or profile_url
        elif "bluesky" in platform and handle:
            d = fetch_json("https://api.bsky.app/xrpc/app.bsky.actor.getProfile?" +
                           urllib.parse.urlencode({"actor": handle})) or {}
            out["name"] = (d.get("displayName") or "").strip()
            out["name_method"] = "Bluesky display name"
            out["bio"] = (d.get("description") or "")
            out["url"] = f"https://bsky.app/profile/{handle}"
        elif profile_url.startswith("http") and handle and "reddit" not in profile_url:
            page = fetch_text(profile_url)
            from . import web_extract as webx
            pd = webx.read_page(page, profile_url)
            out["bio"] = pd.get("text") or ""
            out["url"] = profile_url
    except Exception as e:                    # noqa: BLE001 - profile absent/blocked is normal
        notes.append(f"profile {platform or profile_url[:40]}: {type(e).__name__}: {str(e)[:110]}")
    return out, notes


def _probe(handle: str, fetch_json, fetch_text, cfg: dict) -> tuple[list[dict], list[str]]:
    """Public profile surfaces keyed by the SAME handle. Deliberately allowlisted (no search-engine
    scraping); each probe is one GET and a 404 simply means the handle is not used there."""
    if (not cfg.get("probes", True) or not handle or _ANON_HANDLE.match(handle)
            or ":" in handle):                    # a DID is not a handle other platforms share
        return [], []
    claims: list[dict] = []
    notes: list[str] = []
    h = urllib.parse.quote(handle)
    probes = [
        ("github", f"https://api.github.com/users/{h}"),
        ("linktree", f"https://linktr.ee/{h}"),
        ("aboutme", f"https://about.me/{h}"),
        ("keybase", f"https://keybase.io/_/api/1.0/user/lookup.json?username={h}"),
    ]
    for kind, url in probes:
        try:
            if kind == "github":
                d = fetch_json(url) or {}
                nm = valid_name(d.get("name") or "")
                if nm:
                    claims.append({"kind": "NAME", "value": d.get("name"), "normalized": nm,
                                   "source_url": url, "source_kind": "handle_probe",
                                   "method": "GitHub profile name", "strength": 0.6,
                                   "evidence": f"GitHub {handle}: name={d.get('name')} "
                                               f"location={d.get('location') or '-'} "
                                               f"blog={d.get('blog') or '-'}"})
                loc = place_claims(f"I live in {d.get('location') or ''}", source_url=url,
                                   source_kind="handle_probe") if d.get("location") else []
                claims += loc
            elif kind == "keybase":
                d = fetch_json(url) or {}
                p = ((d.get("them") or [{}])[0] or {}).get("profile") or {}
                nm = valid_name(p.get("full_name") or "")
                if nm:
                    claims.append({"kind": "NAME", "value": p.get("full_name"),
                                   "normalized": nm, "source_url": url,
                                   "source_kind": "handle_probe",
                                   "method": "Keybase full name", "strength": 0.65,
                                   "evidence": f"Keybase {handle}: {p.get('full_name')} "
                                               f"loc={p.get('location') or '-'}"})
            else:                                   # linktree / about.me: HTML title carries the name
                html = fetch_text(url)
                from . import web_extract as webx
                title = webx.read_page(html, url).get("title") or ""
                nm = valid_name(re.split(r"[|\-–—·]", title)[0]) if title else ""
                if nm:
                    claims.append({"kind": "NAME", "value": nm, "normalized": nm,
                                   "source_url": url, "source_kind": "handle_probe",
                                   "method": f"{kind} page title", "strength": 0.5,
                                   "evidence": f"{kind}/{handle} title: {title[:120]}"})
                    claims += place_claims(f"I live in {title}", source_url=url,
                                           source_kind="handle_probe")
        except Exception as e:                  # noqa: BLE001 - 404 is the common, expected answer
            code = getattr(e, "code", None)
            if code not in (404, 400, 401, 403):
                notes.append(f"{kind} probe: {type(e).__name__}: {str(e)[:90]}")
        time.sleep(0.4)
    return claims, notes


# ------------------------------------------------------------------ decisions
# A name is only a NAME when the person said it about themselves. These are the marker methods that
# count as self-identification; a profile display name or a flair is chosen text and can at best
# corroborate one.
STRONG_NAME_METHODS = frozenset(m for _p, _s, m in _NAME_PATTERNS) | {"\"I'm <common first name>\""}


def person_shaped_display(name: str) -> bool:
    """Two tokens whose first is a common given name ("Margaret Citta"). A one-word display name
    ("Midnight", "Deanna", "Newbie") is a pseudonym, not an identity, and is never presented as one."""
    toks = (name or "").split()
    return len(toks) == 2 and toks[0].lower() in _FIRST_NAMES


def decide_name(claims: list[dict]) -> tuple[str, str, str, float, str]:
    """(name, status, source, confidence, reason). Only an explicit self-identification can produce a
    name; corroboration from a second independent source verifies it; disagreement is CONFLICT."""
    # re-validate on READ as well as on write: a claim stored under an older policy must not keep a
    # name the current rules refuse (that is how "Ohio" and "Neckbeard" survived a corrected re-run).
    good = [c for c in claims if c.get("status", "ACCEPTED") == "ACCEPTED" and c.get("normalized")
            and valid_name(c["normalized"]) == c["normalized"]]
    if not good:
        return "", "UNRESOLVED", "", 0.0, "no self-identification found in any public source read"
    strong = [c for c in good if c.get("method") in STRONG_NAME_METHODS]
    if not strong:
        # a profile display name shaped like a real name is a CANDIDATE, never a verified identity:
        # "Bartram Park", "Girl Lurker", "Air Fryer Aristocrat" and "Moderator" all arrive this way.
        shaped = [c for c in good
                  if c.get("source_kind") in ("platform_profile", "handle_probe", "linked_page")
                  and person_shaped_display(c["normalized"])]
        if shaped:
            best = max(shaped, key=lambda c: c["strength"])
            return best["normalized"], "PROBABLE", best["source_url"], 0.5,                 ("profile display name shaped like a person's name, but nobody self-identified: "
                 "candidate only")
        return "", "UNRESOLVED", "", 0.0,             ("only pseudonymous profile text (flair/handle/display name); no self-identification")
    # "John" and "John Smith" are the SAME identity, not a disagreement: merge a name that is a
    # token-prefix of a fuller one and keep the fuller form.
    groups: dict[str, list[dict]] = {}
    for norm in sorted({c["normalized"] for c in strong}, key=lambda n: -len(n.split())):
        ntok = norm.split()
        rep = next((r for r in groups
                    if r.split() == ntok[:len(r.split())]        # stored name is a prefix
                    or ntok == r.split()[:len(ntok)]),            # this name is a prefix
                   norm)
        groups.setdefault(rep, []).extend(c for c in strong if c["normalized"] == norm)
    if len(groups) > 1:
        names = ", ".join(sorted(groups))
        return "", "CONFLICT", "", 0.0, (f"public sources disagree ({names}); refusing to choose one")
    name, cs = next(iter(groups.items()))
    # agreement from any other source (a profile display name that matches, or the fuller form of the
    # same name) counts as corroboration, and the FULLER form wins: "John" + "John Smith" is one
    # identity, and the operator should see "John Smith".
    cs += [c for c in good if c not in cs
           and (c["normalized"].split() == name.split()[:len(c["normalized"].split())]
                or name.split() == c["normalized"].split()[:len(name.split())])]
    name = max({c["normalized"] for c in cs}, key=lambda n: len(n.split()))
    urls = {c["source_url"] for c in cs if c.get("source_url")}
    kinds = {c["source_kind"] for c in cs}
    best = max(cs, key=lambda c: c["strength"])
    if len(urls) >= 2:
        return name, "VERIFIED", best["source_url"], round(min(0.93, 0.72 + 0.06 * len(cs)), 2),             f"self-identified and corroborated by {len(urls)} public source(s): " + "; ".join(sorted(kinds))
    if best["strength"] >= 0.8:
        return name, "PROBABLE", best["source_url"], round(best["strength"] - 0.1, 2),             f"single explicit self-identification ({best['method']}) — not corroborated"
    return name, "PROBABLE", best["source_url"], 0.45,         f"weak single mention ({best['method']}) — not corroborated"


def decide_phone(claims: list[dict], name_status: str, name_value: str,
                 name_claims_all: list[dict]) -> tuple[str, str, str, float, str]:
    """(phone, status, source, confidence, reason). A number without an established owner is not a
    contact: that is exactly the 'unrelated directory listing' the pipeline must not store."""
    good = [c for c in claims if c.get("status", "ACCEPTED") == "ACCEPTED" and c.get("normalized")]
    if not good:
        rejected = [c for c in claims if c.get("status") == "REJECTED"]
        if rejected:
            return "", "REJECTED", "", 0.0, "; ".join(
                sorted({c.get("reject_reason") or "refused" for c in rejected}))[:300]
        return "", "UNRESOLVED", "", 0.0, "no number published by this identity"
    dials = {c["normalized"] for c in good}
    if len(dials) > 1:
        return "", "CONFLICT", "", 0.0, f"{len(dials)} different numbers published by this account"
    best = max(good, key=lambda c: c["strength"])
    if name_status != "VERIFIED" or not name_value:
        # the number is real and published by this account, but its owner is not established:
        # keep it as a labelled CANDIDATE (never presented as a usable contact record).
        return best["normalized"], "PROBABLE", best["source_url"], 0.4, \
            (f"number published by this account ({best['source_kind']}) but the identity is "
             f"{name_status}: held as a candidate, NOT a usable contact")
    # the number and the name must come from the same identity, not merely the same account
    same = [n for n in name_claims_all
            if n.get("status", "ACCEPTED") == "ACCEPTED"
            and n.get("source_url") == best["source_url"]]
    conf = 0.9 if same else 0.82
    why = ("name and number published together on the same page/post"
           if same else "number published by the identity that spoke the need; name corroborated")
    return best["normalized"], "VERIFIED", best["source_url"], conf, why


# ------------------------------------------------------------------ persistence
def _save_claims(con, pid: int, claims: list[dict], is_test: int) -> dict:
    now = _now()
    accepted = rejected = 0
    for c in claims:
        status = c.get("status", "ACCEPTED")
        cur = con.execute(
            "INSERT OR IGNORE INTO identity_claims (prospect_id, claim_kind, value, normalized, "
            "source_url, source_kind, method, evidence, strength, status, reject_reason, is_test, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, c["kind"], (c.get("value") or "")[:200], c.get("normalized") or "",
             c.get("source_url") or "", c.get("source_kind") or "", c.get("method") or "",
             (c.get("evidence") or "")[:400], float(c.get("strength") or 0),
             status, (c.get("reject_reason") or "")[:200], is_test, now, now))
        if cur.rowcount:
            accepted += 1 if status == "ACCEPTED" else 0
            rejected += 1 if status == "REJECTED" else 0
    con.commit()
    return {"accepted": accepted, "rejected": rejected}


def _claims_for(con, pid: int) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in con.execute("SELECT claim_kind, value, normalized, source_url, source_kind, method, "
                         "evidence, strength, status, reject_reason FROM identity_claims "
                         "WHERE prospect_id=? ORDER BY id", (pid,)):
        d = dict(r)
        out.setdefault(d["claim_kind"], []).append(d)
    return out


def _consented(con, lead_id: str) -> bool:
    if not lead_id:
        return False
    r = con.execute("SELECT 1 FROM consents WHERE lead_id=? AND COALESCE(consent_text,'')!=''",
                    (lead_id,)).fetchone()
    return bool(r)


def resolve(con: sqlite3.Connection, pid: int, *, fetch_json=None, fetch_text=None,
            sleeper=time.sleep, probes: bool = False, cfg: dict | None = None) -> dict:
    """Resolve ONE prospect. Reads public sources, stores every claim with its provenance, and
    writes only sufficiently-supported values onto the row. Never contacts anyone."""
    cfg = cfg if isinstance(cfg, dict) else load_cfg()
    fetch_json = fetch_json or _default_json
    fetch_text = fetch_text or _default_text
    row = con.execute("SELECT * FROM public_prospects WHERE id=?", (pid,)).fetchone()
    if not row:
        return {"id": pid, "status": "missing"}
    row = dict(row)
    handle = actor_for(row)
    platform = (row.get("platform") or row.get("source") or "").lower()
    claims: list[dict] = []
    bad: list[dict] = []
    notes: list[str] = []
    dossier_read = profile_read = 0

    if handle and not _ANON_HANDLE.match(handle):
        if "reddit" in platform or "reddit" in (row.get("source") or "") or not platform:
            for kind in ("posts", "comments"):
                items, errs = _archive(handle, kind, fetch_json, cfg)
                notes += errs
                dossier_read += len(items)
                for it in items:
                    sk = "archive_post" if kind == "posts" else "archive_comment"
                    claims += name_claims(it["text"], source_url=it["url"], source_kind=sk)
                    claims += place_claims(it["text"], source_url=it["url"], source_kind=sk)
                    okp, badp = phone_claims(it["text"], source_url=it["url"], source_kind=sk)
                    claims += okp
                    bad += badp
                sleeper(float(cfg.get("pause_seconds", 1.2)))
        prof, n2 = _profile_facts(row, fetch_json, fetch_text)
        notes += n2
        if prof.get("url"):
            profile_read = 1
        # a profile display name is only a NAME when the account is a person: an org-shaped bio
        # ("our agency", "we sell") or a trade noun in the name is a business, never a consumer
        nm = "" if org_shaped(prof.get("bio") or "") else valid_name(prof.get("name") or "")
        if nm:
            claims.append({"kind": "NAME", "value": prof["name"], "normalized": nm,
                           "source_url": prof["url"] or row.get("source_url") or "",
                           "source_kind": "platform_profile",
                           "method": prof.get("name_method") or "profile display name",
                           "strength": 0.75, "evidence": f"{prof.get('name_method')}: {prof['name']}"})
        if prof.get("bio"):
            claims += place_claims(prof["bio"], source_url=prof["url"] or "",
                                   source_kind="platform_profile")
            okp, badp = phone_claims(prof["bio"], source_url=prof["url"] or "",
                                     source_kind="platform_profile")
            claims += okp
            bad += badp
            hop, hop_notes = _linked_page(linked_url(prof["bio"]), fetch_text, cfg)
            claims += hop
            notes += hop_notes
        if probes:
            pc, n3 = _probe(handle, fetch_json, fetch_text, cfg)
            claims += pc
            notes += n3

    # org-shaped material is an organisation contact, never this consumer's
    if org_shaped(row.get("stated_need") or ""):
        claims = [c for c in claims if c["kind"] not in ("NAME", "PHONE")]
        notes.append("row text is org-shaped: identity/contact claims dropped")

    claims = [c for c in claims if not org_shaped(c.get("evidence") or "")
              and not org_shaped(c.get("value") or "")]
    stats = _save_claims(con, pid, claims + bad, int(row.get("is_test") or 0))
    allc = _claims_for(con, pid)

    name, nst, nsrc, nconf, nwhy = decide_name(allc.get("NAME", []))
    phone, pst, psrc, pconf, pwhy = decide_phone(
        [c for c in allc.get("PHONE", []) if c["status"] == "ACCEPTED"]
        + [c for c in allc.get("PHONE", []) if c["status"] == "REJECTED"],
        nst, name, allc.get("NAME", []))
    locs = sorted({c["normalized"] for c in allc.get("LOCATION", []) if c["status"] == "ACCEPTED"})
    location = locs[0] if len(locs) == 1 else (locs[0] if locs else "")

    match_status = ("MATCHED" if nst == "VERIFIED" else
                    "REJECTED" if nst in ("CONFLICT", "REJECTED") else
                    "PARTIAL" if nst == "PROBABLE" else "UNRESOLVED")
    reason = f"name: {nwhy}" + (f" | phone: {pwhy}" if phone or pst != "UNRESOLVED" else "")
    perm = {"CONSUMER_STATED": "SELF_PUBLISHED", "INVITED_INQUIRIES": "INVITED",
            "OPERATOR_VERIFIED": "INVITED"}.get(row.get("permission_status") or "NONE", "NONE")
    if perm == "NONE" and any(c["claim_kind"] in ("PHONE", "EMAIL") for cs in allc.values()
                              for c in cs if c["status"] == "ACCEPTED"):
        # they published a contact surface themselves - still not permission to market to it
        perm = "SELF_PUBLISHED"
    if _consented(con, row.get("lead_id") or ""):
        perm, elig = "CONSENTED", "ELIGIBLE_CONSENTED"
    elif perm != "NONE":
        elig = "BLOCKED_UNVERIFIED"
    else:
        elig = "BLOCKED_NO_CONSENT"

    seen_kinds = {c["source_kind"] for cs in allc.values() for c in cs if c.get("source_kind")}
    evidence = {"dossier_items_read": dossier_read, "profile_read": profile_read,
                "sources": sorted(seen_kinds),
                "name_rule": nwhy, "phone_rule": pwhy,
                "locations_seen": locs[:5], "notes": notes[:6],
                "rejected_claims": [{"kind": c["kind"], "value": c["value"],
                                     "why": c.get("reject_reason") or ""}
                                    for c in bad[:8]]}
    con.execute(
        "UPDATE public_prospects SET identity_name=?, identity_name_status=?, identity_name_source=?, "
        "identity_name_confidence=?, identity_phone=?, identity_phone_status=?, "
        "identity_phone_source=?, identity_phone_confidence=?, location_resolved=?, match_status=?, "
        "match_reason=?, contact_permission=?, marketing_eligibility=?, identity_evidence=?, "
        "resolved_at=?, resolve_attempts=COALESCE(resolve_attempts,0)+1, resolve_last_error=?, "
        "updated_at=? WHERE id=?",
        (name, nst, nsrc, nconf, phone, pst, psrc, pconf, location[:80], match_status,
         reason[:600], perm, elig, json.dumps(evidence), _now(), "; ".join(notes)[:300], _now(), pid))
    con.commit()
    return {"id": pid, "name": name, "name_status": nst, "name_source": nsrc,
            "phone": phone, "phone_status": pst, "phone_source": psrc,
            "location": location, "match_status": match_status, "match_reason": reason,
            "permission": perm, "eligibility": elig,
            "claims_accepted": stats["accepted"], "claims_rejected": stats["rejected"],
            "dossier_items": dossier_read, "notes": notes[:6]}


def adopt_consented_lead(con: sqlite3.Connection, pid: int) -> bool:
    """THE ONLY lawful route to a phone number we may actually use: the person's own submission.

    Identity resolution can find a name in public, but a consumer seeking coverage on an anonymous
    forum does not publish a personal number - measured: 0 of 82 resolved rows had one anywhere in
    their public history, and the numbers that DO appear are agency/toll-free lines that must be
    refused. The one genuinely matched, dialable number is the one the person types into /check
    together with a consent record. This copies it onto the prospect row and marks marketing
    ELIGIBLE_CONSENTED, but only when all three facts exist: a link to that lead, the lead itself,
    and a non-empty consent row. Test records are never adopted.
    """
    r = con.execute("SELECT id, lead_id, is_test FROM public_prospects WHERE id=?", (pid,)).fetchone()
    if not r or not r["lead_id"] or int(r["is_test"] or 0):
        return False
    lead = con.execute("SELECT lead_id, first_name, last_name, phone, is_test FROM leads "
                       "WHERE lead_id=?", (r["lead_id"],)).fetchone()
    if not lead or int(lead["is_test"] or 0):
        return False
    cons = con.execute("SELECT consent_text FROM consents WHERE lead_id=? "
                       "AND COALESCE(consent_text,'')!=''", (lead["lead_id"],)).fetchone()
    if not cons:
        return False
    name = valid_name(" ".join(x for x in (lead["first_name"], lead["last_name"]) if x))
    phone = normalize_phone(lead["phone"] or "")
    if not name and not phone:
        return False
    src = f"lead:{lead['lead_id']}"
    why = (f"consumer submitted /check themselves with a recorded consent row ({lead['lead_id']}); "
           f"this is the only phone route that is lawful to act on")
    now = _now()
    for kind, value, norm in (("NAME", name, name), ("PHONE", phone, phone)):
        if not norm:
            continue
        con.execute(
            "INSERT OR IGNORE INTO identity_claims (prospect_id, claim_kind, value, normalized, "
            "source_url, source_kind, method, evidence, strength, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, kind, value, norm, src, "consumer_submission",
             "consumer submitted /check with consent", why, 1.0, "ACCEPTED", now, now))
    sets, args = [], []
    if name:
        sets += ["identity_name=?", "identity_name_status='VERIFIED'", "identity_name_source=?",
                 "identity_name_confidence=1.0"]
        args += [name, src]
    if phone:
        sets += ["identity_phone=?", "identity_phone_status='VERIFIED'", "identity_phone_source=?",
                 "identity_phone_confidence=1.0"]
        args += [phone, src]
    sets += ["match_status='MATCHED'", "match_reason=?", "contact_permission='CONSENTED'",
             "marketing_eligibility='ELIGIBLE_CONSENTED'", "resolved_at=?", "updated_at=?"]
    args += [why[:600], now, now, pid]
    con.execute(f"UPDATE public_prospects SET {', '.join(sets)} WHERE id=?", args)
    con.commit()
    return True


def resolve_queue(con: sqlite3.Connection, *, ids: list[int] | None = None, limit: int = 25,
                  min_intent: str = "STRONG", include_rejected: bool = False,
                  fetch_json=None, fetch_text=None, sleeper=time.sleep, probes: bool = False,
                  cfg: dict | None = None) -> dict:
    """Run resolution over the operator's working queue (ACTIVE/STRONG, servable state, fresh) or an
    explicit id list. Returns measured counters — the numbers a report can be held to."""
    cfg = cfg if isinstance(cfg, dict) else load_cfg()
    if ids:
        targets = [int(i) for i in ids][:limit]
    else:
        order = ("CASE intent_strength WHEN 'ACTIVE' THEN 0 WHEN 'STRONG' THEN 1 ELSE 2 END")
        want = ("('ACTIVE','STRONG')" if min_intent == "STRONG" else
                "('ACTIVE')" if min_intent == "ACTIVE" else "('ACTIVE','STRONG','WEAK')")
        sql = ("SELECT id FROM public_prospects WHERE COALESCE(intent_strength,'') IN " + want +
               " AND COALESCE(state_relevant,'') IN ('TARGET','UNKNOWN')")
        sql += " AND COALESCE(is_test,0)=0"          # test rows are never resolved as prospects
        if not include_rejected:
            sql += " AND status NOT IN ('REJECTED','DUPLICATE')"
        sql += f" ORDER BY {order}, COALESCE(resolved_at,'')='' DESC, confidence DESC LIMIT ?"
        targets = [r[0] for r in con.execute(sql, (int(limit),))]
    stats = {"rows_scanned": 0, "rows_resolved": 0, "names_found": 0, "names_verified": 0,
             "phones_found": 0, "phones_verified": 0, "claims_rejected": 0, "errors": 0,
             "by_source": {}, "results": [], "notes": []}
    for pid in targets:
        stats["rows_scanned"] += 1
        try:
            r = resolve(con, pid, fetch_json=fetch_json, fetch_text=fetch_text,
                        sleeper=sleeper, probes=probes, cfg=cfg)
        except Exception as e:                # noqa: BLE001 - one row never kills a run
            stats["errors"] += 1
            con.execute("UPDATE public_prospects SET resolve_last_error=?, updated_at=? WHERE id=?",
                        (f"{type(e).__name__}: {str(e)[:200]}", _now(), pid))
            con.commit()
            stats["notes"].append(f"#{pid}: {type(e).__name__}: {str(e)[:120]}")
            continue
        stats["results"].append(r)
        if r.get("name"):
            stats["names_found"] += 1
            if r["name_status"] == "VERIFIED":
                stats["names_verified"] += 1
                src = (r.get("name_source") or "").split("//")[-1].split("/")[0]
                stats["by_source"][src] = stats["by_source"].get(src, 0) + 1
        if r.get("phone"):
            stats["phones_found"] += 1
            if r["phone_status"] == "VERIFIED":
                stats["phones_verified"] += 1
        stats["claims_rejected"] += int(r.get("claims_rejected") or 0)
        if r.get("match_status") in ("MATCHED", "PARTIAL", "REJECTED"):
            stats["rows_resolved"] += 1
    con.execute("INSERT INTO identity_runs (ts, rows_scanned, rows_resolved, names_found, "
                "names_verified, phones_found, phones_verified, claims_rejected, errors, note) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (_now(), stats["rows_scanned"], stats["rows_resolved"], stats["names_found"],
                 stats["names_verified"], stats["phones_found"], stats["phones_verified"],
                 stats["claims_rejected"], stats["errors"],
                 f"probes={'on' if probes else 'off'} ids={'explicit' if ids else 'queue'}"))
    con.commit()
    return stats


# ------------------------------------------------------------------ reporting
def report(con: sqlite3.Connection) -> dict:
    """What the stage actually produced, split by evidence source and refusal reason."""
    one = lambda sql, a=(): con.execute(sql, a).fetchone()[0]      # noqa: E731
    by_source = [dict(r) for r in con.execute(
        "SELECT source_kind, claim_kind, COUNT(*) claims, "
        "SUM(status='ACCEPTED') accepted, SUM(status='REJECTED') rejected "
        "FROM identity_claims GROUP BY 1,2 ORDER BY claims DESC")]
    reasons = [dict(r) for r in con.execute(
        "SELECT reject_reason, COUNT(*) n FROM identity_claims WHERE status='REJECTED' "
        "GROUP BY 1 ORDER BY n DESC LIMIT 12")]
    return {
        "rows_total": one("SELECT COUNT(*) FROM public_prospects"),
        "rows_attempted": one("SELECT COUNT(*) FROM public_prospects WHERE resolve_attempts>0"),
        "matches": {r[0]: r[1] for r in con.execute(
            "SELECT match_status, COUNT(*) FROM public_prospects WHERE resolve_attempts>0 GROUP BY 1")},
        "names": {r[0]: r[1] for r in con.execute(
            "SELECT identity_name_status, COUNT(*) FROM public_prospects WHERE resolve_attempts>0 "
            "GROUP BY 1")},
        "phones": {r[0]: r[1] for r in con.execute(
            "SELECT identity_phone_status, COUNT(*) FROM public_prospects WHERE resolve_attempts>0 "
            "GROUP BY 1")},
        "usable": one("SELECT COUNT(*) FROM public_prospects WHERE identity_name_status='VERIFIED' "
                      "AND identity_phone_status='VERIFIED'"),
        "name_verified": one("SELECT COUNT(*) FROM public_prospects "
                             "WHERE identity_name_status='VERIFIED'"),
        "phone_verified": one("SELECT COUNT(*) FROM public_prospects "
                              "WHERE identity_phone_status='VERIFIED'"),
        "marketed": one("SELECT COUNT(*) FROM public_prospects "
                        "WHERE marketing_eligibility='ELIGIBLE_CONSENTED'"),
        "by_source": by_source, "reject_reasons": reasons,
        "runs": [dict(r) for r in con.execute(
            "SELECT * FROM identity_runs ORDER BY id DESC LIMIT 5")],
    }
