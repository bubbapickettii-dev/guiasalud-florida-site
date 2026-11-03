# Discovery Report: where the next high-intent Florida health-insurance consumers are hiding

As of October 8, 2026 (research Oct 5, verification pass Oct 8). Nothing here has been validated by real consumers yet. There is no live domain, no real traffic and no real leads. Every opportunity below is at most **READY TO TEST**.

Evidence labels:

- **FACT**: read from the source linked.
- **INFERENCE**: my reasoning from facts.
- **UNKNOWN**: I could not find or verify it.

## The answer in one paragraph

The best untapped pool is the people the Marketplace is about to move to an insurance company they didn't choose.

- **The exits.** Cigna (two entities), Molina and Sunshine State Health Plan are leaving Florida's individual market for 2027.
- **What happens to members who do nothing.** Federal rules ([45 CFR 155.335(j)(3)](https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-B/part-155/subpart-E/section-155.335)) let the Exchange re-enroll them with a *different* issuer. HealthCare.gov says it outright: *"If your company isn't offering plans for next year, we'll re-enroll you in a plan with a different insurance company"* ([source](https://www.healthcare.gov/keep-or-change-plan/)).
- **The size.** Cigna alone had roughly **97,000 Florida enrollees** ([ACASignups, Apr 30 2026](https://acasignups.net/node/9836); estimate from 2025 filings).
- **Why nobody can see it.** Florida's trade-secret law hides per-carrier enrollment, so no competitor can size this pool either ([ACASignups FL 2027, updated Sep 24](https://acasignups.net/rate_changes/2027/fl)).

**Two findings from the Oct 8 verification pass:**

- **The January window is likely longer than Jan 15.** HealthCare.gov lists "your Marketplace plan is discontinued" as a reason for a Special Enrollment Period ([source](https://www.healthcare.gov/coverage-outside-open-enrollment/special-enrollment-period/)). For a plan that ended Dec 31, that normally means 60 days after, to about **Mar 1**. Whether it applies to someone who was already auto-moved is case-by-case (UNVERIFIED), so the pages say "act before Jan 15" and mention the possible extension carefully.
- **Off-exchange members aren't moved at all.** People who bought Cigna or Molina directly from the insurer (not through HealthCare.gov) get no automatic move: their plan simply ends Dec 31 ([agent-facing source, Sep 17](https://premierhsllc.com/aca-insurer-exits-2027-what-agents-should-tell-clients/); consistent with how the rule works, since an Exchange can only re-enroll its own enrollees). This is the one subgroup that truly goes uninsured on Jan 1. No competitor page separates them. It is now **HID-15** (rank #3), and the Check asks "Where did you buy this plan?" only when the plan is ending.

This pool has three moments in a row:

1. **The letter (Oct–Nov).**
2. **The Dec 15 deadline.**
3. **The January network shock (Jan 1–15).** This is the moment nobody serves: people find out at the doctor or pharmacy that their new plan doesn't take their doctor. Open Enrollment is still open, so they can still switch.

What I checked on Oct 5:

- **The carriers say nothing.** Cigna's own Spanish Florida page still sells plans with no exit notice. Molina's page still lists Florida.
- **The Spanish agency pages are thin or wrong.** They stop at Dec 15, don't explain auto-placement, and one tells readers auto-renewal ends in 2027 (wrong: it starts with plan year 2028).
- **No lead seller shows up.** I found none publishing for this moment.

The second engine runs all year: public WARN layoff notices.

- **The pages.** Employer-specific pages for each notice.
- **Closures mean no COBRA.** When the whole employer shuts down, its plan ends too, so a "closure" notice means Marketplace now.
- **A predictable COBRA calendar.** Florida's spring-2025 WARN wave (322 notices, 20,423 workers in 2025; April alone 2,980) hits its 18-month COBRA limit between now and January.

**The constraint isn't discovery any more. It's the calendar.** The letters are arriving now. Every week without a live domain and a named licensed agency burns the best window of the year.

---

## 1. Ranked opportunities (13-criterion score)

Each criterion is scored 1–5, where 5 is best. For competition, 5 means low competition; for cost, 5 means free.

Weights:

- ×3: intent, phone potential.
- ×2: freshness, audience quality, reach, cost, competition, conversion, speed, free-resource leverage.
- ×1: scalability, defensibility, AI leverage.

The scores are research judgment, not evidence. The full database (26 entries) is in `config/opportunities.json` and `docs/OPPORTUNITIES.md`.

HID-15 (off-exchange members whose plan just ends, scored 88.8) was added on Oct 8 and now ranks #3. The rows below keep their Oct 5 order.

| # | ID | Opportunity | Int | Fresh | Aud | Reach | Cost | Comp | Phone | Conv | Speed | Scale | Def | AI | Free | **Score** |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | OPP-01 | Carrier-exit + renewal letters (Oct–Dec 15) | 5 | 5 | 4 | 4 | 5 | 4 | 5 | 5 | 5 | 3 | 3 | 5 | 5 | **92.0** |
| 2 | OPP-01B | **January network shock** (auto-moved members, Jan 1–15) | 5 | 5 | 5 | 3 | 5 | 5 | 5 | 5 | 3 | 3 | 4 | 4 | 5 | **90.4** |
| 3 | HID-08 | **COBRA 18-month exhaustion wave** | 5 | 4 | 5 | 2 | 5 | 4 | 5 | 5 | 4 | 3 | 4 | 4 | 5 | **87.2** |
| 4 | HID-09 | **Employer shutdowns = no COBRA** | 5 | 5 | 4 | 2 | 5 | 4 | 5 | 5 | 5 | 2 | 3 | 4 | 5 | **87.2** |
| 5 | OPP-02 | WARN employer-specific layoff pages | 5 | 5 | 4 | 3 | 5 | 4 | 4 | 4 | 5 | 4 | 4 | 4 | 5 | **85.6** |
| 6 | HID-12 | **Carrier-silence gap** (carriers' own pages show no exit) | 4 | 5 | 4 | 3 | 5 | 5 | 4 | 4 | 5 | 3 | 3 | 5 | 5 | **85.6** |
| 7 | HID-10 | **Hialeah warehouse layoff cluster (Spanish)** | 5 | 5 | 4 | 2 | 5 | 5 | 4 | 4 | 5 | 2 | 3 | 4 | 5 | **84.8** |
| 8 | HID-03 | Florida Blue PPO renewal shock (~+26% filed) | — | — | — | — | — | — | — | — | — | — | — | — | — | **84.0** |
| 9 | HID-01 | "Is enrollment still open?" → merged into #2 | — | — | — | — | — | — | — | — | — | — | — | — | — | 84.0 |
| 10 | OPP-08 | Free bilingual "What changed for 2027" webinars | — | — | — | — | — | — | — | — | — | — | — | — | — | 80.8 |
| 11 | OPP-05 | New Florida residents | — | — | — | — | — | — | — | — | — | — | — | — | — | 77.6 |
| 12 | OPP-11 | Click-to-WhatsApp (Spanish) | — | — | — | — | — | — | — | — | — | — | — | — | — | 76.0 |
| 13 | HID-02 | AI-answer citation capture | — | — | — | — | — | — | — | — | — | — | — | — | — | 76.0 |
| 14 | OPP-07 | December grads losing student plans | — | — | — | — | — | — | — | — | — | — | — | — | — | 73.6 |
| 15 | HID-11 | Defense/space contract-turnover layoffs | 4 | 4 | 4 | 2 | 5 | 5 | 3 | 3 | 4 | 2 | 3 | 3 | 4 | 72.8 |
| 16 | OPP-09 | Tax season 2027: uncapped subsidy repayment | — | — | — | — | — | — | — | — | — | — | — | — | — | 70.4 |
| 17 | HID-13 | Unemployment claimants (income-counting insight) | 4 | 4 | 3 | 3 | 5 | 3 | 3 | 3 | 4 | 4 | 2 | 3 | 4 | 70.4 |
| 18 | HID-14 | Postpartum Medicaid cliff (**FL rule unverified**) | 4 | 3 | 3 | 2 | 4 | 5 | 3 | 3 | 2 | 3 | 3 | 3 | 4 | 65.6 |

Rows marked "—" were scored in the earlier wave; their full scores are in OPPORTUNITIES.md. Lower-ranked entries are there too: RBTs, nurse-registry caregivers, new realtors, early retirees, divorce, disaster SEP, the re-entry group, and the excluded immigration-related one.

Required subsets:

- **Hidden / non-obvious (9, including HID-15 added Oct 8):**
  - OPP-01B January shock
  - HID-08 COBRA exhaustion
  - HID-09 closures
  - HID-12 carrier silence
  - HID-10 Hialeah cluster
  - HID-11 defense turnover
  - HID-13 unemployment-income insight
  - HID-14 postpartum
- **Attackable for $0 (6):**
  - OPP-01 / OPP-01B / HID-12: pages, WhatsApp cards, Bubba's IG, AI citations
  - OPP-02 / HID-09 / HID-10: employer pages that coworkers share
  - HID-08: COBRA pages linked from every layoff page
- **Public data gives an unfair advantage (4):**
  1. **SERFF rate filings.** Read through ACASignups, they show which carriers exit and the rate changes before carriers publish anything consumer-facing.
  2. **WARN notices.** They give employer, city, worker count, effective date and closure vs layoff, so the 60-day window can be computed per employer.
  3. **WARN + 18 months.** This gives the COBRA-exhaustion calendar.
  4. **DOL weekly claims.** Florida had 4,955 initial claims in the week ending Sep 26 and 31,953 continuing claims ([DOL, Oct 1](https://www.dol.gov/ui/data.pdf)). These size the job-loss stream.

  All four are company-level or aggregate data. **No individual is ever identified or contacted.**
- **Repeatable automated systems (4):**
  1. **WARN watcher** (`tools/warn_watch.py`, built): it drafts employer pages from a WARN CSV.
  2. **COBRA calendar:** the same WARN feed, read 18 months later.
  3. **Date-driven Open Enrollment page:** its answer changes by itself on Nov 1, Dec 16 and Jan 16.
  4. **Annual carrier-exit cycle:** the same pages and cards, reused every August–January in any HealthCare.gov state where a carrier exits.

---

## 2. The deep dive: carrier exits (OPP-01 + OPP-01B + HID-12)

**Who is affected.**

- **Which carriers.** For 2027, these are exiting Florida's individual market:
  - Cigna Health & Life Insurance Co.
  - Cigna HMO
  - Molina Healthcare of Florida
  - Sunshine State Health Plan (its enrollees are expected to move to Ambetter, part of the same Centene family).

  Sources: [ACASignups, updated Sep 24, preliminary](https://acasignups.net/rate_changes/2027/fl); [healthinsurance.org, Aug 3](https://www.healthinsurance.org/states/florida/). **FACT.**
- **Cigna.** Cigna offered Florida plans in 11 counties. It had about 97,000 Florida enrollees (estimate from 2025 filings, possibly outdated). Nationally it is leaving about 369,000 members in 11 states ([ACASignups](https://acasignups.net/node/9836)). **FACT (estimate).**
- **Molina.** Molina's exit is listed by ACASignups. healthinsurance.org called it "unclear" on Aug 3, Molina's own page still lists Florida, and Molina's Q2 call said ACA membership would be concentrated in 6 states, down from 14 ([Healthcare Dive, Jul 23](https://www.healthcaredive.com/news/molina-plans-aca-cuts-q2-2026-moh/825934/)). **Treat as likely; confirm from a member letter or the HealthCare.gov plan list on Nov 1.**
- **Sunshine.** Sunshine's enrollment count is **UNKNOWN**: Florida's trade-secret rules redact it.
- **Market size.** 4,538,772 Floridians selected Marketplace plans for 2026. That makes Florida the largest Marketplace in the country, with plenty of room even at 2–5% exposure.

**What exactly happens to them.**

1. Their plan ends Dec 31, 2026.
2. If they pick nothing, HealthCare.gov re-enrolls them in a plan from a *different* company ([HealthCare.gov](https://www.healthcare.gov/keep-or-change-plan/); [45 CFR 155.335(j)(3)](https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-B/part-155/subpart-E/section-155.335)).
3. They keep coverage, but their doctors and drugs may not be in that plan's network (INFERENCE, but this is how networks work).
4. The auto-move picks the same metal level and product type, the most similar network, and the lowest premium. It does not check prescriptions ([secondary source, Sep 17](https://premierhsllc.com/aca-insurer-exits-2027-what-agents-should-tell-clients/)).
5. Open Enrollment runs Nov 1–Jan 15. A choice by Dec 15 starts Jan 1; a choice by Jan 15 starts Feb 1.
6. After Jan 15: HealthCare.gov lists a discontinued Marketplace plan as a Special Enrollment reason, normally lasting 60 days after the plan ended (about Mar 1). **I corrected my Oct 5 claim that Jan 15 was the last exit.** Whether that window applies to someone already auto-moved is UNVERIFIED. The pages tell people to act by Jan 15 and say the later window "may" apply.
7. **Off-exchange members (bought directly from the insurer) are not moved.** Their plan ends Dec 31, and losing it opens the 60-day window (before or after).

**Exact questions they ask** (English / Spanish):

- "Got a letter that my plan ends Dec 31 — what do I do?" / "Me llegó una carta que mi plan termina."
- "Will I lose my insurance on January 1?"
- "Is Molina leaving Florida?" / "¿Cigna se va de Florida?"
- "Will I keep my doctor?"
- "Who picked this new plan for me?"
- "Can I still change? I thought the deadline was Dec 15."
- "Sunshine is now Ambetter. Is it the same?"

**What consumers get wrong** (each one is a content hook):

1. They think doing nothing keeps them on their plan. It moves them to a company they didn't pick.
2. They think they'll be uninsured Jan 1 and panic-buy the first plan an agent shows them. They actually have time to compare until Dec 15, and a second chance until Jan 15.
3. They think Dec 15 is the last day. Jan 15 still works, for a Feb 1 start.
4. They think "same parent company" means the same doctors (Sunshine → Ambetter). Networks can differ (INFERENCE; check per plan).
5. They believe auto-renewal ends in 2027. A Spanish agency page says so; it's wrong. Pre-enrollment verification under OBBBA §71303 starts with plan year 2028.
6. They expect the old subsidy. The enhanced tax credits expired, the 400% FPL cliff is back ($63,840 for one person, $132,000 for a family of four), and repayment is uncapped starting with tax year 2026.

**Where their attention is:**

- The letter itself.
- Google and AI-assistant answers.
- WhatsApp family groups. In Miami, coverage questions go to the family member "who knows."
- Spanish TV and digital (Telemundo 51, Univision 23, DirectorioCubano).
- Doctors' front desks and pharmacy counters in January.

**What competitors publish (checked Oct 5):**

| Source | What it says | Gap |
|---|---|---|
| tuseguromedico.com (updated Sep 18) | General 2027 changes | Doesn't name the exiting carriers. Says nothing about auto-placement. Wrongly claims auto-renewal ends in 2027. |
| Lavita (May 2) | Members "tendrán que escoger" (will have to choose) | No deadlines, no January scenario. |
| Cigna's Spanish Florida page | Still sells Florida plans | **No exit notice.** |
| Molina's individual-plans page | Lists Florida | No exit notice. |
| English results for "Cigna leaving Florida 2027 what to do" | ACASignups, KFF, healthinsurance.org, NBC Miami | National news. No consumer "what to do this week" page, no Florida January guidance, no lead seller visible in that snapshot. |

**The better resource** (built: 5 ES + 5 EN carrier pages, plus EN/ES January pages):

- One dated page per carrier, each saying "Reviewed <date>" and citing HealthCare.gov and the federal regulation.
- What happens if you do nothing, in one sentence, quoting HealthCare.gov.
- Three dates: Dec 15 (Jan 1 start), Jan 15 (Feb 1 start) and "after Jan 15, stuck".
- "Check your doctors and medicines first" as the action.
- A WhatsApp share button carrying the experiment id.
- The 60-second Check, whose "plan ending" result now says: *"If you do nothing, HealthCare.gov says it will move you to a plan from a different insurance company…"*

**Why ours beats a broker's page.** A broker page sells a consultation. Ours answers the question first, with the deadline and the auto-move fact, in Spanish, per carrier, with sources. It hands off to a licensed agent only when the person asks for help. Competitors either don't know about the auto-move or don't say it.

**$0 distribution:**

1. Organic search and AI-answer citations (dated, sourced pages are what AI assistants cite).
2. WhatsApp share cards in Spanish.
3. Bubba's IG story: X013, now redirected to this.
4. Pitching the "you'll be moved to a company you didn't choose" angle to Spanish local TV and digital outlets as a public-service story. This is earned media and needs your OK.
5. In January: pharmacy and front-desk counter cards. B2B, needs approval.

**Why the big lead companies aren't dominating it** (INFERENCE):

- Their model buys high-volume generic keywords and resells volume. Carrier-by-carrier, Spanish, date-specific pages are small and perishable, and need editorial accuracy.
- They can't size the pool: Florida's trade-secret law hides per-carrier enrollment.
- In January their buyers are swamped and they are already selling Open Enrollment volume.
- Insurer-specific claims are a compliance risk for call-center scripts.

---

## 3. Deep dive: job loss (OPP-02 + HID-08 + HID-09 + HID-10 + HID-11 + HID-13)

**The signals:**

- **WARN notices.**
  - Florida 2026 year to date: 242 filings, 89 companies, 19,252 workers. The May peak was 6,209; Orlando had 2,977 workers across 24 filings ([layofftracker FL 2026](https://www.layofftracker.us/layoffs/state/florida/2026)).
  - Fresh events with effective dates around now:

    | Employer | Location | Workers | Effective |
    |---|---|---|---|
    | Saddle Creek | Plant City | 178 | Oct 15 |
    | Nearly Natural | Hialeah | 60 | Oct 12 |
    | Temco Logistics | Hialeah | 60 | Oct 3 |
    | West Marine | Fort Lauderdale | 80 | Oct 3 |
    | COLSA | Eglin | 97 | Sep 30 |
    | MV Transportation | Naples | 151 | Sep 30 |
- **DOL weekly claims.** 4,955 new Florida claims in the week ending Sep 26 and 31,953 continuing ([DOL](https://www.dol.gov/ui/data.pdf)). WARN covers only the big events; claims show the full stream.
- **2025 WARN.** 322 filings, 20,423 workers; April 2025 = 2,980 ([layofftracker FL 2025](https://www.layofftracker.us/layoffs/state/florida/2025)). Their 18-month COBRA limit lands **Oct 2026–Jan 2027**.

**The rules** ([45 CFR 155.420](https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-B/part-155/subpart-E/section-155.420)):

- Losing job-based coverage opens 60 days to enroll, before or after the loss.
- Running out of COBRA (18 months) counts as a loss.
- Dropping COBRA or not paying for it does NOT count, which is a common mistake.
- Losing Medicaid or CHIP gives 90 days.

**What they get wrong:**

1. They think COBRA is the only option.
2. They let COBRA lapse by not paying, believing it opens a window. It doesn't.
3. They wait until COBRA ends instead of enrolling up to 60 days before.
4. They report their weekly unemployment check as income. The Marketplace uses expected *annual* income, which includes pre-layoff wages. In a non-expansion state like Florida, under-reporting can drop them below 100% FPL, where there's no tax credit.
5. They expect COBRA when the employer closes entirely. There usually isn't any.

**Attention surfaces:**

- Searches for their own employer: "<company> layoffs".
- Coworker WhatsApp, Facebook and Blind threads.
- Local news.
- CareerSource rapid-response sessions.

**What competitors do.** For company-specific layoff searches I found news, Blind and trackers but no insurance resource. The exception was Spirit, a mega-event where national affiliates showed up. For "COBRA ending what to do", I found KFF FAQs from the 2021 subsidy era, generic national pages and a divorce site. There is no Florida-specific resource timed to COBRA running out.

**Our resource (built):**

- A hub page plus 13 employer pages. Each page computes the 60-day window from the effective date and shows a **closure → no-COBRA** warning when the employer is shutting down.
- EN/ES "COBRA ending after 18 months" pages.
- A Spanish companion page.
- A WARN watcher that drafts new pages.

**Why the big lead companies aren't dominating it** (INFERENCE): a page per 60-person layoff in Hialeah makes no sense for a volume buyer, and nobody times content to COBRA running out because it needs WARN data from 18 months back.

---

## 4. The 17 answers per opportunity (top 10)

| | **OPP-01B · January network shock** | **OPP-01 · Carrier-exit letter (Oct–Dec 15)** |
|---|---|---|
| 1. Exact person | A Cigna/Molina/Sunshine Marketplace member who didn't choose a 2027 plan and was auto-moved to another company | The same member, holding the discontinuation letter; also Florida Blue PPO members with a big renewal increase |
| 2. Event | First doctor visit or pharmacy fill on an unfamiliar plan, Jan 2–15 | Letter arrives (Oct–Nov) |
| 3. Public signal | Exits in SERFF via ACASignups; HealthCare.gov crosswalk rule | Same, plus the rate filings (avg +15.9% proposed) |
| 4. Freshness | The window opens Jan 1, 2027 | Live now |
| 5. Exposure size | Cigna ~97k FL (est.); Molina/Sunshine UNKNOWN; share who don't actively choose: UNKNOWN | Same, plus Florida Blue PPO members (count UNKNOWN) |
| 6. Where attention is | Front desks, pharmacies, WhatsApp, Google ("me cambiaron de seguro") | Google/AI answers, WhatsApp, Spanish TV |
| 7. Exact question | "Can I still change? Who put me in this plan?" | "My plan ends — what do I do and by when?" |
| 8. Resource | "Moved to a plan you didn't choose? You can still pick your own until Jan 15" (EN/ES, built) | Per-carrier ES/EN pages (built) |
| 9. Why ours vs a broker | Answers "can I still switch" with a date; no broker publishes for January | Names the carrier, explains the auto-move, cites the source |
| 10. Path to the Check | The CTA on the page leads into the plan_change Check | Same |
| 11. Real phone | The person types their own number on the phone step and gives consent naming one licensed agency | Same |
| 12. Why high intent | Concrete problem plus a 2-week clock | Must act by Dec 15 or be moved |
| 13. $0 reach | Pages already indexed by then, WhatsApp card, IG story Jan 2–14 | Search/AI, WhatsApp cards, IG story, earned media |
| 14. First $25/$50 test | **$25**: 250 counter cards ("¿Te cambiaron de seguro? Revisa antes del 15 de enero" + QR → /go/x021?c=counter) left at 5 independent pharmacies in Hialeah/Little Havana with the owner's OK (B2B, needs approval) | **$50**: Meta boost of the ES carrier-exit card in Miami-Dade, run under the Financial products special ad category (15-mile minimum radius, no age targeting), 5 days, link /go/x016?c=meta. Needs your approval |
| 15. Why the majors aren't dominating | They're saturated selling Open Enrollment volume; they can't size the pool; it's a 2-week perishable window | Insurer-specific, Spanish, small, perishable; trade-secret blackout |
| 16. Automation | Date-driven page; the same assets run every January | Annual carrier-exit cycle: reuse the templates |
| 17. Multi-state / niche | Every Exchange can crosswalk (same rule). Cigna is also exiting HealthCare.gov states AZ, IN, MS, NC, TN, TX (INFERENCE: confirm each state's platform) | Same |

| | **HID-08 · COBRA exhaustion** | **HID-09 · Closure = no COBRA** |
|---|---|---|
| 1 | A worker laid off around spring 2025 who has been paying full COBRA for 18 months | A worker at an employer that is shutting down entirely |
| 2 | COBRA ends | The plan ends with the company |
| 3 | 2025 WARN filings + 18 months | WARN "closure" vs "layoff" (closure flag in layoffs.json) |
| 4 | Oct 2026–Jan 2027 wave | Per event (live now) |
| 5 | 20,423 workers in 2025 WARN; share who chose COBRA UNKNOWN | Per event (Spirit was thousands) |
| 6 | COBRA administrator letters, Google, early-retirement forums | Employee Facebook/WhatsApp groups, local news |
| 7 | "COBRA is ending — what now? Can I sign up before it ends?" | "Do I get COBRA if the company closed?" |
| 8 | EN/ES "COBRA ending after 18 months" (built) | Closure block on the layoff page (built) |
| 9 | Florida-specific, explains the 60-days-before rule | The only page about *their* employer that says "Marketplace now" |
| 10 | CTA leads into the Check (life_change) | Same |
| 11 | Consumer enters their own number, with consent | Same |
| 12 | Hard end date; used to paying high premiums | Coverage already gone |
| 13 | Linked from every layoff page; AI citations; WhatsApp | Coworkers share the page link |
| 14 | $0 (no paid test justified until organic data exists) | $0 |
| 15 | Needs WARN data from 18 months back; nobody builds that calendar | Too small per event for volume buyers (except mega-events) |
| 16 | WARN feed + 18 months = automatic calendar | WARN watcher flags closures |
| 17 | Every state publishes WARN notices | Same |

| | **OPP-02 · WARN employer pages** | **HID-10 · Hialeah cluster (ES)** | **HID-12 · Carrier silence** |
|---|---|---|---|
| 1 | Laid-off worker at a named Florida employer | Spanish-speaking hourly worker at Nearly Natural or Temco in Hialeah | Someone researching Cigna or Molina directly |
| 2 | Layoff effective date | Layoffs Oct 3 and Oct 12 | Letter, or shopping |
| 3 | WARN notice | WARN | Carrier pages with no exit notice (checked Oct 5) |
| 4 | Continuous | Now | Now |
| 5 | 19,252 workers in 2026 WARN; ~5k new claims/week statewide | 120 workers (just these two) | Part of the carrier-exit pool |
| 6 | "<company> layoffs" search, Blind | WhatsApp coworker groups, Hialeah Facebook groups | Google, AI assistants |
| 7 | "What happens to my health insurance?" | "¿Qué hago con el seguro?" | "¿Cigna sigue en Florida?" |
| 8 | Employer page with the 60-day date (built) | ES employer page (built via template) | Dated ES pages with sources (built) |
| 9 | About their employer and their deadline | Their language, their employer | The only correct Spanish answer |
| 10 | CTA leads into the Check | Same | Same |
| 11 | Own number, with consent | Same | Same |
| 12 | A 60-day clock is running | Same | A deadline |
| 13 | Coworker sharing, search | WhatsApp share button | Search/AI |
| 14 | $0 | $0 | $0 |
| 15 | Long tail, perishable | Tiny per event | Requires accuracy and dating |
| 16 | WARN watcher (built) | Same | Annual |
| 17 | All states | Any Spanish metro | All exit states |

| | **HID-03 · Florida Blue PPO renewal** | **HID-11 · Defense/space turnover** | **HID-13 · Unemployment income** |
|---|---|---|---|
| 1 | Florida Blue PPO member whose renewal shows ~+26% (filed) | Contractor employee at Eglin, Patrick or KSC | New jobless claimant |
| 2 | Renewal notice | Contract ends | Files a claim |
| 3 | Rate filing | WARN (COLSA 97, Astrion 220) | DOL weekly claims |
| 4 | Oct–Dec | Sep 30–Dec | Weekly |
| 5 | UNKNOWN (largest Florida carrier) | ~300+ | ~5k/week |
| 6 | Google, the renewal letter | Clearance-job forums, base groups | Google, Facebook |
| 7 | "Why is my premium going up 26%?" | "How do I cover the gap?" | "Do I qualify on unemployment?" |
| 8 | Florida Blue renewal page (built) | Employer pages | Income guidance in the Check and copy |
| 9 | Explains the HMO vs PPO filing difference | Hyperlocal | Prevents the below-100% FPL mistake |
| 10–11 | Check, then own phone number with consent | Same | Same |
| 12 | Price shock, must act by Dec 15 | Gap | Medium |
| 13 | Search/AI | Search | Content |
| 14 | $0 | $0 | $0 |
| 15 | Carrier-specific | Tiny | — |
| 16 | Annual | WARN watcher | — |
| 17 | Any state with rate shock | Defense hubs | All states |

---

## 5. Full chain for the chosen combination (with economics)

**PUBLIC SIGNAL → AUDIENCE → ATTENTION SURFACE → MESSAGE/RESOURCE → CLICK → CHECK → PHONE → CONSENT → QUALIFICATION → AGENT DELIVERY → OUTCOME → ECONOMICS**

**A. Carrier-exit spine: OPP-01 → OPP-01B (X016/X017/X013 now, X021 in January)**

1. **Public signal:** SERFF exits and rate filings.
2. **Audience:** Cigna/Molina/Sunshine members, plus Florida Blue PPO members.
3. **Attention surface:** the letter → Google/AI, WhatsApp, Bubba's IG.
4. **Message/resource:** "If you do nothing you'll be moved to a company you didn't pick."
5. **Click:** carrier page → **Check** (plan_change path: insurer + letter type → result with dates) → **phone step** (copy A/B) → **consent** naming one agency.
6. **Qualification:**
   - `plan_ending` = urgency HIGH.
   - `renewal_increase` = MEDIUM.
   - Florida ZIP; phone validated (NANP rules plus Florida area-code flag).
7. **Agent delivery:** the agent desk gets phone provenance (CONSUMER_SUBMITTED), consent timestamp and version, and the Florida calling rules (8am–8pm, max 3 calls per 24 hours).
8. **Outcome:** the agent marks contacted / appointment / sold, which feeds the experiment metrics.
9. **Economics:**
   - Market prices: exclusive health leads ~$22–80, live transfers ~$32–100 (earlier research).
   - What an agent earns: ~$20–30 per member per month. A 1.5-member household kept 12 months ≈ **$360–540 per sale (INFERENCE)**.
   - At a $50 lead price, the agent breaks even at a 1-in-7 to 1-in-11 close rate (INFERENCE from those numbers).
   - Our cost: $0 organic. The $50 Meta test breaks even at **1 delivered lead at $50** (or 2 at $25).
   - No conversion rates are assumed. The test is designed to *measure* them.

**B. Job-loss calendar (X018: WARN pages + closures + COBRA exhaustion), year-round**

1. **Public signal:** WARN notice, or WARN plus 18 months.
2. **Audience:** workers at that employer.
3. **Attention surface:** "<company> layoffs" search → coworker group.
4. **Message/resource:** "Your 60-day window ends <date>." Closure version: "no COBRA, Marketplace now."
5. **Click:** Check (life_change) → own phone number → consent.
6. **Qualification:**
   - SEP likely.
   - Income guidance uses annual income.
   - Under 100% FPL is flagged (no tax credit in Florida).
7. **Agent delivery:** same desk, same rules.
8. **Outcome:** same feedback loop as A.
9. **Economics:**
   - Special-enrollment leads arrive outside Open Enrollment, when agents have capacity. **Lead value is likely at the high end because volume is scarce (INFERENCE).**
   - Cost: $0.

**Kill rules (written before the data, so the data can't argue).** Thresholds are on REAL traffic only:

- **X016/X017:** after 300 real visits or 21 days, kill if phone-step reach is under 1% of visitors, or if there are 0 consented phone leads.
- **X021:** kill on Jan 10 if under 1% reach the phone step. Primary read Jan 15; secondary read Mar 1 (possible discontinuation window).
- **X018:** after 60 days, kill employer pages that get under 20 real visits; keep the hub if it produces leads.
- **PROVEN** requires at least 3 real consented phone leads AND at least 1 agent-confirmed contact. Nothing else counts.

---

## 6. Decisions: what I killed, redirected and built

Applied idempotently at startup from `config/experiment_decisions.json`:

| Test | Decision | Why |
|---|---|---|
| X014 AI-presenter Spanish shorts | **KILLED** (pre-launch, no data) | A generic 1099 audience with no trigger, competing with every agency's Reels |
| X015 Google "self-employed/Obamacare 2027" | **KILLED** (no spend) | The most contested auction in the category; paid money goes to a trigger audience instead |
| X013 Bubba's IG (1099 trainers) | **REDIRECTED** to the carrier-exit card (/go/x013 → EN hub, plan_change) | Same reach, aimed at a dated trigger; the channel is still measured separately |
| X020 "still open?" page | **MERGED** into X021 | A date fact alone is weak; paired with the January network shock it's the strongest January test |
| X018 job loss | **EXPANDED** with the COBRA-exhaustion pages | The COBRA calendar is the same audience 18 months later |
| X021 January network shock | **NEW** (Dec 16–Mar 1) | Hidden pool #1 |

What was built, kept to what the research required:

- **Check result.** For exiting carriers it now says HealthCare.gov will move you to a different company (EN/ES).
- **Conditional question (Oct 8).** "Where did you buy this plan?" appears only when the plan is ending. "Directly from the insurer" leads to a "nobody moves you, your plan just ends" result, the flag `plan_ending_offexchange` and urgency HIGH.
- **Carrier pages, extra paragraphs (Oct 8).** An off-exchange paragraph on all 8 carrier-exit pages, and a "missed Jan 15?" paragraph on the January pages.
- **Carrier pages.** All 10 rewritten around the auto-move quote.
- **New pages.** January "moved to a new plan" (EN/ES) and "COBRA ending after 18 months" (EN/ES).
- **Layoff events.** 6 new WARN events (13 total), plus the closure → no-COBRA warning.
- **Opportunity database.** Rebuilt: 26 entries, 8 new, re-ranked.

Verification: all on a fresh database, all TEST DATA, zero real events.

| Suite | Result |
|---|---|
| Unit tests | 35/35 |
| Check E2E | 8 personas |
| Acquisition E2E | 24/24 |
| Wave-2 E2E | 18/18 (adds the off-exchange branch) |

---

## 7. What I could not verify, and what blocks the test

**Unverified:**

- **Molina's exit.** Still unconfirmed on Oct 8. ACASignups (Sep 24) lists it. Becker's (updated Oct 6) and an agent-facing guide (Sep 17) say Molina is cutting from 14 to 6 states without naming Florida. Molina's own page still lists Florida. Confirm on Nov 1 with HealthCare.gov window shopping. Until then, the Molina pages are the weakest-sourced pages in the set.
- **Per-carrier Florida enrollment.** Hidden by trade-secret law. The CMS risk-adjustment report I could reach gives transfer dollars, not member counts.
- **Florida's postpartum Medicaid extension.** HHS approved 12 months in May 2022 ([WUSF](https://health.wusf.usf.edu/health-news-florida/2022-05-26/feds-sign-off-on-extended-florida-postpartum-care-through-medicaid)). Whether it is operating today wasn't re-verified.
- **Whether the discontinuation SEP applies after an auto-move.** Ask HealthCare.gov or a licensed agent before any copy promises it.
- **Search volumes.** I don't have Keyword Planner access. Check Google Trends manually for "Cigna Florida", "Molina Florida", "me cambiaron de seguro".
- **Reddit/Facebook group signals.** Blocked from this sandbox.

**Blockers to test anything with real people** (all yours):

1. **Domain + hosting.** The app is deploy-ready (Dockerfile / render.yaml).
2. **A named licensed agency** for consent and delivery. Without one, the phone step doesn't show, by design.
3. **LLC/contact info** for the site footer.
4. **Approval for:** the $50 Meta boost, the $25 pharmacy counter cards, and B2B asks to CareerSource and Spanish media.

**Timing:** letters are landing now. The Marketplace notice deadline is Nov 1, and Open Enrollment opens Nov 1. If the site isn't live by about Oct 25, OPP-01 loses its best two weeks. OPP-01B (January) and the job-loss calendar still work later.
