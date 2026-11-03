# Signal sources — feasibility rulings (verified Oct 27, 2026)

Companion to [LEADGEN_RESEARCH.md](LEADGEN_RESEARCH.md) (the tool survey) and the
runtime registry on `/admin/integrations`. This file records **why** each platform
got its status; the registry enforces the same statuses in code.

## Working collectors (status COLLECTS)

| Source | Endpoints | Rules encoded in code |
|---|---|---|
| Reddit | public `search.json` per subreddit / all-site | explicit `--network` command; 1 req / 2 s; ≤100 items/query; UA string names us; author usernames dropped before storage; every fetch logged |
| Hacker News | public Algolia `search_by_date` | same etiquette |
| YouTube (uploads) | public Atom feeds `youtube.com/feeds/videos.xml?channel_id=…` | operator lists channel IDs in `config/sources.json`; RSS-reader equivalence is the access path the channel already publishes; titles/descriptions filtered by the same EN/ES taxonomy; no author handles stored; full-YouTube **search** would need the keyed Data API and is NOT implemented |
| Google News RSS | public `news.google.com/rss/search?q=…` | carrier-exit / deadline news context added to the same `fetch --network` sweep; headlines survive only the taxonomy filter (they are context, never a person) |
| Public web pages + AI extraction | any operator-configured public page (`config/sources.json > webreader`) | `engine/web_extract.py`: robots.txt respected (cached per host), polite pause, bounded breadth-first same-domain walk; content normalized into the SAME signal schema, classified EN/ES, URL-deduped. **Local-AI extraction (3 modes, derived live)**: `sga` = instructions-driven `SmartScraperGraph` via a subprocess runner (`tools/sga_runner.py`) inside the OPTIONAL `C:\gs-test\.ai-scrape` venv; `ollama_json` = direct structured extraction over the fetched page by the LOCAL Ollama model (`engine/ollama_client.py`, stdlib HTTP, no extra dependency — the working default on this PC, see `docs/LOCAL_AI_SETUP.md`); `fallback_reader` = stdlib reader when Ollama is off. Modes upgrade the SAME loop, store provenance in `signals.db > signal_extract`, and only ever send PUBLIC page text to the model. OpenAI-backed graphs are billed and never run without explicit operator approval — the code refuses rather than assume. AI state is inspectable: `tools/collect.py ai-health` / `ai-smoke` + the /admin/integrations panel
| Opportunity distillation | `data/signals.db` (topic+lang clusters) | `python tools/collect.py clusters-build` → LOCAL Ollama model distills recurring public intent into prioritized clusters; `/admin/intel/clusters` reviews them with the facts-vs-inference split (counts + cited titles are facts; model text is labeled inference) and links each cluster to the existing `/admin/opportunities` theses the topic map grants. Operator-triggered only; never a contact or outreach surface |
| First-party | our own `/check` + events | always on; the only source that yields consumer inquiries |

## Needs setup (status NEEDS_SETUP) — feasible, free, awaiting operator action

- **YouTube search** (all-platform): needs a Google Cloud key (free tier) — this is
  a decision, not a build: add `fetch_...` only after the operator requests it; the
  channel-feed collector covers creator-content discovery $0.
- **Instagram / Facebook / TikTok via official APIs**: real adapter skeletons exist in
  `engine/platforms.py` (Graph `ig_hashtag_search` + Page posts, TikTok
  `/v2/research/video/query/`). Capability detection is env-based and honest:
  without `IG_GRAPH_ACCESS_TOKEN` / `FB_GRAPH_ACCESS_TOKEN` /
  `TIKTOK_RESEARCH_CLIENT_KEY(+SECRET)` every entry point returns
  `NEEDS_CREDENTIALS` and performs ZERO network calls. The day approvals land,
  collection starts by setting env vars — no rewrite, same whitelist/review loop.
- **Forums / Q&A sites**: no universal API; each site is a manual case. The offline
  `ingest file.json` path records hand-found posts under the identical whitelist.
  The webreader (above) can walk a public forum's public pages after operator choice.

## Not integrated, by explicit decision (statuses NEEDS_KEY / TOS_BLOCKED / AGREEMENT_REQUIRED)

| Platform | Ruling | Why not code it now |
|---|---|---|
| Instagram | TOS_BLOCKED | hashtag/comment collection via Graph needs an approved App Review; scraping violates ToS; owner-account dashboards via partner collabs later |
| Facebook | TOS_BLOCKED | public *group* collection is barred; Pages API is identity-permissioned; value here is search-visibility, which our content engine serves instead |
| TikTok | TOS_BLOCKED | Research/Commercial APIs require an application + approval that has not been filed |
| Web search SERPs | NEEDS_KEY | compliant access = paid SERP API; a purchase decision, never a scraper |
| Publishers / lead providers | AGREEMENT_REQUIRED | contract + TrustedForm consent certificate precede any feed; every external row stays supply-disabled (see `/admin/intel/supply`) |

## Buyer-prospect discovery (business records — a separate registry, not the signal loop)

`engine/prospects.py` + `data/prospects.db` + Admin → Buyer prospects. A prospect row
is a PUBLIC RECORD business (agency/brokerage/partner), never a consumer inquiry and
never an activated buyer; activation only happens through the operator
onboarding/verification flow.

| Path | Status | Ruling |
|---|---|---|
| Operator research / CSV import | COLLECTS | working today: `tools/prospects.py import csv` or the admin page; rows dedupe on website (scheme/www-insensitive) else name+ZIP. Lifecycle + gated onboarding handoff: `docs/BUYER_ACQUISITION.md` |
| Google Places API (official) | NEEDS_KEY (`+ NEEDS_APPROVAL`) | adapter `fetch_places()` is gated twice: `GOOGLE_PLACES_API_KEY` must be set AND `config/sources.json > places > approved: true` recorded — per-request billing never starts implicitly |
| google-maps-scraper-kit (MIT) | TOS_BLOCKED | its method DRIVEs a headless browser over google.com/maps against Google's ToS (its own README states this); the MIT license does not change the access method, so it is documented, not integrated |
| GeoLeadScraper export (browser extension, MIT) | OPERATOR_FILE | an operator-run extension export of Google/Yandex/2GIS listings; `tools/prospects.py import geoleads` maps it onto the same registry rows (business identity only — its emails/socials are dropped). Same Maps access caveat as above; measured capability, security findings and the full test run: `docs/GEOLEADSCRAPER_REVIEW.md` |

## Running everything at once + recurrence

`python tools/collect.py run --network` = ONE sweep over every source above marked
COLLECTS, in one bounded operator action (same collectors, same etiquette, same
fetch_log — `tools/signals_watch.py fetch --network` and the sweep share the same
implementation now). Per-source results, skips and errors print at the end and are
permanent in `data/signals.db > fetch_log` + `/admin/integrations` → “Collection
activity”. `tools/collect.py status` and `tools/collect.py report` (out/collect_report.md)
summarize; `tools/collect.py schedule-cmd` prints ready-to-use cron / Windows Task
Scheduler lines — scheduling stays an explicit operator decision, nothing registers
itself. Verified live (Oct 28, 2026 machine): Google News 94 fetched/12 accepted,
HN 20/1, Reddit public JSON **403-blocked from this environment's IP** (logged as a
retryable error, retried each sweep), YouTube skipped honestly until channel ids are
configured, healthcare.gov/blog webreader seed **refused by that site's robots.txt**
(the robots gate enforces in the wild), and all three credential-gated platform
adapters returned NEEDS_CREDENTIALS with zero network calls.

## Binding principles (code-enforced)

1. **No collection without an explicit operator action.** Nothing is scheduled or
   on by default; network fetching happens only with `fetch --network`.
2. **Persons are out of scope.** Whitelist ingest (title, community label,
   permalink, excerpt); author usernames/ids cannot be stored even in passing.
3. **Signals are never leads.** The signals DB is a separate file, can't import the
   lead pipeline (AST-tested), and only review statuses ever change a row.
4. **Honesty is queryable.** Every code path knows its own status and the runtime
   registry re-derives it from live configuration; a page can never claim more.
5. **Retention.** Irrelevant findings older than `retention_days_irrelevant`
   (default 90, `config/sources.json`) are deletable via `tools/signals_watch.py prune`;
   NEW rows are never auto-deleted; reviewed/actioned evidence is kept.
