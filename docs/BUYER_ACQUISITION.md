# Buyer acquisition — prospect to onboarded ally (implemented Oct 27, 2026)

How a PUBLIC-RECORD business becomes a buyer who can purchase exclusive health-insurance
leads — with evidence at every step and nothing auto-activated.

## The lifecycle (`engine/prospects.py > STAGES`)

```
 NEW ──► RESEARCHED ──► QUALIFIED (why required) ─► READY_FOR_ONBOARDING
        \                                    \
         └──► DISQUALIFIED (why required)     └─►  HANDOFF (explicit confirm)
                                                   │
                                                   ▼
                                    INACTIVE portal-less buyer draft `/admin?f=<bid>`
                                                   │  (existing onboarding: license, NPN,
                                                   ▼   prices, caps, delivery, token)
                                     ONBOARDED ──► ACTIVATED (only by the real
                                     gate-checked flow; mirrored back automatically)
```

- Stages are set by the operator with a REQUIRED reason for QUALIFIED / DISQUALIFIED /
  READY_FOR_ONBOARDING. `ONBOARDED`/`ACTIVATED` are written by the real flows only —
  never by hand.
- Every transition, every field edit (with the changed value), every verification
  decision is an append-only row in `prospect_log` (Admin → Buyer prospects → full record).

## Verification is evidence, never import

`verify_state` moves ONLY through an explicit operator record naming what was checked and
where/when. Importing CSV rows, Places data, or word-of-mouth never changes verification.
`OPERATOR_VERIFIED` is required (together with a `QUALIFIED` decision and a recorded
service territory) before the handoff gate opens.

## Commercial qualification (only what is actually known)

Per-prospect fields: interested (yes/maybe/no/unknown), lead types, service territory,
stated weekly volume, stated budget (CLAIMS — the ~$1,000/week figure remains an
unverified hypothesis, never a default commitment), delivery preference, proposed price
(our side), agreed price (only with a real agreement), agreement status (none/drafted/
signed), open questions, follow-up note, next action + date. Blank = genuinely unknown;
the UI renders unknown as unknown. Deduplication is enforced against BOTH existing
prospects (website/name+ZIP at ingest) and existing buyERS (website/name at handoff).

## The handoff (`POST /admin/prospects/<id>/handoff`)

Refuses (naming exactly what is missing) unless: decision QUALIFIED · verify_state
OPERATOR_VERIFIED · stage QUALIFIED-or-READY · territory recorded · no duplicate buyer
(same website or business name) · typed confirmation `confirm=convert`. On success it
creates an INACTIVE, portal-less buyer draft carrying forward only recorded facts, links
prospect→buyer and buyer→prospect (`converted_from_prospect`), records `ONBOARDING_STARTED`,
logs the buyer-pipeline trail (APPLIED + operator handoff note), and redirects to the
prefilled onboarding form `/admin?f=<bid>`.

Activation remains impossible except through `Admin → Buyers` with all existing gates
(`_onboard_errors`: exact legal name, license number, NPN, portal token, served states,
positive prices, caps, valid delivery) — and when a handoff-created buyer is actually
activated, the originating prospect records ACTIVATED automatically. The link stays
visible on the buyer side too: the buyer row and the onboarding form both show
"via prospect #N" back to the original record.

## Where things live

| Piece | Where |
|---|---|
| Registry + lifecycle + handoff logic | `engine/prospects.py`, `data/prospects.db` |
| Listing (stage funnel, filters, attention flags) | `/admin/prospects`, `templates/admin_prospects.html` |
| Full record (history, edits, gate, handoff) | `/admin/prospects/<id>`, `templates/admin_prospect.html` |
| Prospect detail: readiness checklist (operator-provided vs independently verified vs pending review), resume-handoff, buyer status + return link | `app.py _prospect_handoff_ctx`, `templates/admin_prospect.html` |
| Daily workspace listing: buckets, sort, inline decide/verify, disqualification reasons | `app.py admin_prospects_list`, `templates/admin_prospects.html` |
| Buyer pipeline trail + origin link + commercial funnel strip (prospect stages + buyer stages in one view) | `/admin/buyer-pipeline`, `engine/buyer_pipeline.py` |
| Buyer onboarding/activation (unchanged ladder: license/NPN/delivery checks, explicit activate button) | `/admin?f=<bid>`, `/admin/buyers` in `app.py` |
| Tests | `tests/test_prospects.py`, `tests/test_prospect_handoff.py` (env-autouse), `tests/test_buyer_pipeline.py` |
