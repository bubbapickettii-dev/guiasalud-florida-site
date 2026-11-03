# LifeButton webinar: what we take, what we skip

**Source.** I watched LifeButton AI's webinar replay ("Free training for life insurance agents"). It runs 1h45m and is presented by Hunter. I captured the full auto-caption transcript on Oct 8, 2026.

**What they sell.** A $4,997 setup plus $497 a month. It lets life and annuity agents run their own Meta and Google ads, using a template library, an "AI media buyer" that makes recommendations you approve, OTP-verified lead forms and landing pages, and a CRM with an AI SMS appointment-setter.

**Evidence labels:**
- **CLAIM:** he said it. Unverified. That covers all his numbers and testimonials.
- **FACT:** I checked it against a source.
- **OURS:** what it means for the Florida health lead engine.

---

## The 12 gems, ranked by value to us

### 1. The creative is the targeting. BUILT INTO OUR STRATEGY ALREADY; now confirmed.

- **What he said.** Insurance ads sit in a Special Ad Category. You can't target age, gender, income, ZIP code, or a radius under 15 miles. He says "creative is king": the hook decides who responds.
- **FACT.** Meta's Financial Products & Services category now covers all insurance, including health ([Media Logic, Aug 2025](https://www.medialogic.com/?p=31549)).
  - His explanation (the ECOA, a lending law) is loose. Meta's rules come from its own policies and settlements.
  - The constraints themselves are real.
- **OURS.** Our trigger hooks do the targeting no checkbox can:
  - "Your Cigna plan ends Dec 31"
  - "Laid off from Nearly Natural?"
  - "COBRA ending after 18 months"
  - "Te cambiaron de seguro"

  A generic "Obamacare 2027" ad gets a random audience. A carrier-exit ad only gets clicks from people holding that letter.
- **Next.** Build a creative matrix: trigger × format × language. The formats he says win are a podcast-style clip ("mom podcast"), a person telling their situation ("veteran on a train"), and a niche identity (trucker, first responder, blue collar). AI video makes 10–20 variants cheap. Production runs on Higgsfield once a paid test is approved.

### 2. Phone verified by text code (OTP). BUILT TODAY.

- **What he said.** His lead forms and pages are "fully OTP": the consumer types a one-time code. Vendors charge a premium for verified numbers.
- **OURS.** This is the strongest proof our product is real: a consumer-submitted number the consumer proved they own.

What's built:
- **Check flow.** After the phone step there's a "Enter the 6-digit code we just texted you" screen in English and Spanish. It has resend, change number, and "continue without the code", which appears after 30 seconds or after a wrong code. **It never blocks a lead.**
- **Server-side truth.** A lead is marked verified only if our server recorded a correct code for that exact number. Before consent only salted hashes are stored. Limits: 3 sends per number per hour, 5 attempts, 10-minute expiry.
- **Downstream.**
  - The agent desk shows "✓ verified by text code".
  - The delivery payload carries `verified_by_code`.
  - The funnel counts `verified_phone_leads`.
- **Provider.** Twilio Verify is wired; set `OTP_PROVIDER=twilio_verify` plus three keys. TEST traffic uses a dry run with no SMS.
- **Switch.** Off by default (`otp_step` in site.json). `?otp=1` forces it on for QA or an A/B test.
- **Cost.** About $0.05 per successful verification (Twilio list price; check at signup). Needs your approval to open the account.
- **Test it.** Verification adds friction. Run it as an A/B test: verified-lead rate × the agent's contact rate vs. the drop in completions.

### 3. Lead price vs. real cost: the market's pricing is about to break.

- **CLAIM.** Vendors put about 30% of the price into media. An OTP-verified IUL landing-page lead costs them $8–11 and sells for $50–70. LifeButton users average about $15 per lead.
- **OURS. This is the most important strategic signal in the video.**
  - In Q&A he said **under-65 health is "coming in Q4."** Florida health agents will soon be able to self-generate Meta leads at roughly $15.
  - An agent who can make a generic lead for $15 won't pay $40–60 for one from us.
  - Our lead has to be something DIY agents can't make themselves:
    - **Trigger-timed:** carrier-exit / January auto-move, WARN employer pages, COBRA calendar.
    - **Spanish.**
    - **Organic or AI-answer traffic** with $0 media.
    - **Exclusive, verified, and showing what the consumer read and answered.**
- **Pricing rule.** Never price a generic paid lead above about 2× its media cost. Price trigger leads on the urgency flag (plan_ending, closure/no-COBRA, COBRA exhaustion), or move to a revenue share with the agency.

### 4. Show the agent exactly what the consumer saw. BUILT TODAY.

- **CLAIM.** A large vendor turned off its good $10 ads, ran $8 ones until agents complained, and agents had zero visibility into what was running.
- **OURS.** Transparency is a selling point to buyer agencies. Every delivery now carries `what_they_saw`:
  - the entry page
  - angle, need and urgency
  - flags
  - the phone-step wording

  The agent desk shows "What they read".

### 5. Speed to lead: the first 5 minutes.

- **CLAIM.** Contact rates collapse after 5 minutes. They use an AI SMS bot that books the appointment instantly.
- **Webinar anecdote.** An agent said she "spent 3,000 on leads, sold one; most don't answer, said they already got something the week before." That's what resold leads do.
- **OURS.**
  - **Already true:** leads are exclusive and delivered in real time.
  - **Add to buyer terms:** the agent calls within 5 minutes during 8am–8pm. Track time-to-first-contact on the agent desk; that's the next build if a buyer signs.
  - **AI texting from our side is phase 2, after counsel review.** Our consent names one agency; automated texts need consent that covers them, Florida's mini-TCPA applies, and the FCC treats AI-generated voices as "artificial" under the TCPA.

### 6. Feed outcomes back to the ad platform. RISK FOR HEALTH; don't copy blindly.

- **What he said.** Disposition each lead (picked up, booked, closed) so the Meta pixel learns what a good lead looks like.
- **FACT.** Health advertisers can be classified under Meta's Health & Wellness restrictions. That limits conversion optimization and the data passed through the Conversions API. One agency's guidance is to use a Landing Page Visits objective instead ([Media Logic](https://www.medialogic.com/?p=31549)).
- **OURS.**
  - Our agent desk already records CONTACTED / APPOINTMENT / SOLD, so the data exists.
  - Before sending anything, check how Meta classifies our ad account on day 1.
  - If we send anything, send only an event name plus a hashed identifier, disclosed in the privacy policy. **Never Check answers. Never anything health-related.**

### 7. Funnel shape by age: trust-heavy for older people, fast for younger.

- **What he said.**
  - The annuity (older) funnel is polished: a "retirement score" quiz, then a free report, then a short video, then a booked call.
  - The IUL (younger) funnel is one fast page.
  - Meta instant lead forms are cheapest but lowest intent.
- **OURS.**
  - Our 60-second Check already is the "score quiz".
  - Add booking a time on the agent's calendar after consent, once a real agency calendar exists.
  - Keep sending ad traffic to our own Check, not instant forms. It's the quality filter, and the Check is what gives the lead its value.

### 8. Small budgets plus discipline.

- **CLAIM.** Small spenders stay in the algorithm's "sweet spot", while vendors spending $500k a month degrade.
- **His advice:**
  - at least $30–50 a day to see consistent results;
  - expect expensive leads for the first 2–4 days, then kill the expensive ads and scale the cheap one;
  - judge on return on ad spend, not 48 hours of no pickups.
- **OURS.**
  - Our $50 approved test is an organic-card boost, not a lead campaign.
  - A real paid lead test should be about $30–50 a day for 7 days (~$210–350), with kill rules written beforehand, like the Lab's.
  - Their "AI media buyer on approval mode" (kill losers, scale winners, a daily report you approve) is a role I can play from a Meta export each morning. No software needed until spend justifies it.

### 9. No bait-and-switch creatives.

- **What he said.** Vendors run "$27/month = $2M tax-free"-style hooks, and that kills stick rate. His creatives are "safe and smart".
- **OURS.** Already policy: no "$0 plan" claims, no government look. Keep it, and say it in buyer pitches.

### 10. Recruit buyers the way he recruits agents.

- **What he did.** A live webinar:
  - interactive Q&A with $100 for all-correct answers;
  - a booking link texted to registrants plus a QR code;
  - live-only bonuses;
  - a seat cap.

  His claim is $4 recruiting leads from ads.
- **OURS.** Our second bottleneck is a named licensed agency. A 30-minute agent-facing session, "Florida 2027: the auto-moved Cigna/Molina member and the January window," shows our trigger lead to Florida health agencies. **We skip** the replay-page countdown that restarts on every visit and the pressure on the seat cap. They're manipulative and would hurt us with buyers.

### 11. White-label and system licensing as a second revenue line.

- **What he does.** Sells the system rather than the leads, and lets agencies put their own brand on it as a recruiting tool.
- **OURS.** An option once we have proof: license the trigger-page engine plus the Check to one Florida agency for a flat monthly fee. The leads go to that agency only, which also makes buyer consent simpler. Needs a Florida licensing and compensation review first.

### 12. AI underwriting and quoting help on the call.

- **OURS.** That's the agent's side, not ours. Skip.

---

## What we reject from the pitch

1. **"Lead quality is completely random."** Wrong for us, and his own product contradicts it: trigger, creative and funnel friction (quiz, OTP) all shift quality. What's true is that platform *targeting* can't pick quality.
2. **His ECOA explanation** of Meta's targeting limits. The limits are real; the stated reason is loose.
3. **Every testimonial and number** (for example "$4,500 → $368,000 commission"). Unverifiable. Treat as marketing.

## What changed in the engine today

- **OTP verification:**
  - `engine/otp.py`
  - `/api/otp/send` and `/api/otp/verify`
  - the Check code step (EN/ES)
  - lead columns `phone_verified` and `phone_verified_via`
  - a funnel metric
  - the agent-desk badge
  - the payload flag
- **"What they saw"** in every delivery, plus "What they read" on the agent desk.
- **Tests:**
  - 36 unit tests, including one that proves a verification can't be forged or reused on a different number;
  - wave-2 E2E 21/21, including a browser run of the code step with a wrong code then the right one;
  - acquisition E2E 24/24;
  - Check E2E with 8 personas.

## Needs your approval

- **Twilio account for OTP:** about $0.05 per verification. Optional; the Check works without it.
- **First paid lead test:** about $30–50 a day for 7 days, sending traffic to the carrier-exit Check (Spanish, Miami-Dade, 15-mile minimum radius), separate from the $50 card boost.
- **Agent-facing webinar** to recruit a buyer agency.
