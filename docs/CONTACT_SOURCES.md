# Contact acquisition — what each candidate source actually yielded

Purpose: find public sources that publish **consumer contact details next to a health-insurance
request**, so real people — not profiles — can be added to `data/leads.db`.

Everything below is measured, not estimated. Numbers come from live reads performed with the
scraper's own paced, robots-checked fetcher (`engine/web_extract.py`) and from the contact miner
(`engine/prospect_contacts.py`). "Contact extracted" means an email address or phone number that a
person published in their own post and that the miner could read from the page text — a profile
link, an organisation's switchboard, or a guessed address never counts.

Current corpus state (`data/leads.db`):

| source | rows | contact available |
|---|---|---|
| legacy:bluesky | 38 | 0 |
| legacy:lemmy | 4 | 0 |
| legacy:stackexchange | 13 | 0 |
| signal_store | 37 | 0 |
| citydata_forum | 3 | 0 |
| **total `public_prospects`** | **95** | **0** |

Corpus-wide contact scan of the signal store (`data/signals.db`, 560 rows):

```
contact_available rows: 0  emails: 0  phones: 0
```

## Measured candidate sources

| source | records examined | genuine insurance prospects | rows publishing contact | contact extracted | permission evidence | verdict |
|---|---|---|---|---|---|---|
| City-Data insurance board | 50 thread pages | 3 kept (4 classified) | 0 | 0 | none | **dead for contacts** — Medicare-age cost talk, no self-published contact |
| Discourse instances (`/search.json`) | adapter built, no permitted instance supplied | 0 | 0 | 0 | n/a | **idle** — needs a named instance whose ToS allows reading |
| Reddit (`.json`, old reddit) | 3 request shapes | 0 | 0 | 0 | n/a | **blocked** — 403 to non-browser clients |
| Facebook groups / pages | 2 attempts | 0 | 0 | 0 | n/a | **blocked** — login wall |
| Nextdoor | 1 attempt | 0 | 0 | 0 | n/a | **blocked** — login wall |
| Craigslist (community/assistance, insurance-services) | listing pages + robots | 0 | 0 | 0 | n/a | **prohibited** — ToS forbids automated collection |
| Medicare.gov / CMS consumer content | plan finder + help pages | 0 | 0 | 0 | n/a | **no consumer posts** — reference data only |
| healthinsurance.org community | 1 attempt | 0 | 0 | 0 | n/a | **404** — community removed |
| VisaJourney health-insurance forum | 2 attempts | 0 | 0 | 0 | n/a | **blocked** — 403 |
| mail-archive.com (public mailing-list archive, robots: all allowed) | 1 search page + 1 message page | 0 | 0 | 0 | none | **dead for contacts** — sender addresses are stripped; only the list address remains |
| narkive.com (Usenet archive) | robots fetch | 0 | 0 | 0 | n/a | **unavailable** — 503 |
| freeforums.net | robots fetch | 0 | 0 | 0 | n/a | **unavailable** — timeout |
| Bluesky / Lemmy / StackExchange (already ingested) | 55 kept rows | 55 | 0 | 0 | none | **intent only** — platforms expose a handle, never an address |
| Government / county assistance directories | sampled pages | 0 | 0 | 0 | n/a | **organisations** — office numbers, not consumers |
| DuckDuckGo HTML / lite (`robots.txt`: Allow: /) | 3 query shapes | 0 | 0 | 0 | n/a | **challenged** — 202 bot page; solving it would bypass a protection, so the adapter never touches it |
| Mojeek / Marginalia / Brave / SearXNG instances | robots + 1 query each | 0 | 0 | 0 | n/a | **closed** — `/search` disallowed by robots, 429/403, or captcha |
| Bing Web Search v7 / Brave Search / Google CSE (supported APIs) | adapter built, **no credential in the environment** | 0 | 0 | 0 | n/a | **configured, waiting on a key** — this is the one discovery channel that reaches the open web lawfully and at scale |
| Bluesky public searchPosts | live attempt | — | — | — | n/a | **403** to anonymous clients now; the legacy rows came from an earlier window |

## The pattern, stated plainly

Every crawlable channel that carries strong consumer insurance intent routes contact through a
**private channel by design** — a DM, a login-walled group, a platform relay address, or a form.
Every channel that publishes contact openly has **no insurance intent** (mailing lists, classifieds,
directories). The intersection the objective needs — public page, self-published email/phone, and a
health-insurance request — is where spam harvested five years ago, and the platforms closed it.

That is why the yield is zero, and no amount of additional crawling changes it. The remaining
lawful routes that *do* produce contacts are:

1. **Our own inbound.** The `/check` journey already collects phone/email with recorded consent.
   It is the only channel in this codebase where intent and permission arrive together.
2. **A permissioned partner feed.** A navigator, agency, or community organisation that collects
   consumer requests with consent can hand them over with the permission basis attached.
3. **A licensed lead vendor.** Purchased, with documented collection permission and TCPA basis —
   money and a compliance read, not a scraper.

## Search-driven discovery (built today)

The board adapters can only see pages whose index they already walk. A search API inverts that: it
finds the page *because* it contains the phrase, which is the only realistic way to reach the long
tail of one-off public requests where somebody wrote "email me at …" beside the ask.

`engine/discovery.py` now carries a `websearch` adapter that uses **only provider-sanctioned APIs**
(Bing Web Search v7, Brave Search, Google CSE). It sends the query pack from
`config/sources.json > discovery > websearch > queries` (10 queries, each pairing an intent phrase
with a contact token, English and Spanish), then reads every result page through the same
robots-gated, paced fetcher as the board adapters, extracts the post body, and hands it to the
contact-first classifier. It never reads a search engine's HTML results, never solves a captcha and
never retries a 401/403/429 — a refused page is counted, not forced.

Measured today (`python tools/discovery.py run --network --sources websearch`):

```
websearch  fetched=0 accepted=0 new=0 errors=0 contacts: 0 row(s), 0 email(s), 0 phone(s)
  note: bing: no credential - set BING_SEARCH_KEY; the adapter will not scrape a search-engine result page
```

That zero is a **credential gap, not a dead source**: the queries are written, the reader is built
and tested offline against a simulated provider response, and the pipeline stores and counts the
email the moment a real result page publishes one. One key (Bing free tier, Brave, or a Google CSE
key + cx) turns it on.

## What is built and ready either way

`engine/prospector.py` now runs **contact-first**: a post that publishes an email or phone next to
coverage vocabulary is kept even when the ask is too vague to classify as a clean need. Those rows
land as `CONTACT_INFORMATION_FOUND` with `weak_intent: true` in the evidence blob and **cannot be
promoted automatically** — a human reads them first. Holding a contact we may not use is still
better than discarding it unseen, and it means the day a source does publish one, it is captured on
the first sweep instead of after a rewrite.

## Reproducing the numbers

```
python -m pytest tests/test_discovery.py -q                       # 17 tests: classifier, contact-first, search adapter
python tools/discovery.py run --network                           # City-Data / Discourse sweep
python tools/discovery.py run --network --sources websearch       # provider-API search sweep
python tools/discovery.py report                                  # pages / prospects / emails / phones per adapter
python tools/collect.py prospect-scan                             # signals.db -> leads.db (contact miner)
```
