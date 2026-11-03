"""PUBLIC PROSPECTS — the automated lead-finding store.

The corrected acquisition direction: an ACTIVE discovery system that finds people publicly
expressing a health-coverage need, classifies them, keeps the evidence, and files them into the
EXISTING lead database (`data/leads.db`) so operators review them beside everything else.

The one boundary that does not move: **a public post is not a lead and not permission.** Rows live
in `public_prospects`, never in `leads`. `leads` keeps its own guarantees untouched (the consumers'
own phone, their consent row, the DB trigger that only accepts CONSUMER_SUBMITTED phone numbers,
routing, billing, exclusivity). A prospect reaches `ROUTING_ELIGIBLE` only when that same person
submitted the /check form themselves and the row is linked to the resulting lead.

Status ladder (exactly the operator vocabulary):
  NEW_PROSPECT                 need found, no usable contact
  CONTACT_INFORMATION_FOUND    a public contact exists; we hold NO permission basis
  CONTACT_PERMISSION_REQUIRED  a public contact AND invitation wording: an operator must read it
  CONSENTED                    the person authorized contact, with recorded evidence
  VERIFIED                     an operator opened the source and confirmed identity + currency
  QUALIFIED                    verified + contactable + relevant need -> may receive the /check link
  ROUTING_ELIGIBLE             that person submitted /check themselves (lead_id set) -> a real lead
  REJECTED                     operator decision with a recorded reason
  DUPLICATE                    same person/post or same published contact already stored

Automation stops at CONTACT_PERMISSION_REQUIRED / CONTACT_INFORMATION_FOUND. Everything above
CONSENTED is a human action with an evidence note, enforced in code.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import prospect_contacts as pc

STATUSES = ("NEW_PROSPECT", "CONTACT_INFORMATION_FOUND", "CONTACT_PERMISSION_REQUIRED",
            "CONSENTED", "VERIFIED", "QUALIFIED", "ROUTING_ELIGIBLE", "REJECTED", "DUPLICATE")
AUTOMATED_STATUSES = ("NEW_PROSPECT", "CONTACT_INFORMATION_FOUND", "CONTACT_PERMISSION_REQUIRED",
                      "DUPLICATE")
NEXT_ACTION = {
    "NEW_PROSPECT": "none yet: need recorded, no contact published. Revisit if they post again.",
    "CONTACT_INFORMATION_FOUND": "do NOT contact. A published number is not permission.",
    "CONTACT_PERMISSION_REQUIRED": "operator: open the source URL; if the author invited contact, record "
                                   "that evidence before anything else.",
    "CONSENTED": "operator: confirm the consent is current and covers an insurance follow-up.",
    "VERIFIED": "operator: confirm the need is still current, then QUALIFY to offer the /check link.",
    "QUALIFIED": "operator may place the /check link where this person will see it. They submit themselves.",
    "ROUTING_ELIGIBLE": "the consumer submitted /check themselves; the normal lead pipeline owns it now.",
    "REJECTED": "closed. Reason kept on the row for audit.",
    "DUPLICATE": "merged into the earlier row (dup_of). Kept so both posts stay verifiable.",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS public_prospects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  prospect_key TEXT UNIQUE,          -- stable hash of (source|url); idempotent ingestion
  source TEXT,                       -- adapter id: citydata_forum | discourse | legacy | manual
  platform TEXT DEFAULT '', community TEXT DEFAULT '',
  source_url TEXT,                   -- the public permalink the operator opens
  profile_url TEXT DEFAULT '',       -- public profile page, when the source gives one
  author_handle TEXT DEFAULT '',     -- public username ONLY (never a real name we inferred)
  display_name TEXT DEFAULT '',
  stated_need TEXT,                  -- what the person wrote, trimmed
  need_topics TEXT DEFAULT '',       -- comma list from the shared topic taxonomy
  lang TEXT DEFAULT 'en', geo TEXT DEFAULT '',
  observed_at TEXT DEFAULT '',       -- post date where the source publishes one
  discovered_at TEXT, last_seen_at TEXT,
  contact_methods TEXT DEFAULT '[]', -- JSON [{kind, value, evidence}]
  contact_status TEXT DEFAULT 'CONTACT_NOT_AVAILABLE',
  contact_evidence TEXT DEFAULT '',
  permission_status TEXT DEFAULT 'NONE',   -- NONE | INVITED_INQUIRIES | OPERATOR_VERIFIED | CONSUMER_STATED
  permission_evidence TEXT DEFAULT '',
  status TEXT DEFAULT 'NEW_PROSPECT', status_reason TEXT DEFAULT '',
  confidence REAL DEFAULT 0.0,
  evidence TEXT DEFAULT '{}',        -- JSON: matched phrases, adapter, robots note, terms note
  dup_status TEXT DEFAULT 'UNIQUE', dup_of TEXT DEFAULT '',
  lead_id TEXT DEFAULT '',           -- set ONLY by link_lead(): the person's own /check submission
  intent_strength TEXT DEFAULT '',   -- ACTIVE (asking now) | STRONG (situation + implied ask) | WEAK
  state_relevant TEXT DEFAULT '',    -- FL (target state) | OTHER (explicitly elsewhere) | UNKNOWN
  engagement TEXT DEFAULT '',        -- THREAD_REPLY (a public thread where an answer is allowed) | NONE
  tracked_link TEXT DEFAULT '',      -- /go/<code> issued for this prospect's thread (utm_content=p<id>)
  link_issued_at TEXT DEFAULT '',
  operator_note TEXT DEFAULT '', reviewed_at TEXT DEFAULT '',
  is_test INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_pp_status ON public_prospects(status, discovered_at);
CREATE INDEX IF NOT EXISTS ix_pp_source ON public_prospects(source, status);
CREATE TABLE IF NOT EXISTS discovery_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, source TEXT,
  fetched INTEGER DEFAULT 0, candidates INTEGER DEFAULT 0, accepted INTEGER DEFAULT 0,
  new_rows INTEGER DEFAULT 0, duplicates INTEGER DEFAULT 0, skipped INTEGER DEFAULT 0,
  errors INTEGER DEFAULT 0, note TEXT DEFAULT '',
  emails INTEGER DEFAULT 0, phones INTEGER DEFAULT 0, contact_rows INTEGER DEFAULT 0
);
"""


def ensure(con: sqlite3.Connection) -> sqlite3.Connection:
    """Create the prospect tables on an EXISTING lead connection (startup, tests).

    Also widens an existing `discovery_runs` with the contact-yield counters: a sweep must report
    how many real emails and numbers it actually extracted, not just how many pages it read.
    """
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(discovery_runs)")}
    for col in ("emails", "phones", "contact_rows", "engagement_ready", "links_issued",
                "out_of_state", "no_intent", "stale"):
        if col not in have:
            con.execute(f"ALTER TABLE discovery_runs ADD COLUMN {col} INTEGER DEFAULT 0")
    # v2 columns on an existing prospect table: intent grading, the state gate and the public
    # engagement route. A row may now be actionable WITHOUT a published email/phone, which is the
    # correction: the old schema made a contact the only door out of NEW_PROSPECT.
    have = {r[1] for r in con.execute("PRAGMA table_info(public_prospects)")}
    for col, typ in (("intent_strength", "TEXT DEFAULT ''"), ("state_relevant", "TEXT DEFAULT ''"),
                     ("engagement", "TEXT DEFAULT ''"), ("tracked_link", "TEXT DEFAULT ''"),
                     ("link_issued_at", "TEXT DEFAULT ''"), ("kit_json", "TEXT DEFAULT ''"),
                     ("kit_at", "TEXT DEFAULT ''")):
        if col not in have:
            con.execute(f"ALTER TABLE public_prospects ADD COLUMN {col} {typ}")
    # identity resolution stage: additive columns (name/phone/location/match/eligibility) plus its
    # own claim + provenance tables. Imported here to keep the discovery module free of the
    # extraction rules it does not use.
    from . import identity as identity_mod
    identity_mod.connect_ok(con)
    # third-party contact admission (imported_contacts + its consent guards). Imported here so
    # every path that builds a lead database — app startup, tools, tests — carries the guarantees.
    from . import admission as admission_mod
    admission_mod.ensure(con)
    con.commit()
    return con


def connect(db_path: Path | str) -> sqlite3.Connection:
    """A lead database with BOTH schemas: the lead pipeline's tables (consumers, consent, routing)
    and the public-prospect tables. One file, two populations, no crossing."""
    from .db import connect as _lead_connect
    con = _lead_connect(db_path)
    return ensure(con)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def key_for(source: str, url: str) -> str:
    """Same post found twice collapses; the URL is the identity, not the author."""
    return hashlib.sha1(f"{(source or '').lower()}|{norm_url(url)}".encode()).hexdigest()[:24]


def norm_url(url: str) -> str:
    """One permalink, one post. Two adapters read the same Reddit thread (a board firehose and a
    scoped search) and the source-qualified key stored it twice: 19 extra rows out of 226. The
    identity is the URL; tracking parameters, fragments, scheme and a trailing slash are not."""
    u = (url or "").strip().lower()
    u = re.sub(r"^https?://(www\.|old\.|new\.)?", "", u)
    u = u.split("#", 1)[0].split("?", 1)[0]
    return u.rstrip("/")


def dedupe_by_url(con) -> dict:
    """Collapse rows that are the SAME permalink reached through different adapters.

    Keeps the earliest row (the original discovery, with its own evidence) and marks the later ones
    DUPLICATE with dup_of pointing at it, so both remain auditable and the queue stops double-counting.
    A row a human already advanced is never demoted.
    """
    stats = {"groups": 0, "marked": 0}
    human = ("CONSENTED", "VERIFIED", "QUALIFIED", "ROUTING_ELIGIBLE")
    groups: dict[str, list[dict]] = {}
    for r in con.execute("SELECT id, prospect_key, source_url, status, discovered_at "
                         "FROM public_prospects ORDER BY id"):
        groups.setdefault(norm_url(r["source_url"]), []).append(dict(r))
    for _, rows in groups.items():
        if len(rows) < 2:
            continue
        keeper = next((x for x in rows if x["status"] in human), rows[0])
        stats["groups"] += 1
        for x in rows:
            if x["id"] == keeper["id"]:
                continue
            if x["status"] in ("DUPLICATE", "REJECTED") or x["status"] in human:
                continue
            con.execute("UPDATE public_prospects SET status='DUPLICATE', dup_status='DUPLICATE', "
                        "dup_of=?, status_reason=?, operator_note=?, updated_at=? WHERE id=?",
                        (keeper["prospect_key"], NEXT_ACTION["DUPLICATE"],
                         "same permalink already stored as #%d via %s" % (keeper["id"], keeper["prospect_key"]),
                         _now(), x["id"]))
            stats["marked"] += 1
    con.commit()
    return stats


# ------------------------------------------------------------------ classification
# Signals that the author is selling rather than seeking. These rows are never prospects: an agent
# answering questions, an agency promo, a lead-gen spam post, a content-farm rewrite.
_SELLER = re.compile(
    r"\b(i am (?:a|an) (?:licensed )?(?:agent|broker|advisor)|as an agent|our agency|my agency|"
    r"we offer|we sell|contact our office|call our office|get a free quote today|"
    r"best rates guaranteed|i can get you|dm me for (?:a )?quote|licensed in \d+ states|"
    r"insurance group|financial services|we are hiring|commission|book a call|"
    r"i help (?:people|families|clients|anyone)|i (?:can )?help (?:people|families|clients)|"
    r"i work with (?:carriers|multiple carriers|clients)|my clients|my book of business|"
    r"(?:\$0|zero)(?:/| a | per )month plans?|plans? (?:starting|as low as)|"
    r"for those who qualify|enroll (?:you|them)|i'?ll (?:help|get) you|"
    r"soy agente|nuestra agencia|le ofrecemos|cotizaci[oó]n gratis|te ayudo a (?:inscribirte|encontrar))\b",
    re.IGNORECASE)
_ORG_SHAPE = re.compile(
    r"\b(llc|inc\.?|corp\.?|l\.?l\.?c|company|agency|brokerage|consulting|group|associates|"
    r"clinic|hospital|foundation|nonprofit|non-profit|department of)\b", re.IGNORECASE)
_SPAM = re.compile(
    r"(https?://\S+){3,}|\b(click here|buy now|limited time offer|crypto|casino|viagra|"
    r"seo services|work from home \$)\b", re.IGNORECASE)
# A NEED, not a mention: coverage vocabulary + a first-person stake + a seeking verb.
_COVERAGE = re.compile(
    r"\b(health insurance|health coverage|health plan|coverage|medicaid|medicare|cobra|marketplace|"
    r"obamacare|premium|premiums|subsid(?:y|ies)|open enrollment|deductible|aca|"
    r"aseguranza|seguro m[eé]dico|cobertura|subsidio|p[oó]liza)\b", re.IGNORECASE)
# An ASK, not a circumstance. 'I am nervous about losing it' is a feeling, not a request; the
# first live sweep produced exactly that false positive, so circumstance verbs were removed and
# only seeking/enrolling/affording/asking language qualifies.
_SEEK = re.compile(
    r"\b(need|needs|looking for|shopping for|searching for|help (?:me )?(?:get|find)|how do i|"
    r"where (?:can|do) i|can(?:'|no)?t afford|cannot afford|afford|"
    r"apply for|enroll|sign up|qualify|my options|what are my options|recommend|anyone know|"
    r"what should i|any advice|does anyone|can someone|"
    r"necesito|busco|no tengo|me qued[eé] sin|c[oó]mo (?:puedo|obtengo)|opciones|ayuda)\b",
    re.IGNORECASE)
# ACTIVE shopping: the person is choosing/enrolling NOW, not musing. This is what separates a
# prospect an operator can serve today from a conversation about insurance in general.
_ACTIVE = re.compile(
    r"\b(looking for (?:a |some |new )?(?:health )?(?:insurance|coverage|plan)|shopping for|"
    r"need (?:to get |help getting |a )?(?:health )?(?:insurance|coverage)|"
    r"how do i (?:get|enroll|sign up|apply)|where (?:can|do) i (?:get|enroll|sign up|apply)|"
    r"what are my options|any (?:plan )?recommendations?|which plan|open enrollment|"
    r"enroll(?:ing)? (?:now|this (?:week|month))|need a quote|get a quote|"
    r"busco (?:un )?(?:plan|seguro)|necesito (?:un )?(?:plan|seguro)|c[oó]mo (?:puedo|hago) para)\b",
    re.IGNORECASE)
# STRONG (not ACTIVE yet): the situation that opens an enrollment window. Someone laid off last
# month is a real prospect even when they have not typed the word "shop".
_STRONG = re.compile(
    r"\b(lost (?:my |our )?(?:job|coverage|insurance)|laid off|layoff|fired|"
    r"cobra (?:is|ends|runs out|expires)|turned 26|aging out|"
    r"premium (?:went up|doubled|increase[ds]?)|plan (?:is )?(?:ending|leaving)|"
    r"moving to (?:florida|fl)|just moved|self[- ]employed|1099|"
    r"me despidieron|perd[ií] (?:mi )?(?:trabajo|cobertura|seguro)|se me acaba)\b",
    re.IGNORECASE)
# A coverage word next to a seek verb is not enough: 'I need IVIG / my medication / a doctor' is a
# TREATMENT need that happened to mention insurance. The full-corpus review found exactly that row
# accepted as a prospect, so the object of the need is checked.
_MEDICAL_OBJECT = re.compile(
    r"\b(ivig|immunoglobulin|infusion|chemo|chemotherapy|dialysis|surgery|transplant|"
    r"medication|medications|prescription|prescriptions|insulin|ozempic|wegovy|mounjaro|"
    r"treatment|treatments|doctor|dentist|dental|therapy|therapist|specialist|hospital bill|"
    r"medical bill|medical bills)\b", re.IGNORECASE)
# Commentary about the SYSTEM is not a personal need: policy arguments, articles, scams, elections.
# The City-Data sweep surfaced an op-ed ('How Medicare Advantage Scams the Government') whose text
# matched need+coverage+first-person, so the guard is explicit.
_COMMENTARY = re.compile(
    r"\b(the government|congress|senate|legislation|the article|read the article|study shows|"
    r"scam|scams|scamming|lobby|policy makers|election|news|report says|according to|"
    r"i'?d encourage|everyone should|we should all)\b", re.IGNORECASE)
# A reporter asking readers to email them is a published contact beside coverage words, and the
# contact-first path stored one on 2026-10-30. It is an interview request, not a consumer need.
_MEDIA = re.compile(
    r"\b(i'?m a (?:reporter|journalist)|i am a (?:reporter|journalist)|for (?:a|my) (?:story|article)|"
    r"working on a (?:story|article|piece)|looking to interview|interview request|"
    r"soy periodista|para un reportaje)\b", re.IGNORECASE)
_NEGATED = re.compile(
    r"\b(don'?t need|do not need|no need|didn'?t need|already have|i have (?:health )?"
    r"(?:insurance|coverage)|no necesito|ya tengo)\b", re.IGNORECASE)

# Advertising copy addresses the READER. 'Whether you're self-employed...' + 'Looking for Health
# Insurance in Florida?' passed as a consumer because a broken title fragment ('Health I 🏥') looked
# like the pronoun I. Second-person solicitation with no first-person stake is a PROMO, not a person.
_PROMO = re.compile(
    r"\b(whether you'?re|if you'?re (?:looking|self|between|uninsured|paying)|you and your family|"
    r"for you and your|we (?:can|will) help you|let us help|message me|dm me|link in bio|"
    r"book a (?:free )?consultation|call today|apply today|sign up today|free quote|get covered today|"
    r"(?:health|insurance) (?:marketplace|agency|group) can help|are you looking for|"
    r"do you need (?:health )?(?:insurance|coverage))\b", re.IGNORECASE)

# A first-person STAKE. A bare "I" is not one: a truncated title ("Health I 🏥") or a stray capital
# in a headline matched it and let an insurance ad through. "I" now counts only when a verb follows.
# 'Coverage' is not a health word. A sweep with consumer phrases admitted car-insurance posts
# ("dealer … proof of insurance", "my policy … driving records") because a generic coverage mention
# plus first person plus a seeking verb is exactly the shape of an auto question. A window that is
# about a vehicle/home and carries NO health token is not a consumer opportunity for this product.
# Visitor/travel cover is a different product (and often a different country's resident).
_TRAVEL = re.compile(
    r"\b(travel (?:medical )?insurance|visitor insurance|travelling from|traveling from|"
    r"visiting the (?:us|u\.s\.|united states)|us trip|trip to the (?:us|u\.s\.)|"
    r"tourist visa|student visa|j1 visa|f1 visa)\b", re.IGNORECASE)

_NONHEALTH = re.compile(
    r"\b(car insurance|auto insurance|vehicle|driver|driving records?|dealer|dealership|collision|"
    r"liability only|towing|motorcycle|renters insurance|homeowners|dwelling|windshield|"
    r"uninsured motorist|policy renewal|my car|the car)\b", re.IGNORECASE)
_HEALTH_TOKEN = re.compile(
    r"\b(health|medical|medicaid|medicare|aca|obamacare|marketplace|healthcare|cobra|subsid\w*|"
    r"prescription|doctor|hospital|dental|vision|deductible|enroll\w*)\b", re.IGNORECASE)
# Advice/warning posts address an audience ('before anyone signs up', 'if anyone reading this is
# searching … do not make the mistake my family made'). Helpful, but not a person asking for cover.
_ADVICE = re.compile(
    r"\b(i strongly (?:recommend|advise)|before anyone signs up|if anyone (?:reading this )?is "
    r"(?:searching|looking)|do not make the mistake|learn from my mistake|warning to anyone|"
    r"psa:?|public service announcement)\b", re.IGNORECASE)

_PERSONAL = re.compile(r"(?:\b(?:i'm|i've|i'll|i'd|my|me|we|our|us|mi|mis|necesito)\b|\bi\s+[a-z])",
                       re.IGNORECASE)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\s*\|\s*|\n+")


def classify(text: str) -> dict:
    """Is this a real consumer expressing a CURRENT coverage need, and how strong is the intent?

    Precision first: the coverage word, the seeking verb and the first-person stake must sit in the
    SAME short window (1-3 sentences, <=450 chars), and the object of the ask must be coverage, not a
    treatment. Matching them anywhere in a long page turns informational threads ('New to Medicare?
    Start here') into fake prospects - the first live sweep proved it, so the rule is window-scoped
    and the matched sentence is stored as the evidence an operator reads.

    `strength` lets the queue be worked top-down:
      ACTIVE  - choosing/enrolling now ("looking for a plan", "how do I enroll", "what are my options")
      STRONG  - a situation that opens a window plus an implied ask ("lost my job", "COBRA ends")
      WEAK    - a coverage need stated with no shopping posture
    """
    t = re.sub(r"\s+", " ", (text or "")).strip()
    out = {"person": False, "need": False, "reason": "", "intent": "generic", "topics": "",
           "matched": [], "sentence": "", "strength": "", "verdict": "NOT_CONSUMER"}
    if len(t) < 40:
        out["reason"] = "too short to judge"
        return out
    if _SELLER.search(t):
        out.update(reason="seller/agent voice", verdict="AGENT")
        return out
    if _PROMO.search(t):
        out.update(reason="advertising copy addressed to readers, not a consumer", verdict="PROMO")
        return out
    if _TRAVEL.search(t):
        out.update(reason="travel/visitor insurance, not US health coverage", verdict="TRAVEL")
        return out
    if _NONHEALTH.search(t) and not _HEALTH_TOKEN.search(t):
        out.update(reason="vehicle/property insurance, not health coverage", verdict="NON_HEALTH")
        return out
    if _MEDIA.search(t):
        out.update(reason="media interview request, not a consumer", verdict="MEDIA")
        return out
    if _SPAM.search(t):
        out.update(reason="spam shape", verdict="SPAM")
        return out
    sents = [x.strip() for x in _SENTENCE.split(t) if x.strip() and len(x.strip()) >= 12]
    reasons = {"coverage": 0, "seek": 0, "person": 0}
    for x in sents:
        reasons["coverage"] += 1 if _COVERAGE.search(x) else 0
        reasons["seek"] += 1 if _SEEK.search(x) else 0
        reasons["person"] += 1 if _PERSONAL.search(x) else 0
    # Tightest evidence first: one sentence, then two, then three (a real question usually splits
    # the situation and the ask: "I lost my job. How do I get coverage?"). Never longer than ~450
    # characters, so a page-wide coincidence cannot qualify.
    for span in (1, 2, 3):
        for i in range(len(sents) - span + 1):
            w = " ".join(sents[i:i + span]).strip()
            if len(w) > 450:
                continue
            cov, seek, per = _COVERAGE.search(w), _SEEK.search(w), _PERSONAL.search(w)
            if not (cov and seek and per) or _NEGATED.search(w) or _COMMENTARY.search(w):
                continue
            if _ADVICE.search(w):
                out["reason"], out["verdict"] = "advice/warning to an audience, not a personal ask", "ADVICE"
                continue
            if _NONHEALTH.search(w) and not _HEALTH_TOKEN.search(w):
                out["reason"], out["verdict"] = "vehicle/property insurance, not health coverage", "NON_HEALTH"
                continue
            if _ORG_SHAPE.search(w) and not re.search(r"\b(i|my|me|we|our)\b", w, re.IGNORECASE):
                continue
            # the ask must be about coverage itself, not about a treatment that mentions coverage
            med = _MEDICAL_OBJECT.search(w)
            if med and not _ACTIVE.search(w):
                out["reason"] = "medical need (%s), not coverage shopping" % med.group(0)
                out["verdict"] = "MEDICAL_NEED"
                continue
            strength = ("ACTIVE" if _ACTIVE.search(w)
                        else "STRONG" if (_STRONG.search(w) or seek.start() <= cov.start())
                        else "WEAK")
            out.update(person=True, need=True, sentence=w, strength=strength,
                       verdict="CONSUMER", matched=[cov.group(0), seek.group(0), per.group(0)])
            out["intent"] = pc.signals.intent_category(w, "")
            out["topics"] = ",".join(sorted({x.lower() for x in re.findall(
                r"\b(cobra|medicaid|medicare|marketplace|obamacare|aca|premium|subsid\w*|"
                r"open enrollment|deductible|job loss|self.employed|pregnan\w*|spouse|divorce)\b",
                w, re.IGNORECASE)}))
            return out
    if not out["reason"]:
        out["reason"] = ("no statement ties a coverage need to this person ("
                         "coverage sentences=%d, seeking=%d, first-person=%d)"
                         % (reasons["coverage"], reasons["seek"], reasons["person"]))
    return out


_STATE_NAMES = ("Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|"
                "Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|"
                "Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|"
                "Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|"
                "Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|"
                "Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming")
_STATE_NAME_RE = re.compile(rf"\b({_STATE_NAMES})\b", re.IGNORECASE)
_STATE_ABBR_RE = re.compile(r"\b(A[LKZR]|C[AOT]|DE|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|N[CDEHJMVY]|"
                            r"O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[AT]|W[AIVY])\b")


# A person who names their city is stating their location as clearly as one who names the state.
# Florida metros only: every entry maps to FL, so a city mention can never imply another state.
_FL_CITIES = re.compile(
    r"\b(miami|hialeah|tampa|orlando|jacksonville|tallahassee|fort lauderdale|lauderdale|"
    r"st\.? petersburg|saint petersburg|clearwater|gainesville|naples|sarasota|daytona|"
    r"palm beach|boca raton|hollywood|pembroke pines|miramar|kissimmee|lakeland|ocala|"
    r"cape coral|fort myers|port st\.? lucie|pensacola|melbourne|bradenton|pompano|"
    r"coral springs|doral|west palm|fort pierce|panama city|ocala)\b", re.IGNORECASE)


# A post can name several states. 'i'm 22 living in NJ, and my father who lives in Florida' was
# tagged FLORIDA because the first state name in the text won - the wrong person's state. A
# first-person location statement therefore outranks any other state mention.
_SELF_LOC = re.compile(
    r"\b(?:i(?:'m| am)?\s+(?:currently\s+)?(?:living|live|based|located)\s+in|i\s+live\s+in|"
    r"living\s+in|based\s+in|moving\s+to|relocat\w*\s+to|just\s+moved\s+to|i(?:'m| am)\s+in)\s+"
    r"([A-Za-z][A-Za-z .'-]{1,24}?)(?=[,.;!?]|\s+(?:and|but|so|which|for|with|since|because|after|now|my|i)\b|$)",
    re.IGNORECASE)


def extract_geo(text: str) -> str:
    """The US state the person actually NAMED about THEMSELVES (or the Florida city they named).

    Priority: an explicit self-location statement ("living in NJ", "moving to Tampa"), then a plain
    state name, then a Florida metro. Nothing is inferred from area codes, IP or a ZIP guess, and one
    person's mention of a relative's state no longer tags the prospect as servable.
    """
    t = text or ""
    for m in _SELF_LOC.finditer(t):
        raw = (m.group(1) or "").strip().strip(".'").lower()
        for name, code in _STATE_CODES.items():
            if raw == name or raw.startswith(name + " "):
                return name.title()      # keep what the person wrote ("Florida"), not a code
        if len(raw) == 2 and raw.upper() in _STATE_CODES.values():
            return raw.upper()
        if _FL_CITIES.fullmatch(raw) or any(c in raw for c in
                                           ("miami", "tampa", "orlando", "jacksonville", "hialeah")):
            return "FL"
    m = _STATE_NAME_RE.search(t)
    if m:
        return m.group(1).title()
    m = _STATE_ABBR_RE.search(t)
    if m:
        return m.group(1)
    m = _FL_CITIES.search(t)
    return "FL" if m else ""


_STATE_CODES = {"alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
                "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
                "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
                "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
                "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
                "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
                "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
                "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
                "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
                "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
                "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
                "wyoming": "WY"}


def state_code(geo: str) -> str:
    g = (geo or "").strip()
    if not g:
        return ""
    if len(g) == 2:
        return g.upper()
    return _STATE_CODES.get(g.lower(), "")


def max_age_days() -> int:
    """How old a public post may be and still count as a CURRENT need. A 2017 thread is not a
    prospect; measured on the first targeted sweep, 16 of 53 rows were over a year old."""
    try:
        cfg = json.loads((Path(__file__).resolve().parents[1] / "config" / "sources.json")
                         .read_text(encoding="utf-8"))
        return int((cfg.get("discovery") or {}).get("max_age_days") or 180)
    except Exception:                                   # noqa: BLE001
        return 180


def age_days(observed_at: str) -> int | None:
    """Days since the post was published, or None when the source gave no date (unknown is not
    stale). Timestamps from every adapter are naive UTC ISO strings."""
    raw = (observed_at or "").strip()
    if not raw:
        return None
    try:
        then = datetime.fromisoformat(raw[:19].replace("Z", ""))
    except ValueError:
        return None
    return max(0, (datetime.utcnow() - then).days)


def target_states() -> tuple:
    """The states this marketplace can actually serve. Read from config so a second state is a
    config change; default Florida because that is the only buyer territory configured."""
    try:
        cfg = json.loads((Path(__file__).resolve().parents[1] / "config" / "sources.json")
                         .read_text(encoding="utf-8"))
        st = (cfg.get("discovery") or {}).get("target_states") or ["FL"]
        return tuple(str(x).upper() for x in st if str(x).strip())
    except Exception:                                   # noqa: BLE001 - config is optional here
        return ("FL",)


# Platforms/paths where a PUBLIC REPLY in the thread is possible. That is an engagement route that
# needs no email or phone at all - the correction to "no contact -> nothing to do".
_DISCUSSION_HOSTS = ("reddit.com", "city-data.com", "stackexchange.com", "stackoverflow.com",
                     "lemmy", "mastodon", "discourse", "beehaw", "hexbear")


def engagement_route(url: str, source: str) -> str:
    u = (url or "").lower()
    if any(h in u for h in _DISCUSSION_HOSTS):
        return "THREAD_REPLY"
    if source in ("signal_store", "legacy", "legacy:bluesky", "legacy:lemmy", "legacy:stackexchange"):
        return "THREAD_REPLY" if "http" in u else "NONE"
    return "NONE"


def intent_confidence(strength: str, engage: str = "", state_rel: str = "",
                      has_contact: bool = False) -> float:
    """How much this row deserves an operator's attention, on a 0-1 scale.

    The old score was `pc.confidence(methods, intent)` and collapsed to 0.15 whenever no email or
    phone was published - i.e. every consumer row scored the same as noise, which is the contact bias
    this correction exists to remove. Evidence quality now drives it: how clearly the person is
    shopping, whether a public reply route exists, and whether the need is in a state we can serve.
    A published contact still adds weight, but it is a bonus, never the foundation.
    """
    base = {"ACTIVE": 0.55, "STRONG": 0.40, "WEAK": 0.22}.get((strength or "").upper(), 0.10)
    score = base
    score += 0.10 if engage == "THREAD_REPLY" else 0.0
    score += 0.10 if state_rel == "TARGET" else 0.0
    score += 0.15 if has_contact else 0.0
    return round(min(0.95, score), 2)


def _methods(evid: dict) -> list[dict]:
    m = []
    for v in evid.get("emails", []):
        m.append({"kind": "email", "value": v, "evidence": "published in the post text"})
    for v in evid.get("phones", []):
        m.append({"kind": "phone", "value": v, "evidence": "published next to a contact verb"})
    for v in evid.get("links", []):
        m.append({"kind": "link", "value": v, "evidence": "public contact surface"})
    return m


# Reasons a post is not a consumer at all, whatever contact it publishes. Contact-first capture
# exists for the vague-but-human ask, never for a seller, a reporter or a spam blast.
_CONTACT_FIRST_REFUSED = {"seller/agent voice", "media interview request, not a consumer",
                          "spam shape", "advertising copy addressed to readers, not a consumer"}


# ------------------------------------------------------------------ ingest
def ingest(con: sqlite3.Connection, rows: list[dict], source: str) -> dict:
    """Classify + store public findings. Idempotent per (source, url).

    `rows` items: {url, title, text, observed_at?, author?, community?, platform?, geo?, terms?, is_test?}
    Returns counters: candidates, accepted, new, duplicates, skipped, errors, reasons{...}.
    """
    now = _now()
    stats = {"candidates": len(rows), "accepted": 0, "new": 0, "duplicates": 0, "skipped": 0,
             "errors": 0, "emails": 0, "phones": 0, "contact_rows": 0, "reasons": {}}
    contact_idx: dict[str, str] = {}
    url_idx: dict[str, sqlite3.Row] = {}
    for r in con.execute("SELECT id, status, source_url, prospect_key FROM public_prospects"):
        url_idx.setdefault(norm_url(r["source_url"]), r)
    for r in con.execute("SELECT prospect_key, contact_methods FROM public_prospects"):
        try:
            for m in json.loads(r["contact_methods"] or "[]"):
                v = (m.get("value") or "").strip().lower()
                if v and m.get("kind") in ("email", "phone"):
                    contact_idx.setdefault(v, r["prospect_key"])
        except ValueError:
            continue
    for row in rows:
        try:
            url = (row.get("url") or "").strip()
            if not url:
                stats["skipped"] += 1
                stats["reasons"]["missing url"] = stats["reasons"].get("missing url", 0) + 1
                continue
            # title and body are separate sentences: joining them with a bare space let the title
            # bleed into the matched window and hid the location the person actually named.
            text = ". ".join(x.strip() for x in (row.get("title"), row.get("text")) if x).strip()
            cls = classify(text)
            weak = False
            if not cls["need"]:
                # CONTACT-FIRST: a post that publishes an email/phone NEXT TO coverage vocabulary is
                # kept even when the ask is too vague to classify as a clean need. It is stored as
                # CONTACT_INFORMATION_FOUND with weak_intent evidence and zero promotion path until a
                # human reads it - holding a contact we cannot use is still better than losing it.
                ev0 = pc.extract_contacts(text)
                # A refused author stays refused. The first two live sweeps showed the contact-first
                # path re-admitting exactly the rows classify() had just rejected: a broker pitch and
                # a reporter's interview request. Holding a contact we cannot use is only better
                # than losing it when the person is a CONSUMER we failed to read, not a seller.
                if cls["reason"] in _CONTACT_FIRST_REFUSED:
                    stats["skipped"] += 1
                    stats["reasons"][cls["reason"]] = stats["reasons"].get(cls["reason"], 0) + 1
                    continue
                if ev0["contact_status"] == "CONTACT_AVAILABLE" and _COVERAGE.search(text):
                    cls = {**cls, "person": True, "need": True, "sentence": text[:400],
                           "intent": "weak", "topics": "", "matched": ["contact-first"]}
                    weak = True
                else:
                    stats["skipped"] += 1
                    stats["reasons"][cls["reason"]] = stats["reasons"].get(cls["reason"], 0) + 1
                    continue
            geo = ((row.get("geo") or extract_geo(text)) or "").strip()
            sc = state_code(geo)
            target = target_states()
            if sc and sc not in target:
                stats["skipped"] += 1
                stats["out_of_state"] = stats.get("out_of_state", 0) + 1
                stats["reasons"]["outside %s (%s)" % ("/".join(target), sc)] = \
                    stats["reasons"].get("outside %s (%s)" % ("/".join(target), sc), 0) + 1
                continue
            max_age = max_age_days()
            age = age_days(row.get("observed_at") or "")
            if age is not None and max_age and age > max_age:
                stats["skipped"] += 1
                stats["stale"] = stats.get("stale", 0) + 1
                key = "stale post (older than %d days)" % max_age
                stats["reasons"][key] = stats["reasons"].get(key, 0) + 1
                continue
            engage = engagement_route(url, source)
            stats["accepted"] += 1
            stats["engagement_ready"] = stats.get("engagement_ready", 0) + (1 if engage == "THREAD_REPLY" else 0)
            key = key_for(source, url)
            exists = con.execute("SELECT id, status FROM public_prospects WHERE prospect_key=?",
                                 (key,)).fetchone()
            if not exists and norm_url(url) in url_idx:
                # cross-adapter identity: the same permalink found by a board sweep AND by a scoped
                # search is one post, not two prospects.
                exists = url_idx[norm_url(url)]
            if exists:
                stats["duplicates"] += 1
                con.execute("UPDATE public_prospects SET last_seen_at=?, updated_at=? WHERE id=?",
                            (now, now, exists["id"]))
                continue
            evid = pc.extract_contacts(text)
            methods = _methods(evid)
            invite = pc.permission_evidence(text) if evid["contact_status"] == "CONTACT_AVAILABLE" else ""
            dup_of = ""
            for m in methods:
                if m["kind"] in ("email", "phone"):
                    ck = m["value"].strip().lower()
                    if ck and ck in contact_idx:
                        dup_of = contact_idx[ck]
                        break
            if dup_of:
                status, reason = "DUPLICATE", "same published contact as an earlier prospect"
                stats["duplicates"] += 1
                stats["contact_rows"] += 1
            elif weak and methods:
                status, reason = ("CONTACT_INFORMATION_FOUND",
                                  "contact published next to coverage talk, but the ask is too vague "
                                  "to call a need - a human must read it before any use")
            elif methods:
                if invite:
                    status = "CONTACT_PERMISSION_REQUIRED"
                    reason = "contact + invitation wording: a human must read it before any use"
                else:
                    status = "CONTACT_INFORMATION_FOUND"
                    reason = "contact published, no invitation wording: no permission basis"
            else:
                status, reason = "NEW_PROSPECT", "need found, no public contact"
            conf = intent_confidence(cls.get("strength") or "", engage,
                                     "TARGET" if sc in target else ("OTHER" if sc else "UNKNOWN"),
                                     bool(methods))
            evidence = {"adapter": source, "platform": row.get("platform") or "",
                        # the whole post, so a later re-grade sees the context the matched sentence
                        # alone hides ("travel medical insurance" and "living in NJ" both sat outside
                        # the stored window and produced a wrong verdict until this was kept)
                        "post_text": text[:4000],
                        "matched": cls["matched"], "intent": cls["intent"],
                        "intent_strength": cls.get("strength") or "",
                        "state": {"named": geo, "code": sc, "target": list(target)},
                        "engagement": engage, "verdict": cls.get("verdict") or "",
                        "weak_intent": weak,
                        "terms": (row.get("terms") or "")[:300],
                        "collector": "public page read, robots checked", "classifier": "prospector/v1"}
            con.execute(
                """INSERT INTO public_prospects (prospect_key, source, platform, community, source_url,
                   profile_url, author_handle, display_name, stated_need, need_topics, lang, geo,
                   observed_at, discovered_at, last_seen_at, contact_methods, contact_status,
                   contact_evidence, permission_status, permission_evidence, status, status_reason,
                   confidence, evidence, dup_status, dup_of, intent_strength, state_relevant,
                   engagement, is_test, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (key, source, row.get("platform") or "", row.get("community") or "", url,
                 row.get("profile_url") or "", (row.get("author") or "")[:80], row.get("display_name") or "",
                 (cls.get("sentence") or text)[:600], cls["topics"], (row.get("lang") or "en")[:5],
                 geo[:40], (row.get("observed_at") or ""), now, now, json.dumps(methods),
                 evid["contact_status"], (pc.permission_evidence(text) or "not provided")[:300],
                 ("INVITED_INQUIRIES" if invite else "NONE"), invite[:300], status, reason,
                 conf, json.dumps(evidence),
                 ("DUPLICATE" if dup_of else "UNIQUE"), dup_of,
                 cls.get("strength") or "", ("TARGET" if sc in target else "UNKNOWN"), engage,
                 1 if row.get("is_test") else 0, now, now))
            if methods:
                stats["contact_rows"] += 1
                stats["emails"] += sum(1 for m in methods if m["kind"] == "email")
                stats["phones"] += sum(1 for m in methods if m["kind"] == "phone")
            for m in methods:
                if m["kind"] in ("email", "phone"):
                    ck = m["value"].strip().lower()
                    if ck:
                        contact_idx.setdefault(ck, key)
            stats["new"] += 1
        except Exception as e:                        # noqa: BLE001 - one bad row never kills a run
            stats["errors"] += 1
            stats["reasons"][f"error:{type(e).__name__}"] = stats["reasons"].get(f"error:{type(e).__name__}", 0) + 1
    con.commit()
    return stats


def signals_ingest(con, sig_con, limit: int = 400) -> dict:
    """Feed the EXISTING signal store into the lead database.

    `engine/signals.py` already collects public posts (Reddit public JSON, HN, Stack Exchange,
    Mastodon/Bluesky/Lemmy posts). Those findings were never visible in the lead list; this reads
    the INTEREST rows, re-runs the strict classifier, and files the survivors as public prospects
    under the platform that produced them. Read-only against signals.db."""
    try:
        rows = [dict(r) for r in sig_con.execute(
            "SELECT id, source, community, url, title, snippet, lang, geo, observed_at "
            "FROM signals WHERE kind='INTEREST' ORDER BY id DESC LIMIT ?", (int(limit),))]
    except sqlite3.Error:
        return {"candidates": 0, "accepted": 0, "new": 0, "duplicates": 0, "skipped": 0,
                "errors": 0, "reasons": {"signals store unreadable": 1}, "fetched": 0}
    payload = [{"url": r["url"], "title": r["title"] or "", "text": r["snippet"] or "",
                "observed_at": (r["observed_at"] or "")[:19], "geo": r["geo"] or "",
                "community": r["community"] or "", "platform": r["source"] or "",
                "terms": "public post collected by engine/signals.py; read-only reuse"}
               for r in rows]
    stats = ingest(con, payload, "signal_store")
    stats["fetched"] = len(rows)
    record_run(con, "signal_store", stats, "existing signal store (Reddit/HN/SE/fediverse)")
    return stats


def record_run(con: sqlite3.Connection, source: str, stats: dict, note: str = "") -> None:
    con.execute("""INSERT INTO discovery_runs (ts, source, fetched, candidates, accepted, new_rows,
                   duplicates, skipped, errors, note, emails, phones, contact_rows,
                   engagement_ready, links_issued, out_of_state, no_intent, stale)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (_now(), source, int(stats.get("fetched") or 0), int(stats.get("candidates") or 0),
                 int(stats.get("accepted") or 0), int(stats.get("new") or 0),
                 int(stats.get("duplicates") or 0), int(stats.get("skipped") or 0),
                 int(stats.get("errors") or 0), (note or "")[:300],
                 int(stats.get("emails") or 0), int(stats.get("phones") or 0),
                 int(stats.get("contact_rows") or 0), int(stats.get("engagement_ready") or 0),
                 int(stats.get("links_issued") or 0), int(stats.get("out_of_state") or 0),
                 int(stats.get("no_intent") or 0), int(stats.get("stale") or 0)))
    con.commit()


# ------------------------------------------------------------------ status transitions
def _row(con, pid: int) -> dict | None:
    r = con.execute("SELECT * FROM public_prospects WHERE id=?", (pid,)).fetchone()
    return dict(r) if r else None


def _set(con, pid: int, status: str, *, note: str = "", permission: str | None = None,
         permission_evidence: str | None = None, lead_id: str | None = None) -> None:
    sets = ["status=?", "status_reason=?", "updated_at=?"]
    args: list = [status, NEXT_ACTION[status], _now()]
    if note:
        sets += ["operator_note=?", "reviewed_at=?"]
        args += [note[:600], _now()]
    if permission:
        sets.append("permission_status=?")
        args.append(permission)
    if permission_evidence:
        sets.append("permission_evidence=?")
        args.append(permission_evidence[:300])
    if lead_id is not None:
        sets.append("lead_id=?")
        args.append(lead_id)
    args.append(pid)
    con.execute(f"UPDATE public_prospects SET {', '.join(sets)} WHERE id=?", args)
    con.commit()


def reject(con, pid: int, note: str) -> bool:
    """Operator rejection, reason required (>= 8 chars) so the queue stays auditable."""
    if len((note or "").strip()) < 8 or not _row(con, pid):
        return False
    _set(con, pid, "REJECTED", note=note.strip())
    return True


def mark_duplicate(con, pid: int, dup_of_key: str, note: str = "") -> bool:
    row = _row(con, pid)
    if not row or not dup_of_key:
        return False
    _set(con, pid, "DUPLICATE", note=note or f"merged into {dup_of_key}")
    con.execute("UPDATE public_prospects SET dup_status='DUPLICATE', dup_of=? WHERE id=?", (dup_of_key, pid))
    con.commit()
    return True


def verify(con, pid: int, note: str) -> bool:
    """VERIFIED = an operator opened the source URL and confirmed this is a real person with a
    current need. Requires a real note; refused on REJECTED rows."""
    row = _row(con, pid)
    if not row or row["status"] in ("REJECTED", "DUPLICATE") or len((note or "").strip()) < 10:
        return False
    _set(con, pid, "VERIFIED", note=note.strip())
    return True


def record_consent(con, pid: int, evidence: str) -> bool:
    """CONSENTED = the person themselves authorized an insurance follow-up (e.g. they said so
    publicly, or replied to an operator's public message). The evidence text is mandatory and is
    stored verbatim; nothing here can be set by an adapter."""
    ev = (evidence or "").strip()
    row = _row(con, pid)
    if not row or len(ev) < 15:
        return False
    _set(con, pid, "CONSENTED", note=f"consent recorded: {ev[:200]}",
         permission="CONSUMER_STATED", permission_evidence=ev)
    return True


def qualify(con, pid: int, note: str) -> bool:
    """QUALIFIED = this person may be OFFERED the public /check link. Two lawful doors:

      * a published contact with a permission basis (CONSENTED), or
      * a public thread where a reply is allowed (engagement=THREAD_REPLY) on a VERIFIED row.

    The old rule demanded a published email/phone, which no discussion platform exposes - that single
    line is why every discovered consumer stalled at NEW_PROSPECT. QUALIFIED still does NOT authorize
    contacting anyone: it authorizes placing the check link in the same public thread, where the
    person decides for themselves whether to submit.
    """
    row = _row(con, pid)
    if not row or len((note or "").strip()) < 8:
        return False
    if row["status"] not in ("VERIFIED", "CONSENTED"):
        return False
    has_contact_route = (row["contact_status"] == "CONTACT_AVAILABLE"
                         and row["status"] == "CONSENTED")
    has_public_route = row.get("engagement") == "THREAD_REPLY" and row["status"] == "VERIFIED"
    if not (has_contact_route or has_public_route):
        return False
    _set(con, pid, "QUALIFIED", note=note.strip())
    return True


# Which situation the bridge page speaks to, chosen from what the person actually wrote. The page has
# one block per situation (config/bridge.json); a mismatch is a worse page, not a different claim.
_SITUATION_RULES = (
    # FIRST, because it overrides the rest: someone who already missed the window (or cannot wait for
    # it) is not helped by a 60-day SEP answer. The truthful reply to them is the enrollment dates, so
    # the block that carries those dates has to win over the job-loss one.
    ("enrollment", re.compile(r"missed (?:the )?(?:open )?enrollment|cannot wait|can'?t wait|"
                              r"wait (?:un)?til (?:jan|january)|had to wait|barred until", re.I)),
    ("cobra", re.compile(r"\bcobra\b", re.I)),
    ("lost_coverage", re.compile(r"\b(lost (?:my |our )?(?:job|coverage|insurance)|laid off|layoff|"
                                  r"fired|job ends|severance|unemploy\w*)\b", re.I)),
    ("self_employed", re.compile(r"\b(self[- ]employed|1099|independent contractor|freelanc\w*|gig)\b", re.I)),
    ("cost", re.compile(r"\b(afford\w*|too expensive|premium\w*|can'?t pay|cannot pay|cheaper|budget)\b", re.I)),
    ("enrollment", re.compile(r"\b(open enrollment|enroll\w*|special enrollment|deadline|window)\b", re.I)),
    ("family", re.compile(r"\b(family|wife|husband|kids?|children|spouse|pregnan\w*)\b", re.I)),
)


def situation_for(row: dict) -> str:
    """Pick the bridge block from the person's own words, COBRA and job loss first (they change the
    deadline), then self-employment, cost, enrollment and family. Default: the generic coverage block."""
    text = " ".join(str(row.get(k) or "") for k in ("stated_need", "need_topics"))
    try:
        ev = json.loads(row.get("evidence") or "{}")
        # verified_excerpt is what `inspect --save` stores after an operator opens the thread: the post
        # body itself. Without it the block is picked from the (often truncated) title alone, so a
        # laid-off worker asking about a coverage gap landed on the generic block.
        text += " " + str(ev.get("post_text") or "")[:1500]
        text += " " + str(ev.get("verified_excerpt") or "")[:3000]
    except ValueError:
        pass
    for name, pat in _SITUATION_RULES:
        if pat.search(text):
            return name
    return "coverage"


def issue_link(con, pid: int, base_url: str) -> dict | None:
    """Create the tracked /check link for THIS prospect's thread and store its code on the row.

    The link is how a public conversation becomes measurable: every visit it produces carries
    utm_content=p<id>, and if that person submits /check, attribute_lead() links the lead back to
    this prospect automatically. No contact detail is used or needed."""
    row = _row(con, pid)
    if not row or row["status"] != "QUALIFIED":
        return None
    if row.get("tracked_link"):
        sit = situation_for(row)
        # The bridge block is chosen from the person's own words, and `inspect --save` can deepen that
        # evidence after the link was issued. Keep the link's block in step so it never serves the copy
        # picked when the row was thinner (a laid-off worker stuck on the generic block).
        con.execute("UPDATE links SET situation=? WHERE code=? AND COALESCE(situation,'') != ?",
                    (sit, row["tracked_link"], sit))
        con.commit()
        return {"code": row["tracked_link"], "url": f"{base_url.rstrip('/')}/go/{row['tracked_link']}",
                "invite_url": f"{base_url.rstrip('/')}/i/{row['tracked_link']}",
                "situation": sit, "existing": True}
    import secrets as _secrets
    platform = (row["platform"] or row["source"] or "community").split(":")[0][:20]
    community = (row["community"] or "").replace(":", "-").replace("/", "-")[:40] or "thread"
    code = "p%d-%s-%s" % (pid, platform[:10], _secrets.token_hex(2))
    sit = situation_for(row)
    con.execute("""INSERT INTO links (code, campaign, channel, utm_source, utm_medium, utm_campaign,
                   utm_content, partner_id, note, created_at, clicks, angle, lang, landing_page, situation)
                   VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?,?,?)""",
                (code, "oep-2027-health", "community", platform, "community", "oep-2027-health",
                 "p%d" % pid, None, "prospect #%d thread reply (%s)" % (pid, community),
                 _now(), "es_check" if (row.get("lang") or "en") == "es" else "health_check",
                 row.get("lang") or "en", None, sit))
    con.execute("UPDATE public_prospects SET tracked_link=?, link_issued_at=?, updated_at=? WHERE id=?",
                (code, _now(), _now(), pid))
    con.commit()
    return {"code": code, "url": f"{base_url.rstrip('/')}/go/{code}",
            "invite_url": f"{base_url.rstrip('/')}/i/{code}", "situation": sit, "existing": False}


# ---------------------------------------------------------------- engagement kit (never sent)
# The operator's ready-to-post reply for ONE qualified prospect. Facts only: no eligibility promise,
# no price, no "$0 plan", no government look. It answers the question the person actually asked and
# ends with the bridge link. Nothing here sends, posts or contacts anybody — `sent` does not exist as
# a field because the platform cannot send; the operator posts by hand from their own account and
# records that separately.
KIT_RULES = (
    "Reply in the SAME public thread, from your own account. Never DM, email, text or call.",
    "Read that community's rules on links/self-promotion first. If links are restricted, do not post.",
    "Answer the question in your own words first; the link comes last, not instead of an answer.",
    "Include the disclosure line: you run the site.",
    "One reply per thread. Never post the same link twice or reply to your own comment.",
    "If anyone asks you to stop, stop and record it.",
)

_KIT_COPY = {
    "lost_coverage": {
        "en": "When a job ends, losing the plan usually opens a 60-day Special Enrollment window, so "
              "it is worth pinning down your exact date before it closes. I run a free check that "
              "shows your 2027 window and whether your income is in the range that gets help paying "
              "the premium: {url} No charge, nothing sold, and nobody contacts you unless you ask.",
        "es": "Cuando termina un trabajo, perder el plan normalmente abre un Periodo Especial de "
              "Inscripción de 60 días, así que vale la pena confirmar tu fecha exacta antes de que "
              "cierre. Tengo un chequeo gratis que muestra tu periodo 2027 y si tu ingreso está en el "
              "rango que recibe ayuda con la prima: {url} Sin cobro, no se vende nada y nadie te "
              "contacta si no lo pides.",
    },
    "cobra": {
        "en": "COBRA keeps the same plan but you pay the whole premium yourself, and a Marketplace "
              "plan is a different network at a different price, sometimes with help paying the "
              "premium. This free check shows your window and whether your income is in that range, "
              "so you can compare before you decide: {url} No charge, and nobody contacts you unless "
              "you ask.",
        "es": "COBRA mantiene el mismo plan pero pagas la prima completa tú, y un plan del Mercado es "
              "otra red a otro precio, a veces con ayuda para la prima. Este chequeo gratis muestra tu "
              "periodo y si tu ingreso está en ese rango, para que compares antes de decidir: {url} "
              "Sin cobro y nadie te contacta si no lo pides.",
    },
    "cost": {
        "en": "What you pay depends on household income and size, not on the plan's sticker price. A "
              "free check puts your income against the 2027 ranges and shows your enrollment window on "
              "screen: {url} Two minutes, no account, and nobody calls unless you ask.",
        "es": "Lo que pagas depende del ingreso y del tamaño de tu hogar, no del precio de lista del "
              "plan. Un chequeo gratis compara tu ingreso con los rangos de 2027 y muestra tu periodo "
              "de inscripción en pantalla: {url} Dos minutos, sin cuenta y nadie te llama si no lo pides.",
    },
    "self_employed": {
        "en": "As a 1099 worker you buy your own coverage, and the Marketplace is where the "
              "income-based help is. A free check shows your 2027 window and whether your income is in "
              "that range: {url} No charge, and nobody contacts you unless you ask.",
        "es": "Como trabajador 1099 compras tu propia cobertura, y el Mercado es donde está la ayuda "
              "según el ingreso. Un chequeo gratis muestra tu periodo 2027 y si tu ingreso está en ese "
              "rango: {url} Sin cobro y nadie te contacta si no lo pides.",
    },
    "enrollment": {
        "en": "For 2027 in Florida: enroll by December 15 and coverage starts January 1; December 16 "
              "to January 15 starts February 1. A free check shows your own dates and whether your "
              "income is in the range that gets premium help: {url} No charge, and no calls unless you ask.",
        "es": "Para 2027 en Florida: inscríbete antes del 15 de diciembre y la cobertura empieza el 1 "
              "de enero; del 16 de diciembre al 15 de enero empieza el 1 de febrero. Un chequeo gratis "
              "muestra tus fechas y si tu ingreso está en el rango de ayuda: {url} Sin cobro y sin "
              "llamadas si no lo pides.",
    },
    "family": {
        "en": "Household size changes which income range you fall in, so it changes the help you can "
              "get. A free check with your household size and income shows the 2027 ranges and your "
              "window: {url} No charge, and nobody contacts you unless you ask.",
        "es": "El tamaño del hogar cambia el rango de ingreso en el que caes, y por lo tanto la ayuda "
              "que puedes recibir. Un chequeo gratis con tu tamaño de hogar e ingreso muestra los "
              "rangos 2027 y tu periodo: {url} Sin cobro y nadie te contacta si no lo pides.",
    },
    "coverage": {
        "en": "A free check shows your 2027 enrollment window and whether your income is in the "
              "range that gets help paying the premium, in about two minutes: {url} No charge, "
              "nothing sold, and nobody contacts you unless you ask.",
        "es": "Un chequeo gratis muestra tu periodo de inscripción 2027 y si tu ingreso está en el "
              "rango que recibe ayuda con la prima, en unos dos minutos: {url} Sin cobro, no se vende "
              "nada y nadie te contacta si no lo pides.",
    },
}

_DISCLOSURE = {
    "en": "Disclosure: I run this free check site. No charge, nothing is sold to you.",
    "es": "Aviso: yo administro este sitio de chequeo gratis. Sin cobro, no se te vende nada.",
}


def prepare_kit(con: sqlite3.Connection, pid: int, base_url: str) -> dict | None:
    """Build (or rebuild) the ready-to-post reply for a QUALIFIED prospect. Idempotent.

    Refused unless the row is QUALIFIED, because qualification is exactly 'this person may be OFFERED
    the check link'. The kit is a DRAFT for a human: it stores the copy, the bridge URL and the rules,
    and it is not sent by anything in this codebase.
    """
    row = _row(con, pid)
    if not row or row["status"] != "QUALIFIED":
        return None
    link = issue_link(con, pid, base_url)
    if not link:
        return None
    sit = link.get("situation") or situation_for(row)
    sit = sit if sit in _KIT_COPY else "coverage"
    lang = "es" if (row.get("lang") or "en") == "es" else "en"
    kit = {"situation": sit, "link_code": link["code"], "invite_url": link["invite_url"],
           "reply_en": _KIT_COPY[sit]["en"].format(url=link["invite_url"]),
           "reply_es": _KIT_COPY[sit]["es"].format(url=link["invite_url"]),
           "preferred_lang": lang,
           "disclosure_en": _DISCLOSURE["en"], "disclosure_es": _DISCLOSURE["es"],
           "rules": list(KIT_RULES), "source_url": row.get("source_url") or "",
           "prepared_at": _now(), "approved_to_post": False,
           "note": "Draft only. An operator posts this by hand, in the same public thread, after "
                   "checking that community's rules. Nothing here sends or contacts anyone."}
    con.execute("UPDATE public_prospects SET kit_json=?, kit_at=?, updated_at=? WHERE id=?",
                (json.dumps(kit, ensure_ascii=False), _now(), _now(), pid))
    con.commit()
    return kit


def get_kit(con: sqlite3.Connection, pid: int) -> dict | None:
    row = _row(con, pid)
    if not row or not row.get("kit_json"):
        return None
    try:
        return json.loads(row["kit_json"])
    except ValueError:
        return None


def kits_ready(con: sqlite3.Connection, limit: int = 100) -> list[dict]:
    """QUALIFIED rows holding a prepared kit - the 'opportunities prepared for engagement' count."""
    return [dict(r) for r in con.execute(
        "SELECT id, source, community, source_url, intent_strength, state_relevant, confidence, "
        "tracked_link, kit_at FROM public_prospects WHERE status='QUALIFIED' AND COALESCE(kit_json,'')!='' "
        "ORDER BY confidence DESC, kit_at DESC LIMIT ?", (int(limit),))]


def attribute_lead(con, lead_id: str) -> bool:
    """If a consented lead arrived through a prospect's tracked link, connect the two.

    This is the only automatic path from a public post to ROUTING_ELIGIBLE, and it still requires
    what it always required: the person submitted /check themselves and a real consent row exists
    (link_lead verifies both by reading `leads` + `consents`)."""
    if not lead_id:
        return False
    try:
        lead = con.execute("SELECT link_code FROM leads WHERE lead_id=?", (lead_id,)).fetchone()
    except Exception:                                   # noqa: BLE001 - older DB without the column
        return False
    code = (lead["link_code"] if lead else "") or ""
    if not code:
        return False
    row = con.execute("SELECT id FROM public_prospects WHERE tracked_link=?", (code,)).fetchone()
    if not row:
        return False
    if not link_lead(con, int(row["id"]), lead_id):
        return False
    con.execute("UPDATE links SET clicks=clicks WHERE code=?", (code,))
    con.commit()
    return True


def link_lead(con, pid: int, lead_id: str) -> bool:
    """The ONLY bridge from prospect to sellable lead: the person submitted /check themselves and a
    real, consented lead row exists. Proves it by reading `leads`, not by trusting the caller."""
    row = _row(con, pid)
    if not row or not lead_id:
        return False
    lead = con.execute("""SELECT l.lead_id FROM leads l JOIN consents c ON c.lead_id=l.lead_id
                          WHERE l.lead_id=? AND l.lead_type='PHONE_LEAD' AND COALESCE(l.phone_valid,0)=1
                            AND COALESCE(c.consent_text,'')!=''""", (lead_id,)).fetchone()
    if not lead:
        return False
    _set(con, pid, "ROUTING_ELIGIBLE", note=f"consumer submitted /check themselves (lead {lead_id})",
         lead_id=lead_id)
    # The consumer's OWN submission is the only lawful source of a dialable phone for this row: it
    # carries a consent record, so the identity stage copies it across (and nothing else may set
    # marketing_eligibility). Best-effort: bookkeeping must never fail the lead.
    try:
        from . import identity as identity_mod
        identity_mod.adopt_consented_lead(con, pid)
    except Exception:                                    # noqa: BLE001
        pass
    return True


# ------------------------------------------------------------------ queries / metrics
def list_rows(con, *, status: str = "", source: str = "", contact: str = "", dup: str = "",
              match: str = "", limit: int = 200) -> list[dict]:
    sql = "SELECT * FROM public_prospects WHERE 1=1"
    args: list = []
    if status in STATUSES:
        sql += " AND status=?"
        args.append(status)
    if match in ("UNRESOLVED", "PARTIAL", "MATCHED", "REJECTED"):
        sql += " AND COALESCE(match_status,'UNRESOLVED')=?"
        args.append(match)
    if source:
        sql += " AND source=?"
        args.append(source)
    if contact:
        sql += " AND contact_status=?"
        args.append(contact)
    if dup in ("UNIQUE", "DUPLICATE"):
        sql += " AND dup_status=?"
        args.append(dup)
    sql += " ORDER BY confidence DESC, discovered_at DESC LIMIT ?"
    args.append(int(limit))
    rows = [dict(r) for r in con.execute(sql, args)]
    for r in rows:
        try:
            r["methods"] = json.loads(r.pop("contact_methods") or "[]")
        except ValueError:
            r["methods"] = []
        try:
            r["evidence_data"] = json.loads(r.get("evidence") or "{}")
        except ValueError:
            r["evidence_data"] = {}
        # identity resolution evidence is stored as JSON on the row: parse it here so templates read
        # `identity_evidence_data.rejected_claims` instead of a raw string
        try:
            r["identity_evidence_data"] = json.loads(r.get("identity_evidence") or "{}")
        except ValueError:
            r["identity_evidence_data"] = {}
    return rows


def counts(con) -> dict:
    st = {r[0]: r[1] for r in con.execute("SELECT status, COUNT(*) FROM public_prospects GROUP BY 1")}
    by_source = [dict(r) for r in con.execute(
        """SELECT source, COUNT(*) total,
                  SUM(status IN ('CONTACT_INFORMATION_FOUND','CONTACT_PERMISSION_REQUIRED')) with_contact,
                  SUM(status IN ('QUALIFIED','ROUTING_ELIGIBLE','CONSENTED','VERIFIED')) advanced,
                  SUM(dup_status='DUPLICATE') dupes
           FROM public_prospects GROUP BY 1 ORDER BY total DESC""")]
    engage = con.execute("SELECT COUNT(*) FROM public_prospects WHERE engagement='THREAD_REPLY' "
                         "AND status NOT IN ('REJECTED','DUPLICATE')").fetchone()[0]
    # 'ungraded' means a LIVE row no classifier opinion reached. Rejected and duplicate rows are
    # closed, so counting their empty grade here made the backlog look 108 rows worse than it was.
    graded = {r[0]: r[1] for r in con.execute(
        "SELECT intent_strength, COUNT(*) FROM public_prospects "
        "WHERE status NOT IN ('REJECTED','DUPLICATE') GROUP BY 1")}
    links = con.execute("SELECT COUNT(*) FROM public_prospects WHERE tracked_link!=''").fetchone()[0]
    return {"stages": {s: st.get(s, 0) for s in STATUSES}, "total": sum(st.values()),
            "engagement_ready": engage, "links_issued": links,
            "by_strength": {k or "ungraded": v for k, v in graded.items()},
            "with_contact": con.execute("SELECT COUNT(*) FROM public_prospects WHERE "
                                        "contact_status='CONTACT_AVAILABLE'").fetchone()[0],
            "duplicates": con.execute("SELECT COUNT(*) FROM public_prospects WHERE "
                                      "dup_status='DUPLICATE'").fetchone()[0],
            "by_source": by_source}


def actionable(con, limit: int = 40) -> list[dict]:
    """The operator's working queue: an ACTIVE/STRONG consumer in a target state, unexpired, that
    automation has not closed. Ordered by intent strength then freshness - NOT by whether a public
    email exists, which is the mistake this queue replaces."""
    order = "CASE intent_strength WHEN 'ACTIVE' THEN 0 WHEN 'STRONG' THEN 1 ELSE 2 END"
    return [dict(r) for r in con.execute(
        f"""SELECT id, source, community, source_url, stated_need, intent_strength, state_relevant,
                   engagement, contact_status, status, confidence, observed_at
            FROM public_prospects
            WHERE status IN ('NEW_PROSPECT','CONTACT_INFORMATION_FOUND','CONTACT_PERMISSION_REQUIRED')
              AND COALESCE(intent_strength,'') IN ('ACTIVE','STRONG')
              AND COALESCE(state_relevant,'') IN ('TARGET','UNKNOWN')
            ORDER BY {order}, discovered_at DESC LIMIT ?""", (int(limit),))]


def source_yield(con, limit: int = 50) -> list[dict]:
    """Per-adapter measured yield across every recorded run: pages read -> prospects -> contacts.
    `emails`/`phones` are extracted values on NEW rows, never page hits and never guessed."""
    return [dict(r) for r in con.execute(
        """SELECT source, SUM(fetched) fetched, SUM(candidates) candidates, SUM(accepted) accepted,
                  SUM(new_rows) new_rows, SUM(duplicates) duplicates, SUM(skipped) skipped,
                  SUM(errors) errors, SUM(emails) emails, SUM(phones) phones,
                  SUM(contact_rows) contact_rows, MAX(ts) last_run
           FROM discovery_runs GROUP BY source ORDER BY contact_rows DESC, new_rows DESC, fetched DESC
           LIMIT ?""",
        (int(limit),))]


# ------------------------------------------------------------------ backfill / regrade
def regrade(con, *, dry_run: bool = False) -> dict:
    """Re-run today's classifier over rows stored BEFORE intent, state and engagement existed.

    Why this exists: the v1 schema had no `intent_strength`, no `state_relevant` and no
    `engagement`, so 170 of the stored rows carried no grade at all - an operator could not tell an
    ACTIVE shopper from a vague mention, and a row with no published contact looked like a dead end
    even when its public thread allowed a reply. Regrading makes every stored row speak the same
    vocabulary as a fresh sweep.

    It never touches a HUMAN decision: CONSENTED, VERIFIED, QUALIFIED, ROUTING_ELIGIBLE and
    operator-REJECTED rows keep their status. Automated rows whose text no longer passes the
    consumer classifier are moved to REJECTED with the classifier's own reason, so the queue stops
    counting sellers, reporters and treatment needs as prospects.
    """
    target = target_states()
    max_age = max_age_days()
    stats = {"scanned": 0, "graded": 0, "promoted": 0, "rejected": 0, "stale": 0,
             "out_of_state": 0, "restored": 0, "reasons": {}}
    human = ("CONSENTED", "VERIFIED", "QUALIFIED", "ROUTING_ELIGIBLE")
    rows = [dict(r) for r in con.execute(
        "SELECT id, source, source_url, status, status_reason, stated_need, geo, observed_at, "
        "contact_status, permission_status, evidence FROM public_prospects")]
    for r in rows:
        stats["scanned"] += 1
        try:
            evid = json.loads(r.get("evidence") or "{}")
        except ValueError:
            evid = {}
        # grade the FULL post when it is on the row; the matched sentence alone hid the context that
        # decides several verdicts (travel cover, a relative's state, an ad's second person).
        src = evid.get("verified_excerpt") or evid.get("post_text") or (r.get("stated_need") or "")
        cls = classify(src)
        engage = engagement_route(r.get("source_url") or "", r.get("source") or "")
        geo = (extract_geo(src) if evid.get("post_text") or evid.get("verified_excerpt")
               else (r.get("geo") or ""))
        sc = state_code(geo)
        state_rel = "TARGET" if sc in target else ("OTHER" if sc else "UNKNOWN")
        age = age_days(r.get("observed_at") or "")
        stale = bool(age is not None and max_age and age > max_age and age > max_age)

        conf = intent_confidence(cls.get("strength") or "", engage, state_rel,
                                 r.get("contact_status") == "CONTACT_AVAILABLE")
        sets = ["intent_strength=?", "state_relevant=?", "engagement=?", "confidence=?", "geo=?",
                "updated_at=?"]
        args: list = [cls.get("strength") or "", state_rel, engage, conf, geo[:40], _now()]
        status, reason = r["status"], ""
        # a rejection this function wrote is provisional: a better classifier must be able to
        # reconsider it. A rejection an OPERATOR wrote never is.
        self_rejected = (r["status"] == "REJECTED"
                         and (r.get("status_reason") or "").startswith("regrade:"))
        if r["status"] in human:
            pass                                    # a human already acted; grading only
        elif not cls["need"]:
            if r["status"] != "REJECTED" or self_rejected:
                status = "REJECTED"
                reason = "regrade: " + (cls.get("reason") or "not a consumer need")[:200]
        elif stale:
            if r["status"] != "REJECTED" or self_rejected:
                status = "REJECTED"
                stats["stale"] += 1
                reason = "regrade: stale post (older than %d days)" % max_age
        else:                                       # a consumer the current rules accept
            if self_rejected:
                if r.get("contact_status") == "CONTACT_AVAILABLE":
                    status = ("CONTACT_PERMISSION_REQUIRED" if r.get("permission_status") == "INVITED_INQUIRIES"
                              else "CONTACT_INFORMATION_FOUND")
                else:
                    status = "NEW_PROSPECT"
                reason = NEXT_ACTION[status]
                stats["restored"] = stats.get("restored", 0) + 1
            if state_rel == "OTHER":
                stats["out_of_state"] += 1           # kept, but explicitly not servable
        if status != r["status"]:
            sets += ["status=?", "status_reason=?"]
            args += [status, reason]
            if status == "REJECTED":
                stats["rejected"] += 1
                stats["reasons"][(cls.get("reason") or "?")[:60]] = \
                    stats["reasons"].get((cls.get("reason") or "?")[:60], 0) + 1
        evid.update({"intent": cls.get("intent") or evid.get("intent") or "",
                     "intent_strength": cls.get("strength") or "",
                     "verdict": cls.get("verdict") or evid.get("verdict") or "",
                     "engagement": engage, "state": {"named": geo, "code": sc, "target": list(target)},
                     "regraded_at": _now(), "classifier": "prospector/v2"})
        sets.append("evidence=?")
        args.append(json.dumps(evid))
        args.append(r["id"])
        if not dry_run:
            con.execute(f"UPDATE public_prospects SET {', '.join(sets)} WHERE id=?", args)
        stats["graded"] += 1
        if engage == "THREAD_REPLY" and status in ("NEW_PROSPECT", "CONTACT_INFORMATION_FOUND",
                                                  "CONTACT_PERMISSION_REQUIRED"):
            stats["promoted"] += 1
    if not dry_run:
        con.commit()
        stats["dedupe"] = dedupe_by_url(con)
    return stats


# ------------------------------------------------------------------ legacy migration
def migrate_legacy(con, legacy_con: sqlite3.Connection) -> int:
    """Copy rows from the old standalone `prospect_people.db` into the lead database, once.

    Column names differ, the vocabulary differs (5 stages vs 9 statuses) and the honesty rule does
    not: a legacy DISCOVERED row becomes NEW_PROSPECT, CONTACT_FOUND becomes
    CONTACT_PERMISSION_REQUIRED only when the legacy row recorded invitation wording, otherwise
    CONTACT_INFORMATION_FOUND. Nothing is upgraded. Already-migrated keys are skipped, so this is
    safe to run on every start."""
    moved = 0
    try:
        rows = [dict(r) for r in legacy_con.execute("SELECT * FROM prospect_people")]
    except sqlite3.Error:
        return 0
    for r in rows:
        key = r.get("person_key") or key_for("legacy", r.get("source_url") or str(r.get("id")))
        if con.execute("SELECT 1 FROM public_prospects WHERE prospect_key=?", (key,)).fetchone():
            continue
        methods = r.get("contact_methods") or "[]"
        has_contact = (r.get("contact_status") == "CONTACT_AVAILABLE")
        invited = (r.get("permission_status") or "NONE") in ("INVITED_INQUIRIES", "SELF_PUBLISHED")
        if r.get("dup_status") == "DUPLICATE":
            status = "DUPLICATE"
        elif (r.get("operator_note") or "").startswith("REJECTED"):
            status = "REJECTED"
        elif has_contact and invited:
            status = "CONTACT_PERMISSION_REQUIRED"
        elif has_contact:
            status = "CONTACT_INFORMATION_FOUND"
        else:
            status = "NEW_PROSPECT"
        con.execute(
            """INSERT INTO public_prospects (prospect_key, source, platform, community, source_url,
               profile_url, author_handle, stated_need, need_topics, lang, geo, observed_at,
               discovered_at, last_seen_at, contact_methods, contact_status, contact_evidence,
               permission_status, permission_evidence, status, status_reason, confidence, evidence,
               dup_status, dup_of, operator_note, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (key, "legacy:" + (r.get("source") or ""), r.get("source") or "", r.get("community") or "",
             r.get("source_url") or "", r.get("profile_url") or "", "", r.get("stated_need") or "",
             "", r.get("lang") or "en", r.get("geo") or "", r.get("observed_at") or "",
             r.get("discovered_at") or _now(), _now(), methods, r.get("contact_status") or "",
             r.get("contact_evidence") or "", r.get("permission_status") or "NONE",
             r.get("permission_evidence") or "", status, NEXT_ACTION[status],
             r.get("confidence") or 0.0,
             json.dumps({"adapter": "legacy", "migrated_from": "prospect_people.db",
                         "legacy_stage": r.get("stage") or ""}),
             r.get("dup_status") or "UNIQUE", r.get("dup_of") or "", r.get("operator_note") or "",
             r.get("created_at") or _now(), _now()))
        moved += 1
    con.commit()
    return moved
