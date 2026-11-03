# First-buyer pilot playbook (preparation — nothing has been sent or activated)

One document for taking a legitimate licensed Florida agency from "prospect" to "first real pilot
lead". Every step names the exact route or command that exists today; anything still external is
listed at the end. Prospecting/discovery questions live in `docs/BUYER_VALIDATION.md`; deployment
belongs to `docs/GO_LIVE.md`.

## 1. The offer (what GS Allude actually is — no invented claims)

We generate **exclusive, consented health-insurance phone leads in Florida**:

- A consumer completes the free Health Coverage Check (`/check`, EN + ES), types **their own phone
  number** (never assumed from a list), and reaches a consent screen naming **one** licensed agency.
- On consent we record an auditable consent evidence package: on-screen text version, timestamp,
  page, IP/browser, and a SHA-256 form snapshot — kept 10 years per 45 CFR 155.220(j)(2)(iii).
- Every lead is **exclusive**: one consumer, one consent, one buyer — never resold or shared.
- The lead arrives at the moment of sale by portal, email, or your CRM **webhook** (POST, freshly
  signed with `x-ticket-signature`, HMAC-SHA256 of the body) plus a consumer summary email.
- **Prepaid credits, not invoicing**: each delivered lead charges your balance at your agreed
  per-tier price (a flat fee per lead — never tied to whether the consumer buys).
- **Returns have written rules** (config-driven, shown to you on every lead): a delivered lead may
  be returned for a balance **credit** within the agreed window (72h default, per-deal override
  possible) before any contact is recorded. A refused return is never charged back; it is recorded
  with its reason and reviewed by the operator.
- Your outcomes train the system: sources that produce unreachable or unqualified people are cut;
  clean desks get priority in routing. **Do-not-contact is honored system-wide** when flagged.
- Guarantees you can hold us to: a lead is charged once; a return credits once or not at all; a
  failed webhook is retried with the exact recorded payload (never new data, never a second sale);
  and you can download your full ledger (`/agent/<token>/billing/statement.csv`).

What we do **not** claim anywhere: any conversion rate, any price benchmark, or that our leads sell
policies. Success metrics during the pilot are your recorded status outcomes, and nothing else.

## 2. Buyer onboarding checklist (admin side, in order)

1. **Capture intake.** Prospect discovery → `docs/BUYER_VALIDATION.md` questions (weekly phone-lead
   volume, current CPL, return rule, exclusivity requirement).
2. **Get them to apply.** Give them the public link `/for-agents`. The application records what they
   **supplied** (agency, license #, NPN, states, products, expected volume, delivery preference,
   webhook/email).
   *Blocker: the buyer themselves — do not invent an application.*
3. **Review + configure the draft** at Admin → Buyers (`/admin/buyers`, textarea of the Applicants'
   fields, edit token `t` in staging):
   - legal name **exactly as it should appear in consumer consent text**
   - FL 0215/2-40 license number, NPN, licensed states, products (`health_aca`), tiers + per-tier
     prices (price > 0 required for activation), daily/monthly caps
   - delivery: desk / email / CRM webhook URL (https only)
   - portal link: **copy privately, never email/log it**; rotate with `action=rotate_token` if exposed.
4. **Verify independently — record what you actually checked** at `/admin/buyer/<id>/verify`:
   FL DFS license lookup (`ref` = what you saw + date), NPN lookup, `delivery fan-out` (send the
   "webhook test ping" button next to their row first), `contact reached`. Nothing here activates
   a buyer; verification and activation are separate operator actions.
5. **Test delivery before activation.** Webhook buyers: fire the signed test ping (no consumer
   data). Desk/email buyers: confirm the address/menu.
6. **Activate** (`action=activate` — refused while `_onboard_errors()` is non-empty: missing legal
   name/license/prices block it). A webhook signing secret is issued once at activation.
7. **Record the first credit pack before the buyer can receive a real lead.** A live deployment
   (`LEADENGINE_ENV=production`) always enforces credits on real traffic, whatever `marketplace.json`
   says: an unfunded buyer is excluded from the routing preview, so a consumer is never told someone
   can take their request when that buyer cannot pay. In staging the `enforce_credits` switch stays
   OFF so rehearsals and the fixture suite never stall on an empty balance.
8. **Rehearse** the funnel with TEST data (validation sequence below) before the consumer is real.

## 3. First-buyer validation sequence (staging-safe, $0)

**One command:** `python tools/pilot_rehearsal.py` walks the entire sequence below in an isolated
throwaway app (own temp database + own config copy; live delivery OFF, Stripe rail OFF, TEST leads
only) and prints a PASS/FAIL checklist — buyer draft, independent license/NPN verification,
activation + portal link, TEST lead routed to the rehearsed buyer with exactly one charge, desk
outcomes, in-policy return credited once (repeat clicks can't double-credit), late return refused
→ `/admin/credits`, operator grant/deny, stranded-ROUTED-sale heal, and the ledger reconciling.
Use it before a real pilot; the manual walk below stays valid when you want to demonstrate each
step by hand in the live staging app:

1. `python launch_check.py` — confirms every remaining config blocker by group.
2. Onboard the buyer per §2 with TEST-safe fields; leave `active=true` but keep
   `LEADENGINE_LIVE_DELIVERY` OFF (every delivery is a dry run to `outbox/`) and the Stripe rail OFF.
3. Open `/check?a=health_check?test=1` (or press the TEST flag in QA mode `/qa`), submit a full
   fake request → verify: routed decision recorded, lead appears on the buyer's desk
   (`/agent/<token>`), payload lands in `outbox/`, `buyer_ledger` shows exactly one `LEAD_CHARGE`.
4. Buyer portal rehearsal from the buyer's seat: mark VIEWED → CONTACTED → SOLD; then a second
   TEST lead → RETURNED inside the window → verify `LEAD_CREDIT` posts exactly once; a third →
   RETURNED after the window → verify it lands in `/admin/credits` with its refusal reason and
   **no** ledger credit until granted.
5. Check recovery: reroute a `ROUTED` lead from Admin, deliver a stranded sale from the alert
   panel, resend a failed webhook from `/admin/lead/<id>/resend` — each must heal without a
   duplicate sale or double charge.
6. `python -m pytest -q tests` — full suite green before anything real.
7. Only after steps 1–6: production-approval items (`docs/LAUNCH_BLOCKERS.md`) are opened with the
   buyer themselves — day-1 operations follow `docs/DAY1_RUNBOOK.md`.

## 4. What still blocks the first real pilot (owner input)

| Blocker | Owner/external |
|---|---|
| A real licensed agency + signed agreement | You / the buyer |
| Legal entity, contact & mailing address (`LEGAL_ENTITY` etc.) | You |
| Counsel review of consent/privacy/terms (recommended) | External legal |
| `LEADENGINE_LIVE_DELIVERY=1` for real consumer data | Your approval |
| Payment rail live mode (`payments.live_payments` + `LEADENGINE_ENV=production`) | Your approval |
| Public hosting/domain + `ALLOW_INDEXING=1` | Your approval |
| First traffic source (partner outreach, lists, ads) started by you | You |

Nothing above is completed by code alone; everything else the pilot needs is built and rehearsed.
