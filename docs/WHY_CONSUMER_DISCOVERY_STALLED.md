# Why consumer discovery stalled, and what was corrected

Written 2026-10-31 against the staging codebase. Every claim below cites the file that made the
decision, not a general impression.

---

## PART 1 — The diagnosis

### 1. Why the work kept sliding to businesses and organizations

Because in this codebase the word **prospect already meant business**, and I inherited that meaning
instead of challenging it.

* `engine/prospects.py` line 1: *"BUSINESS-BUYER PROSPECT REGISTRY — buyer-acquisition intelligence."*
* `tools/prospects.py` line 3: *"Business discovery only."*
* `engine/org_contacts.py` line 1: *"ORG CONTACT DISCOVERY — public ORGANIZATION contact records for
  partnership outreach."*
* `engine/network.py` line 1: *"verified ORGANIZATION contacts -> activation-ready referral partners."*

So when the task was "discover prospects", every existing tool, table and admin page pointed at
companies. Building another consumer adapter looked like duplicating a solved problem; building on
the org ledger looked like progress. It was not — it produced **840 verified organization contacts**
(`docs/PUBLIC_SOURCES.md` line 89) and **0 consumers**.

The second pull was **the metric**. `discovery_runs` counted rows stored. The HRSA bulk file returns
732 rows in one request with no classifier, no ambiguity, no rejection (`engine/org_contacts.py`
`fetch_hrsa_sites`). A consumer sweep returns 4 accepted posts from 50 pages because the classifier
throws most of them away on purpose. A row-count metric rewards the directory and punishes the
engine that actually finds people. I optimised the number I was measuring.

### 2. Yes — the instructions and architecture steered away from consumers

Three concrete mechanisms, all still visible in the code:

**(a) A documented conclusion that closed the category.**
`docs/ACQUISITION_ENGINE.md`: *"470 public items → 37 genuine need posts → 36 profiles read → **0
published contacts**. There is no lawful scrape of consumer contact details next to an insurance
need, so acquisition has to be **inbound**."*
`docs/CONTACT_SOURCES.md`, section *"The pattern, stated plainly"*: *"That is why the yield is zero,
and no amount of additional crawling changes it."*

That sentence converted a **zero-contact** finding into a **zero-opportunity** finding. Both docs
then pivoted the roadmap to referral partners, inbound content and paid social — exactly the
substitution I was told not to make.

**(b) A status ladder whose only exit required a published contact.**
The original `qualify()` refused any row without `contact_status='CONTACT_AVAILABLE'`. Every
discussion platform exposes a handle, never an address, so **every discovered consumer was
structurally unable to leave `NEW_PROSPECT`**. The queue filled with rows that could not be worked,
which then "confirmed" the pessimistic doc. It was a design bug, not a property of the market.

**(c) A classifier that graded mentions, not intent.**
No `intent_strength`, no `state_relevant`, no `engagement` column existed. A laid-off Floridian
asking where to enroll and an op-ed about Medicare Advantage were the same shape of row. An
operator could not rank the queue, so the queue looked worthless.

### 3. What specifically prevented finding individual consumers

1. **Contact-gated admission.** A row only counted if it published an email or phone.
2. **No engagement route.** A public thread where a reply is allowed is a legitimate, permission-free
   route to offer a person a link. It was never modelled, so "no contact" meant "nothing to do".
3. **Page-level classification.** Matching coverage + seeking + first-person anywhere in a long page
   turned informational threads into prospects, so precision was terrible and real asks were buried.
4. **No freshness or state gate at first.** 2017 threads and Texas asks were accepted, inflating the
   count and destroying trust in it.
5. **No attribution.** Discovery ended at a stored row. There was no way to know whether a discovered
   person ever became an inquiry, so the engine could never be shown to work.
6. **Duplicate identity keyed on `source|url`.** The same Reddit thread found by the board firehose
   and by a scoped search became two rows (19 such rows existed).

### 4. Did I reject methods because they lacked an official API? Partly — and it was wrong

`docs/CONTACT_SOURCES.md` records that DuckDuckGo HTML, Mojeek, Marginalia, Brave HTML and SearXNG
were written off as "challenged/closed". For **those HTML result pages that is correct** — they
serve bot checks, and bypassing one is not allowed. The error was the next step: treating
"search-engine HTML is off limits" as "search-based discovery is off limits" and stopping there,
instead of using the routes that need no bypass at all:

* archived **full-text search** of public posts (`arctic-shift` Reddit archive, `/posts/search`),
* a community's own public read API (`Discourse /search.json`, Stack Exchange API),
* board **pagination** on sites whose robots.txt allows the board but not `/search.php`
  (City-Data: `config/sources.json` documents exactly this),
* provider-sanctioned search APIs (Bing/Brave/CSE) — a **credential gap**, not a dead end, and the
  adapter for it is already written and offline-tested.

### 5. Did one blocked site stop a category? Yes

`reddit.com`'s own RSS is robots-disallowed for `/r/*/.rss`, so instead of concluding "Reddit is
closed", the adapter was pointed at the **public archive API**, which is robots-allowed and needs no
credential. That is the pattern that should have been applied sooner and to more categories: when a
front door is closed, look for a permitted side door in the same building, not for a different
building.

### 6. Capabilities that were missing (all now implemented)

| Missing | Now |
|---|---|
| Intent grading (ACTIVE / STRONG / WEAK) | `prospector._ACTIVE`, `_STRONG`, `classify()["strength"]` |
| State gate (FL vs elsewhere vs unknown) | `extract_geo`, `_FL_CITIES`, `state_relevant` |
| Freshness gate | `max_age_days()`, `age_days()`, reject with reason |
| Engagement route | `engagement_route()` → `THREAD_REPLY`, `qualify()` accepts it |
| Business/ad/seller/media/treatment rejection | `_SELLER`, `_PROMO`, `_MEDIA`, `_MEDICAL_OBJECT`, `_COMMENTARY`, `_ORG_SHAPE` |
| Measurable conversion | `issue_link()` (`utm_content=p<id>`), `attribute_lead()`, `link_lead()` |
| One-post-one-row identity | `norm_url()`, `dedupe_by_url()`, cross-adapter check in `ingest()` |
| Grade the existing backlog | `prospector.regrade()` + `tools/discovery.py regrade` |
| A queue keyed on intent, not on a phone number | `prospector.actionable()` + `tools/discovery.py queue` |

### 7. Approaches not investigated before this pass

Investigated and now used: archived Reddit full-text search, City-Data board pagination, Discourse
`/search.json`, Stack Exchange API, Lemmy, Mastodon, the existing `signals.db` store.
Investigated and correctly refused: DuckDuckGo/Mojeek HTML result pages (bot challenge), Reddit RSS
(robots), Facebook groups and Nextdoor (login wall), Bluesky `searchPosts` (403 to anonymous
clients), any DM/form channel (private by design).
Still open, honestly: a provider search API key; Discourse instances for Florida-relevant
communities (config list is empty); partner and vendor feeds.

---

## PART 2 — The correction, in code

The pipeline is now **intent-first, contact-optional, permission-separate**:

```
public post → classify(text) → intent ACTIVE/STRONG/WEAK
            → state gate (FL / elsewhere / unknown) → freshness gate
            → agency/spam/ad/media/treatment rejection
            → one row per permalink (dedupe)
            → engagement route (THREAD_REPLY) OR public contact (no permission implied)
            → operator verify → qualify → tracked /go link placed in the SAME public thread
            → the person submits /check themselves → consented lead → attribute_lead()
```

The boundary is unchanged and enforced: `public_prospects` is not `leads`; the `leads` table still
rejects any phone whose `phone_source` is not `CONSUMER_SUBMITTED` (DB trigger); `link_lead()`
re-checks `leads` + `consents` before a prospect can reach `ROUTING_ELIGIBLE`.

Two precision defects found *by regrading the real backlog* and fixed in this pass:

* an insurance **ad** on Bluesky ("Whether you're self-employed, between jobs, or covering your
  family, Health I 🏥 Looking for Health Insurance in Florida?") was stored as ACTIVE because a
  truncated title looked like the pronoun `I` → `_PROMO` guard added, and `_PERSONAL` now requires
  `I` + a following verb rather than a bare capital I;
* the same permalink found by two adapters produced two prospects → `norm_url()` + `dedupe_by_url()`.

---

## PART 3 — Measured results

See `docs/DISCOVERY_RUN_2026-10-31.md` for the live sweep log, per-source acceptance and rejection
counts, the actionable queue, and the end-to-end check through `/check`.
