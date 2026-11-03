# GeoLeadScraper — security review, measured capability, and integration decision

Reviewed 2026-11-02. Upstream: <https://github.com/ozhehkovski/geoleadscraper>
(repo `geoleadscraper`, clone `28505a8`, extension version 1.2.0, MIT).

Everything below was measured on this machine, not read off the README. Section 6 lists the
exact commands and the raw numbers; §8 lists what was deliberately NOT done.

---

## 1. Verdict

**Security: passes.** No malware, no telemetry, no credential collection, no phone-home, no
obfuscated code, no install scripts. It is a small, readable TypeScript product: one browser
extension plus an optional self-hosted contact-scraping backend.

**Capability: real, but narrower than the pitch.** It produces a clean business-identity list
(name, address split into parts, phone, website, category, rating, hours, lat/long) very fast
and for $0. It does **not** produce emails from maps, and it does **not** produce owner or
decision-maker names on any platform.

**Access: the binding constraint.** The maps half is browser scraping of Google Maps / Yandex
Maps / 2GIS, which is exactly the access method this repo already classifies `TOS_BLOCKED` in
`engine/sources.py > PROSPECT_SOURCES`. The extension being operated by a human in their own
browser softens the operational risk but does not change the terms.

**Recommendation:** use it as an unpaid **category scanner** for the business-identity layer of
the prospect registry, imported through the operator-file path added in this change
(`tools/prospects.py import geoleads`), never as a compliance-grade or contact-grade source.
The official Places API stays the sanctioned path once a budget exists; its output is a
near-swap for these rows.

---

## 2. What was inspected

| Area | Files |
| --- | --- |
| Extension manifest, permissions | `apps/extension/chrome-extension/manifest.js` |
| Service worker / message bus | `apps/extension/chrome-extension/lib/background.ts` |
| Website crawler (in-browser fallback) | `apps/extension/chrome-extension/lib/crawler.ts` |
| Maps parsers + field map | `apps/extension/packages/shared/lib/utils/extract.ts`, `reviews.ts` |
| Export (CSV/XLSX/JSON) | `apps/extension/packages/shared/lib/utils/export.ts` |
| Backend API | `apps/api/src/main.ts`, `modules/app/*`, `modules/jobs/*`, `modules/app/extractors/*` |
| Build/install surface | root + per-app `package.json`, `turbo.json`, `.npmrc`, `Dockerfile`, `docker-compose.yml`, `.github/workflows/extension.yml`, `.husky/pre-commit` |
| MCP bridge | `apps/api/mcp-server.mjs`, `MCP.md`, `skill/SKILL.md` |

Dependency weight: 742 packages for the backend (Nest 11, Fastify 5, Puppeteer 24, zod,
libphonenumber-js); ~1,170 for the workspace install.

## 3. Security findings

**Cleared — nothing found in these areas**

- **No telemetry / analytics / crash reporting.** The only absolute URLs in extension code are
  the scraped map domains, the user-configurable backend (`DEFAULT_BACKEND_URL:
  http://localhost:5050`), and marketing links in `config.ts`. No `gtag`, Sentry, or beacon.
- **No credential access.** Manifest permissions are `storage, activeTab, scripting, tabs,
  alarms` — no `cookies`, no `webRequest`, no `history`, no `identity`.
- **No obfuscation.** Source is readable TS; the built content bundle (571 kB) is ordinary Vite
  output. No eval of remote code; `content_security_policy.extension_pages` is
  `script-src 'self'`.
- **No install-time execution.** No `postinstall`/`prepare` in either `package.json`; the only
  git hook is `pnpm dlx lint-staged --allow-empty` (lint on commit). `--ignore-scripts` was used
  for the workspace install anyway.
- **No supply-chain red flag** beyond volume: all deps are mainstream (Nest/Fastify/Puppeteer,
  `webextension-polyfill`, `libphonenumber-js`, `xlsx`). Nothing fetched from a
  non-npm registry; the lockfile pins them.
- **Backend is stateless.** Jobs live in an in-memory `Map` with a 30-minute TTL; there is no
  database, no upload of collected rows anywhere, no outbound call except the URL it is asked
  to scrape.

**Real risks, ranked**

1. **The backend is an open scraping proxy if you follow the docs.** `.env.example` and
   `docker-compose.yml` default to `HOST=0.0.0.0` and `ALLOWED_ORIGINS=*`, with no
   authentication, and `main.ts` binds as told. Anyone who can reach port 5050 can make your
   machine fetch arbitrary URLs and burn your bandwidth/IP reputation. Mitigation used here and
   recommended always: `HOST=127.0.0.1`, keep `ALLOWED_ORIGINS` narrow, and never publish 5050.
2. **SSRF surface.** `AppService.normalizeUrl()` accepts any `http(s)` URL — including
   `http://127.0.0.1:*` and link-local metadata addresses — and follows redirects. Harmless on a
   loopback-only box; a genuine internal-network probe primitive if the port is exposed.
3. **Chromium runs with `--no-sandbox`** (`app.service.ts`) against arbitrary websites. Fine for
   a throwaway container, poor hygiene for a machine holding other work.
4. **No `robots.txt` check.** The enrichment crawler fetches whatever site it is given plus one
   same-host "contact/about" page, ignoring robots directives. Rate limiting is per-request
   (`RATE_LIMIT_MAX=60/min` on the API), not per-target-site.
5. **Build is not reproducible with current dependency ranges.** A clean `npm install` +
   `npm run build` in `apps/api` fails typecheck (`TS2345` in `main.ts:53`: `@fastify/rate-limit`
   v10 types vs Fastify 5.2 instance type). `nest build` still emits `dist/`, so the server runs,
   but CI-style verification is broken upstream. Pin versions before trusting a build.
6. **Google Maps selectors are positional protobuf indexes** (`data?.[78]`, `at(raw, 3, 14, 0,
   0)`). A Maps change breaks extraction silently — you get empty rows, not an error.
7. **`--load-extension` is dead on current stable Chrome.** Chrome 156 (this machine) ignores
   it, even with `--disable-features=DisableLoadExtensionCommandLineSwitch`; the extension never
   loads and the MCP job loop just times out. Automation needs a pinned
   Chrome-for-Testing ≤136 (used here) — production use is a manual "Load unpacked".

## 4. What it actually is

Two independent halves:

- **Extension (the maps scraper).** Content script on `*.google.com`, `yandex.*`, `2gis.*` reads
  the Maps page's own internal data and emits business rows. Optional `<all_urls>` permission
  enables website enrichment. Exports CSV / XLSX / JSON with selectable fields (32 available).
- **Backend (the email finder).** Optional Nest service on `localhost:5050`. Puppeteer renders a
  business website (images/fonts/media blocked) plus one same-host link matching
  `contact|about|impressum|kontakt|…`, then extracts emails/phones/socials from the DOM.
  Completely separable — it needs no extension and no map site.

The MCP bridge (`apps/api/mcp-server.mjs`) is a convenience wrapper: it queues a job, the
extension in your Chrome picks it up every 5 s, scrapes in a background tab, and posts results
back. No new network path.

## 5. Capability answers

### 1. Names, addresses, phones, websites, emails?
| Field | From maps | Measured (45 rows) |
| --- | --- | --- |
| Business name | yes (`title`) | 45/45 |
| Address | yes, plus `street/city/postal_code/state/country_code` split | 45/45 |
| Phone | yes (E.164-ish `+1954…`) | 45/45 |
| Website | yes, but often a **deep link** (`/menu/`, `?utm_source=google`, a branch page) | 42/45 (8 deep links) |
| Email | **no — the maps schema has no email field** | 0/45 |

Also present: `place_id`, `cid`, `maps_url`, `latitude/longitude`, `rating` (the schema also
carries `review_count`, `review_url`, `services`, `latest_post`, `menu_link` and hotel fields,
but the Google parser path used here left them empty), `categories` (full list) +
`main_category`, `labels` (accessibility etc.), `opening_hours`, `claimed`, `booking_link`,
`table_order_link`, `municipality`, `ward`. In the three demo queries that came to 25 / 23 / 24
non-empty columns per row — only `pricing` and `preview_link` came back empty everywhere.

### 2. Owner / decision-maker names?
**No.** No platform emits a person's name. Nothing in the parsers looks for one, and Google
Maps does not publish it. What you get instead are indirect signals: `claimed` (the owner has
taken control of the listing) and, after website enrichment, a first-name local part in an email
(`pierre@pandplawncare.com`, `matt@matteoslandscaping.com`) — suggestive, never verification.
A separate reviews module can capture review text and owner responses, but it is not part of the
business-export path exercised here.

### 3. How does it collect emails?
Only from the business's **own website**, never guessed and never pattern-generated:

1. `mailto:` hrefs (highest confidence), 2. schema.org/JSON-LD `email` fields, 3. visible text,
including light de-obfuscation of `name (at) domain (dot) com`. Every candidate is validated
(shape, no file extensions like `logo@2x.png`) and filtered against a tiny blocklist
(`sentry`, `example.com`, `domain.com`, …); `bestEmail` prefers `info/contact/hello/sales/office`.
No SMTP check, no deliverability score, no Hunter/Apollo-style inference, no address patterns.

Measured yield on 41 distinct South Florida business sites: **20 sites (49%) produced ≥1 email**,
29 distinct addresses in total, **31% of them free-mail** (gmail/aol/yahoo/bellsouth). 12 sites
also yielded phones, 28 yielded social profiles.

### 4. Google Maps, Yandex Maps, 2GIS independently?
Yes — three separate parsers behind one schema, selected by hostname
(`DATA_PLATFORM_DOMAINS`), each with its own 2 s request interval. Google Maps is the rich one;
Yandex and 2GIS return only the 13 base fields (`title/address/phone/website/rating/…`), no
city/zip split, no `claimed`. For South Florida, Yandex and 2GIS yield nothing — their coverage
is Russia/CIS. Functionally irrelevant here.

### 5. CSV / XLSX / JSON export?
All three, with a field picker (32 fields) and an auto-download option.

### 6. Local-only operation?
**Yes, structurally.** The extension sends nothing anywhere except the backend URL you configure
(default `http://localhost:5050`); the backend is stateless and stores nothing. Your records
never leave the machine. The unavoidable third party is the map provider itself — the scraping
traffic necessarily goes to google.com.

### 7. Limitations / source restrictions
- **Google Maps Platform ToS §3.2.3** forbids scraping, caching, and redistributing Maps
  content, and building a derivative database from it. Yandex and 2GIS terms are equivalent.
  This is the same judgment already recorded for `google-maps-scraper-kit`.
- Brittle by construction (positional indexes; silent empty results on layout change).
- Website deep links, and `opening_hours` frequently truncated to the first line.
- Multi-branch businesses collapse: two Gables Family Dental locations on one domain became one
  prospect row (the registry dedupes by website host).
- One page of results per query page; no employee counts, revenue, industry codes, benefits/HR
  signals, or any health-plan information — nothing that identifies an *insurance* need.
- Duplicate-heavy: the same listing appears for overlapping queries; `place_id` dedupe is on you.

## 6. Measured demonstration

Setup: backend built from `apps/api` and run on **loopback only**
(`HOST=127.0.0.1 PORT=5050 PUPPETEER_EXECUTABLE_PATH=<system Chrome>`); extension built with
`VITE_BACKEND_URL=http://127.0.0.1:5050`; collection driven through the real MCP job API on a
throwaway Chrome-for-Testing 136 profile.

```
POST /v1/jobs {"platform":"google_maps","query":"italian restaurant Fort Lauderdale FL","limit":15}
  -> status=done results=15  (~5-10 s per query)
```

Three categories, 15 rows each:

| Query | rows | phone | website | email |
| --- | --- | --- | --- | --- |
| italian restaurant, Fort Lauderdale FL | 15 | 15 | 15 | 0 |
| landscaping company, Miami FL | 15 | 15 | 15 | 0 |
| dental office, Coral Gables FL | 15 | 15 | 12 | 0 |

Sample row as returned (fields verbatim):

```json
{"place_id":"ChIJ77N9tvj92IgR5P8v1psOAFg","cid":"0x88d8fdf8b67db3ef:0x58000e9bd62fffe4",
 "title":"Casa Calabria","address":"4040 Galt Ocean Dr L15, Fort Lauderdale, FL 33308",
 "street":"4040 Galt Ocean Dr L15","city":"Fort Lauderdale","postal_code":"33308","state":"Florida",
 "country_code":"US","phone":"+19549822191","website":"https://casacalabriaftl.com/menu/",
 "rating":4.8,"categories":"Italian restaurant, Fine dining restaurant, Live music bar, Piano bar",
 "main_category":"Italian restaurant","claimed":true,"opening_hours":"Monday: Closed",
 "latitude":26.177865,"longitude":-80.09721,"maps_url":"https://www.google.com/maps?cid=6341084337813848036"}
```

Then enrichment of all 41 distinct websites through `POST /v1/extract-website`, in three
batches (the first, 13 URLs, completed in 10.7 s): 20/41 with an email, 29 distinct addresses,
e.g.

```
info@ilmulinofl.com · info@vitoloitaliano.com · info@bellagambas.com · phb.landscaping@aol.com
pierre@pandplawncare.com · matt@matteoslandscaping.com · elibiton810@gmail.com (Zuckerello's)
```

Manual spot-check: every address above is a real address published on that business's own site;
the five zero-email sites really are form-only or JS-only contact pages. Two of the dental
practices (`dentistsofcoralgables.com`, `universitydentalgrouponline.com`) returned a Facebook
link and nothing else — enrichment does not fail loudly, it just returns empty.

## 7. Integration into the GS Allude prospect workflow

No second application, no second database. The export enters the existing registry through the
existing ingest path:

```bash
# operator runs the extension, exports CSV, then:
python tools/prospects.py import geoleads --file ~/Downloads/geoleadscraper-….csv
python tools/prospects.py list NEW
```

What `engine/prospects.py > geoleads_rows()` maps:

| GeoLeadScraper | registry |
| --- | --- |
| `title` | `name` (rows without a name are skipped, never guessed) |
| `main_category` (or `categories`) | `category` |
| `phone` / `phones` | `public_phone` |
| `website` (tracking params dropped) | `website` — and therefore the dedupe key |
| `address` | `address`, with `city`/`state`/`zip5` filled from the columns or parsed from `"... , FL 33308"` (`Florida` → `FL` to match existing rows) |
| `maps_url` | `evidence_url` (where the row was observed) |

Deliberate exclusions: the export's `email`, `socials`, `rating`, `reviews`, `opening_hours` and
hotel columns are **dropped**. The registry holds business identity only, and an email that came
from a Maps-adjacent export is exactly the kind of record that must be re-derived from the
business's own website before anyone acts on it.

Rows land `decision=NEW`, `verify_state=UNVERIFIED`, `source=geoleadscraper`, and appear on the
existing `/admin/prospects` page with the rest of the queue. Nothing in this path contacts
anyone; `prospect_sources()` now lists the source as `OPERATOR_FILE` with the Maps caveat
attached, next to the still-`TOS_BLOCKED` headless scraper.

Real end-to-end check on this machine (temp DB, `PROSPECTS_DB=C:/tmp/prospects_demo.db`):

```
added 44, updated 1, skipped 0 (mapped 45 export rows -> business identity only)
{"NEW": 44, "TOTAL": 44}
```

The one "updated" is the correct merge of the two Gables Family Dental branches onto one domain.

**Suggested operator sequence:** scan a category → import → `list NEW` → `verify … 
PUBLIC_DETAILS_CHECKED "own website + FL license lookup"` → `decide … PRIORITIZED`. That is the
same gate the rest of the pipeline already enforces, so these rows cannot skip verification.

## 8. What was not done

- No messages, emails, or calls to any business; nothing was submitted anywhere.
- No writes to `data/prospects.db` or `data/leads.db` — the demo used `C:/tmp/prospects_demo.db`
  and a throwaway Chrome profile.
- No Yandex/2GIS run (no South Florida coverage; would only prove the parser loads).
- No Docker path (the local build already exercised the same code).
- No assertion that this clears Google's terms. It does not; §5.7 stands.
