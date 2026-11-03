# Public data sources for contacts — what was tested, what was built, what it yields

Measured 2026-10-29 against live public endpoints. Two separate pipelines, never mixed:

| pipeline | store | who it holds | can it become a lead? |
| --- | --- | --- | --- |
| consumer prospect (`engine/prospect_contacts.py`, `engine/public_profiles.py`) | `data/prospect_people.db` | individual people who publicly expressed a coverage need | only after the consumer submits `/check` themselves |
| organization contacts (`engine/org_contacts.py`) | `data/org_contacts.db` | clinics/nonprofits that publish a contact for inquiries | never — outreach review only, explicit operator approval |

## Candidates investigated

| source | public + free? | individual contact? | permitted? | verdict |
| --- | --- | --- | --- | --- |
| Bluesky public AppView (searchPosts + getProfile) | yes, anonymous | profile bio / linked page only | public endpoints, no key | **implemented** |
| Mastodon public tag timelines + `/api/v1/accounts/<id>` | yes, anonymous | profile note + custom fields | public endpoints | **implemented** |
| Lemmy `/api/v3/search` + `/api/v3/user?person_id=` | yes, anonymous | `person.bio` | public endpoints | **implemented** |
| Stack Exchange user profiles | yes, API | `about_me` essentially never carries contact | public API | rejected — no contact surface |
| CMS “Find Local Help” directory API | — | — | — | rejected — `data.healthcare.gov` dataset id 404s; no public JSON endpoint exists (verified) |
| Craigslist / Locanto / Milanuncios classifieds | pages public | posters often publish a phone | **terms forbid automated access** | rejected on permitted-use grounds |
| Facebook groups / Instagram / TikTok | login-walled | yes in comments | requires credentialed scraping | rejected — no public access path |
| Socrata “local help” / navigator catalog | — | — | — | rejected — catalog API 404s |
| HRSA health-center site file (`data.hrsa.gov` bulk CSV) | yes, 14 MB, no key | organization phones (not individuals) | official public download | **implemented (org pipeline)** |

## Implemented: public profile contact surfaces

`engine/public_profiles.py` + `tools/public_contacts.py`

A row is stored only when **the same public account** (a) posted a first-person, current
coverage need in the last 30 days, and (b) itself published an email/phone on its own public
profile (or, with `page_hops`, on a page it linked from that profile — robots-checked, one hop,
max 10 per run).

Guardrails: `.gov/.edu/.mil` and system mailboxes refused; a bare website link is not a
contact; org-shaped profiles (agency/broker/llc/“we offer”/agencia) are counted under
`org_skipped` and never stored as consumers; one row per public account; a contact value
published by a second account is stored `DUPLICATE`; the row never advances past
`CONTACT_FOUND`, and `/check` links stay refused until an operator verifies and qualifies.

The relevance filter was tightened after the first live pass produced two false positives
(a game-dev thread saying studios “don't need health insurance”, and a reporter explaining she
“has options”). A need now requires a coverage term + first person + a seek verb + no
negation/already-covered phrasing + a shopping/question intent category
(`tests/test_public_profiles.py` pins both real-world negatives).

```bash
python tools/public_contacts.py scan --network [--sources bluesky,mastodon,lemmy] [--limit 40]
python tools/public_contacts.py yield      # read-only: post vs profile contact, per source
```

### Measured yield (live, 2026-10-29)

| source | items fetched | genuine need posts | profiles read | contacts stored |
| --- | --- | --- | --- | --- |
| bluesky_profile | 109 | 24 | 24 | 0 |
| mastodon_profile | 200 | 3 | 3 | 0 |
| lemmy_profile | 161 | 10 | 9 | 0 |

Of the 24 Bluesky need-posters: 2 had no bio at all and 22 had a bio with no email/phone in it;
4 robots-checked hops were taken to pages those bios linked, and none of those pages published a
contact. Written to `out/public_profile_yield.json`.

**Conclusion: this source class yields ~0 usable consumer contacts.** The bottleneck is
structural, not a code defect: people deciding on coverage today do not publish an email or
phone in their profile, and the accounts that do publish contact details are businesses,
creators or agencies — i.e. organization contacts, which is what the second adapter captures.
Do not keep tuning this source expecting consumer contacts; the consumer path stays the
voluntary `/check` funnel plus partner referrals.

## Implemented: official HRSA health-center directory (organization pipeline)

`engine/org_contacts.fetch_hrsa_sites()` + `python tools/org_pipeline.py bulk --state FL`

`https://data.hrsa.gov/DataDownload/DD_Files/Health_Center_Service_Delivery_and_LookAlike_Sites.csv`
— 19,488 rows, free, no key, robots-checked before download. Active sites only, one state per
run, rows without a usable 10-digit published phone skipped, dedupe by normalized name + E.164,
official identifiers (BPHC number, NPI) retained, `verify_status=VERIFIED_PRIMARY` because the
source is the registry of record.

### Measured yield (live, 2026-10-29, FL)

| metric | value |
| --- | --- |
| rows scanned | 19,488 |
| accepted (active FL sites with published phone) | **732** |
| duplicates | 0 |
| errors | 0 |
| distinct site addresses | 644 |

Org ledger now: 840 verified organization contacts (732 HRSA + 93 NPI registry + 15 directory
seeds). All flagged `PUBLIC_CONTACT_VERIFIED`, `PARTNER_CANDIDATE` only after operator review;
`python tools/org_pipeline.py partner-sync` still sends nothing.

## Focused checks

```bash
python -m pytest tests/test_public_profiles.py tests/test_org_directories.py \
                 tests/test_prospect_contacts.py tests/test_org_contacts.py -q
python tools/collect.py people-metrics          # consumer funnel, unchanged safeguards
python tools/org_pipeline.py status             # org ladder by source
```

## State changes

* `data/prospect_people.db` — new columns `profile_url`, `contact_origin` (`post` | `profile` |
  `profile_link`); the two first-pass false positives were deleted, not reclassified.
* `data/org_contacts.db` — new source `hrsa_health_centers` (732 rows).
* `config/sources.json` — new `public_profiles` block (queries, pacing, `page_hops`).
