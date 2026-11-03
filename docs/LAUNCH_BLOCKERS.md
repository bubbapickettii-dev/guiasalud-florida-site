# Launch blockers (consolidated matrix — generated from the actual checks)

Read together with `docs/GO_LIVE.md` (deployment) and `docs/DAY1_RUNBOOK.md` (operations).
Regenerate the authoritative machine list any time: `python launch_check.py` — it reads
`config/` and the current environment and prints every group with its category.

## Categories

| Category | Meaning |
|---|---|
| OWNER INPUT | Information/config only the owner supplies; code cannot invent it |
| EXTERNAL VERIFICATION | A licensed buyer / counsel / platform not present in this environment |
| MY APPROVAL | Operator-run gate: flipping it is your deliberate decision, not a config bug |
| RESOLVED IN CODE | Software-side safety already enforced and tested |
| RESOLVED IN CONFIG | Configured, verified on the pages, nothing outstanding |

## Matrix (updated 2026-11-02)

| # | Area | Status | Category | What resolves it |
|---|------|--------|----------|------------------|
| 1 | Public URL | **pending** — no free Render service exists yet | MY APPROVAL (account) | Create the Render account, connect the repo, apply `render-free.yaml`; the assigned `https://giasalud.onrender.com` arrives in `RENDER_EXTERNAL_URL` and the app uses it for canonical URLs, hreflang, sitemap, robots and QR codes with no further configuration (`docs/RENDER_DEPLOY.md`) |
| 1b | Support inbox | **pending** — no verified mailbox | MY APPROVAL (account) | Create `giasalud.support@gmail.com` through Google's normal sign-up (availability can only be checked there), then set `SUPPORT_EMAIL`; until then `/contact` shows the on-site form and no address is printed |
| 2 | Hosting durability | free tier = read-only public preview | REQUIRES CONFIGURATION | The free Render plan keeps no disk, so the app refuses consumer submissions instead of losing them. Records need `render.yaml` (starter + `/var/data` disk) or PythonAnywhere `/home/<user>/data`. Flip `LEADENGINE_STORAGE=persistent` on such a host |
| 3 | Licensed agency (buyer) | **owner-confirmed: a licensed Florida agency is in place** — no longer an external blocker | MY APPROVAL (commercial) | Entering that agency's row in `config/buyers.json` (exact name for the consent, FL licence #, NPN, recorded checks, prices, cap, delivery) is how LEAD SALES start. The website does not depend on it |
| 4 | Legal review | consent v4 + privacy + terms not reviewed by counsel | EXTERNAL VERIFICATION (recommended) | Counsel pass over consent + privacy + terms (`docs/GO_LIVE.md` → questions for counsel) |
| 4b | Business identity | **configured:** Gia Salud, Miami, Florida, founded Miami, Florida | RESOLVED IN CONFIG | No street address is required for this website and none is invented (the public location stands in). No LLC/Inc. suffix, registration number or phone is asserted. `docs/BUSINESS_IDENTITY.md` |
| 5 | Tracking pixels | optional, absent | MY APPROVAL | None required for launch (first-party attribution works); GA4/Meta only with a paid test |
| 6 | Ad account | absent | MY APPROVAL | Only for paid tests; insurance Special Ad Category; budget is agent-funded |
| 7 | Partner outreach | 0 contacted | MY APPROVAL | You send the first pitch (`partner_kit/pitches.md`, Admin → Partners creates link/QR/guide) |
| 8 | Organic / indexing | `ALLOW_INDEXING=0` | MY APPROVAL | Flip at launch; then work `/admin/organic` |
| 9 | Payment rail | OFF (safe default) | MY APPROVAL | Stripe `sk_test_` + webhook secret enable self-serve TEST rail; LIVE needs `payments.live_payments=true` AND `LEADENGINE_ENV=production` AND explicit approval |
| 10 | Live lead delivery | OFF (safe default; dry run to `outbox/`) | MY APPROVAL | `LEADENGINE_LIVE_DELIVERY=1` only with a signed agent + legal sign-off |

## Resolved in code (evidence)

Consent-gated submissions (evidence + 10-year retention), no-buyer safe mode (funnel collects
nothing in production without a buyer), production guards (weak admin tokens, demo buyers, plain-HTTP
webhooks refused), charge-once / credit-once / deliver-once invariants, per-buyer isolation (404 across
tokens), test-data separation end to end, backup/restore with env-loss warning, IP throttle on real
lead submissions, signed webhooks — all enforced and covered by the test suite (`python -m pytest -q tests`).

Added 2026-11-02 (see `docs/LAUNCH_CORE_2026-11-02.md`): credit-before-routing is automatic on a live
deployment (no unfunded buyer can be named in a consent), an existing opt-out can never be sold a new
lead, consumer contact messages are stored + notified + readable by the operator (empty ones are
refused instead of falsely acknowledged), operators edit buyers from the pre-filled form without
resetting tiers/prices/caps, portal-link rotation is a dedicated action, and robots/canonical/sitemap
can no longer advertise a staging origin or contradict each other.
