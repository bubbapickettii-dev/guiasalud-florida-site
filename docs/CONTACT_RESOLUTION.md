# Contact resolution: from a public post to a named, reachable consumer

`engine/identity.py` · CLI `python tools/discovery.py enrich --live` · admin `/admin/people`

Discovery finds a person describing a health-coverage need in public. That is not a contact record.
This stage is the one that answers **who is behind the post**, from public sources, with the URL
that proves it — and refuses to answer when the evidence is not there.

## The chain

| hop | what it reads | what it can yield | credential |
|---|---|---|---|
| 1. author dossier | the same public account's **other** public posts + comments (permitted Reddit archive API) | self-stated name, Florida city, a number the person published themselves | none |
| 2. platform profile | the profile the source points at: Discourse `/u/<name>.json`, Mastodon `accounts/lookup`, Lemmy `/api/v3/user`, Bluesky `getProfile` (actor taken from the handle **or** the DID in the post URL) | display name, bio, custom fields ("Phone: …") | none |
| 3. handle probe (opt-in `--probes`) | a short allowlist keyed by the same handle: GitHub API, Linktree, about.me, Keybase | full name, location, links | none |
| 4. linked-page hop | one robots-checked fetch of a page the account linked from its own bio | name / number the person published on their own site | none |

No search-engine scraping, no login, no paid broker, no data-broker file. One GET per surface,
paced from `config/sources.json > identity`.

## The rules (why a match is trustworthy)

**A name** exists only where the person identified *themselves*:

* accepted markers: `my name is`, `name's`, `call me`, `I go by`, `me llamo`, `soy X y …`,
  `this is X …`, and `I'm <Name>` **when `<Name>` is a common first name** (the list is the guard —
  "I'm tired", "I'm moving", "I'm looking" cannot fire).
* refused: handles, city names, trade/org words (`Random Jeweler`, `LLC`, `Insurance Agency`),
  adjectives, numbers.
* `PROBABLE` = one source. `VERIFIED` = **two independent sources** (different URL, different
  surface) agreeing, or a pattern plus a profile that agree on the fuller form (`John` +
  `John Smith` is one identity, not a conflict).
* `CONFLICT` = two different names. Nothing is chosen; both stay on the row as evidence and the
  match is `REJECTED`.

**A phone** is stored only when the *same identity* published it:

* must be a real US 10-digit number, published with ownership wording (`text me`, `my number`,
  `llámame`) **or** in a labelled profile field (`Phone: …`);
* refused, with the reason kept: toll-free/premium ranges (800/833/844/855/866/877/888/900);
  business/agency context (`our office`, `licensed agent`, `llc`, `salon`, `realty`, …); numbers a
  bystander quoted ("the number that called me"); numbers with no ownership wording;
* **a number whose owner is not established stays `PROBABLE`** — shown to the operator as a
  candidate, never presented as a usable contact record.

**Permission ≠ eligibility.** Two separate columns:

* `contact_permission`: `NONE` · `SELF_PUBLISHED` · `INVITED` · `CONSENTED`
* `marketing_eligibility`: `BLOCKED_NO_CONSENT` · `BLOCKED_UNVERIFIED` · `ELIGIBLE_CONSENTED`

Publishing a phone number is not consent to be called. `ELIGIBLE_CONSENTED` is reachable only when
that person's own `/check` submission exists (a `consents` row), which is the normal lead pipeline's
job — this stage never contacts anyone, never messages, never dials.

## What is stored

Additive columns on `public_prospects` (the original discovery fields — `stated_need`,
`source_url`, `author_handle`, `evidence`, `contact_methods` — are never rewritten):

`identity_name`, `identity_name_status`, `identity_name_source`, `identity_name_confidence`,
`identity_phone`, `identity_phone_status`, `identity_phone_source`, `identity_phone_confidence`,
`location_resolved`, `match_status`, `match_reason`, `contact_permission`,
`marketing_eligibility`, `identity_evidence`, `resolved_at`, `resolve_attempts`,
`resolve_last_error`.

`identity_claims` keeps **every** claim, accepted or refused, with the URL it came from:

```
prospect_id | claim_kind | value | normalized | source_url | source_kind | method |
evidence | strength | status | reject_reason
```

`identity_runs` records each run's counters, so a zero is visible instead of silent.

## Commands

```bash
python tools/discovery.py enrich --live --ids 303,324          # specific rows
python tools/discovery.py enrich --live --intent ACTIVE --limit 25 --probes
python tools/discovery.py identity                              # stored state, no network
```

`--live` is mandatory: resolution reads third-party public pages. Without it the command refuses
rather than silently doing nothing.

In the admin, `/admin/people` shows the resolved identity, its confidence, the source URL, the
refusals, and a **resolve identity** button (one row, operator-triggered).

## Status meanings

| match_status | meaning |
|---|---|
| `UNRESOLVED` | nothing public identifies this person; the row keeps its discovery evidence and its public thread route |
| `PARTIAL` | a name exists but is not corroborated — a candidate, not a contact record |
| `MATCHED` | name verified by two independent public sources |
| `REJECTED` | sources conflict (or the material is org-shaped); nothing is chosen |

## Measured result (live, 2026-10-31)

Run: `enrich --live --intent STRONG --probes` over the ACTIVE/STRONG queue, plus one fresh
ACTIVE-intent discovery sweep (`where can i get health insurance`, `need insurance now`,
`insurance options florida`, `health insurance help`, `perdi mi seguro`, `busco seguro medico`;
366 posts read, 19 new rows).

| measure | value |
|---|---|
| consumer-intent prospects in the queue | 171 (269 live rows total) |
| prospects attempted | 165 |
| names resolved (PROBABLE) | 3 — Aayush, Donnie, Margaret Citta |
| names VERIFIED (corroborated by 2+ public sources) | 1 — Joseph (GoFundMe post + a second post, both self-identifying) |
| phones published by any of the 165 identities | 1 candidate, 0 accepted |
| refused claims | 1 — a number with no ownership wording, kept with its reason |
| independently verified contact records (name + phone) | 0 from the public web |
| from the consumer's own submission | verified by test (`adopt_consented_lead`), waiting on the first real `/check` |

**Why 0 phones is a real finding, not a bug.** The residency of this population is anonymous forum
accounts: across ~1,000+ public posts by 165 people, exactly one published a number at all, and it
carried no ownership wording. A person asking "where can I get coverage?" does not post their cell
number in public — Reddit users are reached by DM. The numbers that DO appear in coverage threads
are agency lines, toll-free ad numbers and scam callbacks, which the rules refuse by design.

That is why the only lawful, genuinely matched phone route is the person's own `/check` submission:
`identity.adopt_consented_lead()` copies name + phone from that consented lead onto the prospect row
(`VERIFIED`, `contact_permission=CONSENTED`, `marketing_eligibility=ELIGIBLE_CONSENTED`) and is
called automatically when a /check submission is attributed back to a prospect. Test records are
refused. Until somebody submits, the stage's job is names and locations — which the operator uses to
place a reply in the right thread, not to call anyone.

### What improved in the first live pass (precision)

The first run produced 21 "names", including `Moderator`, `Newbie`, `Bartram Park`,
`Air Fryer Aristocrat`, `Girl Lurker`, `Midnight`, `Ohio`, `Neckbeard`, `Track Rider`. Every one came
from author flair or a platform display name — chosen text, not a self-identification. Fixes:

* author flair is no longer a name source at all;
* a profile display name only counts as a CANDIDATE when it is two tokens whose first is a common
  given name (`Margaret Citta`), never as a verified identity;
* role words, states, cities, trade nouns and common nouns are refused as names;
* names are re-validated on READ, so a claim stored under an older rule cannot keep a bad name.
