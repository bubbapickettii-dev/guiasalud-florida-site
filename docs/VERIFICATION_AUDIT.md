# Verification audit (as of Oct 5, 2026)

Every assumption the system relies on, the source checked, and what changed in the build.
Status: **VERIFIED** (primary source read) · **CORRECTED** (earlier build was wrong; fixed) · **SECONDARY** (only a secondary source could be read; re-verify) · **OPEN** (not verified; flagged in the build).
Not legal advice. Items marked for counsel are in GO_LIVE.md.

## Enrollment and money

| Claim used in the build | Source | Status | Where it lives |
|---|---|---|---|
| 2027 Open Enrollment runs Nov 1, 2026 to Jan 15, 2027. Enroll by Dec 15 for Jan 1 start; Dec 16 to Jan 15 starts Feb 1 | HealthCare.gov "Dates and deadlines" (healthcare.gov/quick-guide/dates-and-deadlines/) | VERIFIED | check.html, health.js result, content pages, guides, ads |
| Losing job-based coverage usually opens a 60-day Special Enrollment Period (Medicaid/CHIP loss: 90 days) | HealthCare.gov SEP pages | VERIFIED | health.js `lost` path, content |
| Voluntarily dropping COBRA does **not** open an SEP | HealthCare.gov COBRA page | VERIFIED | content "lost employer insurance" |
| 2027 subsidies use 2026 HHS poverty guidelines: $15,960 + $5,680 per extra person (2 = $21,640, 3 = $27,320, 4 = $33,000) | ASPE 2026 Poverty Guidelines | VERIFIED | config/health_flow.json facts, income bands, guide PDF table |
| Premium tax credit band 100% to 400% FPL (1 person: $15,960 to $63,840; family of 4: $33,000 to $132,000) | IRC 36B as it applies after enhanced credits expired | VERIFIED | income bands, ad OEP-EN-2 |
| Enhanced (no-cap) credits expired Dec 31, 2025; House passed a 3-year extension Jan 8, 2026; not enacted | NACo, ASTHO, PBS coverage (searched Oct 5, 2026: no enactment found) | SECONDARY - re-check weekly | Copy now says "under current law" / "unless Congress changes the law" |
| Florida has not expanded Medicaid; childless adults generally ineligible; below 100% FPL usually no Marketplace subsidy | KFF / state sources | VERIFIED | health.js `lt100` note + `coverage_gap_risk` flag (score -12, routes to Navigator mention) |
| Self-employed premium deduction | IRS Form 7206 instructions (HealthCare.gov's self-employed page doesn't cover it) | VERIFIED | content says "may be able to deduct... ask your tax professional" (no amounts) |
| 2027 rule: SEP pre-enrollment verification for most SEPs; extra income verification under 100% FPL; changes to APTC for some lawfully present immigrants | CMS 2027 Notice of Benefit and Payment Parameters final fact sheet | VERIFIED | agent alert email + lead page: "ask for SEP documents early". Form does **not** ask immigration status |

## Marketplace marketing and agent rules

| Claim | Source | Status | Change made |
|---|---|---|---|
| Agents must document consumer consent before Marketplace assistance: scope, purpose, duration, date, consumer name, agent name, how to rescind; keep 10 years | 45 CFR 155.220(j)(2)(iii) (eCFR) | VERIFIED | Agent desk, lead page, alert email, buyer kit now cite it |
| "Use the CMS model consent form" | HHS model form is **required starting plan year 2028** | **CORRECTED** | Old text in notify.py and buyer kit PDF replaced |
| Prohibited: cash/gift-card/rebate incentives, false "$0 plan" claims, government-logo look-alikes, misstating deadlines | 45 CFR 155.220(j)(3)(iii) / CMS marketing guidance | VERIFIED | Ad kit compliance column; no incentives anywhere; gov disclaimer EN+ES on every page |
| "CMS 2026 bans deceptive AI video" | Appeared in a **proposed** rule summary (DLA Piper); not in the CMS final fact sheet | **CORRECTED** | Now a house policy: no AI-generated people presented as real. channels.json, LAUNCH.md, ad kit updated |
| Medicare marketing (TPMO disclaimer, data-sharing consent naming each entity) is a separate regime | 42 CFR 422.2274(g) | VERIFIED | Medicare is **excluded**: need=medicare or age 65+ shows an exit screen pointing to Medicare.gov / 1-800-MEDICARE and saves nothing |

## Consent and contact

| Claim | Source | Status | Change made |
|---|---|---|---|
| Prior express written consent needed for automated/prerecorded calls & texts to mobiles for marketing; must be clear, signed (checkbox OK under E-SIGN), not a condition of purchase | 47 CFR 64.1200(f)(9), (a)(2) | VERIFIED | Consent text (EN+ES) |
| FCC one-to-one consent rule | Vacated, 11th Cir. Jan 24, 2025 | VERIFIED | Built to one-to-one anyway: one named agent in the consent, lead never resold |
| Revocation by any reasonable means; honor within 10 business days | 47 CFR 64.1200(a)(10) | VERIFIED | /optout + agent "Do not contact" button blocks phone+email system-wide immediately |
| Calls only 8am-9pm (federal) | 47 CFR 64.1200(c)(1) | VERIFIED | superseded by stricter FL rule below |
| Florida: telephone solicitation 8am-8pm, max 3 calls / 24h on the same subject | Fla. Stat. 501.616(6) | VERIFIED | **Added** to agent desk, lead page, alert email, buyer kit; "Best time" says 8am-8pm |
| Florida FTSA: automated-system calls/texts need prior express written consent incl. checkbox signature; STOP honored (15 days) | Fla. Stat. 501.059 | VERIFIED | Consent text + STOP language |
| Spanish ads need Spanish disclosures | FTC enforcement pattern ("disclosures in the language of the ad") | SECONDARY (enforcement guidance, not a statute) | Every ES page has ES disclosure, ES gov disclaimer, ES consent, /privacidad, /terminos |

## Florida licensing (who can say what)

| Claim | Source | Status | Change made |
|---|---|---|---|
| "Solicitation" includes describing a policy's benefits/terms, comparing products, recommending a policy | Fla. Stat. 626.112(1)(b) | VERIFIED | Content guardrail (Admin > Content). Partner guides **corrected**: removed "term life is usually the lowest-cost..." and "Most booth renters' incomes fall in the range..." |
| Unlicensed referral fees can't depend on a purchase | Fla. Stat. 626.112(8) | VERIFIED | Agent pricing is flat per delivered lead; partners are not paid per lead or sale (stated on partner stats page) |

## Ad platforms

| Claim | Source | Status | Change made |
|---|---|---|---|
| Meta: insurance = Financial products & services Special Ad Category: no age/gender narrowing, location >=15-mile radius (no ZIP), no lookalikes, reduced detailed targeting | jonloomer.com SAC guide. Meta's own help page could not be fetched | SECONDARY - confirm in Ads Manager when creating the account | **CORRECTED** from "state-level, 18-65+" in channels.json, ad kit, LAUNCH.md, regional briefs |
| Google: health-related personalized advertising (remarketing / customer match) restricted | Google Ads policy page "Health in personalized advertising" (answer 16701855) found but **not read** (fetch limit hit) | OPEN | Channel table marks Google remarketing as "assume not allowed until verified". Search ads unaffected |
| Meta pixel on health pages | Sends PageView only; Lead event fires without any form data; CAPI payload contains no health fields (unit-tested) | VERIFIED in code (TEST DATA) | - |

## Data use

- Public-record DB (75,608 prospects) is used for aggregate counts only: regional briefs, partner discovery, lead enrichment after a consumer submits. Nobody in it is contacted. It is **not** deployed to the server (INTEL_DB empty in production; briefs ship as aggregate JSON).
- The form never asks for health conditions, SSN, immigration status, or exact income (bands only).
- Partners receive counts only (unit-tested: no names, phones or emails on the partner page).
