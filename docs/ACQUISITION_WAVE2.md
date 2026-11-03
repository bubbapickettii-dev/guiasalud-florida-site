# Acquisition wave 2: where the high-intent consumers are (Oct 5, 2026)

Status: **research done, tests built, nothing live.**
- No real visitors, no real leads, no proven channel.
- Everything below is a hypothesis backed by public evidence until real consumer behavior says otherwise.
- The full opportunity database (18 entries, all fields, sources) is in `docs/OPPORTUNITIES.md` and Admin → Opportunities.

## 1. What the research found

The biggest insurance demand of the next 90 days isn't an audience. It's a set of **forced decisions created by the 2027 market**.

| Signal | Evidence | Why it matters |
|---|---|---|
| 4 carriers leave Florida's individual market for 2027: Cigna Health & Life, Cigna HMO, Molina Healthcare of FL, Sunshine State Health Plan (Sunshine members expected to move to Ambetter) | [ACASignups FL 2027](https://acasignups.net/rate_changes/2027/fl) (from rate filings; confirm against carrier letters) | Those members **must** act. Letters arrive in October; Dec 15 decides Jan 1 coverage |
| Proposed 2027 increases average about +15.9%; Florida Blue PPO filed about +26% | [healthinsurance.org](https://www.healthinsurance.org/blog/health-insurance-premium-increases-for-2027-proposed-rates-by-state-and-what-consumers-should-know/), [Moran](https://moraninsurancegroup.com/blog/florida-blue-2027-rate-increase-renewal) | Renewal-letter shock at the largest Florida carrier |
| Florida lost about 443-450k paying Marketplace enrollees in 2026 (-10%) after the enhanced credits expired | [Florida Phoenix](https://floridaphoenix.com/2026/09/10/up-up-and-away-are-double-digit-health-insurance-rate-increases-ahead/), [WLRN](https://www.wlrn.org/health/2026-08-19/florida-lost-450-000-aca-enrollees-in-2026-as-premiums-rose-nationally-new-data-shows) | A large, price-shocked, recently-insured pool |
| Spanish results for 2027: one serious agency competitor; no Spanish page found on Molina leaving FL; a 2017 article still ranks for 2027 cost queries | Research agent, non-Google index | Low-competition search pockets, by insurer name and in Spanish |
| Florida averages 5,880 initial jobless claims per week in 2026; WARN names the employers | [DOL](https://www.dol.gov/ui/data.pdf), [LayoffTracker](https://www.layofftracker.us/layoffs/state/florida/2026) | About 20-25k people a month enter a 60-day SEP. Searches for each employer's layoffs return news, not coverage help |
| OEP 2027 still ends Jan 15 (a court vacated the Dec 15 end); some outlets still print Dec 15; HHS appeal pending | [CMS Aug 4 2026](https://www.cms.gov/files/document/statement-re-city-columbus-080426.pdf), [Florida Phoenix](https://floridaphoenix.com/2026/09/10/up-up-and-away-are-double-digit-health-insurance-rate-increases-ahead/) | Late-season "did I miss it?" searchers |
| The cap on repaying excess premium tax credit is gone from tax year 2026 | [IRS](https://www.irs.gov/newsroom/one-big-beautiful-bill-provisions) | Tax-season shock (Feb-Apr 2027); tax preparers are the distribution point |
| 1,310 nurse registries whose caregivers are independent contractors by Florida law; 57k RBTs in Florida, with South Florida ABA agency closures | [FL Senate HB1013 analysis](https://www.flsenate.gov/Session/Bill/2026/1013/Analyses/h1013.HFS.PDF), [BACB](https://services.bacb.com/o.php?page=101134), [Periódico Cubano](https://www.periodicocubano.com/cubanos-certificados-como-rbt-pierden-sus-empleos-en-florida-que-hay-detras-del-cierre-de-agencias-aba/?amp=1) | 1099 workforces nobody markets health insurance to by name |
| Navigator funding was cut about 90% | [KFF](https://www.kff.org/quick-take/a-90-cut-to-the-aca-navigator-program/) | Free human help is scarce this OEP |

**What a lead is worth** (vendor blogs, they disagree):
- Exclusive ACA leads sell for about $22-80.
- Inbound calls / live transfers sell for about $32-100 ([OneLife](https://onelifemarketingsolutions.com/blog/insurance-lead-costs-2026), [AllCalls](https://allcalls.io/blog/best-lead-sources-for-high-volume-aca-enrollment-5-top-picks-2026/)).
- Agents earn about $20-30 per member per month ([BenaVest](https://www.benavest.com/aca-commissions-for-agents/)).
- INFERENCE: a consented, exclusive phone lead with household and income is worth about $50-150 to an agent.

**What I couldn't verify:**
- Reddit and Facebook blocked automated access, so community thread titles and group sizes are UNVERIFIED.
- The search-ranking observations come from a non-Google index.
- The research hit the session's web-search cap.

## 2. Where I disagree with the current plan

1. **AI avatars are not the best first mechanism.** An avatar video creates attention; this business needs intent.
   - Use AI as the production engine instead: dated pages per insurer and per layoff, bilingual copy, exact-text share cards, programmatic layoff pages, the Check's branching logic. That's how one person can cover every carrier exit and every WARN notice the week it happens.
   - Bring the avatar back once a page proves it converts, to push that page's message on video.
2. **The occupational public-data segments (realtors, beauty, trucking) rank below event-driven triggers.** "New realtor license" says someone may need coverage someday. "Molina letter received" or "employer filed WARN" says they need a decision within weeks.
   - The public-data engine stays valuable for two things: WARN employer detection, and partner discovery (registries, schools, agencies).
3. **X013 (your boxing IG) should carry the carrier-exit hook, not the 1099 hook.** Your audience isn't mostly Floridians, and the carrier-exit message self-selects the ones who are. The tagged link `/go/x016?c=ig-story` is ready.
4. **Paid search isn't where to find the first leads.** Bloomberg documented Obamacare paid results dominated by telemarketers ([Bloomberg Law](https://news.bloomberglaw.com/insurance/people-googling-obamacare-find-junk-health-insurance-instead)). Organic and AI-cited pages, shares and partners are where the room is.

## 3. Ranking

Top of the database (full table in `docs/OPPORTUNITIES.md`):

| Rank | Opportunity | Score | Status |
|---|---|---|---|
| 1 | OPP-01 2027 forced re-shoppers (carrier exits + rate-shock renewals) | 92.0 | READY TO TEST |
| 2 | OPP-02 Job-loss SEP engine (WARN-triggered employer pages) | 85.6 | READY TO TEST |
| 3 | HID-01 "Is Open Enrollment still open?" late-season capture | 84.0 | READY TO TEST (page live in build; push Dec 16) |
| 4 | HID-03 Florida Blue PPO renewal shock (part of the OPP-01 build) | 84.0 | READY TO TEST |
| 5 | OPP-08 Free bilingual webinars on event calendars | 80.8 | HYPOTHESIS (needs an agent presenter) |
| 6 | OPP-05 New residents: the checklist that forgot health insurance | 77.6 | RESEARCHED |
| 7-8 | OPP-11 WhatsApp sharing / HID-02 AI-answer citation capture | 76.0 | built into the top tests |
| 9-10 | OPP-04 RBTs / OPP-03 nurse-registry caregivers | 75.2 / 73.6 | RESEARCHED (partner-dependent) |

Excluded:
- HID-07 (2027 immigration-related tax-credit changes): targeting by status is off-limits, and lead value is low.
- OPP-10 (early-retiree calculator): crowded, and the audience is DIY.

## 4. The three tests (built)

### Test 1: X016 (Spanish) + X017 (English), the 2027 carrier-exit and renewal pages. Launch first.

- **Audience:** Floridians whose 2027 plan is ending (Cigna, Molina, Sunshine) or whose renewal jumped (Florida Blue PPO).
- **Pages built** (dated, FAQ schema, sources, WhatsApp share button):

  | Topic | Spanish | English |
  |---|---|---|
  | Hub | `/aprende/cambios-seguro-medico-florida-2027` | `/learn/florida-health-plan-changes-2027` |
  | Molina | `/aprende/molina-se-va-de-florida-2027` | `/learn/molina-leaving-florida-marketplace-2027` |
  | Cigna | `/aprende/cigna-se-va-de-florida-2027` | `/learn/cigna-leaving-florida-marketplace-2027` |
  | Sunshine → Ambetter | `/aprende/sunshine-health-pasa-a-ambetter-2027` | `/learn/sunshine-health-ambetter-2027` |
  | Florida Blue | `/aprende/florida-blue-aumento-2027` | `/learn/florida-blue-2027-renewal-increase` |

- **Check path:** a new "My 2027 plan is ending or going up" path.
  - It asks for ZIP, current company and what the letter says, then household, age and income.
  - Result: if the plan is ending, "Your plan ends Dec 31; choose by Dec 15; don't assume your doctors will be in whatever plan you're placed in." If the price is going up, it explains auto-renewal at the new price.
  - Ending = urgency HIGH (flag `plan_ending`); renewal = MEDIUM.
- **Why they'd give a phone:** keeping their doctors and drugs through a forced switch is the problem a licensed agent solves.
- **Share cards** (exact text, feed + story, in `assets/cards/`): `es_plan_change`, `en_plan_change`, `es_florida_blue`. They re-render with your domain via `BASE_URL=... python tools/build_cards.py`.
- **Tracking:**
  - Untagged arrivals on these pages are credited to X016/X017 automatically.
  - The source comes from the referrer: Google/Bing = organic; ChatGPT/Perplexity = `ai_answer`.
  - WhatsApp shares keep X016 credit with `whatsapp/share`.
  - Tagged links: `/go/x016?c=ig-story`, `/go/x016?c=media`, `/go/x016?c=wa-card`, `/go/x017?c=card`.
- **Distribution, $0 first:**
  1. Publish.
  2. Submit the sitemap to Google Search Console and Bing Webmaster Tools (free).
  3. Your IG story with the ES card + `/go/x016?c=ig-story`.
  4. WhatsApp share from the page.
  5. Pitch the insurer table to the Spanish outlets already covering 2027 changes, e.g. DirectorioCubano, Periódico Cubano, Telemundo 51 (needs your OK).

  Honest caveat: a brand-new domain won't rank in Google for weeks or more, so shares, your IG and media do the early lifting.
- **Where $ creates leverage:** about $50 of boosted Spanish card in Miami-Dade (Meta Special Ad Category, 15-mile radius) during Nov 1-15 buys the first few hundred visits while the pages index. Only after the pages are live and QA'd.
- **Read point:** 21 days after publishing or 300 real visits.
- **Decision rules** (ours, not benchmarks):
  - Continue: ≥1 consented phone lead, or ≥5 phone submissions.
  - Stop: 300 visits with 0 phone submissions.
  - Spanish vs English is compared inside the Lab.

### Test 2: X018, the job-loss engine

- **Built:**
  - `/layoffs` hub, with Spanish at `/aprende/me-despidieron-seguro-medico-florida`.
  - 7 employer pages from WARN/news: Magic Leap (Plantation, 465), Astrion (Eglin, 204), AssistRX + Hancock Whitney (Orlando, Nov 30), Amentum (KSC, Nov 30), Amazon (Homestead, 616), Spirit (5,683).
  - Each page computes the 60-day window from the effective date and says when it has likely passed, pointing those people to OEP instead.
  - "Not affiliated with <Company>" on every page; company-level data only.
- **`tools/warn_watch.py`** turns a WARN CSV export into **draft** pages: new employers, ≥25 workers, effective within the window. You review at `/layoffs/<slug>?token=…` and set `status: ready`.
- **Check angle:** `life_change` (lost-coverage path).
- **Why they'd give a phone:** COBRA price vs Marketplace with the same doctors, before a hard deadline.
- **Distribution:**
  - Search on "<company> layoffs" (results are news-only today).
  - The page link in local news comment threads where allowed.
  - A free one-page handout for CareerSource rapid-response teams (B2B, needs your OK).
- **Read point:** 30 days.
- **Decision rules:** continue at ≥1 phone lead or ≥3 phone submissions; stop at 0 Check completions after 30 days across ≥5 live employer pages.

### Test 3: X020, "Is Open Enrollment still open?" (EN `/open-enrollment-2027`, ES `/inscripcion-abierta-2027`)

- **What it does:** the answer changes with the date: opens Nov 1 → open, enroll by Dec 15 → STILL open until Jan 15 (coverage Feb 1) → closed. It shows a countdown and explains the misreported Dec 15 date.
- **Distribution:**
  - Search/AI ("se acabó la inscripción", "missed Dec 15").
  - The `es_oep_late` card on Dec 16.
  - Your IG.
- **Read point:** Jan 15. Decision rule: continue next season if it produces ≥1 phone lead.

## 5. What's verified in the build

All of it TEST DATA:
- 34 unit tests
- `tests/e2e_wave2.py`, 15/15: Google→Molina ES→plan-ending lead credited X016/google/organic; ChatGPT→Florida Blue→X017/ai_answer; layoff page→X018; WhatsApp share→friend→X016/whatsapp/share; late-season page→X020/bing/organic
- e2e_acquisition 23/23
- the 8-persona Check E2E
- the legacy E2E

## 6. What you need to do

| # | Action | Why | Cost |
|---|---|---|---|
| 1 | Domain + hosting (approve) | Nothing reaches real people on localhost | ~$10-15/yr + ~$7/mo |
| 2 | Your agent contact: agency legal name, FL license #, NPN, OK to be named, lead email | Consented phone leads need a named agency. Without one the Check stops before the phone step | $0 |
| 3 | LLC name, contact email, mailing address | Footer, privacy, consumer email | $0 |
| 4 | Turn QA mode on (`/qa?on=1`) on your phone, then post the ES card story with `/go/x016?c=ig-story` | First real distribution | $0 |
| 5 | After launch: Google Search Console + Bing Webmaster sitemap submit (I can walk it once the domain exists) | Indexing for search/AI | $0 |
| 6 | OK to pitch Spanish outlets and CareerSource (B2B emails; I draft, you send or approve) | Earned distribution | $0 |
| 7 | Optional: approve ~$50 Spanish card boost, Nov 1-15 | Buys first visits while pages index | ~$50 |

## 7. Genuine blockers

- No live site.
- No named licensed agency: the Check stops before the phone step until there is one.
- B2B outreach and any spend need your approval.

## 8. Next research loop

Tell me to run any of these:
- Confirm carrier letters' actual wording when members post them.
- Pull final OIR-approved 2027 rates (floir.gov was blocked here) and update pages the day they're public.
- Manual check of Reddit/Facebook communities (blocked to automation).
- Build OPP-05 (new-resident checklist) and the RBT/caregiver partner kit after the first read.
