# Business model (source of truth, Oct 5, 2026)

We are a **health insurance lead generation company**, not an outbound agency. The product is an inbound lead: a consumer who showed insurance intent and voluntarily gave us their phone number and consent, so one licensed agent can follow up. The agent pays for that lead.

The operating question: **how do we get more voluntary, high-intent submissions with valid phone numbers?** The question is never "how do we contact more people?"

## The flow

```
PUBLIC DATA / MARKET INTELLIGENCE  (../leadgen)
  -> which audiences and circumstances create demand (new 1099 licenses, new authorities, new businesses, layoffs)
  -> where those audiences can legitimately be reached (schools, brokerages, agencies, employers, communities, creators, search)
  -> content / offers / resources put in front of them (Distribution Lab experiments, partners, /learn pages)
CONSUMER voluntarily clicks -> completes the Health Coverage Check -> sees their summary
  -> types their own phone number (validated inline) -> checks consent naming ONE licensed agency
WE validate + qualify -> lead stored (phone_source = CONSUMER_SUBMITTED) -> delivered to that agency
AGENT calls -> marks the outcome on the desk -> Funnel / Lab / Quality measure it -> we shift effort to the sources that produce sold leads
```

## Three kinds of data. They never mix.

| | What | Used for | Never used for |
|---|---|---|---|
| 1. Public prospect data | Licenses, carrier authorities, NPIs, filings, layoff notices (`../leadgen/data/leadgen.db`) | Audience sizing, where demand is, which language/message/content, which organizations reach them, partner discovery, score context once someone submits on their own | Contacting anyone. No mail, email, calls, texts or DMs. Contact fields are not scored, not exported and not enriched. Not deployed to the server |
| 2. Distribution / partner data | Schools, brokerages, agencies, employers (WARN), communities, creators, newsletters | Getting our resource in front of the audience (Admin > Partners). Partners see counts only | Receiving any consumer's information |
| 3. Inbound consumer leads | A person who completed the Check and submitted their own phone + consent | The product. Delivered to the one agency named in their consent | Reselling, sharing with anyone else, swapping in any other number |

## Phone provenance: where it's enforced

| Rule | Where |
|---|---|
| The lead's phone is the number the consumer typed; nothing else can be stored | DB trigger `leads_phone_must_be_consumer_submitted` (insert aborts unless `phone_source='CONSUMER_SUBMITTED'`) |
| Once stored, the phone can't be edited or swapped | DB trigger `leads_phone_immutable` |
| A phone typed without consent is not kept | `qualify.qualify` only stores a phone when the consent checkbox naming the agent was checked |
| Analytics never carry phone numbers | `/api/event` strips phone/email keys and 7+ digit runs. Phone steps are logged as pass/fail only |
| The intelligence layer never reads public phones/emails | Unit test greps `engine/intel.py`. `leadgen` exports are aggregate-only |
| Only consented phone leads are sold | `route.route` refuses anything that isn't `lead_type=PHONE_LEAD` with a phone |
| No agent available means we don't ask for a phone | The Check hides "Yes, help me compare" and offers the summary by email instead |

## Lead record (what the agent buys)

| Field | Meaning |
|---|---|
| `lead_type` | `PHONE_LEAD` (sellable) or `EMAIL_ONLY_INQUIRY` (no phone; never routed or sold; gets only the one summary email they asked for) |
| `phone`, `phone_e164` | The consumer's own number (national and +1 format) |
| `phone_valid`, `phone_check` | Structural NANP validation result and reason (`ok`, `not_10_digits`, `toll_free_not_personal`, `n11_service_code`, `junk_pattern`...) |
| `phone_area_region` | `FL` or `US_OTHER` area code. A flag, never a rejection |
| `phone_line_type`, `phone_line_type_source` | `UNKNOWN`: line type needs a paid carrier lookup, which isn't connected. A hook exists in `engine/phone.py` |
| `phone_source` | Always `CONSUMER_SUBMITTED` |
| `consent_ts`, `consent_version` (+ `consents` table) | When they agreed, the exact text, the agency named, IP, user agent, page, SHA-256 of the form snapshot |
| `landing_url`, `campaign`, `utm_*`, `tracking_id`, `experiment_id`, `partner_id` | Where the lead came from |
| `created_at`, `state`, `zip`, `lang` | Lead timestamp, location, language |
| `need_path`, `household_size`, `income_band`, `enrollment_situation`, `urgency`, `age_range` | What they told us (no health conditions, SSN or immigration status) |
| `qual_status`, `tier`, `score` | Qualification |
| `routing_status`, `buyer_id`, `delivered_at`, `price` | Delivery status, agent, revenue |
| `agent_status` + `lead_outcomes` | Agent-reported outcome: CONTACTED / APPOINTMENT / SOLD / NOT_QUALIFIED / BAD_INFO / DNC / OTHER |

## KPIs (Admin > Funnel, by source / experiment / angle / language / partner / campaign; real vs TEST)

visits → form starts → Check completed → **phones submitted → valid phones → consented phone leads** → qualified → delivered → contacted by agent → appointments → sales → **revenue per lead**

Not KPIs: number of public records, public phones or emails, or anything about contacting people.

## Levers for more valid phone submissions (all UNTESTED until real traffic)

| Lever | Built | Test next |
|---|---|---|
| Value before the ask: summary shown before any contact field | Yes | Result copy per angle |
| One clear reason for the phone ("a licensed agent checks real 2027 plans and prices with you; only the agency you approve gets it") | Yes | Wording variants |
| Fewer fields: email optional on phone leads | Yes | - |
| Inline validation + auto-formatting, plain-language errors (toll-free, missing digits) | Yes | - |
| Phone or text preference, 8am-8pm time choice | Yes | - |
| Spanish-first flow ("tú") | Yes | ES vs EN phone rate in Miami-Dade |
| Email-only fallback | Yes | On vs off: does it cannibalize phone leads? (Lab experiment) |
| Agent's name/photo on the consent screen (trust) | No | Needs a real agent |
| SMS code to confirm the number | No | Would mean texting consumers through a paid provider. Needs your approval and counsel's view |
| Carrier line-type lookup (mobile vs landline) | Hook only | Paid API, needs approval |

## What changed from earlier versions (Oct 5)

- The v1 public-data engine scored people on whether a public phone or email existed, flagged them for mail, email or manual dialing, and appended phone and email from the voter file. That was the wrong business. All of it is removed: contactability isn't scored, channel flags are always 0, voter enrichment takes DOB only, and exports are aggregate-only (`Market_Intelligence.xlsx`).
- Superseded files: `Lead_Engine_v1.xlsx` and `lead_engine_prospects_2026-10-03.zip` in Downloads\lead_engine are v1 contact lists. Don't use them; deleting them is your call.
- The Channels matrix marks contacting public-record individuals as REJECTED.
- Campaign follow-ups no longer include email sequences. An email-only inquiry gets exactly the one summary it asked for.
