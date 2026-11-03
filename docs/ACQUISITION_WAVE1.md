# Acquisition wave 1: first controlled traffic (Oct 5, 2026)

Goal: find out which source can produce **voluntary consumer phone leads**, and know exactly what happened to every visitor.
Nothing here contacts anyone from public records. Every number below is a decision rule we chose, not an industry benchmark. Nothing has been tested with real traffic: all three experiments are UNTESTED.

## 1. Audit of v3.1 (tested in a real browser before changing anything)

| Question | v3.1 result | Evidence | Fix |
|---|---|---|---|
| 1. Create unique sources/tests? | YES | `lab.create` makes an experiment + `/go/<code>` link + QR | Added fields: demand trigger, content concept, offer/CTA, explicit tracking ID, phone-wording variant |
| 2. Track source → landing → Check → phone? | PARTIAL | Direct `/go` → `/check` worked. Entering on a /learn article: no page view recorded, and the article's CTA overwrote the source with `utm_source=content` (experiment lost). Return visits lost the source too | Shared first-party attribution (`static/attr.js`): last tracked touch kept 30 days, every public page tracked, server fills gaps from the visitor's first touch |
| 3. Test vs real separated? | PARTIAL | Only `?test=1` on the current URL. Your own click on a live link counted as REAL. Found while testing: clicking from an article to the Check dropped the test flag, and admin screens were counted as visits | `/qa?on=1` marks a device as TEST permanently; `?test=1` now persists for the session; admin/agent/partner screens are never traffic; QA/test clicks never count |
| 4. Compare content angles? | YES | `angle` dimension in Funnel/Quality | - |
| 5. Compare EN vs ES? | YES | `language` dimension | Also works inside one experiment (X015 has EN + ES ad groups on one tracking ID) |
| 6. Compare landing pages? | NO | Experiment `landing_page` was stored but `/go` ignored it; no landing dimension | `/go` honors the experiment's landing page; new `landing` (entry page) dimension |
| 7. Compare phone-step wording? | NO | Wording hard-coded; no "phone step viewed" event | Wording A/B in `config/health_flow.json` (sticky per browser, or forced per experiment / `?pv=`), `phone_step_view` event, `phone_variant` on events and leads |
| 8. Exactly which source produced each lead? | PARTIAL | Correct on the direct path, wrong on article and return-visit paths | Fixed by #2; each lead carries experiment, tracking ID, source/medium/content (placement), entry page, angle, language and phone wording |

Verified end to end with fake traffic: `tests/e2e_acquisition.py` (fresh DB), 23/23 checks pass. All earlier suites still pass (29 unit tests, 8-persona Check E2E, legacy E2E).

## 2. What an experiment looks like now

Admin > **Distribution Lab** (create + table) → click an ID for the **experiment page**:

- **Definition:** audience, demand trigger, channel/source, content concept, offer/CTA, landing page, Check angle, language, campaign, tracking ID, status, dates.
- **Funnel, REAL and TEST shown separately:** clicks → visits → form starts → Check completed → **phone step viewed → phones submitted → valid phones → consented phone leads** → qualified → delivered → appointments → sales.
- **Breakdowns** by phone wording, landing page, language, angle and source.
- **Lead list:** lead ID, type, phone valid, wording, entry page, qualification, delivery, agent outcome.

Admin > **Audiences** connects market intelligence to experiments. Each segment shows:

- its aggregate signal count (not people)
- the questions people in it ask
- where to reach them
- the Check angle
- the experiments aimed at it
- its evidence status, computed from real traffic only: `HYPOTHESIS - no experiment yet` → `HYPOTHESIS - test planned` → `TESTING - n real visits` → `SUPPORTED BY DATA - n real consented phone leads`

Per-placement tags on one tracking link: `/go/x013?c=story` vs `/go/x013?c=bio`. The angle can be overridden too: `/go/x015?a=es_check&c=es-adgroup`.

## 3. The three experiments (records exist: X013, X014, X015)

### X013 (launch first): Bubba's boxing Instagram, "Fighters & trainers are 1099" (EN)

| | |
|---|---|
| Audience | Fitness & combat-sports pros (1099). No public dataset exists: Florida doesn't license trainers |
| Problem / trigger | Trainers, coaches and fighters are independent contractors with no employer plan; 2027 Open Enrollment runs Nov 1 to Jan 15 (Dec 15 for Jan 1 coverage) |
| Content concept | 3-frame Story with a link sticker, plus 1 talking-head Reel (you, in the gym), with the link in bio |
| Exact CTA | "Tap the link: free 60-second 2027 Health Coverage Check (Florida). See your enrollment window and whether you're in the range for help paying premiums." |
| Destination | Story sticker: `https://YOUR-DOMAIN/go/x013?c=story`. Bio: `https://YOUR-DOMAIN/go/x013?c=bio` → `/check?a=self_employed` |
| Check angle | `self_employed` (EN) |
| Channel | Instagram organic (your existing account) |
| Expected cost | $0 |
| Tracking ID | `x013` (placements `story`, `bio`) |
| Measuring | Clicks, visits, out-of-area exits (non-FL followers), Check completions, phone-step views, phone submissions, valid phones, consented phone leads (needs agent), qualified |
| Read point | 7 days after the first post, or 150 real visits, whichever comes first |
| Success | ≥1 consented valid phone lead, OR ≥10 Check completions with ≥3 phone submissions (the funnel works; volume is limited by your Florida share) |
| Failure | ≥150 real visits and 0 phone submissions (message/audience mismatch), OR more than half of Check starters exit at the Florida ZIP screen (audience isn't in Florida → move effort to X014) |

**Story frames**
1. (You on camera, gym) "Fighters, coaches, trainers: we're all 1099. Nobody's handing us health insurance."
2. Text card: "Florida Open Enrollment: Nov 1 - Jan 15. Enroll by Dec 15 → covered Jan 1."
3. Link sticker **"2027 Health Check"** + text: "Free. 60 seconds. See your window and if you're in the range for help paying."

**Reel (20-25s)**
- Hook: "If you train people for a living, who's covering *you*?"
- Beats:
  1. "No gym gives 1099 coaches a health plan."
  2. "Open Enrollment starts Nov 1. Dec 15 is the date for Jan 1 coverage."
  3. "I put a free 60-second check in my bio. It shows your window and if your income's in the range for help."
- Caption: "Trainers + fighters in Florida: 60-second check, link in bio. Not insurance advice. A licensed agent helps only if you ask."

**Rules:**
- Don't say "free insurance" or "$0."
- Don't compare plans or carriers. That's solicitation under Fla. Stat. 626.112.
- Turn QA mode on (`/qa?on=1`) on your phone before you click your own link.

### X014: AI explainer in Spanish (clearly labeled AI), "¿Trabajas por tu cuenta?"

| | |
|---|---|
| Audience | Spanish-speaking self-employed, Miami-Dade: 15,286 audience signals (4,138 in the last 90 days) across new beauty/barber/realtor licenses, new businesses and new owner-operators in Miami-Dade ZIPs. Spanish-first is an inference, not verified |
| Problem / trigger | New 1099 work + 2027 Open Enrollment; losing job coverage (60-day SEP) |
| Content concept | 5 vertical shorts, 20-25s each, one question each. AI-generated presenter, labeled on screen "Presentadora generada con IA" plus the platform's AI label. New dedicated Instagram account (the bio link works from day one), cross-posted to TikTok and Shorts for reach |
| Exact CTA | "Haz el chequeo gratis de 60 segundos. Link en el perfil." |
| Destination | Bio: `https://YOUR-DOMAIN/go/x014` → `/check?a=es_self_employed` |
| Check angle | `es_self_employed` (ES) |
| Channel | Instagram Reels (primary); TikTok/Shorts cross-posts |
| Expected cost | AI video credits only. The talking-clip recipe that worked for June runs about 70 Higgsfield credits per 10s, so about 210 per short and about 1,050 for five. **Your Higgsfield balance is 2.66 credits**, so this needs a top-up (your approval). No ad spend |
| Tracking ID | `x014` |
| Measuring | Same funnel. Per-short attribution isn't possible from a single bio link: compare by posting day |
| Read point | 10 shorts posted or 21 days |
| Success | ≥1 consented phone lead, OR ≥50 real visits with ≥3 phone submissions |
| Failure | <20 real visits in 21 days (reach problem: change hook/format), OR ≥50 visits and 0 phone submissions (offer problem) |

**Scripts** (tú; dates and numbers from HealthCare.gov / 2026 HHS guidelines; "según la ley actual")
1. "¿Trabajas por tu cuenta en Florida y no tienes seguro médico? Hay una fecha que importa: el 15 de diciembre. Si te inscribes antes, tu cobertura empieza el 1 de enero. La inscripción abierta va del 1 de noviembre al 15 de enero. Haz el chequeo gratis de 60 segundos, link en el perfil."
2. "¿Perdiste el seguro médico del trabajo? Normalmente tienes 60 días para inscribirte en un plan del Mercado. No esperes a enero. El chequeo te dice tu periodo en 60 segundos."
3. "Barberos, estilistas, realtors: si trabajas 1099, nadie te da seguro médico. Pero puedes comprar tu propio plan del Mercado, y según tu ingreso podrías recibir ayuda con la prima."
4. "¿Califico para ayuda con la prima? Para 2027, según la ley actual, la ayuda generalmente aplica si tu ingreso está entre $15,960 y $63,840 para una persona. Revisa tu rango en el chequeo."
5. "¿Cómo calculo mi ingreso si trabajo por mi cuenta? Para el Mercado cuenta tu ganancia neta: lo que cobras menos tus gastos del negocio. Si cambia durante el año, lo actualizas. El chequeo te da un estimado."

**Production:**
- A new presenter (not June).
- On-screen AI label for the whole video, plus the platform's AI label.
- No testimonials, no "I saved $X."
- Each script passes the 626.112 check (education only).

### X015: Google Search micro-test (EN + ES ad groups)

| | |
|---|---|
| Audience | People actively searching for individual / self-employed coverage in Miami-Dade, Broward, Palm Beach |
| Problem / trigger | Searching during the 2027 enrollment window |
| Content concept | Two ad groups (EN, ES), exact/phrase match, text ads |
| Exact CTA | EN: "Free 2027 Health Coverage Check - see your enrollment window in 60 seconds." ES: "Chequeo gratis de cobertura 2027 en 60 segundos." |
| Destination | EN final URL `https://YOUR-DOMAIN/go/x015?a=timing&c=en-adgroup`. ES `https://YOUR-DOMAIN/go/x015?a=es_check&c=es-adgroup` |
| Check angle | `timing` (EN), `es_check` (ES) |
| Channel | Google Search |
| Expected cost | $150 cap ($10/day for 15 days). I don't know current CPCs for these terms; Keyword Planner shows estimates once the account exists |
| Tracking ID | `x015` (placements `en-adgroup`, `es-adgroup`; gclid captured) |
| Measuring | Funnel by language inside one experiment; cost per phone submission and per consented phone lead |
| Read point | $150 spent or 15 days |
| Success | ≥1 consented phone lead, at a cost per consented phone lead ≤ the per-lead price the agent agrees to pay (that price doesn't exist yet) |
| Failure | $150 spent and 0 phone submissions → stop. Check completions but no phone submissions → fix the phone step (A/B data) before spending more |

**Keywords**
- EN: "health insurance self employed florida", "obamacare florida 2027", "marketplace health insurance florida", "health insurance for 1099 workers"
- ES: "seguro médico obamacare florida", "seguro médico trabajador independiente", "seguro médico 2027 florida"
- Negatives: medicare, medicaid, jobs, careers, login, "healthcare.gov login"

**Ad copy (lengths checked)**
- Headlines EN: "2027 Health Coverage Check" (26) · "Open Enrollment Nov 1-Jan 15" (28) · "Self-Employed in Florida?" (25) · "Takes About 60 Seconds" (22)
- Headlines ES: "Chequeo de Cobertura 2027" (25) · "¿Trabajas por tu Cuenta?" (24) · "Inscripción 1 nov - 15 ene" (26) · "Toma Unos 60 Segundos" (21)
- Descriptions:
  - EN: "Free check: see your 2027 enrollment window and if your income is in the range for help." (88)
  - EN: "Enroll by Dec 15 for Jan 1 coverage. A licensed agent can help if you ask." (74)
  - ES: "Chequeo gratis: tu periodo de inscripción 2027 y si tu ingreso está en el rango de ayuda." (89)

I don't know whether Google requires extra verification for US health insurance advertisers beyond its standard advertiser verification.

## 4. Recommendation

**Launch X013 first.**
- It costs $0 and goes out the day the site is live.
- You control it fully.
- It puts real people who already trust you into the funnel, which gives the first honest read of every stage.
- Its one real unknown, how much of your audience is in Florida, can be checked before posting (IG Insights → Audience → Top locations), and the ZIP screen measures it anyway.
- X014 follows once credits are approved. X015 only once an agent is signed, so paid clicks can become sellable leads.

## 5. What's needed to launch X013

| Item | Why | Without it |
|---|---|---|
| Domain + hosting live (~$10-15/yr + ~$7/mo; your approval) | Real people can't reach localhost | No test at all |
| Your agent contact: agency legal name, FL license #, NPN, OK to be named in the consent, an email to receive leads | The consent must name the agency that will call (TCPA prior express written consent; Fla. Stat. 501.059). With no named agency the Check doesn't ask for a phone (production guard, unit-tested) | The test still runs but stops at Check completion: no phone metrics |
| LLC name, contact email, mailing address | Footer, privacy page, consumer email | Pages carry placeholders |
| Your IG Florida share (Insights) | Predicts whether completions can be Florida leads | We learn it from ZIP exits instead |
| Post 3 Story frames + 1 Reel (above) with the two links | The test itself | - |
| `/qa?on=1` on your phone first | Keeps your own clicks out of REAL | Your clicks pollute the data |
| Counsel review of the consent text | Recommended before collecting real phone leads | Your risk call |

## 6. Genuine blockers

1. **Hosting/domain:** nothing real can happen on localhost.
2. **A named licensed agency for consented phone leads:** a legal requirement, not an infrastructure gap. Every stage up to Check completion can be measured without one.
3. **X014: Higgsfield credits (balance 2.66)** and a new Instagram account. I can't create accounts or buy credits.
4. **X015: Google Ads account, payment method, and approval of the $150 cap.**
