# Buyer validation package (preparation only — nothing has been sent)

> Operational counterpart: `docs/FIRST_BUYER_PILOT.md` — the offer, admin onboarding checklist and the
> staging-safe validation sequence once a prospect answers these questions.

For the exclusive Florida health-insurance lead pilot. Everything here is a question or a
draft to review; we have **no validated pricing, no committed buyer, and no market evidence
beyond what is cited from public sources** in `docs/LEAD_CAPTURE_REQUIREMENTS.md` (vendor-authored
published ranges, marked UNVERIFIED — we do not represent them as facts about our leads).

## 1. What we need from a prospect (the discovery checklist)

Ask all eight; the viability score at the bottom needs 1–4, 6a, and 7 answered honestly.

| # | Question | Why it decides the pilot |
|---|----------|--------------------------|
| 1 | How many health-insurance **phone leads** do you buy per week today? | Capacity to absorb a pilot dose (we start at 2–5 leads/week). |
| 2 | What do you **pay per lead**, and per what (exclusive vs shared, tier, state)? | Anchors pricing against their current invoice, not our wish. |
| 3 | Which **suppliers** do those leads come from now? | Tells us whether we compete on price, quality or terms. |
| 4 | What problems do you have with those leads? Record verbatim (bad numbers, echoes, unreachable, wrong state, slow delivery, no proof of consent…). | Each confirmed pain is a feature we already have or must fix. |
| 5 | Of the leads you receive, what share do you actually **contact** and what share become appointments? | A rejection-capable enterprise needs a realistic acceptance window. |
| 6 | (a) What is your **return/credit rule** with current suppliers — window, which statuses count, who decides? (b) What proof do you need for returns (recording, timestamp)? | Our configured return policy must match their terms before the first sale, or the pilot sourers on day one. |
| 7 | Do they insist on **exclusive** leads, and would they pay more for verified exclusivity with consent proof attached? | Our initial product is exclusive-only; if they want shared packages we say "not yet" honestly. |
| 8 | What are your **lead criteria** (state, age band, plan type, language, income band) and delivery method (email, webhook, portal)? | Maps 1:1 onto `config/buyers.json`; a prospect we cannot serve with the existing fields is not a pilot yet. |

## 2. Initial outreach message (draft — needs approval, then goes out by the operator, not code)

> Subject: Exclusive Florida health leads — one pilot question
>
> Hi {first_name},
>
> I run a small Florida lead platform that qualifies health-insurance shoppers via a
> consumer-initiated coverage check, and sells each lead **exclusively** — one lead, one agent,
> with the consumer's written consent naming you.
>
> Before I pitch anything: I'd rather learn how you buy today. Two questions:
> 1) How many health/ACA phone leads do you buy per week, and roughly what do you pay?
> 2) What do your current leads do worst — bad numbers, echoes, unreachable people?
>
> If your answers match what we can genuinely deliver, I'll propose a paid pilot of
> 2–5 exclusive leads with your own return rules pre-agreed in writing. If they don't,
> I'll tell you honestly and won't waste your time.
>
> {sender_name}

Rules recorded here so they aren't lost: numbers in the message are questions, not claims;
no price is quoted before question 2 is answered; nothing in it promises volume ("2–5 leads"
is a *proposed pilot dose*, not a forecast).

## 3. Recording responses (one row per prospect)

Keep it in `docs/BUYER_VALIDATION_LOG.md` (create on first real response) or any CRM row with exactly
these fields — no more, so the log stays fillable:

`date · name · firm · contact · channel · q1_leads_per_week · q2_price_per_lead · q3_suppliers ·
q4_problems_verbatim · q5_acceptance_rate · q6_return_terms · q7_exclusive_willingness ·
q8_criteria_and_delivery · viability · next_step · owner`

## 4. Pilot-viability decision (apply mechanically, do not overrule)

A prospect is a viable pilot buyer when ALL hold:

1. Buys at least 2 FL health phone leads per week today, or explicitly wants to.
2. Tells us a concrete current price per lead (any number > $0 we can anchor to).
3. Names at least one concrete dissatisfaction (else we are a price-only conversation and lose).
4. Exclusive is acceptable, or they want shared (record "future shared package" — do not sell it).
5. Their return terms fall inside what `config/marketplace.json` `returns` can express without code changes
   (or we deliberately reconfigure the terms before the pilot — operator decision, never invented per-deal).
6. They can receive leads the way we can deliver: portal `/agent/<token>`, outbox JSON, or email.

Anything else → not yet; revisit only with new information.

## 5. Honest status (read this before sending anything)

- The test-mode payment rail (`engine/payments.py`) and prepaid credit ledger are built and tested with stubs
  (Stripe keys not configured: no live purchase has ever run end-to-end).
- No real buyer has been contacted, no response log exists, no pricing validated, and the
  "~$1,000/week buyer" remains an unverified hypothesis from a private conversation.
- Before ANY buyer money moves: configure test-mode Stripe, run one full test-mode purchase against a
  staging DB, and record the first credit pack. Credit-before-routing is automatic on a live
  deployment, so an unfunded buyer cannot receive a real lead regardless of the config switch.
