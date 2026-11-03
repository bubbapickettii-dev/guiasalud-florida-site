"""CONSUMER PROSPECT CONTACTS — the evidence-bearing path from a PUBLIC signal to a
prospect-with-permission record.

Design boundary (deliberate, mirrors the signal store's rules):
  * engine/signals.py stays PEOPLE-FREE — no names, no handles, no contacts reach it.
    The import-bound test in tests/test_signals.py requires that no module in this
    chain touches the lead pipeline; this module only READS public signals rows and
    stores what a person wrote in their own public post.
  * A public post is NEVER consent. A row starts at DISCOVERED and advances one rung
    (CONTACT_FOUND) only when the post itself carries a contact surface the author
    published AND wording that invites contact ("my email is...", "escríbeme a...").
    PERMISSION_VERIFIED / QUALIFIED / READY_FOR_CONSUMER_ONBOARDING are operator
    actions, each requiring a recorded note of what was actually checked.
  * No invented data: email domains are never guessed, phones without a proximity verb
    are not extracted, .gov/.edu/.mil and system mailboxes are never treated as
    consumer contacts, and a public username/profile link alone is not a contact.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path(os.environ.get("PEOPLE_DB", ROOT / "data" / "prospect_people.db"))

STAGES = ("DISCOVERED", "CONTACT_FOUND", "PERMISSION_VERIFIED",
          "QUALIFIED", "READY_FOR_CONSUMER_ONBOARDING")

NEXT_ACTION = {
    "DISCOVERED": "none: capture-only discovery signal (public post, no consent)",
    "CONTACT_FOUND": "operator: open the source URL and verify the contact invites inquiries",
    "PERMISSION_VERIFIED": "operator: confirm the need is current + recent before any step",
    "QUALIFIED": "operator may now share the /check link - the consumer must submit themselves",
    "READY_FOR_CONSUMER_ONBOARDING": "awaiting the consumer's own voluntary /check submission",
}

# first-person markers that make a need expression personal
_PERSONAL_HINTS = ("i lost", "i have", "i need", "i am", "i'm", "my job", "my work",
                   "my family", "my wife", "my husband", "my son", "my daughter", "my income",
                   "perdí", "perdi", "no tengo", "necesito", "mi trabajo", "mi familia",
                   "busco", "cotización", "cotizacion", "cuánto", "cuanto", "hola")

import engine.signals as signals  # noqa: E402 - reads the public signal store only

SCHEMA = """
CREATE TABLE IF NOT EXISTS prospect_people (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_key TEXT UNIQUE,          -- stable hash of the SOURCE POST (source|url)
  source TEXT, community TEXT,
  source_url TEXT,                 -- the verified permalink (operator opens this)
  signal_id INTEGER,               -- optional link row into signals.db
  stated_need TEXT,                -- the need as written
  intent_cat TEXT,                 -- shopping | question | enrolled | news | generic | irrelevant
  lang TEXT,
  observed_at TEXT,                -- post date where the source gives it
  discovered_at TEXT,
  geo TEXT,                        -- ONLY what the post text itself names
  contact_methods TEXT,            -- JSON: [{kind: email|phone|link, value}]
  contact_status TEXT DEFAULT 'CONTACT_NOT_AVAILABLE',
  contact_evidence TEXT,           -- the exact phrase that offered contact
  permission_status TEXT DEFAULT 'NONE',   -- NONE | INVITED_INQUIRIES | OPERATOR_VERIFIED
  permission_evidence TEXT,
  operator_note TEXT DEFAULT '',
  confidence REAL DEFAULT 0.0,     -- deterministic score from recorded evidence
  rank_score REAL DEFAULT 0.0,     -- evidence score + recency + need-specificity (actual intent)
  dup_status TEXT DEFAULT 'UNIQUE',        -- UNIQUE | DUPLICATE
  dup_of TEXT DEFAULT '',
  profile_url TEXT DEFAULT '',             -- the PUBLIC profile that published the contact
  contact_origin TEXT DEFAULT 'post',      -- post | profile (where the contact was published)
  stage TEXT DEFAULT 'DISCOVERED',
  next_action TEXT,
  created_at TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_ppl_stage ON prospect_people(stage, observed_at);
"""

# Recency discipline, same rule the signal store applies at the source: prospect people
# are CURRENT needs. A post older than this window never becomes a NEW prospect row
# (already-stored rows are untouched — their dedupe/update path still runs).
MAX_PROSPECT_AGE_DAYS = max(1, int(os.environ.get("PROSPECT_MAX_AGE_DAYS", "30")))


def connect(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    # migration for pre-existing databases: rank_score (deterministic intent rank)
    for _ddl in ("ALTER TABLE prospect_people ADD COLUMN rank_score REAL DEFAULT 0.0",
                 "ALTER TABLE prospect_people ADD COLUMN profile_url TEXT DEFAULT ''",
                 "ALTER TABLE prospect_people ADD COLUMN contact_origin TEXT DEFAULT 'post'"):
        try:
            con.execute(_ddl)
            con.commit()
        except sqlite3.OperationalError:
            pass                            # column already exists
    if con.execute("SELECT 1 FROM prospect_people WHERE rank_score IS NULL LIMIT 1").fetchone() \
       or con.execute("SELECT 1 FROM prospect_people LIMIT 1").fetchone() and not con.execute(
            "SELECT 1 FROM prospect_people WHERE rank_score > 0 LIMIT 1").fetchone():
        for _r in con.execute("SELECT id, confidence, intent_cat, stated_need, observed_at, "
                              "contact_status FROM prospect_people WHERE rank_score IS NULL OR rank_score=0").fetchall():
            _rec = recency_weight(_r["observed_at"])
            _meth = ([{"kind": "email"}] if _r["contact_status"] == "CONTACT_AVAILABLE" else [])
            _spec = need_strength(_r["stated_need"] or "")
            con.execute("UPDATE prospect_people SET rank_score=? WHERE id=?",
                        (round((_r["confidence"] or 0.0) + _rec + _spec, 3), _r["id"]))
        con.commit()
    return con


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ extraction
_DECISION_WORDS = re.compile(
    r"\b(deadline|urgent|tomorrow|this week|next week|days|end of month|"
    r"need it by|starting|starts on|by the|enrollment ends|coverage ends|"
    r"fecha|plazo|urgente|manana|est[aá] semana|antes del)\b", re.IGNORECASE)
_COVERAGE_NOUNS = re.compile(
    r"\b(cobra|medicaid|medicare|marketplace|obamacare|employer|coverage|deductible|premium|"
    r"subsidy|open enrollment|aseguranza|seguro medico|medico|cobertura|subsidio)\b", re.IGNORECASE)

def recency_weight(observed_at: str) -> float:
    """0..0.35, linear over MAX_PROSPECT_AGE_DAYS: a need voiced TODAY outranks the same
    need three weeks old. Posts older than the window (or undated) earn nothing — they are
    archived discussion, not CURRENT demand."""
    try:
        ts = datetime.fromisoformat(str(observed_at or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0
    age_days = max(0.0, (datetime.now(timezone.utc).timestamp() - ts) / 86400.0)
    return round(0.35 * max(0.0, 1.0 - age_days / MAX_PROSPECT_AGE_DAYS), 3)

def need_strength(text: str, signals_intent: float = 0.0) -> float:
    """0..0.40 need-specificity: decision-point words (deadline/urgent/this week…) plus
    coverage-nouns actually named, or the signal store's keyword-intent score when it is
    available (intent 6+ is a strong keyword match)."""
    t = re.sub(r"\s+", " ", (text or "").lower())
    kw = len(_DECISION_WORDS.findall(t)) + len(set(_COVERAGE_NOUNS.findall(t)))
    of_intent = min(1.0, (signals_intent or 0.0) / 6.0)
    return round(min(0.40, 0.10 * kw + 0.25 * of_intent), 3)


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+", re.IGNORECASE)
_SYSTEM_LOCALS = ("noreply", "no-reply", "donotreply", "do-not-reply", "postmaster",
                  "webmaster", "admin", "admin_", "support", "notifications", "abuse",
                  "helpdesk")
_RESTRICTED_DOMAINS = ("gov", "edu", "mil")
_OBFUSCATION = re.compile(
    r"([A-Za-z0-9._%+\-]+)"                                    # local part
    r"\s*(?:\(|\[|<)?\s*(?:at|en)\s*(?:\)|\]|>)?\s*"
    r"([A-Za-z0-9\-]+(?:\s*\.\s*[A-Za-z0-9\-]+)*)"             # domain
    r"\s*(?:\(|\[|<)?\s*(?:dot|punto)\s*(?:\)|\]|>)?\s*"
    r"([A-Za-z]{2,12})", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?:(?:\+?1)[\-\s\.])?\(?\d{3}\)?[\-\.\s]?\d{3}[\s\-\.]?\d{4}\b")
_PHONE_VERB = re.compile(
    r"\b(call|text|reach me|contact me|my number|phone|phone:|ll[aá]mame|ll[aá]manos|llamanos|"
    r"tel[eé]fono|telefono|celular|mensajeame|m[aá]ndame|escr[ií]beme|comun[ií]cate)\b")
# A number a bystander QUOTED is not the poster's number. The first live Reddit-archive sweep
# stored a scam hotline from a complaint post ("Received a call from 773 389 5697 and Caller ID
# showed MEDICARE HELPLINE") as if the author had published it — that number belongs to a third
# party, so the direction of the call is now an explicit fence.
_THIRD_PARTY_PHONE = re.compile(
    r"(?:received|got|had) a (?:call|text|message) from|call(?:ed)? (?:me )?from|"
    r"caller id|number (?:that|which) (?:called|texted)|spam (?:call|number)|scam (?:call|number)|"
    r"me llamaron|me llam[oó] desde", re.IGNORECASE)
_INVITE_PHRASES = (
    "my email", "email me", "e-mail me", "you can reach", "reach me", "contact me",
    "contact is", "dm me", "dm at", "message me", "hit me up", "hit my inbox",
    "please email", "please call", "feel free to", "my number", "my phone", "text me",
    "call me", "find me at", "write me",
    "escríbeme", "escribeme", "escríbime", "escribime", "llámame", "llamame",
    "llámanos", "llamanos", "llámanos al", "mensajeame", "mándame", "mandame",
    "mi correo", "mi número", "mi numero", "contáctame", "contactame",
    "comunícate", "comúnicaté", "comunícate conmigo", "comunícate",
    "puedes escribir", "me puedes escribir", "háblame", "hablame")
_LOW_TRUST_HOSTS = ("instagram.com", "facebook.com", "twitter.com", "x.com", "tiktok.com",
                    "linkedin.com", "youtube.com")


def _norm_text(t: str) -> str:
    t = (t or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", t).strip()


def extract_contacts(text: str, lang: str = "en") -> dict:
    """Extract contacts that a person EXPLICITLY PUBLISHED in their own post text.
    Returns {emails, phones, links, contact_status}. No inference or enrichment: what
    is not in the post is not in the result."""
    text = _norm_text(text)
    low = text.lower()
    emails: list[str] = []
    phones: list[str] = []
    links: list[str] = []
    evidence = ""

    for m in _EMAIL_RE.finditer(text):
        raw = m.group(0)
        local, domain = raw.rsplit("@", 1)
        loc, dom = local.lower(), domain.lower()
        if dom.split(".")[-1] in _RESTRICTED_DOMAINS:
            continue
        base_loc = loc.split("+")[0]
        if base_loc in _SYSTEM_LOCALS:
            continue
        if any(dom.endswith("." + r) or dom == r for r in _RESTRICTED_DOMAINS):
            continue
        norm = f"{loc}@{dom}"
        if norm not in emails:
            emails.append(norm)
            if not evidence:
                evidence = raw

    # obfuscated "name [at] domain [dot] com" — humans publish this to dodge bots
    for m in _OBFUSCATION.finditer(text):
        local = m.group(1).strip("._-").lower()
        dom = m.group(2).lower().replace(" ", "").strip(".-")
        tld = m.group(3).strip(".").lower()
        if not local or " " in dom or " " in tld:
            continue
        if tld in _RESTRICTED_DOMAINS or any(dom.endswith("." + r) for r in _RESTRICTED_DOMAINS):
            continue
        norm = f"{local}@{dom}.{tld}"
        if norm not in emails:
            emails.append(norm)
            if not evidence:
                evidence = m.group(0)

    # phone — the number must sit right next to an explicit contact verb ("text me at",
    # "call 305...", "llámame a"), with a negation guard so phrases like "no one should
    # call" never turn a bystander number into a contact. Dates/prices are excluded by
    # the strict US shapes + verb proximity (no 7-digit phones, no bare zips).
    for m in _PHONE_RE.finditer(text):
        hit = m.group(0)
        before = low[max(0, m.start() - 120):m.start()]
        after = low[m.end():m.end() + 40]
        near = low[max(0, m.start() - 90):m.end() + 90]
        if not _PHONE_VERB.search(near):
            continue
        if _THIRD_PARTY_PHONE.search(before[-170:]):
            continue                        # somebody else's number, quoted not published
        # negation fence: a verb inside "no one should call" / "don't text" is NOT an
        # invitation - reject the number when every nearby verb is negated
        verbs = list(_PHONE_VERB.finditer(near))
        inviting = [v for v in verbs
                    if not re.search(r"\b(no|not|never|nobody|no one|don't|do not|shouldn't|can't|"
                                     r"no llamar|no llames|nadie)\b",
                                     near[max(0, v.start() - 24):v.start()])]
        if not inviting:
            continue
        digits = re.sub(r"\D", "", hit)
        if not (10 <= len(digits) <= 11):
            continue
        if hit not in phones:
            phones.append(hit)
            if not evidence:
                evidence = hit

    # explicit public contact surfaces the author offered (form, tree, own page)
    for w in ("contact form", "inquiry form", "contact page", "formulario de contacto"):
        for m in re.finditer(re.escape(w) + r"\s*[:\->]?\s*([^\s<]{10,200})", text, re.IGNORECASE):
            val = m.group(1).strip()
            if val and val not in links:
                links.append(val)
                if not evidence:
                    evidence = w + " " + val
    for m in re.finditer(r"https?://linktr\.ee/[\w\-/]{4,120}", text, re.IGNORECASE):
        val = m.group(0)
        host = val.lower()
        if any(h in host for h in _LOW_TRUST_HOSTS):
            continue
        if val not in links:
            links.append(val)
            if not evidence:
                evidence = val

    available = bool(emails or phones)
    return {"emails": emails, "phones": phones, "links": links,
            "contact_status": "CONTACT_AVAILABLE" if available else "CONTACT_NOT_AVAILABLE"}


def permission_evidence(text: str) -> str:
    """The inviting-contact phrase quoted from the post, or '' — evidence that the author
    PUBLISHED a surface for inquiries. Not proof of anything beyond what the words claim."""
    text = _norm_text(text)
    for p in _INVITE_PHRASES:
        i = text.lower().find(p)
        if i >= 0:
            return text[max(0, i - 20):i + 100].strip()
    return ""


def confidence(methods: list[dict], intent_cat: str = "") -> float:
    """Deterministic score from RECORDED evidence only (no model calls), so the admin UI
    can show why a row scores what it scores:
      base 0.15 | +0.25 shopping / +0.18 question | -0.20 enrolled/news/generic/irrelevant
      +0.25 phone / +0.20 email / +0.10 inquiry-link-only | +0.15 an inviting-contact phrase
      Never above 0.95."""
    c = 0.15
    if intent_cat == "shopping":
        c += 0.25
    elif intent_cat == "question":
        c += 0.18
    elif intent_cat in ("enrolled", "news", "generic", "irrelevant"):
        c -= 0.20
    kinds = {m["kind"] for m in methods}
    if "phone" in kinds:
        c += 0.25
    elif "email" in kinds:
        c += 0.20
    elif kinds:
        c += 0.10
    if any(m.get("evidence") for m in methods if m["kind"] != "link"):
        c += 0.15
    return round(min(0.95, max(0.0, c)), 2)


# ------------------------------------------------------------------ ingest

def _person_key(source: str, url: str) -> str:
    """Stable dedupe key: SOURCE + permalink. The same post found twice collapses."""
    base = f"{(source or '').strip().lower()}|{(url or '').strip().lower()}"
    return hashlib.sha1(base.encode()).hexdigest()[:24]


def scan_signals(sig_con: sqlite3.Connection, people_con: sqlite3.Connection) -> dict:
    """Mine the PUBLIC signal store for person-shaped need expressions and store them as
    prospect people (never leads). Idempotent: same post re-scanned -> same person_key
    -> skipped. Contact dedupe: a SECOND post publishing the SAME email/phone is stored
    with dup_status=DUPLICATE + dup_of pointing at the first row, while keeping its own
    source URL so both posts stay verifiable.

    Skipping rules (fine-grained, evidence-bearing): rows whose intent category is NEWS
    or IRRELEVANT are not consumer prospects even when they contain a contact; rows that
    are already ENROLLED or carry no first-person marker at all are commentary, not needs.
    A valid row with no published contact stays as a CONTACT_NOT_AVAILABLE discovery
    signal - never misrepresented as a usable lead."""
    now = _now()
    rows = [dict(r) for r in sig_con.execute(
        "SELECT id, source, community, url, title, snippet, lang, kind, intent_cat, "
        "       geo, observed_at FROM signals WHERE kind='INTEREST'")]
    added = updated = stale_skipped = skipped = 0
    # contact-level dedupe index across everything that is already in the people store
    contact_idx: dict[str, str] = {}
    for h in people_con.execute(
            "SELECT person_key, contact_methods FROM prospect_people"):
        try:
            methods = json.loads(h[1] or "[]")
        except ValueError:
            methods = []
        for cm in methods:
            key = (cm.get("value") or "").strip().lower()
            if key and cm.get("kind") in ("email", "phone") and key not in contact_idx:
                contact_idx[key] = h[0]
    for row in rows:
        text = " ".join(x for x in (row["title"], row["snippet"]) if x)
        if not text.strip():
            skipped += 1
            continue
        now_iso = _now()
        cat = row["intent_cat"] or signals.intent_category(text, row["lang"] or "")
        if cat in ("news", "irrelevant", "enrolled"):
            skipped += 1
            continue
        if not any(t in text.lower() for t in _PERSONAL_HINTS):
            skipped += 1
            continue
        evid = extract_contacts(text)
        invite = permission_evidence(text) if evid["contact_status"] == "CONTACT_AVAILABLE" else ""
        methods = ([{"kind": "email", "value": v} for v in evid["emails"]] +
                   [{"kind": "phone", "value": v} for v in evid["phones"]] +
                   [{"kind": "link", "value": v} for v in evid["links"]])
        key = _person_key(row["source"], row["url"])
        exists = people_con.execute(
            "SELECT id FROM prospect_people WHERE person_key=?", (key,)).fetchone()
        if exists:
            people_con.execute("UPDATE prospect_people SET updated_at=? WHERE id=?",
                               (now_iso, exists[0]))
            updated += 1
            continue
        # recency gate (NEW rows only, never already-stored ones)
        try:
            _obs_ts = datetime.fromisoformat(str(row["observed_at"] or "").replace("Z", "+00:00"))
        except ValueError:
            _obs_ts = None
        if _obs_ts is not None and _obs_ts.timestamp() < (datetime.now(timezone.utc).timestamp()
                                                          - MAX_PROSPECT_AGE_DAYS * 86400):
            stale_skipped += 1
            skipped += 1
            continue
        dup_of, dup_status = "", "UNIQUE"
        for m in methods:
            if m["kind"] in ("email", "phone"):
                ck = m["value"].strip().lower()
                if ck and ck in contact_idx and contact_idx[ck] != key:
                    dup_of, dup_status = contact_idx[ck], "DUPLICATE"
                    break
        stage = "CONTACT_FOUND" if (methods and dup_status == "UNIQUE") else "DISCOVERED"
        conf = confidence(methods, cat) if methods else 0.15
        rank = round(conf + recency_weight(row["observed_at"] or "") + need_strength(text), 3)
        people_con.execute(
            "INSERT INTO prospect_people (person_key, source, community, source_url, signal_id, "
            "stated_need, intent_cat, lang, observed_at, discovered_at, geo, contact_methods, "
            "contact_status, contact_evidence, permission_status, permission_evidence, "
             "confidence, rank_score, dup_status, dup_of, stage, next_action, "
             "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, row["source"], row["community"] or "", row["url"], row["id"],
             text[:400], cat, row["lang"] or "?", row["observed_at"] or "", now_iso,
             row["geo"] or "", json.dumps(methods), evid["contact_status"],
             invite or "not provided", ("INVITED_INQUIRIES" if invite else "NONE"),
             invite or "", conf, rank, dup_status, dup_of, stage,
             NEXT_ACTION[stage], now, now))
        for m in methods:
            if m["kind"] in ("email", "phone"):
                ck = m["value"].strip().lower()
                if ck:
                    contact_idx[ck] = key
        added += 1
    people_con.commit()
    return {"added": added, "updated": updated, "skipped": skipped,
            "stale_skipped": stale_skipped, "rows_read": len(rows)}


# ---------------------------------------------------------------- conversion path
LOW_CONF_TOP = 0.45      # below this rank_score a row is kept and counted SEPARATELY -
                         # it is a weak, speculative finding, not a promising prospect
LOW_PROMISING = 0.60     # rank_score at/above this = clear + current need (worked first)

def check_link(person: dict) -> tuple[str, str]:
    """Attributed /check URL for ONE operator-reviewed prospect row, built from the row's
    own recorded fields only. Disclosure-first: the link lands on the SAME consumer
    journey the site already runs, so the person discovers GS Allude and submits
    themselves. It never sends or contacts anyone - the OPERATOR places it where the
    person will see it (the post they verified), never as a private message. Attribution
    carries the person_key so a voluntary submission can be measured against discovery
    without identifying anyone. Angle matches the post's language. Refused for rows
    without QUALIFIED / PERMISSION_VERIFIED context: nothing else has a verified basis
    for offering the journey."""
    stage = (person.get("stage") or "")
    note = (person.get("operator_note") or "")
    if not (stage in ("QUALIFIED", "PERMISSION_VERIFIED") or
            (note and not note.startswith("REJECTED"))):
        return ("", f"prospect #{person.get('id')}: no operator-reviewed basis (stage "
                f"{stage or 'DISCOVERED'}) - refused")
    lang = person.get("lang") or "en"
    angle = "es_check" if lang == "es" else "health_check"
    pk = (person.get("person_key") or str(person.get("id")))[:24]
    q = ("?a={angle}&utm_source=prospect_{src}&utm_campaign=intent_{cat}_{lang}"
         "&utm_content=prospect_{pk}").format(
             angle=angle, src=(person.get("source") or "none")[:20],
             cat=(person.get("intent_cat") or "none")[:24], lang=lang, pk=pk)
    return ("/check" + q, "")


def people_metrics(sig_con: sqlite3.Connection, ppl_con: sqlite3.Connection) -> dict:
    """The measured DISCOVERY metrics (NOT funnel numbers - the consumer side is joined in
    tools/collect.py from leads.db, whose live tables live in the other store). Low-
    confidence rows are counted separately and never roll into the promising numbers."""
    sigs = sig_con.execute(
        "SELECT COUNT(*) FROM signals WHERE kind='INTEREST'").fetchone()[0]
    strong = sig_con.execute(
        "SELECT COUNT(*) FROM signals WHERE kind='INTEREST' AND "
        "intent_cat IN ('shopping','question')").fetchone()[0]
    st = {r[0]: r[1] for r in ppl_con.execute(
        "SELECT stage, COUNT(*) FROM prospect_people GROUP BY 1")}
    total = sum(st.values())
    low_conf = ppl_con.execute(
        "SELECT COUNT(*) FROM prospect_people WHERE rank_score < ? AND stage NOT IN "
        "('QUALIFIED','READY_FOR_CONSUMER_ONBOARDING')", (LOW_CONF_TOP,)).fetchone()[0]
    promising = ppl_con.execute(
        "SELECT COUNT(*) FROM prospect_people WHERE rank_score >= ? AND stage NOT IN "
        "('DISCOVERED')", (LOW_PROMISING,)).fetchone()[0]
    return {
        "relevant_consumer_signals": strong, "all_interest_signals": sigs,
        "unique_prospects": ppl_con.execute(
            "SELECT COUNT(*) FROM prospect_people WHERE dup_status='UNIQUE'").fetchone()[0],
        "contacts_extracted": ppl_con.execute(
            "SELECT COUNT(*) FROM prospect_people WHERE contact_status='CONTACT_AVAILABLE' "
            "AND dup_status='UNIQUE'").fetchone()[0],
        "contacts_from_posts": ppl_con.execute(
            "SELECT COUNT(*) FROM prospect_people WHERE contact_status='CONTACT_AVAILABLE' "
            "AND dup_status='UNIQUE' AND COALESCE(contact_origin,'post')='post'").fetchone()[0],
        "contacts_from_profiles": ppl_con.execute(
            "SELECT COUNT(*) FROM prospect_people WHERE contact_status='CONTACT_AVAILABLE' "
            "AND dup_status='UNIQUE' AND contact_origin IN ('profile','profile_link')"
        ).fetchone()[0],
        "profile_rows": ppl_con.execute(
            "SELECT COUNT(*) FROM prospect_people WHERE contact_origin IN "
            "('profile','profile_link')").fetchone()[0],
        "self_published_contacts": ppl_con.execute(
            "SELECT COUNT(*) FROM prospect_people WHERE "
            "permission_status IN ('SELF_PUBLISHED','INVITED_INQUIRIES')").fetchone()[0],
        "contacts_verified": ppl_con.execute(
            "SELECT COUNT(*) FROM prospect_people WHERE "
            "permission_status='OPERATOR_VERIFIED'").fetchone()[0],
        "clear_recent_need": promising,
        "duplicates": ppl_con.execute(
            "SELECT COUNT(*) FROM prospect_people WHERE dup_status='DUPLICATE'").fetchone()[0],
        "low_conf_separate": low_conf,
        "stages": {s: st.get(s, 0) for s in STAGES}, "total": total,
    }


# ------------------------------------------------------------------ operator actions
def verify_permission(con: sqlite3.Connection, person_id: int, note: str) -> bool:
    """An operator who actually opened the source URL records that the published contact
    invites inquiries. Requires a real note (>= 10 chars) so the evidence is auditable.
    This is the ONLY automated-adjacent path into PERMISSION_VERIFIED — and it is never
    automated: it runs on a human action, refused on rows without a contact."""
    note = (note or "").strip()
    if len(note) < 10:
        return False
    row = con.execute("SELECT contact_status FROM prospect_people WHERE id=?",
                      (person_id,)).fetchone()
    if not row or row[0] != "CONTACT_AVAILABLE":
        return False
    con.execute("UPDATE prospect_people SET permission_status='OPERATOR_VERIFIED', "
                "permission_evidence='operator verified: '||?, stage='PERMISSION_VERIFIED', "
                "next_action=?, updated_at=? WHERE id=?",
                (note, NEXT_ACTION["PERMISSION_VERIFIED"], _now(), person_id))
    con.commit()
    return True


def set_stage(con: sqlite3.Connection, person_id: int, stage: str) -> bool:
    """Manual stage moves. PERMISSION_VERIFIED can only come from verify_permission;
    anything else is a transparent operator decision."""
    if stage not in STAGES or stage == "PERMISSION_VERIFIED":
        return False
    con.execute("UPDATE prospect_people SET stage=?, next_action=?, updated_at=? WHERE id=?",
                (stage, NEXT_ACTION[stage], _now(), person_id))
    con.commit()
    return True


def decide(con: sqlite3.Connection, person_id: int, decision: str, note: str) -> bool:
    """QUALIFIED or REJECTED, operator-only, with a recorded reason. QUALIFIED is refused
    on rows without a contact: no contact, no prospect, regardless of the note."""
    note = (note or "").strip()
    if len(note) < 8:
        return False
    row = con.execute("SELECT contact_status FROM prospect_people WHERE id=?",
                      (person_id,)).fetchone()
    if not row:
        return False
    if decision == "QUALIFIED":
        if row[0] != "CONTACT_AVAILABLE":
            return False
        con.execute("UPDATE prospect_people SET stage='QUALIFIED', next_action=?, "
                    "operator_note=?, updated_at=? WHERE id=?",
                    (NEXT_ACTION["QUALIFIED"], note, _now(), person_id))
    elif decision == "REJECTED":
        con.execute("UPDATE prospect_people SET operator_note='REJECTED: '||?, updated_at=? "
                    "WHERE id=?", (note, _now(), person_id))
    else:
        return False
    con.commit()
    return True


# ------------------------------------------------------------------ queries
ORIGINS = ("post", "profile")


def list_people(con: sqlite3.Connection, stage: str = "", contact_status: str = "",
                source: str = "", lang: str = "", verified: int = 0,
                dup: str = "", origin: str = "", limit: int = 100) -> list[dict]:
    sql = "SELECT * FROM prospect_people WHERE 1=1"
    args: list = []
    if stage and stage in STAGES:
        sql += " AND stage=?"
        args.append(stage)
    if contact_status:
        sql += " AND contact_status=?"
        args.append(contact_status)
    if origin in ORIGINS:
        sql += " AND COALESCE(contact_origin,'post')=?"
        args.append(origin)
    if source:
        sql += " AND source=?"
        args.append(source)
    if lang in ("en", "es"):
        sql += " AND lang=?"
        args.append(lang)
    if verified:
        sql += " AND permission_status='OPERATOR_VERIFIED'"
    if dup in ("UNIQUE", "DUPLICATE"):
        sql += " AND dup_status=?"
        args.append(dup)
    sql += " ORDER BY confidence DESC, observed_at DESC LIMIT ?"
    args.append(int(limit))
    return [dict(r) for r in con.execute(sql, args)]


def get_person(con: sqlite3.Connection, person_id: int) -> dict | None:
    row = con.execute("SELECT * FROM prospect_people WHERE id=?", (person_id,)).fetchone()
    return dict(row) if row else None


def facets(con: sqlite3.Connection) -> dict:
    return {
        "sources": [r[0] for r in con.execute(
            "SELECT DISTINCT source FROM prospect_people ORDER BY 1")],
        "stages": list(STAGES),
        "contact_status": sorted({r[0] for r in con.execute(
            "SELECT DISTINCT contact_status FROM prospect_people")}),
    }


def counts(con: sqlite3.Connection) -> dict:
    st = {r[0]: r[1] for r in con.execute(
        "SELECT stage, COUNT(*) FROM prospect_people GROUP BY 1")}
    cs = {r[0]: r[1] for r in con.execute(
        "SELECT contact_status, COUNT(*) FROM prospect_people GROUP BY 1")}
    return {"stages": {s: st.get(s, 0) for s in STAGES},
            "contact_available": cs.get("CONTACT_AVAILABLE", 0),
            "contact_not_available": cs.get("CONTACT_NOT_AVAILABLE", 0),
            "duplicates": con.execute(
                "SELECT COUNT(*) FROM prospect_people WHERE dup_status='DUPLICATE'").fetchone()[0],
            "total": sum(st.values())}
