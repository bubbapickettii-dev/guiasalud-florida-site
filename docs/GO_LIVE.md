# One real visitor: dependency map

Everything that doesn't need an approval is built and tested on TEST DATA. Nothing is deployed, published, paid for, or sent. This page lists what stands between the system and its **first real visitor**, then its **first real lead**.

Going live is a config change: set the env vars in `.env.example`, deploy the repo, and every link, QR code, guide PDF and sitemap regenerates from `BASE_URL`. No rebuild.

## The two milestones are different

| Milestone | Needs | Doesn't need |
|---|---|---|
| **First real visitor / responder** | Domain, hosting, your legal entity + contact details on the pages | An agent. With no real agent configured, the Check runs in **email-only mode**: people get their summary, nobody is called, and the lead is stored as NURTURE (not sellable) |
| **First real lead** | All of the above, plus one real licensed health agent named in the consent, plus counsel sign-off on the consent text | Ads, pixels, partners |

You can go live in email-only mode first. That gets real traffic and real completion data while the agent and legal pieces finish. The production guard (`LEADENGINE_ENV=production`) makes sure a real visitor can never see or consent to a demo agency.

## Dependency map

| Blocker | Who approves | Info required | What I already prepared | What you must do | Est. cost | What happens next |
|---|---|---|---|---|---|---|
| **1. Public URL** | You (account) | A Render account + repo access | `render-free.yaml` (free plan, read-only public preview) and `engine/hosting.py`, which takes the assigned `https://<name>.onrender.com` from `RENDER_EXTERNAL_URL` for every canonical URL, sitemap entry and QR code | Create the account, connect the repo, apply the Blueprint (`docs/RENDER_DEPLOY.md`) | $0 on the free plan. A custom domain later is ~$10-15/yr and is just `BASE_URL` | The site answers at its assigned URL; `/healthz` names the origin and its source |
| **2. Hosting durability** | You | A paid host the moment records must persist | `render.yaml` (starter plan + `/var/data` disk), `Dockerfile`, `requirements-prod.txt`, `/healthz`, `backup_db.py`, `.env.example` | Use the free URL to review the site, then move to the starter service (or PythonAnywhere `/home/<user>/data`) for real collection | Render starter ~$7/mo + disk (verify on render.com/pricing). The free tier keeps no disk, so it runs a READ-ONLY preview: submissions are refused, never stored and lost | Consent/lead records survive restarts; run `python launch_check.py` on the server |
| **3. Business identity** | You | Confirmed: Gia Salud, Miami, Florida, founded Miami, Florida | Footer, privacy (EN/ES), terms (EN/ES), contact all read `config/site.json` (+ env overrides) | Done. A street address is not required for this website and no suffix/registration/phone is invented | $0 | Pages are publishable; `docs/BUSINESS_IDENTITY.md` lists every surface |
| **3b. Support inbox** | You (account) | A mailbox someone reads | Contact page (`mailto:`) and privacy "write to" (EN/ES) read `SUPPORT_EMAIL`; the opt-out page is a form. Nothing prints an address before it exists | Done: `giasaludellc@gmail.com` created, set as `SUPPORT_EMAIL` on the live service. Open the inbox once and pass Google's device check so mail to it is actually read | $0 | Consumer contact gets a real destination (before that: the on-site form) |
| **4. Legal review** | Counsel | Questions below | Consent v4 EN+ES (names one agent, matches the real button label, server-side re-render, SHA-256 snapshot), privacy/terms templates, verification audit | Send counsel `docs/VERIFICATION_AUDIT.md` + the consent text from `config/consent.json`; set `consent_reviewed_by_counsel: true` after sign-off | I don't know your counsel's rate. Get 2 flat-fee quotes for a lead-gen consent + privacy review | Phone-consent leads become legally usable |
| **5. Agent (buyer)** | The agent + you | Agency legal name (exactly as in consent), FL 2-15 or 2-40 license #, NPN, 2027 FFM registration, delivery email, daily cap, flat per-lead fee | Offer PDF (corrected), agent desk with one-click statuses, alert email with FL contact-window + 155.220 reminders, DNC propagation | Close the agent, add them to `config/buyers.json` (copy `demo-health-agent`, set `is_demo: false`), set SMTP_* and `LEADENGINE_LIVE_DELIVERY=1` | Agent pays per lead. SMTP provider: $0-15/mo (verify provider pricing) | Qualified leads route and deliver; feedback loop starts |
| **6. Tracking** | You | GA4 ID and/or Meta Pixel ID | First-party tracking already works without any pixel: every click, step, completion, lead and agent status is attributed by experiment/partner/angle/language | Nothing for organic/partner tests. Add IDs only for paid tests | $0 | Paid platforms can optimize |
| **7. Ad account** | You (+ agent funding) | Meta/Google account, payment method, Special Ad Category acknowledgement | Ad kits (UNTESTED), SAC targeting notes corrected (>=15-mile radius, no age/gender/ZIP, no lookalikes) | Create account, approve a capped budget | Whatever you approve. Nothing is spent until you do | Paid experiments X007-X009 can go LIVE |
| **8. Partner** | Partner + you | Partner's yes; whether they'll show their name | 25 partner targets, instant link + QR + co-branded guide + counts-only stats page per partner | Approve and send the first pitches (`partner_kit/pitches.md`) | $0 (+ print) | X003-X005 go LIVE when the first partner shares |

## Cheapest path to the first real lead

Ranked on cost, speed, intent, control, scalability and compliance complexity. None of these has been tested: there is no conversion data, and I'm not inventing any.

| Rank | Path | Cost | Speed to first visitor | Intent | Control | Scale | Compliance complexity | Verdict |
|---|---|---|---|---|---|---|---|---|
| 1 | **$0 organic: your IG/TikTok, EN + ES** (X001/X002) | $0 | Days after domain | Low-medium | High | Medium (spiky) | Low (education only) | **Start here.** You already know how to get views; each video gets its own `/go` code, so the Lab shows which hook produces completions |
| 2 | **$0 partner: Spanish RE school + ABA agency** (X003, X005) | $0 + print | 1-3 weeks (needs their yes) | High | Medium | High (new cohorts every month) | Low | Run in parallel. Slower to start, but highest intent per visitor and it compounds |
| 3 | **Google Search micro-test** (X007) | ~$100-300 test cap (your call) | 1-2 days after account | Very high | High | High | Medium (no health remarketing until verified, no $0 claims) | Best paid option for a fast, honest signal. Only after the agent is signed, so clicks can become sellable leads |
| 4 | Reddit / community answers (X006) | $0 | Days | High | Low | Low-medium | Medium (sub rules) | Good supplement. Link the /learn page, not the form |
| 5 | Local partnership: QR at salon suites / truck stops / coworking (X011) | $20-60 print | 1-2 weeks | Medium | Medium | Low-medium | Low | Cheap, slow, location-level learning |
| 6 | Creator partnership (Spanish finance / realtor creators) | $0-500 flat | 1-3 weeks | Medium | Low-medium | High | Medium (FTC disclosure; creator can't compare plans) | Second wave, after you know which angle converts |
| 7 | Meta paid, agent-funded (X008/X009) | Agent's budget | 1-3 days after account | Medium | High | Very high | Medium (Special Ad Category limits) | Scale lever after a winning angle exists, not a discovery tool |
| 8 | SEO /learn + /aprende (X012) | $0 | 60-120 days to rank | Very high | High | High | Low | Already built. Won't help OEP 2027 unless pages rank by December |
| 9 | Search (local search pages) | $0 | 60-120 days | High | High | Medium | Low | Build after the first pages index |
| 10 | Other (radio segments, chambers, HR/layoff resources) | $0 | 3-6 weeks | Varies | Low | Medium | Low | Opportunistic |

**My recommendation:** go live in email-only mode as soon as the domain and hosting exist, run X001/X002 the same week, and send the two partner pitches. Sign the agent and get legal sign-off in parallel; once both land, flip on phone consent and live delivery. Add the Google micro-test only after that, so every paid click can turn into a sellable lead.
**Main risk:** OEP closes Jan 15. Every week the agent and legal pieces slip is a week of the only high-volume window lost.
**What I'd do next:** domain + Render this week (~$20 total), counsel quote in parallel.

## Legal review: questions for counsel

1. Does consent v4 (EN + ES) meet 47 CFR 64.1200 PEWC and Fla. Stat. 501.059 for calls/texts by the one named agency?
2. As an unlicensed lead generator in Florida, does a flat per-delivered-lead fee from the agent stay outside 626.112 (no dependence on a sale)? Can content pages stay at "dates/eligibility education" without being solicitation under 626.112(1)(b)?
3. Do the partner arrangements (no payment, counts-only reporting) need anything further? If a partner wants payment, what structure is allowed?
4. Privacy policy: is the template sufficient for Florida, and what data-retention period should we state, given that agents must keep consent records 10 years under 45 CFR 155.220(j)(2)(iii)?
5. Is the email-only nurture consent (CAN-SPAM) adequate as written in English and Spanish?
6. Anything required on the site because the agent will do Marketplace enrollments (CMS agent/broker marketing rules, 155.220(j)(3))?

## Go-live steps (once approved)

1. `cp .env.example .env`, fill it in, and deploy (Render: New > Blueprint > this repo).
2. `python launch_check.py` on the server: every line should say READY, except partner/organic items that are your actions.
3. Open `/check?test=1` on your phone and run one test submission (flagged TEST DATA, never counted as real).
4. Admin > Distribution Lab: set X001/X002 to LIVE and put the `/go` links in your bio.
5. Daily: Lab (evidence column), Lead Quality (by angle and language), Agent Desk feedback coverage.
