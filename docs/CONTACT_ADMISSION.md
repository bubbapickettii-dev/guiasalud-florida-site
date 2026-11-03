# Contact admission — holding real records without claiming consent

Two questions get confused constantly, and this document keeps them apart:

1. **Do we possess a real person's contact details?** (`imported_contacts`)
2. **May we use them for a specific purpose?** (consent / licence / provenance — and only then
   routing and sale)

`data/leads.db` therefore holds three populations, and they never blur:

| population | table | created by | routable / sellable |
|---|---|---|---|
| a person's own submission | `leads` + `consents` | the /check form (their own phone, their own consent click) | **yes**, if qualified |
| a record we hold from elsewhere | `imported_contacts` | `tools/import_contacts.py` or Admin → Imported contacts | **no**, ever |
| a public post showing a need | `public_prospects` | the discovery sweep | **no** until the person submits themselves |

> **Measured source results** (record counts, field coverage, duplicates, what was actually
> ingested) live in [DATA_SOURCES_EVALUATED.md](DATA_SOURCES_EVALUATED.md). This document covers
The admission model and operator workflow.

## The two repositories that were evaluated

Both were inspected at source level, not judged by their READMEs. Neither produces a genuine
individual consumer contact record (a real person with a name *and* their own phone *and* email).

### `joeyism/linkedin_scraper`

* **What it is:** Playwright browser automation for LinkedIn profile pages (v3.x, Apache-2.0,
  Python 3.8+). It needs an **authenticated LinkedIn session** (`session.json` from a manual
  login, or programmatic login with credentials), so every request is attributable to a real
  member account.
* **What it actually extracts:** a `Person` model — `name`, `headline`, `location`, `about`,
  `open_to_work`, `experiences`, `educations`, `interests`, `accomplishments`, and a `contacts`
  list of `{type, value, label}` entries.
* **Names:** yes — professional display names, as published on a profile.
* **Phones:** no field on the model. A number only appears if a member typed one into their own
  contact-info panel, which LinkedIn shows **only to 1st-degree connections**.
* **Emails:** no dedicated field. `contacts` is the profile's "Contact info" modal (websites,
  messaging handles, sometimes a work address when the connection chooses to share it).
* **Dataset?** No. It is a live page scraper; there is no downloadable corpus in the repo.
* **Cost / limits:** free software, but you pay in account risk: pacing is your problem, and
  automated collection violates LinkedIn's User Agreement (§8.2 prohibits scraping). Practically
  it yields a name + profile URL per person, at a rate LinkedIn's anti-automation will police.
* **Verdict for GS Allude:** **not a source of usable consumer contacts**, and not usable under
  LinkedIn's terms. It could only ever contribute a *professional identity* to a name/company
  enrichment — and even that would require a licensed data partner, not scraping.

### `gosom/google-maps-scraper`

* **What it is:** a Go scraper for Google Maps place pages — CLI, web UI, REST API and a Docker
  image (~120 places/min claimed). MIT licensed, actively maintained.
* **What it actually extracts:** ~36 fields per **place**: `title` (the business name), `category`,
  `address`, `phone`, `website`, `latitude/longitude`, `place_id`, `cid`, rating/review counts,
  opening hours, `owner` (claimed-listing status). `-email` additionally crawls the **business's
  own website** for addresses published there.
* **Names:** the business's trading name. Reviewer display names appear inside `user_reviews`
  but carry no contact details and are not a contact record.
* **Phones:** the business's published number. Not a consumer's.
* **Emails:** only with `-email`, only addresses published on the business's own site.
* **Dataset?** No. It scrapes live Maps pages (a paid `LeadsDB` companion exists upstream).
* **Cost / limits:** free software; proxy spend is real at volume, and Google's terms forbid
  automated access to Maps — the project's own README carries that warning. GS Allude already
  records this source as TOS-blocked (`docs/SOURCES.md`, `engine/prospects.py` places adapter is
  double-gated behind an approved budget).
* **Verdict for GS Allude:** **not a source of consumer contacts.** It *is* a legitimate shape of
  **business/organisation** data (agencies, brokerages — buyer prospects), which already has a home
  in `engine/prospects.py` (`data/prospects.db`), and, if ever used, it must be through a licensed
  provider rather than by scraping Maps.

**Conclusion:** neither repository can hand us "real people with names, phones and emails". What
they produce is (a) professional profiles with no phone/email, and (b) business listings with a
business phone. Neither is consent, and neither belongs in `leads`. They are exactly the kind of
third-party record the new admission store exists to hold honestly.

## How an imported record is stored

Two independent axes, never merged:

| axis | column | values | answers |
|---|---|---|---|
| **authenticity** | `data_status` | `VERIFIED_REAL` (authoritative registry) · `UNVERIFIED` (unattested export) · `SYNTHETIC` (generated) · `REJECTED` | *is this a real person/organisation?* |
| **permission** | `marketing_eligibility` | `NOT_ELIGIBLE` · `USE_LIMITED` · `ELIGIBLE_CONSENTED` | *may we use it for marketing?* |

`record_type` says what the record is: `CONSUMER`, `BUSINESS`, `PROVIDER` (individual practitioner),
`PROVIDER_ORG` (facility), `PROFESSIONAL`, `UNKNOWN`. An operator "attests" authenticity with a note
(`admission.attest`) — which cannot make anything marketable, and cannot turn a `SYNTHETIC` row into
a person. A generated row is also refused by a trigger if anything tries to make it eligible.

`quality` (JSON, measured at import) records `has_name`, `has_phone`, `phone_valid`, `has_email`,
`email_valid`, `has_city`, `has_state`, `has_zip`, `org_name`, `shared_channel` and the raw phone
value when it was present but unusable.

`engine/admission.py` → `imported_contacts` (same `data/leads.db`, its own table):

| group | columns |
|---|---|
| identity anchor | `contact_key` (unique): `e:<email>` → `p:<phone10>` → `n:<name>|<state>` |
| contact | `full_name`, `first_name`, `last_name`, `email`, `phone`, `phone_e164`, `telegram`, `city`, `state`, `zip`, `country`, `has_contact` |
| provenance | `source_id` (**required**), `source_name`, `source_url`, `source_record_id`, `collected_at` (the *source's* own date), `imported_at`, `import_batch`, `license`, `license_note` |
| evidence | `source_fields` (the row verbatim), `evidence` (JSON: source, url, record id, dates, licence, any matching lead) |
| eligibility | `marketing_eligibility`, `eligibility_reason`, `permitted_uses`, `consent_ref`, `lead_id` |
| bookkeeping | `dup_status`, `dup_of`, `record_type`, `is_test`, `created_at`, `updated_at` |

Every import also writes an `import_batches` row (received / imported / duplicates / rejected /
missing-contact), so a bad export is diagnosable after the fact.

Any column naming works: `name/full_name/title`, `email`, `phone/mobile/tel`, `telegram/username`,
`url/profile_url/website`, `date/signup_date`, `id/place_id`, `city`, `state`, `zip`. Unmapped
columns are preserved in `source_fields`.

## Deduplication

* The anchor is the **email** if there is one, else the **normalized phone** (US 11-digit folded to
  10), else **name + state**. Nothing else is invented.
* **A shared phone is not one person.** Measured on NPPES: 8 of 300 individual providers share a
  number with a different name (clinic switchboards, group practices). When a phone collision
  carries two different names, BOTH records are kept — the second keyed `p:<phone>#<name-slug>` and
  flagged `shared_channel`. Merging them would silently delete a real human being. An email collision
  is still merged: the same inbox with "Jane Doe" / "J. Doe" is one person.
* A repeat import of a record already held is stored as a **new row with `dup_status='DUPLICATE'`,
  `dup_of=<original>`, `contact_key = NULL`**. The original is not touched: its phone, email,
  source, collection date, eligibility, `consent_ref` and `lead_id` are all frozen. So a re-import
  can never overwrite consent evidence or move a record between eligibility states.
* Duplicate rows are inert: they cannot be promoted, and the admin page filters them out by default
  while still showing the count.
* A row with no name, phone or email is **rejected** (it has no identity to store). A row with a
  name but no phone/email is **kept** and flagged `has_contact=0` — it is information, just not a
  contactable record.

## Why an imported record cannot be sold

Four independent guards, three of them in the database:

1. `engine/route.py` reads **only `leads`** rows, and only `QUALIFIED` + `PHONE_LEAD` + a phone.
   `imported_contacts` is not consulted anywhere in routing, delivery or billing.
2. `engine/db.py` trigger `leads_phone_must_be_consumer_submitted` aborts any `leads` insert whose
   `phone_source` is not `CONSUMER_SUBMITTED`.
3. `engine/admission.py` triggers refuse `marketing_eligibility='ELIGIBLE_CONSENTED'`, a `lead_id`
   or a `consent_ref` unless a real `consents` row exists **for that exact lead** — and
   `consent_ref` must equal `lead_id`. A future refactor cannot relabel a third-party record.
4. Imported evidence (`phone`, `phone_e164`, `email`, `contact_key`, `source_id`, `source_url`,
   `collected_at`) is immutable at the trigger level.
5. Defence in depth: `route()` returns `NOT_ROUTABLE` for any lead dict that declares a
   `phone_source` other than `CONSUMER_SUBMITTED`.

`admission.report()` returns the live audit of all this: `sellable` (only linked-and-consented
rows), `lead_rows_not_consumer_submitted` (must be **0**), and `routable_imported` (rows pointing at
a lead without being linked to its consent — must be **0**).

## The legitimate path to eligibility

```
third-party record  --import-->  NOT_ELIGIBLE            (possession only)
                                    |
                                    |  operator records what the licence permits
                                    v
                                USE_LIMITED              (a permitted use that is NOT marketing)
                                    |
                                    |  the SAME person submits /check themselves
                                    |  (a real leads row + its consents row now exist)
                                    v
                            ELIGIBLE_CONSENTED           (lead_id + consent_ref set)
```

`admission.promote(cid, lead_id)` — Admin → Imported contacts → *link to own consent*, or
`python tools/import_contacts.py promote --id <cid> --lead <lead_id>` — refuses unless **all** of
these hold:

* the row is a unique original (not a duplicate attempt),
* the lead exists,
* that lead has a `consents` row,
* the lead's **own submitted** phone or email matches this record (a name alone is not proof).

It then writes only the link on the imported row. It never inserts a lead, never writes a consent,
never edits a lead field, and never touches routing. The consumer still has to do the submitting;
that is the whole point.

## Operator runbook

```bash
# 1. admit an export (CSV or JSON), with its provenance
python tools/import_contacts.py import --source panel-2026-04 \
    --name "Licensed panel export" --url https://example.com/panel \
    --license "vendor agreement §3 (inbound follow-up only)" --type CONSUMER --file panel.csv

# 2. what do we hold, and is any of it saleable? (it is not)
python tools/import_contacts.py report
python tools/import_contacts.py sources

# 3. review and act per record (browser, operator token)
#    /admin/contacts          registry, provenance, eligibility + the exact reason
#    record use               what the licence permits (stays non-marketing)
#    link to own consent      only if that person submitted /check themselves
```

## Remaining blockers (authorisation, not code)

* **No licensed consumer contact source is wired in yet.** The store accepts any export, but the
  *source* has to be one we may lawfully use. The two repos evaluated here are not: LinkedIn's
  User Agreement forbids scraping, and Google Maps' terms forbid automated access.
* **A licence is not marketing consent.** Even a permitted export lands as `USE_LIMITED` at best;
  buying or licensing a record does not create TCPA-clear prior express written consent for a
  licensed agent to call, and it does not name a buyer.
* **Business listings are not consumer leads.** google-maps-scraper output belongs in the buyer
  prospect registry (`engine/prospects.py`), not here, and Google Maps scraping remains TOS-blocked
  per `docs/SOURCES.md`.
* **Nothing in this build contacts anyone.** No sends, no dials, no DMs, no payments, nothing
  published.
