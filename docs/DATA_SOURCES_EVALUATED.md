# Data sources, evaluated by measuring them

Every number here was produced by `python tools/import_contacts.py evaluate --limit 300`, which
fetches from each source's own API and counts what came back with the SAME normalizer the importer
uses (`engine/source_eval.py` → `engine/admission.normalize`). Raw output:
[out/source_eval/report.md](../out/source_eval/report.md) · `report.json`.

A field the source does not publish is reported as **0 % coverage**. Nothing is estimated.

## What actually contains contact data

| source | access | records measured | name | phone | email | exact dups | shared channels | authenticity |
|---|---|---|---|---|---|---|---|---|
| **CMS NPPES NPI Registry** (individual providers) | public API, no key | 300 | 300 (100 %) | 300 (100 %) | **0 (0 %)** | 0 | 8 | **VERIFIED_REAL** |
| **CMS provider-data** facility listings | public datastore API | 300 | 300 (100 %) | 300 (100 %) | **0 (0 %)** | 3 | 1 | **VERIFIED_REAL** |
| HuggingFace `ziadatalabs/FreeCustomerData50M` | public download | 50 | 50 (100 %) | 50 (100 %) | 50 (100 %) | 0 | 0 | **SYNTHETIC** |
| Census CBP / ZBP | API key now required | — | none | none | none | — | — | aggregate counts only |
| SEC EDGAR | public JSON | — | company names | field exists, usually empty | none | — | — | company-level |
| Harvard Dataverse `doi:10.7910/DVN/YGLYDY` | public (313 MB) | — | none | none | none | — | — | anonymous survey microdata |
| Mendeley `10.17632/xv99b2mtfk.1` | public | — | none | none | none | — | — | purchase-behaviour survey |
| `joeyism/linkedin_scraper` | authenticated session | — | names | **no field** | **no field** | — | — | profiles only |
| `gosom/google-maps-scraper` | live Maps scraping | — | business titles | business numbers | business site addresses only | — | — | business listings |
| Operator-supplied CSV/JSON export | local file | varies | — | — | — | — | — | as declared by the vendor |

### What that means in one line each

* **NPPES is the only supplied source that yields genuine individual people with a usable contact
  channel** — real names, real published practice phone numbers, NPI as a stable record id, and an
  enumeration date as provenance. **It has no email field at all** (0 of 300), and its terms of use
  prohibit marketing or solicitation, so it is `NOT_ELIGIBLE` by design — held, not called.
* **CMS provider-data gives healthcare organisations**, not people: facility name + published
  telephone. Its rows carry no `first_name`/`last_name`/`email` and no per-record date (0 %
  `collected_at`), and three exact duplicates turned up in 300 rows (the same facility listed more
  than once under a shared name+phone).
* **The HuggingFace "FreeCustomerData50M" set is generated.** Its own parquet is named
  `synthetic_users_50M.parquet`; the sample contains `example.com` addresses and NANP-reserved
  `555-01xx` numbers, and cities like "New Kathyhaven". It scores **100 % coverage on every field** —
  which is exactly the trap: a synthetic set looks better than real data on a coverage report. It is
  stored as `SYNTHETIC` and cannot be promoted or marked eligible, even by an operator note.
* **Census / Dataverse / Mendeley** contain zero contact fields by construction (aggregates or
  anonymous survey rows). Census additionally now requires an API key.
* **The two GitHub scrapers** are collection tools, not datasets: LinkedIn exposes no phone or email
  field on its `Person` model and its User Agreement forbids automated collection; google-maps-scraper
  produces place listings (business phone/website) and Maps' terms forbid automated access.

## Data-quality findings that changed the importer

Measuring real records surfaced three defects that guessing would have missed:

1. **Shared phone numbers are not one person.** 8 of 300 NPPES records share a number with a
   different name (clinic switchboards, group practices). Phone-only dedupe silently deleted real
   people. Now: a phone collision between two different names keeps both rows, the second keyed
   `p:<phone>#<name-slug>` and flagged `shared_channel` in `quality`. Email collisions are still
   merged (the same inbox with "Jane Doe" / "J. Doe" is one person).
2. **An organisation name is not a person's name.** CMS facility rows were being split into
   invented first/last names ("SOUTHEAST HEALTH" / "CENTER"). Org-name aliases are no longer split.
3. **A present-but-unusable phone is its own fact.** `quality` records `has_phone` and
   `phone_valid` separately plus the raw value, so "no number published" and "a number we cannot use"
   stop looking identical on the page.

## Ingested into the staging database (measured, not simulated)

`data/leads.db` → `imported_contacts`, after a pre-import snapshot at
`data/snapshots/leads_pre_contact_import_20261102_002046.db`:

| batch | source | rows read | imported | duplicates | data status | eligibility |
|---|---|---|---|---|---|---|
| `b8544b6ec2175` | `nppes-npi1-fl-2026-11-02` (400 FL individual providers) | 400 | 400 | 0 | VERIFIED_REAL | NOT_ELIGIBLE |
| `bcb15fc0955be` | `cms-hospital-general-2026-11-02` (300 hospitals) | 300 | 297 | 3 | VERIFIED_REAL | NOT_ELIGIBLE |
| **total** | | 700 | **697 unique + 3 duplicate rows kept** | | | **0 sellable** |

Each row carries `source_id`, a per-record `source_url` (the NPI provider page / the CMS dataset),
`source_record_id` (NPI or CCN), `collected_at` (enumeration date where the source publishes one),
the licence label, the source row verbatim in `source_fields`, and its `quality` flags.

Nothing else moved: `leads` 41, `consents` 41, `deliveries` 33, `buyer_ledger` 17,
`routing_decisions` 42 — identical before and after, and 0 imported rows are routable or sellable.

## Reproducing / extending

```bash
python tools/import_contacts.py evaluate --limit 300          # measure every registry source
python tools/import_contacts.py evaluate --source nppes --limit 500
python tools/import_contacts.py fetch --source nppes --limit 400 \
    --source-id nppes-npi1-fl-2026-11-02                     # admit them (always NOT_ELIGIBLE)
python tools/import_contacts.py fetch --source cms-facility --limit 300
python tools/import_contacts.py report                        # counts + the no-imported-lead audit
```

A licensed consumer-contact vendor's export (the only category that could contain consumer email
addresses) goes through the same path with its own `--source-id`, `--license` and `--type CONSUMER`;
it lands as `UNVERIFIED` until an operator attests it, and still needs the person's own submission
before it can be sold. Details: [CONTACT_ADMISSION.md](CONTACT_ADMISSION.md).
