# What must exist before the first contact form collects anything (researched 2026-10-09)

Facts below come from a source-cited research pass (statutes, CMS, FCC, FTC, vendor terms). It is not legal advice; it is the brief for a lawyer. Items marked UNVERIFIED were not confirmed in a primary source.

## Biggest risk: the Check result screen
Fla. Stat. 626.112(1)(b) defines "solicitation" to include "describing the benefits or terms of insurance coverage, including premiums". DFS treats unlicensed insurance activity as a felony. Showing premium or subsidy amounts, plan comparisons or carrier suggestions could be read as unlicensed solicitation (this reading is an inference, not a published ruling). Today the result screen shows dates, official-source guidance and a generic "you may qualify for premium help" range, with no premiums, plans or carriers recommended. Before any contact field ships, counsel should review the result text. Do not add premium estimates or plan lists.
A flat per-lead fee is fine under 626.112(8) ONLY if it is not "dependent upon whether the referral results in the purchase". No bonus, clawback or rate tied to enrollment.

## Consent (TCPA / Florida FTSA)
- FCC one-to-one consent rule was vacated by the 11th Circuit on Jan 24, 2025. Written consent that "clearly authorizes the seller", includes the phone number and says consent is not a condition of purchase is still required (47 CFR 64.1200(f)(9)).
- Revocation by any reasonable method within 10 business days is in force (Apr 11, 2025); the "revoke all" piece is delayed to Jan 31, 2027.
- FTSA (s.501.059): signed written consent (a checked box counts) for autodialed/prerecorded sales calls; texts need STOP honored (15-day window is a pre-suit cure period, not the standard); damages $500/violation. Calling limits 8am-8pm and 3 calls/24h (s.501.616(6)) bind the buyer; put them in the buyer contract.
- Naming ONE seller remains market practice (Integrity lead guidelines).
- Gap in consent v4: it does not name us as the collecting party. Draft v5 for counsel: add "{brand}, operated by {legal_entity}, collects this request and will share it only with {seller}", the phone number being consented, and a revocation line. Do not switch the live text until counsel agrees.

## CMS / Marketplace (buyer side)
Agents must document consumer consent (45 CFR 155.220(j)(2)), keep it 10 years; CMS says an online checkbox "would likely not be sufficient" for enrollment consent, so the agent re-collects consent at first contact. Our lead consent covers contact only. CMS tells agents to vet lead generators and avoid leads sold to several agents.

## Buyer acceptance criteria (market practice, not law)
Exclusive or shared labeled; timestamp and source on every lead; consent proof (TrustedForm/Jornaya) on request; DNC/litigator scrubs by buyer; credits for disconnected phone, fake data, wrong state, duplicates (typical windows: 30-day duplicate, returns reported within about 7-10 days; ACA-specific window UNVERIFIED). Voicemail/no callback/"did not buy" are not returnable. Published price ranges (vendor-authored, July 2026): ACA shared $10-30, exclusive $30-80. Not a quote for our leads.
What we already have: 30-day duplicate suppression (engine/qualify.py), RETURNED/BAD_INFO/DNC statuses, consent snapshot with hash.

## Minimum data to collect
First name, last name, phone (verified format), ZIP/state. Optional context already asked: need, age range, household size, income range. Never SSN, health conditions or immigration status. Quiz answers are health-adjacent: keep Meta Pixel and GA4 OFF health/quiz pages (FTC GoodRx/BetterHelp precedents; Meta business tools terms bar health data). If TrustedForm/Jornaya are used, scope them to the contact form only (UNVERIFIED how they treat quiz answers).

## Privacy notice
Done for the no-buyer phase (templates/simple.html, EN+ES): entity, address, contact, what is stored, no ad trackers, retention, change notice. Lead-phase text is also in the template and switches on when a real buyer is configured; it states the one named agency, flat per-lead fee not tied to a purchase, and 10-year consent record. Florida Digital Bill of Rights thresholds ($1B revenue) likely do not apply, but s.501.715 (consent before selling sensitive data) has no threshold: do not sell quiz answers.

## Preconditions checklist (all must be true before a phone field goes live)
1. Signed buyer agreement: flat fee not tied to a sale; buyer's TCPA/FTSA/501.616 compliance and own CMS consent at first contact; return rules and duplicate window; opt-out and DNC propagation within 10 business days.
2. Real legal entity, address and monitored email on the site (set via environment variables on the host).
3. Consent text with collecting brand, ONE named agency, the phone number, "not a condition", revocation, link to policy; counsel-approved; set consent_reviewed_by_counsel true.
4. Result screen reviewed against 626.112(1)(b).
5. Retention period chosen and stated (10 years for consent records).
6. Opt-out handling live (/optout) and tested; duplicate rule on (30 days).
7. No ad pixels on health pages.
8. A named licensed buyer configured in config/buyers.json (legal agency name exactly as in consent, license, NPN, 2027 FFM registration, delivery method, cap, flat fee).
UNVERIFIED across all: what a specific Marketplace-registered agency demands in its vendor contract; statutes of limitations; Premom specifics.
