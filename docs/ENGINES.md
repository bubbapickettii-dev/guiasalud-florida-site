# Lead machine: engines → one system

**Order (Bubba, Oct 8):**
1. Launch the lowest-cost channel.
2. Measure real results and improve it.
3. Add more low-cost channels.
4. Paid ads only when the numbers justify it.

Meta stays OFF until `tools/engine_report.py` shows the paid gate = GO (≥10 real organic leads delivered, acceptance ≥60%) **and** Bubba approves.

**New audience (Oct 8): separating service members (X026).** One dated trigger (separation) starts the 120-day SGLI conversion, the 240-day VGLI no-health-review window and the 60-day CHCBP deadline. Pages: `/learn/leaving-military-life-insurance-florida` (life) and `/learn/leaving-military-health-insurance-florida` (health). Reached through 7 installation/agency transition offices (`mil-*` in outreach). Built by `research/build_military.py`; test `tests/e2e_military.py`.

**First live engine:** distribution of the layoff pages through employer HR teams and CareerSource Rapid Response, plus Spanish community page owners for the carrier-exit guide. The queue is at `/admin/organic`; the send-ready messages are in `out/organic_pack/OUTREACH.md`; hosting is covered in `deploy/GO_LIVE_CHEAP.md`.

**Search evidence (Google Trends, Florida, last 90 days, Oct 8):**
- "health insurance" and "life insurance" are the big generic terms.
- "Cigna marketplace", "Molina marketplace", "Obamacare 2027" and "group life insurance" only started registering in late September, at tiny levels.
- So trigger pages win through distribution (partners, communities, shares, AI answers), not search volume. Keep them indexed and watch Search Console impressions, but don't count on search for early volume.

```
ENGINE 1 ORGANIC          ENGINE 2 PAID (Meta)        ENGINE 3 SAUCE                 PARTNERS
search + AI pages (36)    3 campaigns / 6 ads         carrier exits + auto-move      CareerSource handouts
Reddit / FB groups /      special ad category,        WARN layoffs -> HEALTH (60d)   pharmacy counter cards
Nextdoor / LinkedIn /     creative = targeting,       + LIFE (31-day group-life      Spanish media
shorts (brand accounts)   lint-checked copy           conversion) from ONE signal
        \                       |                            |                         /
         \___ every placement / ad has its own tracked link (/go/<code> or utm+x) ____/
                                        |
             Check (health) / estimator (life) -> consent naming ONE agent -> validate, dedupe, score
                                        |
                       instant delivery to OUR agent -> agent marks accepted / returned / outcome
                                        |
     funnel by engine · channel · category · experiment · campaign (visits → leads → accepted → revenue - spend)
                                        |
             tools/engine_report.py: SCALE / FIX OFFER / FIX QUALITY / KILL / WATCH (REAL data only)
```

## Daily loop (Engine 4)
`python tools/daily_run.py [--warn warn.csv] [--spend meta_export.csv]` does six things. It never posts, sends or spends anything.
1. Turns WARN layoff notices into draft employer pages.
2. Imports ad spend.
3. Lints all copy against platform and insurance-advertising rules.
4. Refreshes the posting pack, tracked links and flyers.
5. Writes the decision report.
6. Writes `out/ops_alerts.md` — outstanding tasks read from recorded state only: stranded sales,
   exhausted webhook retries, negative real balances, stuck payments, unbilled deliveries, pending
   credit requests, and overdue prospect follow-ups. Repairs and decisions stay on the admin pages.

## Where things live
| What | File |
|---|---|
| Channel → engine rules, health/life split, decision thresholds | `config/engines.json` |
| Organic and partner placements, copy, rules, status | `config/distribution.json` (built by `research/build_distribution.py`) |
| Meta campaigns, ads, economics | `config/meta_campaigns.json` (built by `research/build_meta.py`) + `assets/ads/meta/` (creatives, `build_sheet.csv`) |
| Ready-to-post pack and short-form scripts | `out/organic_pack/` |
| Printables | `assets/print/handout_job_{en,es}.pdf`, `counter_jan_es.pdf` |
| Life pages fed by layoffs | `research/content_life.py` → `/learn/life-insurance-after-layoff-florida`, `/aprende/seguro-de-vida-despues-de-despido-florida` |
| Our agents | `config/buyers.json`: fill `agent-health-1` / `agent-life-1`, set `active: true` |

## Exact blockers (everything else is built)
1. **Live domain + hosting.** No real person can submit until the site is live (Render config is in the repo). Then run `BASE_URL=https://domain python tools/distribution_pack.py` and `python research/build_meta.py` so every link and QR points at the domain.
2. **Our agents' details.** For each agent we need:
   - the exact legal name shown in the consent
   - FL license number / NPN
   - products (health or life)
   - price per lead
   - delivery email

   Until then the form can't name a buyer, so it never asks for a phone number.
3. **Brand accounts** (we can't create them):
   - Facebook page
   - Instagram
   - TikTok
   - YouTube
   - Reddit
   - Nextdoor business
   - LinkedIn company page
4. **Approvals:**
   - Meta test: X022 $245, then X024 $175 and X025 $175.
   - Sending the CareerSource, pharmacy and media drafts.
   - Higgsfield credits for video versions of the short-form scripts.
5. **Before the first ad:**
   - Confirm Molina's exit when HealthCare.gov plans post Nov 1.
   - Check whether Meta classifies the ad account as Health & Wellness; if so, use the Landing Page Views objective.
