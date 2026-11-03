# Deployment readiness (controlled staging -> production)

Status and live blocker list: run `python launch_check.py`. This doc explains what to
configure and how to operate; **launch_check.py** decides what is still missing. Nothing
here authorizes a public launch, live payments, or live lead delivery — those are
operator approvals (categories below).

## 1. Environment separation (staging vs production)

- **`LEADENGINE_ENV`** is the master switch.
  - Unset/anything else = staging: demo buyers visible, dev settings allowed.
  - `production`: demo buyers hidden from real traffic (`is_demo`), strong `ADMIN_TOKEN`
    required, startup fail-safes enforced.
- Startup fail-safes (enforced at import time in `app.py` — bad settings abort with a
  clear `Refusing to start` message visible in the host's process logs;
  regression-tested in `tests/test_deployment_guards.py`):
  - production refuses to start with a missing/short/predictable `ADMIN_TOKEN`;
  - `sk_live_` Stripe keys refuse to start outside production (no accidental live rail in
    staging); unknown-prefix keys refuse to start in any mode;
  - `INTEL_DB` must be **empty** in production: the public-record prospect database never
    leaves this machine — regional briefs ship as `config/regional_briefs.json`.
- Test data: buyer test leads carry `is_test=1` and never enter financial or delivery
  totals. Production databases must start empty (`engine/db.py` creates the schema).

## 2. Environment variables

`.env.example` is the authoritative template. Required at first production start:

| Variable | Purpose | Guard |
|---|---|---|
| `LEADENGINE_ENV=production` | enables safety guards | startup |
| `BASE_URL` | links, QR codes, sitemap, consent copy. **Optional on Render**: `RENDER_EXTERNAL_URL` (the assigned `https://<name>.onrender.com`) is used when unset (`engine/hosting.py`) | launch_check 1 |
| `ADMIN_TOKEN` | admin auth (`/admin?token=...`), 16+ random chars | startup fail-safe |
| `LEADENGINE_DB` | SQLite path on persistent disk (`/var/data` Render, `/home/<user>/data` PythonAnywhere) | launch_check (consent records must survive deploys) |
| `LEADENGINE_STORAGE` | `persistent` (path is a mounted disk) or `ephemeral` (host keeps nothing → read-only public preview: consumer submissions, buyer applications and contact messages are refused, never stored and lost). A free Render service has no disk, so its blueprint declares `ephemeral` | launch_check / `/healthz` / Admin ▸ Readiness |
| `ALLOW_INDEXING` | `0` until counsel review; `1` enables search indexing | launch_check 8 |
| `BRAND` / `LEGAL_ENTITY` / `LOCATION` / `FOUNDED` | public identity (Gia Salud, Miami, Florida), footer + legal pages | launch_check 4 |
| `SUPPORT_EMAIL` (alias `CONTACT_EMAIL`) | contact page, privacy "write to", opt-out path. Leave empty until the inbox is verified — no fabricated address is printed | launch_check 4 |
| `MAILING_ADDRESS` | optional: not required for this website; becomes the CAN-SPAM postal address if consumer email is enabled | launch_check 4 |

The free public URL and its limits are documented separately in `docs/RENDER_DEPLOY.md`
(`render-free.yaml` = free, read-only preview; `render.yaml` = paid + persistent disk).

Deferred until the corresponding approval: `SMTP_*` (email delivery), `META_PIXEL_ID` /
`META_CAPI_TOKEN` / `GA4_ID` (paid-test tracking), `STRIPE_SECRET_KEY` /
`STRIPE_WEBHOOK_SECRET` (payment rail — see §4), `LEADENGINE_LIVE_DELIVERY=1` (see §5).
Do not set any of these "to be ready" — each one turns on money or data flow.

## 3. Database

- Single SQLite file (`LEADENGINE_DB`, schema in `engine/db.py`): leads, consent
  snapshots, transactions, buyers, partners, events. No server component.
- **Backups:** `python backup_db.py [dest_dir]` — hot SQLite backup, keeps the 30 newest
  copies; run daily from the host's cron (Render cron / scheduled job). Consent records
  must be retained 10 years (45 CFR 155.220(j)(2)(iii)).
- **Restore:** `python backup_db.py --restore <backup.db> <target.db> [--force]` — writes
  the chosen backup to the target and verifies `PRAGMA integrity_check`. A missing backup
  file is refused; an existing target is never overwritten without `--force`. To make the
  restored file live: STOP the app, restore (or copy over the live path), keep
  `LEADENGINE_DB` pointing at it, then start (Day-1 runbook §restore).
- **PythonAnywhere persistence:** the whole app + data live in your persistent home
  directory. On the FREE tier the web app itself must be renewed manually every month
  (Web tab → "Run until 1 month from today") — the code and DB survive, serving stops
  if you don't. Developer ($10/mo) removes the renewal. Details: deploy/GO_LIVE_CHEAP.md.
- **Restore:** stop the app, replace the DB file with the chosen `leads_*.db` backup,
  start. Do not edit the DB with external tools while the app runs.
- **Migration/rollback of code:** the schema is created idempotently at startup; rolling
  back to an older commit keeps the same database file.

## 4. Payment rail (test vs live cannot be confused)

- `engine/payments.py` derives the rail from the key prefix: `sk_test_` = test
  (bookings land in the Stripe **test** dashboard), `sk_live_` = live.
- `rail_approved()` requires live mode to have **both** `marketplace.payments.live_payments`
  in `config/marketplace.json` **and** `LEADENGINE_ENV=production` (AND-gate,
  `tests/test_deployment_guards.py` covers the OR-gap regression). `configured()` also
  requires the webhook secret.
- Idempotency: webhook granting is keyed by Stripe event id; a repeated or delayed event
  cannot double-credit (`tests/test_webhook_retry.py`, Pass 2 suite).
- Live activation requires **REQUIRES MY APPROVAL**: rotate real keys in the host's
  secret store (never in files), set `live_payments` deliberately, and confirm in the
  Stripe live dashboard. Nothing in this doc or the tests verifies live Stripe — that is
  **REQUIRES EXTERNAL VERIFICATION**.

## 5. Lead delivery

- `LEADENGINE_LIVE_DELIVERY` defaults to off: every delivery is a dry run written to
  `outbox/` (`engine/route.py::deliver()` and `engine/notify.py`); no consumer data
  leaves the server.
- Delivery runs only against buyers that are active and non-demo in
  `config/buyers.json`; unapproved/deactivated buyers are never routed (Pass 3 suite).
- Turning delivery on is an operator approval: signed agent agreement + legal sign-off
  first. Webhooks require HTTPS (enforced at all three entry points: public application,
  admin onboarding, portal settings).

## 6. Health, logs, monitoring

- `GET /healthz` returns `"ok": true` (plus `base_url` and `live_delivery` state) —
  Dockerfile HEALTHCHECK and render.yaml both use it.
- Structured events land in the `events` table; guard failures abort startup with a
  clear message.
- **Day-to-day operation, backup, delivery retries/resend, reconciliation and restore
  procedures:** `docs/DAY1_RUNBOOK.md` — the operator's practical runbook, verified
  against the code and rehearsed on isolated staging copies.

## 7. Deployment checklist categories

`launch_check.py` prints each group as one of:

- **READY** — verified in code/tests; nothing to configure.
- **REQUIRES CONFIGURATION** — depends on a future domain, host, or secret (domain,
  hosting, buyer record, tracking).
- **REQUIRES EXTERNAL VERIFICATION** — depends on a real external system (counsel
  review of consent/privacy; real Stripe live verification; CRM/ads later).
- **REQUIRES MY APPROVAL** — public launch actions: ad spend, contacting partners,
  enabling indexing/organic outreach, live payments, live delivery.

Sequence: domain + host configured → `launch_check.py` shows only approval/external
items → operator approval → go live. `deploy/GO_LIVE_CHEAP.md` and
`deploy/pa_setup.sh` cover the cheapest concrete host path.
