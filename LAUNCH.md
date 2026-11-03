# Experiment board (status as of Oct 4, 2026)

> **Superseded by v3 docs (Oct 5):** `docs/GO_LIVE.md` (one-real-visitor dependency map + cheapest path to first lead), `docs/CHANNELS.md` (30 channels), `docs/VERIFICATION_AUDIT.md` (sources + corrections). Live experiment tracking now happens in Admin > Distribution Lab (X001-X012). Kept below for history.


**Bottleneck:** nobody can see the system yet. It isn't deployed, it has no domain and no real buyer, and no traffic source is switched on. Every route to the first real visitor crosses an approval line (publishing, spending, contacting). Everything up to that line is built.
**Timing:** 2027 Open Enrollment opens **Nov 1** (28 days away). Enroll by Dec 15 for Jan 1 coverage, closes Jan 15. That's the main window for health leads, so the first experiment is health, not life.

| # | Experiment | Status | Blocked by (your action) | Cost | Read after | Iterate / kill rule (starting assumptions, UNTESTED) |
|---|---|---|---|---|---|---|
| E1 | **Agent-funded OEP funnel**: Meta (Special Ad Category; >=15-mile city radius or FL) → 2027 Health Coverage Check, EN + ES, 5 landing variants, 6 ads | BUILT, TEST DATA only | Real agent buyer signed (offer PDF in buyer_kit/) · domain + hosting · Meta ad account/pixel · ad budget from agent · counsel review | Agent pays ads. You pay ~$7/mo hosting + ~$12 domain | 500 visits per language | Tool completion <30% → fix tool screen. Result→form <15% → fix CTA. Cost per qualified lead > agent's per-lead fee → don't scale. Spanish beats English → shift budget |
| E2 | **Partner seeding**: 25 targets from public data (12 brokerages, 8 RE schools incl. Spanish ones, salon suites, factoring/dispatch, CDL schools, ABA agencies, CPAs). Each gets a tracked guide + stats page | BUILT (31 PDFs, pitches) | You send pitches · domain (QR codes) | $0 + print | 2 weeks after send | <2 of 25 active after 2 follow-ups → switch to team leaders and associations. Any partner >20 scans/week → clone that partner type |
| E3 | **Your accounts**: 1099 / OEP content on boxing IG/TikTok (EN + ES). June only for final-expense *education* (AI-labeled, never a testimonial, never ACA/Medicare) | Hooks BUILT | You post · domain | $0 | 10 posts | Keep formats with click→tool-start >25% |
| E4 | Spanish vs English | Inside E1 | - | - | - | - |
| E5 | SEO calculator pages | BUILT (17 pages incl. Spanish) | Domain + ALLOW_INDEXING=1 | $0 | 60-90 days | Slow; doesn't move OEP 2027 |

**Killed / changed after research:**
- Cold email to public-record prospects: low intent, reputation risk, not the business.
- Meta ZIP targeting from public data: not allowed. Insurance is a Meta Special Ad Category: no age/gender narrowing, location radius at least 15 miles, no ZIP, no lookalikes (secondary source; confirm in Ads Manager). The public data now drives *which partners, which angles, which language, which cities* instead (Admin > Regional briefs).
- "Salon hub" partner list: weak data. Only 7 of 128 addresses are verified shops. Use salon-suite operators instead.
- AI persona (June) for health/Medicare: correction - a CMS ban on deceptive AI video was only *proposed*; it is not in the final 2027 rule. House policy stays the same anyway: no AI-generated people presented as real in health ads. Final-expense education only, labeled.
- Life-first funnel: your agent contact wants **health**, and the money window is OEP. Life campaigns stay live as secondary.

**Getting to the first 100 visitors / 10 leads:**
- Fastest: E1 (paid, agent-funded) or E3 (your following). E2 compounds but lags 1-3 weeks.
- 10 → 100 leads: scale whichever variant/language/partner type wins on cost per qualified lead, *using agent outcomes* (portal) rather than lead counts.
- 100 → 1,000: more agents (your contact's network at 10% referral) + more partner categories + retargeting tool-starters.

**Before launch:**
- `python launch_check.py` lists every remaining blocker.
- `python -m pytest -q tests/test_engine.py` (18 tests).
- `python tests/e2e_check.py` (v3 Health Coverage Check, real browser, TEST DATA).
- `python tests/e2e_browser.py` (real browser, TEST-flagged).
