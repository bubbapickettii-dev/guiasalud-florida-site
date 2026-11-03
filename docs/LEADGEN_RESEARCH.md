# Lead-generation research (Oct 27, 2026): what exists, what is possible, what we built

Research question: **can existing tools and existing supply give GS Allude real health-insurance
consumer inquiries without money and without agreements we don't have?** Answer up front:

- No open-source tool produces consented consumer quote leads. They find PAGES and PUBLIC
  DISCUSSION. We built the discovery layer we actually needed (see §5).
- No lead source hands a downstream marketplace consumer data by default. **Buying a lead is not
  permission to redistribute it** — at least one reviewed publisher agreement forbids resale absent
  an insertion-order exception. Every viable path is a signed publisher/vendor agreement (§2).
- Competitor quote-link click data is definitively NOT accessible (§4). What IS accessible:
  third-party click referrals (paid), lead-provider feeds (contract), first-party events (ours).
- **So: first-party is the only launch-ready supply path** (§6). The Check already is that path.
  Everything external is a later, agreement-gated diversification step.

Evidence labels: **VERIFIED** = source page read. **VENDOR CLAIMS** = their docs, not validated by
delivery. **AST** = my reasoning. Everything below is as of Oct 27, 2026.

## 1. GitHub open-source tools actually worth something

The complete survey returned one clear pattern: audiences exist for scrapers; **nobody ships
health-insurance compliance lead generation**. What IS real (license checked):

| Category / pick | Repo, stars, last push, license | What it can do | Paid needed | Verdict |
|---|---|---|---|---|
| Web scraping framework | **scrapy/scrapy** — 65k★, pushed Oct 27 2026, BSD-3-Clause | Site-specific crawlers / extractors | no | Industrial-grade but needs custom spiders per site; nothing here to classify intent |
| AI page extraction | **unclecode/crawl4ai** — 86k★, Oct 2026, Apache-2.0 | JS-rendered pages → structured/Markdown | self-host = free; cloud = paid | Strong, but its security history (SSRF fixes) means pinning versions and sandboxing; content is still not intent |
| Headless browser crawling | **apify/crawlee** — 26k★, Oct 2026, Apache-2.0 | HTTP/Cheerio/Playwright crawling | optional Apify cloud | Same: infrastructure, not demand |
| Search results, no key | **deedy5/ddgs** — 3k★, Oct 2026, MIT | Text/news search results, local server | no | Cheap discovery engine for finding public intent pages/conversations; watch throttling |
| Self-hosted SERP API | **karust/openserp** — 1.5k★, Oct 2026, MIT | SERP across Google/Bing/etc. | no | Same as ddgs; heavier (Go + browser engines) |
| Self-hosted metasearch | **searxng/searxng** — 39k★, Oct 2026, AGPL-3.0 | Meta-search instance | no | AGPL + copyleft; fine internally, sharing configuration is a caveat |
| Reddit monitoring (API) | **praw-dev/praw** — 4.3k★, Oct 2026, BSD-2-Clause | OAuth-app monitoring building block | Reddit OAuth credentials, data-API terms | THE maintained Reddit building block; our need is a public read → covered today with Reddit's plain public search endpoints, PRAW only as the future credential path |
| Ready-made listener | **VladUZH/harken** — 44★, Jul 2026, MIT | Multi-source self-hosted listener, SQLite + sentiment | no (HN/Bluesky credential-free) | Young/small but the shape of what an ops team仪表 needs; good reference |
| ML dedupe/linkage | **dedupeio/dedupe** — 4.5k★, MIT · **moj-analytical-services/splink** — 2.5k★, Oct 2026, MIT | Probabilistic record matching | no | Only becomes relevant when 3rd-party supply agreements land (validating provider records), not for consumer leads |
| Business listing scraping | **gosom/google-maps-scraper** — 6.9k★, Oct 2026, MIT | Business listings (partner targeting) | proxies for scale | Legitimate, but targets BUSINESSES not consumers; useful for partner-kit lists |
| CRM | **krayin/laravel-crm** — 24k★, Oct 2026, MIT | General lead/CRM pipeline | no | We already HAVE our CRM layer (admin leads, buyers, partners); replacing it is net-negative |

Rejected because useless for the actual job (or dead / license-shady): GOPA-adjacent crawler
(2017-era abandonware, license NOASSERTION), RedSignal (no license file), HasData social-listening
(requires paid HasData API), generic "AI lead gen" repos (empty wrappers around another LLM).

**What none of these can do (determined):** they find PAGES, POSTS and BUSINESS DATA. None
produce identifiable CONSUMER quote submissions — those exist only where a consumer on someone's
form (ours or a supplier's) submitted contact details. Nothing we fetched from GitHub changed that.

## 2. Lead-supply landscape — who can actually feed us inquiries

Full detail with evidence links per source is in `config/lead_supply.json`, rendered live at
`/admin/intel/supply`. The industry structure:

- **Buy-side aggregators** (QuoteWizard, NextGen, Aged Lead Store, InsureLeads): sell to licensed
  buyers. Buying grants USAGE, not redistribution rights. A downstream marketplace RESELLING data
  is contractually forbidden by default (e.g., SmartFinancial terms: consumer info may not be
  sold/disclosed/transferred unless the IO permits).
- **Publisher programs** (Contactability/SmartFinancial publishing side, MediaAlpha publisher
  exchange): monetize OUR OWN traffic / clicks / leads against their network. This is the SEED-side
  path: we generate consented inquiries and monetize them through the marketplace. Requirements:
  approval, healthy consent documentation, production-consistent carding/GA4.
- **Vendor marketplace onboarding** (IntegrityCONNECT): the marketplace accepts external lead
  SOURCES (vendors) with strict published evidence standards (consent records, TrustedForm/Jornaya,
  caps on shared-aged leads). When we have real leads, we can apply as a vendor.
- **Affiliate/link networks** (FlexOffers, Pivot Health, SASid): referral revenue per click/enrollment.
  NO consumer data transfers to us. Complements instead of supplies.
- **Consent infrastructure** (TrustedForm free Certify / $0.15 verify+retain, Jornaya LeadiD):
  roadmap item for when real buyers demand it — it upgrades our consent story, not our inventory.
- **Dead ends / excluded, recorded so nobody re-litigates**: BenaVest (closed, exclusive to their
  agents), PingTree Systems (claims without retrievable program docs), aged-lead data (obsolete
  consent, contradicts our CONSUMER_SUBMITTED-only rule), Medicare-only shops (wrong product).

## 3. Public intent signals — surfaced and operationalized

Where publicly visible questions live: r/HealthInsurance (EN), all-of-Reddit search for Spanish
(the communities vary), local news comments, WhatsApp forwards (not durable/public, NOT ingestible),
HN for occasional affordability threads. What they mean: a POST is one person's stated problem at a
moment. It is a **SIGNAL, not a lead** — no contact details, no consent, no permission.

**Built today** (see `docs` → this file, §5): discovery loopclassifying signals against the
  same taxonomy the campaigns answer, storing place + moment but never authors, with a review queue that
  forces each finding to name the page or test the finding serves.
Nothing here ever sends anything.

## 4. Quote-link click intent — the definitive determination

- **Google Ads / GA4 / "Delta"-style trackers cannot reveal another company's user-level quote-link
  clicks.** Conversion events exist only on accounts where tracking was configured; GA4 data is
  account-permissioned; Auction Insights gives aggregated impression share, not leads.
- **Affiliate networks track clicks on YOUR OWN links.** Click IDs/subIDs reflect your placements;
  sellers/enrometers receive what the network was contracted to pass (usually the lead, not the
  upstream journey).
- **TrustedForm documents the session on pages where the Certify SDK was installed** — never
  arbitrary browsing history on unrelated sites. Its "Contributed URLs" claim from the brief was
  NOT verifiable in official docs (no such named field found); treat related data cautiously.
- **A legitimate "insurance click" product exists** (shopper finish a form, clicks your listing,
  you pay per click) — MediaAlpha's existing model. It is PAID and CONSENTED at the marketplace,
  not a free scraper's legal substitute.
- **No public feed exists of "consumers who clicked competitor quote pages"** at a person level.
  That fact settles the scrape-first idea: a scraper can find the QUOTE PAGES and PUBLISH/PARTNER
  with sites; it cannot obtain the click events.

## 5. What we built locally (sandboxed, no contact, no deploy): the signal watch loop

| Piece | Where | What it does |
|---|---|---|
| Signal store | `engine/signals.py`, `data/signals.db` (SEPARATE from guarded leads.db) | Classified public posts keyed by permalink; topics mirror the Check angles + opportunity IDs; per-row intent score |
| Taxonomy | `engine/signals.py > TOPICS` | carrier_exit · auto_moved · job_loss · cobra · self_employed · timing · cost_savings · family_aging · enroll_help (EN+ES keywords each) |
| Fetchers | `tools/signals_watch.py fetch --network` | Explicit CLI-gated pulls from Reddit public search JSON (r/HealthInsurance + all-Reddit Spanish search) and HN Algolia; polite UA/limits/pauses; results CLEANED before storage — **author fields are dropped before ingest so they cannot reach storage even transiently** |
| Offline ingest | `tools/signals_watch.py ingest file.json` | Durable public URLs an operator found by hand (screenshots and DMs are NOT acceptable — URLs only) |
| Review | `/admin/intel/signals?token=…` | NEW → REVIEWED → ACTIONED / IRRELEVANT; server refuses IRRELEVANT/ACTIONED without an evidence note naming the page/test addressed; no contact action exists anywhere |
| Report | `tools/signals_watch.py report` → `out/signals_report.md` | Review queue + recorded decisions with the signals-not-leads framing |
| Supply registry | `config/lead_supply.json` + `/admin/intel/supply` | 17 researched source rows with verification level, integration  obtained how the payload can be ingested |
| Boundary tests | `tests/test_signals.py` (7) | AST import-bounds test (no route/notify/billing/requests/sms libs, no contact-capable imports), username-leak tests, dedupe, evidence-gate tests |

**Itemized distinctions the system now enforces:**

- **Discovered source** (config/lead_supply.json row) — a possible supply relationship, needs a
  signature before it receives a single consumer record.
- **Intent signal** (signals.db row) — one public post naming a need; informs CONTENT and
  CHANNEL decisions; never contactable.
- **Consumer inquiry** (leads.db row, qual_status) — a real submission; still requires consent +
  qualification + a routable buyer.
- **Saleable lead** — an inquiry that qualified, has an assignable exclusive buyer, prepaid
  balance, and passes phone/consent checks. The routing engine refuses anything less.

## 6. Build vs buy for the discovery/supply engine

- **Buying an existing scraper** gets us pages, not the pipeline; the integration cost exceeds the
  engine cost and licenses (Firecrawl AGPL, SearXNG AGPL) bring copyleft obligations.
- **Building was worth it BECAUSE the unique part is ours:** classification against OUR campaign
  taxonomy, enforcement of signal-≠-lead and evidence-gated decisions, and the NO-contact invariant.
  Total custom code: ~350 lines, no paid APIs, no credentials, offline-first.
- **What remains genuinely hard (realistic assessment):** coverage volume per source changes
  monthly; Spanish-language intent is spread across WhatsApp groups (undurable), community Facebook
  groups (closed) and news comments (no API); scraping LinkedIn/Blind/Facebook is to-skip by policy.
  Our realistic ceiling: reddit + HN + offline intake = a thin but real signal stream that must be
  paired with the published landing/content pages that earn the actual form fills.
- **Remaining external decisions:** domain + deploy approval (owner), application to one publisher
  program when real traffic exists (owner + provider approval), TrustedForm/Jornaya adoption when
  real buyers exist (owner, paid), and all of the above are BLOCKED ON THE OPERATOR by design.

## 7. Implication for the marketplace

No external row in the supply registry can start this marketplace. The Check (first-party,
exclusive sale_type, versioned consent) is the product; signal watch feeds the CONTENT that earns
those Check visits; the supply registry records the switching-on criteria for partners once there
is a functioning, launched marketplace to diversify from.
