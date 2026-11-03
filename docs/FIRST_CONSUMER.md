# First real consumer: what is ready, what is missing

Status 2026-10-31. This page is the handoff for the one thing the system still has never done:
carry a real person from a public discussion to a consented inquiry. Discovery is not the finish
line; a prospect row is not a lead.

## What is built and verified (this pass)

* **Source-attributed bridge** — `/i/<code>`: one page per qualified prospect, EN + ES, matched to
  the situation their own post described (lost coverage / COBRA / premium cost / 1099 / enrollment /
  family), asking for nothing, with a single CTA into `/check` that carries `lc` plus
  `utm_source|medium|campaign|content` so the visit and any later submission stay attributed.
* **Bridge content exists for every block** in `config/bridge.json`; a block never promises
  eligibility, a price, a `$0 plan`, or a government look.
* **Attribution through submission** — `POST /api/lead` keeps `link_code` + utm on the lead;
  `prospector.attribute_lead()` links a consented lead back to the prospect row
  (`ROUTING_ELIGIBLE`), and refuses when no consent row exists.
* **Consent before the pipeline** — the server re-renders the consent text from the versioned
  template, stores it with the snapshot hash, IP and page URL; nothing is routed without it.
* **Situation detection fixed** — `situation_for()` now reads the operator-saved excerpt
  (`inspect --save`), and "missed enrollment / cannot wait" outranks the job-loss block. Before
  this, a person three months past the window was told they had a 60-day SEP.
* **Kit links follow `BASE_URL`** — `tools/discovery.py kit|issue` now resolve the origin exactly
  like `app.py`, so the go-live switch cannot leave localhost links in a ready-to-post reply.
* **Journey replayed end to end on a throwaway DB copy** (`out/uat/bridge_journey.py`):
  bridge (EN/ES) → attributed view → `/check` → TEST submission → QUALIFIED tier A →
  ROUTED/DELIVERED to the demo health buyer → consent row → prospect `ROUTING_ELIGIBLE`.
  No real buyer delivery, no real charge, no row written to the staging database.
  The same script pins the other outcome too: a low-intent profile is recorded `NO_BUYER`
  with a reason and never delivered or charged.

## Three opportunities prepared for engagement (drafts only, nothing posted)

| # | Thread | Situation | Why it fits |
|---|--------|-----------|-------------|
| 242 | r/HealthInsurance `1x1qd1y` — laid off from a FL union job, Florida Blue ending, 90-day wait at the new job | `lost_coverage` | The gap between two employer plans is exactly the SEP / Marketplace question |
| 246 | r/HealthInsurance `1w6cv6l` — couple in NE Florida, ~$600/mo budget, burned by lead-gen sites | `cost` | Two adults + a stated budget: the income range decides the price |
| 247 | r/HealthInsurance `1vjcz01` — moved to central FL, missed enrollment, needs a cardiologist | `enrollment` | Open Enrollment opens Nov 1; the dates are the answer they need today |

Two rows were rejected with written reasons: #238 (employer PPO vs HDHP choice — our check would
not answer it) and #216 (vague, ~3 months old). Kits live in `public_prospects.kit_json`; print one
with `python tools/discovery.py kit --id 242`.

## The remaining gap, in order

1. **No public origin.** `config/site.json` still says `localhost:8000`, so every prepared link is
   unpostable. Flipping `BASE_URL=https://<domain>` regenerates links/QRs/guides; hosting is an
   owner action (`docs/DEPLOYMENT.md`, `deploy/GO_LIVE_CHEAP.md`). Until this exists, a consumer
   cannot arrive, no matter how good the reply is.
2. **Approval to post.** Three replies are drafted. Posting is manual, from the operator's own
   account, one per thread, after reading that community's rules on self-promotion links.
3. **The buyer only prices tiers A and B.** `demo-health-agent` is the only active buyer for
   `health_aca` and accepts A/B at 18-64. A qualified tier C health lead (low urgency or income
   under 100% FPL / over 400%) is held as `NO_BUYER` with a recorded reason — correct behaviour,
   but it means a real submission is sellable only when the profile lands A or B. This is the
   honest conversion ceiling today: a real consumer can arrive and qualify and still not be
   routable. A real licensed FL agency is still not onboarded (`docs/FIRST_BUYER_PILOT.md`).
4. **Discovered demand is thin at the top of the funnel.** A 7-query ACTIVE-intent sweep fetched
   43 posts and accepted 1 new row; 5 stored rows are ACTIVE + Florida, of which 3 were worth a
   reply. Volume has to come from repeat sweeps and from the channels in `docs/CHANNELS.md`, not
   from widening what counts as a prospect.

## One non-test lead exists in staging and is not a consumer acquisition

`L20261028-5d3efff0` (2026-10-28) is `is_test=0`, arrived at `http://localhost:8000/check` from
`127.0.0.1` with no campaign attribution, and was delivered to `demo-health-agent` in dry-run with
a test-flagged ledger row. It is a local QA submission that was not flagged as one, not a consumer.
It is left untouched rather than rewritten; count it as zero real consumer inquiries.

## Exact order once the domain exists

```
BASE_URL=https://<domain> python tools/discovery.py issue --id 242   # verify the link resolves
BASE_URL=https://<domain> python tools/discovery.py kit   --id 242   # reprint with the live origin
# operator posts by hand, one reply, disclosure line included
python tools/discovery.py status                                     # bridge views -> submissions
```

`status` separates prospects, prepared kits, real visits, real submissions, consented leads and
routed leads, and excludes TEST traffic from every consumer line.
