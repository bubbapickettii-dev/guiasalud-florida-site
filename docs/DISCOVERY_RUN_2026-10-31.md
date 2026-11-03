# Live discovery run — 2026-10-31

Consumer discovery, executed against live public sources, then carried through to a consented test
inquiry. Every number below comes from `data/leads.db` (`discovery_runs`, `public_prospects`,
`leads`, `consents`), not from a plan.

Reproduce:

```
python tools/discovery.py run --network --sources reddit_archive_search,stackexchange,lemmy,mastodon,citydata
python tools/discovery.py report
python tools/discovery.py queue
python out/uat/prospect_conversion.py        # labelled test, DB copy
```

---

## 1. Sources accessed

| Source | Access basis | Requests | Pages/posts read |
|---|---|---|---|
| Reddit public archive (`arctic-shift`), subreddit-scoped full-text search | public read API, robots-allowed, no credential, paced ~12 s | 2 sweeps | 549 |
| Reddit public archive, board firehose | same | earlier today | 474 |
| City-Data `health-insurance` board | robots.txt allows boards/threads (only `/forum/search.php` disallowed); HTML, no login | 1 sweep | 20 |
| Stack Exchange API | public read API | 2 sweeps | 16 |
| Lemmy `/api/v3/search` | public read API | 2 sweeps | 120 |
| Mastodon tag timelines | public read API | 2 sweeps | 150 |
| `signals.db` store (Reddit/HN/SE/fediverse, collected earlier) | read-only reuse | — | 400 |

Refused or unavailable, and why (no attempt to bypass anything):

* `r/florida`, `r/tampa`, `r/orlando` archive queries → **HTTP 422** (volunteer archive back-pressure);
  the adapter backs off once and moves on. 3 such errors this run.
* Reddit's own `.rss` — disallowed by robots.txt; not used. The archive API replaced it.
* Bing/Brave/Google CSE — adapter built, **no key configured**, so it reports 0 and never scrapes a
  result page. Credential gap, not a dead category.
* DuckDuckGo/Mojeek HTML — bot-challenged; refusing to solve a challenge is the correct answer.
* Facebook groups, Nextdoor, DMs, application forms — login or private by design; not touched.

## 2. What was accepted, and how much of it was relevant

Today, across all adapters: **2,447 pages/posts read → 365 accepted as consumers → 170 new prospect
rows → 1,977 duplicates → 2,082 rejected → 23 errors.**

The two clean sweeps in this pass:

```
05:21-05:22  reddit_archive_search 181 read  36 accepted   0 new  36 dup  145 skipped  2 err
             stackexchange           8 read   0 accepted
             lemmy                  60 read   1 accepted   0 new   1 dup
             mastodon               75 read   2 accepted   0 new   2 dup
             citydata               20 read   0 accepted  20 skipped
05:28        reddit_archive_search 368 read  38 accepted  34 new   4 dup  330 skipped  3 err
```

The 05:21 sweep is mostly **duplicates by design**: those posts were first stored at 05:03–05:06, and
re-finding them is the engine proving it is repeatable, not discovering anything new. The 05:28 sweep
was the productive one — the query cap rotated across **12 boards** instead of spending every request
on the first two, so r/florida, r/tampa, r/Miami, r/orlando, r/Jacksonville, r/povertyfinance and
r/Unemployment were actually queried for the first time: **34 new prospects from 368 posts.**

Stored corpus right now (`python tools/discovery.py report`):

```
NEW_PROSPECT            153     engagement route open    152
REJECTED                 89     with a public contact      0
DUPLICATE                18     intent ACTIVE 45 · STRONG 64 · WEAK 44
```

Live rows by intent and location: 45 ACTIVE, 64 STRONG, 44 WEAK; **40 of them explicitly Florida**
(5 ACTIVE, 23 STRONG, 12 WEAK), 36 more ACTIVE where the person did not name a state.
Communities: r/HealthInsurance 58, r/Medicaid 48, bsky.app 24, r/Insurance 9, r/obamacare 8,
r/povertyfinance 1, HN 1, Mastodon 2, Lemmy 1.

Real, source-backed examples now in the queue:

* `reddit.com/comments/1w6cv6l` — *"My wife and I live in north east Florida and need health insurance."* (ACTIVE, FL)
* `reddit.com/comments/1vjcz01` — *"How do you get state insurance, i missed open enrollment central Florida … I need help getting insurance."* (ACTIVE, FL)
* `reddit.com/comments/1x1qd1y` — *"If there's a 90-day waiting period with my new employer, what are my options for maintaining coverage in the meantime?"* (ACTIVE, FL)
* `reddit.com/comments/1xj21ri` — *"What are my options. Was informed suddenly that i lost insurance coverage."* (ACTIVE)
* `reddit.com/comments/1xjsncs` — *"I need health insurance for myself (single adult) COBRA is $836.40 per month …"* (ACTIVE)

## 3. Contact opportunities

**0 of 260 rows published an email or phone.** That is the finding the old design misread. What the
rows *do* have is an **engagement route**: 152 of them sit on a public discussion thread where a reply
is allowed. That is a legitimate way to put the `/check` link in front of the person without holding,
guessing or dialling anything, and it is now modelled as `engagement='THREAD_REPLY'` instead of being
treated as failure.

Contact-first capture still exists for the rare post that publishes a number, and every such row is
stored as `CONTACT_INFORMATION_FOUND` with no permission basis — nothing may be dialled, texted or
emailed from it.

## 4. Why records were accepted or rejected

Accepted only when one 1–3 sentence window contains **coverage vocabulary + a seeking verb + a
first-person stake**, with no negation, no commentary, no second-person ad copy, and no treatment
object standing in for coverage. Then gated on **state** (explicit other state → not servable),
**freshness** (> 180 days → rejected) and **identity** (one row per permalink across adapters).

Rejections this pass (top reasons, from `status_reason`):

```
29  stale post (older than 180 days)
 7  no statement ties a coverage need to this person (coverage sentences=3, seeking=0)
 6  no statement ties a coverage need to this person (coverage sentences=1, seeking=0)
 5  too short to judge
 5  no statement ties a coverage need to this person (coverage sentences=0, seeking=0)
 4  medical need (prescription), not coverage shopping
 +  City-Data 20/20 and Stack Exchange 8/8 rejected: informational threads with no personal ask
```

Two precision defects were found by regrading the real backlog and fixed in this pass:

1. a Bluesky **insurance ad** ("Whether you're self-employed, between jobs, or covering your
   family, Health I 🏥 Looking for Health Insurance in Florida?") was stored as ACTIVE because a
   truncated title read as the pronoun `I` → added `_PROMO` and required `I` + a following verb;
2. the **same permalink** found by two adapters produced two rows → `norm_url()` + `dedupe_by_url()`
   (24 rows collapsed so far).

## 5. Did anything progress toward a consented, qualified inquiry?

Yes — on a DB copy, with a **labelled test** submission (`out/uat/prospect_conversion.py`):

```
1. discovered  #238  intent=ACTIVE state=TARGET route=THREAD_REPLY
   wrote      "need help with open enrollment options ... - FL"
   source     https://www.reddit.com/comments/1xg4box
2. verify=True qualify=True            (operator actions, both requiring a written note)
3. tracked link http://127.0.0.1:8000/go/p238-reddit_arc-8516
4. POST /api/lead -> 200 ok=True lead=L20261031-bd2281bf
   lead row   is_test=1 tier=C qual=QUALIFIED routing=NO_BUYER
              link_code=p238-reddit_arc-8516 phone_source=CONSUMER_SUBMITTED
   consent rows 1
5. attribute_lead=True
   prospect now ROUTING_ELIGIBLE lead_id=L20261031-bd2281bf
```

The chain is real: a public post found by the sweep → an operator qualification → a tracked link →
the person's **own** form submission with a consent row → the prospect marked `ROUTING_ELIGIBLE` and
owned by the normal lead pipeline. `NO_BUYER` is the honest staging answer: the test lead scores
tier C and the configured buyer prices A/B. No buyer was charged, nothing was sent to anyone, and the
staging `leads` table was not modified by the test.

## 6. What is still missing (stated plainly, not as a blocker)

* A provider **search API key** (Bing free tier / Brave / Google CSE) — the only lawful way to reach
  one-off posts that no board index covers. The adapter is written and offline-tested.
* **Discourse instances** for Florida-relevant communities: the adapter exists, `config` has none.
* **Volume policy.** ~38 accepted posts per 368 read (10%) matches the 180-day window; the lever that
  actually increases volume is more boards and more keyless read APIs, not looser classification.

---

## 7. First verified consumer match (same day, keyless sweep)

**Row #303 · `https://www.reddit.com/comments/1wve6oj` · r/tampa · 2026-10-01 · u/Mammoth-Song-6523**

> "I'm currently unemployed (job searching, but not having any movement) and I don't qualify for
> Medicaid. I was on Tricare Young Adult, but I stopped it at the end of September because I can no
> longer afford it and I age out of it in November. does anyone here have the Hillsborough County
> Healthcare Plan? is it okay coverage? … I'm also not sure if I qualify. I'm 26 and live with my
> parents, but we don't combine finances, I pay my own bills, and they don't claim me on taxes."

Why it qualifies: an individual (26, Hillsborough County FL), unemployed, dropped her own coverage
over cost, loses Tricare Young Adult in November, is not Medicaid-eligible, and is asking **now**
what she qualifies for. Not an agent, broker, business, journalist, or general discussion.

Stored row (no contact published, no permission claimed):

```
status=VERIFIED            intent=STRONG   state=TARGET (geo FL)   confidence=0.60
engagement=THREAD_REPLY    contact_status=CONTACT_NOT_AVAILABLE     permission=NONE
tracked_link=""            lead_id=""      discovered_at=2026-10-31T05:56:04Z
evidence: verified_excerpt (1,139 chars) + thread {subreddit, author, created, comments=5, score=13}
```

Engagement route: the thread is public and open (5 comments, not removed), so a reply in that thread
is a legitimate public route. **No DM, email, phone or comment was sent**, and the row cannot become
a saleable lead from here: that requires her own `/check` submission with consent.

How it was found, and what each keyless route actually returned on 2026-10-31:

| Route | Access result |
|---|---|
| reddit.com RSS (`/r/<sub>/new/.rss`) | **refused** — `robots.txt` is `User-agent: * → Disallow: /` (returns 200, not used) |
| Reddit public archive (`arctic-shift`) search | **open** (`Disallow:` empty) — with the operator phrase pack: 787 posts read → 79 accepted → **62 new prospects** |
| Reddit public archive single-post read | **open** — used to verify author, date, body, comment count |
| Bluesky public AppView `searchPosts` | **403** to anonymous clients; robots allows, the service does not |
| Mastodon tag timelines (`#healthinsurance`, `#healthcare`) | **open** — 75 posts, 2 accepted, 1 new |
| Lemmy `/api/v3/search` | **open** — 60 posts, 1 accepted |
| Discourse `/search.json` (4 public instances probed) | **open** (200/429) but no US health-insurance consumer community among them; 0 rows |
| Stack Exchange API | **open** — 8 questions, 0 accepted (informational, not personal asks) |
| City-Data health-insurance board | **open** — 20 threads, 0 accepted (informational) |
| Bing / Brave / Google CSE | adapter ready, **no key** — not used |

Second candidate inspected but deliberately left un-promoted: `#324` r/orlando
(`1vykbhy`, 2026-08-26) — an Orlando family asking for a Spanish-speaking agent for a 64-year-old
retiring father whose COBRA is unaffordable. Genuine consumer household, FL, thread open; still at
`NEW_PROSPECT` so the ladder is seen to work rather than asserted.

---

## 8. Second sweep, second verified match, and the precision holes it exposed

Second phrase pack (Florida/family/Spanish: "looking for health insurance in florida", "need insurance
for my family", "where can i get affordable health coverage", "me quede sin seguro medico",
"no tengo seguro medico", …):

```
reddit_archive_search  fetched=434  accepted=37  new=27  dupes=10  skipped=397  errors=17
  skipped: stale 61 · seller/agent voice 9 · outside FL 7 · no personal ask 6
```

**Row #324 · https://www.reddit.com/comments/1vykbhy · r/orlando · 2026-08-26 · u/chuweeen**

> "looking for a health insurance agent that speaks spanish in the orlando area. Need a recommendation
> for my dad he just retired and COBRA is well very expensive to say the least lol he's 64 so will be
> on for only a year. And healthcare.gov is still a bit expensive as well, looking in to private
> insurance. So just hoping to get in touch with someone that can guide us and sit down to see what
> our options are."

Verified: consumer household, Orlando FL, actively seeking help now, thread open (4 comments, not
removed). `status=VERIFIED intent=STRONG state=TARGET(FL) confidence=0.60 engagement=THREAD_REPLY
permission=NONE tracked_link="" lead_id=""`. Still not a lead: she has not submitted `/check`.

The sweep also produced four **real precision defects**, each now closed with a test:

| Defect | Example admitted | Fix |
|---|---|---|
| bare "coverage" matched vehicle cover | "the dealer … proof of insurance", "my policy … driving records" | `_NONHEALTH` + `_HEALTH_TOKEN`: a vehicle/home window with no health token is rejected (6 rows) |
| advice addressed to an audience | "If anyone reading this is searching … do not make the mistake my family made" | `_ADVICE` guard (2 rows) |
| visitor/travel cover from abroad | "travelling from India … travel medical insurance" (same text posted in two subs) | `_TRAVEL` guard (2 rows) |
| **a relative's state tagged as the poster's** | "i'm 22 living in NJ, and my father who lives in Florida" → tagged FL, servable | `_SELF_LOC`: a first-person location statement outranks any other state mention |

That last one matters commercially: it would have routed a New Jersey resident into a Florida buyer
queue. `#334` is now `geo=NJ, state=OTHER` — kept as the real consumer it is, excluded from the
servable queue.

Also learned, and recorded rather than guessed:

* **Mastodon search is not keyless-usable.** `/api/v2/search?type=statuses` returns HTTP 200 with an
  EMPTY array on mastodon.social, mstdn.social, fosstodon.org, mas.to, mastodon.world,
  infosec.exchange and hachyderm.io — status full-text search requires authentication. The tag
  timelines remain the working Mastodon route; the adapter records the zero honestly.
* **No public Discourse instance for US health insurance was found.** `community.healthcare.gov`,
  `forum.healthcare.gov`, `talk.healthcare.gov`, `community.coveredca.com`, `forum.coveredca.com` and
  `community.medicaid.gov` do not resolve. Generic instances answer `/search.json` but carry no such
  community, so the adapter stays at 0 rather than being pointed at irrelevant content.

State after both sweeps: **2 VERIFIED, 0 tracked links, 0 leads from discovery**, 349 prospect rows,
140 in the servable ACTIVE/STRONG queue.
