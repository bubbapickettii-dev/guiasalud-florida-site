# Business-prospect acquisition — measured results, 2026-11-02

Open-enrollment run against the existing registry (`data/prospects.db`). Nothing here contacts
anyone, changes buyer configuration, activates payments, or touches the consumer lead database.

## 1. What is in the registry now

| Measure | Count |
|---|---|
| Prospect records | **25,818** |
| …with a professional phone | 23,310 |
| …with a published business email | 1,173 |
| …with a named owner / decision-maker | 22,025 |
| …named AND reachable (phone or email) | **20,571** |
| …usable via phone or email | 23,337 |
| …with neither phone nor email | 2,481 |
| Started the run with (legacy import) | 44 |

By source:

| Source | Records | Phone | Email | Owner | Note |
|---|---|---|---|---|---|
| `npi_individuals` | 19,066 | 19,066 | 0 | 19,066 | CMS NPPES NPI-1: the licensee **is** the business (self-employed professionals) |
| `osm_business` | 2,717 | 2,690 | 1,151 | 0 | OpenStreetMap POIs publishing a phone/email (ODbL); mailbox read from the business's own site |
| `mdc_lbt` | 2,495 | 14 | 0 | 1,463 | Miami-Dade Local Business Tax receipts, county-published officer string |
| `npi_practices` | 1,496 | 1,496 | 0 | 1,496 | CMS NPPES NPI-2: practice + registered authorized official |
| `geoleadscraper` | 44 | 44 | 22 | 0 | the original operator import; enriched, not replaced |

By county (city, then ZIP):

| County | Records | Phone | Email | Owner |
|---|---|---|---|---|
| Miami-Dade | 13,110 | 10,616 | 584 | 10,645 |
| Broward | 7,466 | 7,457 | 273 | 6,868 |
| Palm Beach | 3,474 | 3,470 | 257 | 2,926 |
| Monroe (Upper/Middle Keys) | 19 | 19 | 2 | 15 |
| ZIP only, no city field | 1,749 | 1,748 | 57 | 1,571 |

The 19 Monroe rows sit in the 330xx block and were previously counted as Miami-Dade; they are
the only rows outside the three target counties. Total reconciles to 25,818.

Segments that match the open-enrollment target (owner-operated / independent / self-employed):
19,066 individual licensed professionals, 1,496 health practices with a named owner, 2,717
owner-run storefronts and trades (restaurants, dental/medical offices, law/accounting/insurance
offices, electricians, plumbers, HVAC, roofers, landscapers…), 2,495 county-registered
businesses. Chains are skipped at ingest (OSM `brand*` tags), and every row keeps its source URL
and provenance note.

## 2. How each record was obtained (reproducible commands)

```
cd staging_source/leadengine_staging
python tools/prospects.py npi-individuals --max 9000 --all-cities --all-taxonomies
python tools/prospects.py npi-individuals --max 6000 --cities "Coral Springs,Pembroke Pines,…" --all-taxonomies
python tools/prospects.py npi --max 1500            # 12 cities x 8 taxonomies (NPI-2)
python tools/prospects.py lbt --limit 2500
python tools/prospects.py osm --limit 20000                 # targeted owner-operator classes
python tools/prospects.py osm --extended --limit 20000      # long tail: shops, trades, offices
python tools/prospects.py enrich --limit 650 --workers 12 --newest
python tools/prospects.py match                             # name+ZIP phone join
python tools/prospects.py stats
python tools/prospects.py export --out out/prospects_working_list_2026-11-02.csv
```

Only free, published, permitted sources were used: the official CMS NPPES API, the Miami-Dade
county open-data feature service, and OpenStreetMap via Overpass (ODbL). No Google Maps
extraction was attempted, and `google-maps-scraper-kit` stayed blocked.

## 3. Enrichment of the original 44 records

The 44-row Broward/Palm Beach Italian-restaurant import was enriched in place, never duplicated:

- professional phone: 44/44 (unchanged — already complete);
- published email: 20 → **22** (crawled from each business's own site, robots.txt respected);
- 21 non-deliverable "mailboxes" across the registry (`intl-segmenter@11.7.10`,
  `gsap@3.12.2`, `leaflet@1.9.4` — JavaScript package strings, not addresses; one of them sat on
  these 44 rows) were cleared, and the removal was noted on each row;
- owner/decision-maker: **still 0**. Restaurant ownership is not published on these sites and was
  not guessed.

## 4. Email coverage is the real constraint

1,173 of 25,818 records (4.5%) carry a published mailbox, and they are almost all storefronts.
NPPES publishes no email and no website; the county tax file publishes neither. Mailboxes were
only recorded when the business itself printed one (`mailto:` link or visible address) — no
pattern-building (`info@domain` guesses), no purchased lists. Homepage + up to three
contact/about/team pages were read per site, with a browser-compatible User-Agent (many WAFs
reject a custom one) and `robots.txt` honoured; social/platform pages are never crawled.

So the honest shape of the list is: **phone-first**. 20,571 records are callable *and* name the
decision-maker, which is what an outbound insurance conversation needs; email is a bonus channel
on the storefront segment.

## 5. Records that remain incomplete, and why

- 2,481 rows have no phone and no email (`mdc_lbt` mostly): the county file publishes name,
  officer, address, ZIP only. They were kept because the officer string is real and can be
  matched later, but they are not callable today.
- 3,793 rows have no named owner: OSM mappers do not record ownership, and the 44 restaurant rows
  have no published owner. 2,679 of those reach the working list (2,654 with a phone, 25 mailbox
  only); the remainder are duplicates or non-business POIs. Owner names were never inferred from
  first names, domains, or photos.
- 1,454 rows name an owner but publish no phone and no email — all `mdc_lbt`. That is the join
  gap described in the worklist summary: county filings carry the officer, never the number.
- No insurance status, plan, or eligibility is asserted anywhere. Verification state stays
  `UNVERIFIED` until an operator records a named check (`prospects.py verify`).

## 6. Operator artifact

The flat usable export (`prospects.py export`) is superseded for working purposes by the
segmented worklist, which splits the same rows by what an operator can actually do with them and
adds the phone-evidence and priority columns:

```
python tools/prospects.py segments          # → out/prospect_worklist_2026-11-02/
```

| File | Rows | Contents |
|---|---|---|
| `01_owner_named_phone_professionals.csv` | 19,047 | licensee + direct/possibly-shared line |
| `01_owner_named_phone_health_practices.csv` | 1,476 | practice + authorized official |
| `01_owner_named_phone_businesses.csv` | 9 | storefront/registry owner + phone |
| `02_business_phone_no_owner.csv` | 2,654 | number published, owner not |
| `03_email_only.csv` | 25 | mailbox, no phone |
| `04_excluded_incomplete.csv` | 2,481 | name only, no channel |
| `05_excluded_duplicate.csv` | 53 | thinner copy of a name+ZIP |
| `06_excluded_irrelevant.csv` | 73 | non-business POI class |

Working list **23,211** rows; all eight files sum to the registry's 25,818. The 23,337 figure in
§1 counts every row with *any* channel before removing the 53 duplicates and 73 irrelevant POIs
(23,337 − 126 = 23,211).

Columns: `record_id, priority, segment, audience, business_name, decision_maker, owner_type,
professional_phone, phone_evidence, professional_email, website, street_address, city, state,
zip, industry, source, source_url, collected_at, last_updated, verification, decision,
pipeline_stage, outreach_status, phone_shared_by, provenance_notes`.

## 7. Imported into the live registry (executed)

The worklist was imported into `data/prospects.db` itself, keyed by the stable `record_id`, with
`python tools/prospects.py import-worklist` (`--dry-run` supported). The export is a projection
of this same table, so nothing was re-uploaded: each registry row now carries `segment`,
`audience`, `priority`, `phone_evidence`, `phone_shared_by`, `outreach_status` and
`worklist_synced_at`, which is what the admin filters and sorts on.

| Result | Count |
|---|---|
| Attempted / matched / imported | 23,211 / 23,211 / 23,211 |
| Rejected (unknown id, name mismatch, malformed) | 0 |
| Re-run | 0 updated, 23,211 unchanged (idempotent) |
| Registry total after import | 25,818 (unchanged) |
| On the working list in the DB | 23,211 (20,532 · 2,654 · 25) |

The import writes only those seven fields — contacts, `evidence_url`, `created_at`, `notes`,
`decision`, `verify_state` and `stage` are never touched, `updated_at` is untouched, and an
already-recorded outreach status is never reset. The 2,481 incomplete, 53 duplicate and 73
irrelevant rows stay in the registry with a blank `segment`. Full receipt:
`out/prospect_worklist_2026-11-02/IMPORT_RECEIPT.md`.

## 8. Highest-yield next actions (not done here)

1. Deep-paginate NPPES per (city, taxonomy): each query caps at 200 hits, so several dense
   taxonomies are truncated (Nurse Practitioner, Physical Therapist, Internal Medicine).
2. Add Broward and Palm Beach county business-tax files if/when their open-data portals publish
   an owner field (Miami-Dade is the only one ingested).
3. Buy or verify emails only through a consented channel — do not pattern-generate them.
4. Work the imported list from Admin → Prospects, filtered to `segment=owner_named_phone` and
   `priority=1`; nothing in the import records an attempt, so this is still a cold list.
