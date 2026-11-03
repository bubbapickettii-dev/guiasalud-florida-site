# Day-1 operator runbook

Everything here is taken from the actual implementation (`app.py`, `templates/admin.html`,
`engine/route.py`, `engine/billing.py`, `backup_db.py`, `launch_check.py`,
`docs/DEPLOYMENT.md`) and the regression suites in `tests/`. Nothing below sends anything,
spends money, activates live payments, enables live lead delivery, or publishes the app:
the runbook assumes its staging defaults (`LEADENGINE_LIVE_DELIVERY` off, payment rail off
or test) and says so where a step would change that.

Path conventions:

- **Production host:** `python` from the app root, `LEADENGINE_DB=/data/leads.db`,
  ledger.sqlite3 CLI if present (else the Python one-liners shown).
- **Windows staging (S: drive):** `.venv/Scripts/python.exe` from
  `staging_source/leadengine_staging`, set `LEADENGINE_DB` explicitly — the staging DB at
  `data/leads.db`. Without that env var `backup_db.py` falls back loudly (see §2) so a
  cron or shell can never silently snapshot the wrong file.

## 1. Preflight — reading `launch_check.py`

```
python launch_check.py
```

Reads `config/*.json` and the current environment only; never contacts anyone. Output
format is one `== N Group:` line with `Base URL:` first, e.g.:

```
== 2 Hosting: [REQUIRES CONFIGURATION] 3 item(s)
   - LEADENGINE_ENV is not 'production' (guards...)
```

| Label                                | Meaning                                      |
| ------------------------------------ | -------------------------------------------- |
| `READY - verified in code/tests`    | nothing to configure for this group          |
| `REQUIRES CONFIGURATION`            | blocked on a domain, host, or secret         |
| `REQUIRES EXTERNAL VERIFICATION`    | blocked on an outside system (counsel, etc.) |
| `REQUIRES MY APPROVAL`              | an operator decision (money / contact people / launch) |

Day-1 rule of thumb: configuration groups are work you were always going to do; things
labeled "MY APPROVAL" stay open *on purpose* — flipping `ALLOW_INDEXING`, ad spend, partner
pitches, live payments and delivery are deliberate operator decisions, per
`docs/DEPLOYMENT.md` §7. On any day, the payment group must read `mode=off` or `mode=test`
with `approved=False` and `webhook secret set=False` — `mode=live`/`approved=True` before
you decided means a misconfiguration, not progress (`rail_approved()` is an AND of
`marketplace.payments.live_payments` + `LEADENGINE_ENV=production`,
`tests/test_deployment_guards.py::test_rail_approval_is_and_not_or`).

## 2. Database backup — the first thing to do, always

```
LEADENGINE_DB=/data/leads.db python backup_db.py [dest_dir]
```

- **Exact output:** a single line — the absolute path of a new `leads_YYYYMMDD_HHMMSS.db`
  in `dest_dir` (default: `data/backups/` next to the DB). It keeps the 30 newest copies
  automatically.
- **A run that prints `WARNING: LEADENGINE_DB not set; backing up the DEFAULT local path:`
  on stderr is not a production backup** — the env a cron job needs was lost. Rerun it with
  the env set; never file that copy with real archives/recovery points.
- A nonexistent source aborts outright with `Refusing to back up: source database not
  found: ...` — an empty or wrong DB must never become something that looks like a restore
  point.

Verify the new file immediately (Windows staging has no sqlite3 shell; the Python one-liner
works everywhere):

```
python -c "import sqlite3;print(sqlite3.connect(r'backups/leads_YYYYMMDD_HHMMSS.db').execute('PRAGMA integrity_check').fetchone()[0])"
```

Expected: exactly `ok`. Then eyeball row counts one-liner per table against the live DB
(`leads`, `consents`, `buyer_ledger`); no zero-row backup on a day that had work. All three
guarantees above come from `tests/test_backup_restore.py` (hot copy keeps every row,
including the Unicode consent snapshot: consent records must survive 10 years under
45 CFR 155.220(j)(2)(iii)).

## 3. Buyer eligibility, pricing, credits and routing readiness

Open `/admin?token=...` → **Buyers (routing)** table. Each row shows the gates routing
uses (`app.py:admin()` builds `bstat` from the same fields `engine/route.py:preview()`
filters on): today / cap, month / monthly cap, delivered total, credit balance (real / TEST),
verification pills, delivery method, portal token, Activate / Deactivate.

What `route.preview()` actually requires before ANY consumer can be consent-sold to a
buyer: `active: true` and not demo; licensed in the state; trades the product and tier;
under daily and monthly caps; real balance above the credit floor (always enforced on a live
deployment, switch-controlled in staging; a drained
balance reads "lacks prepaid credits" — the consumer sees NONE of this, they just see no
availability); not health-blocked by recent outcomes; and a **positive `price_by_tier`
entry for the requested tier** — a zero/missing price excludes the buyer outright since the
billing defect was fixed (previously an unpriced tier would deliver for free silently;
now it refuses with reason "carries no positive price for tier X" in `routing_decisions`).

Live per-lead availability during the actual funnel: `GET /api/route-preview` takes
`campaign=<campaign-id>` (the campaign id, e.g. `oep-2027-health` — verify the real ids
under Admin > Distribution Lab or `config/site.json`; an unknown id returns 404), plus
`state` and `age_range`. It answers `available` (true = at least one buyer passes every
preview() gate) and serves the current consent text only — it never names a buyer on
purpose (the consumer consents to the *platform*; the assignment is platform-controlled at
submit time, never chosen or leaked in the browser).

## 4. Reading the "Sale integrity — needs attention" panel

The `/admin` alerts table has five rows it can print; each one has a distinct meaning
(`app.py:admin()` builds exactly this dict, `admin.html` renders it):

| Alert row                    | What it tells you                                                                                                       |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `Stranded sale`               | sale committed (paid consent, buyer assigned) but delivery did not finish — the buyer's desk retries it on load; if the buyer stays inactive nothing heals it → click **Deliver now** (§5) |
| `Webhook out of retries`      | the buyer's CRM still hasn't received a SOLD lead — heavy retry budget exhausted (5 failures); fix the CRM, then either click **Re-post original payload to buyer's webhook** on the lead page or call the buyer (§5) |
| `Negative balance`            | a buyer's real-lead ledger is overdrawn (enforce_credits off or a dispute arrived sideways) → next credit pack / ledger adjustment (§6) |
| `Payment stuck pending`       | a self-serve checkout was never confirmed by the Stripe webhook → verify on Stripe's dashboard FIRST, then **Mark resolved** with what you saw (§6) |
| `Buyer email failed`          | the new-lead notification email bounced/SMTP error — the sale stands and the portal still has the payload; warn the buyer |

Failed **notification** vs failed **sale**: a lead with `routing_status='DELIVERED'` whose
`deliveries` show `FAILED` is an undelivered *notification*, sale intact — never re-offer a
lead that was already sold (every resend endpoint refuses a non-DELIVERED lead for exactly
that reason). A lead stuck in `ROUTED` (not yet DELIVERED) is a *stranded sale*, not an
unsold lead.

## 5. Retrying and resending delivery — nothing here can re-sell

- **Unmatched lead** (`Unmatched — no eligible buyer` list, per-row **Try again**, or
  **Run sweep now** for the whole queue): re-runs ONE platform-controlled assignment for the
  NO_BUYER lead. Audited per attempt in Distribution decisions; never touches a
  delivered sale. Staging refuses nothing here without a real buyer, so this just records
  another `NO_BUYER` decision.
- **Stranded sale** → per-row **Deliver now** on the alert: re-runs `_sell_lead` for the
  SAME assigned buyer — atomic deliver+charge, `deliver()` guards `already DELIVERED` and
  the ledger guard makes a second charge impossible (verified in `test_recovery.py`, so a
  click can never re-sell or double-bill, even after a mid-run crash).
- **Failed webhook on a SOLD lead** → lead page button **Re-post original payload to
  buyer's webhook** (`/admin/lead/<lead_id>/resend`): `route.operator_resend` re-posts the
  EXACT recorded payload bytes to the buyer's CURRENT webhook URL, records one deliveries
  row (`RESENT` / `FAILED`), never sells, charges or re-routes. It is the unbudgeted
  escalation after the automatic retries stop; use it once the buyer's CRM is confirmed
  working (a test ping from the Buyers table — **Send webhook test ping** — proves their
  receiver without touching any consumer data; staging keeps it a no-op until
  `LEADENGINE_LIVE_DELIVERY=1`).
- Every re-offer/redelivery decision lands in **Distribution decisions** and
  `deliveries` — the operator's audit trail. If you need the exact reason a lead went
  unmatched, `routing_decisions.reason` carries each attempt's exclusion list.

## 6. Incomplete reconciliation and stuck payments

The ledger is charged from the price stamped on the lead at routing time — not looked up
fresh — so re-runs always match. Reconciliation is guaranteed idempotent via the SQL unique
index on (`buyer_ledger` lead+kind): `agent_desk()` calls `billing.reconcile_lead()` on
every desk load, and a delivered-but-uncharged lead is healed exactly once no matter how
many times the buyer reloads. A crash mid-`_sell_lead` rolls everything back and leaves the
lead `ROUTED` (stranded), never half-charged — so the only real corruption gap left is an
app that *forgot* a charge entirely.

Detect a genuine gap directly:

```
sqlite3 /data/leads.db "SELECT l.lead_id, l.buyer_id, l.price FROM leads l WHERE l.routing_status='DELIVERED' AND l.price>0 AND NOT EXISTS (SELECT 1 FROM buyer_ledger b WHERE b.lead_id=l.lead_id AND b.kind='LEAD_CHARGE')"
```

(Windows: `python -c "..."` executing the same query through any `sqlite3` connection is
equivalent.) Rows appearing mean the buyer's desk hasn't loaded yet on a delivered lead —
the fix is that same reconcile path, nothing custom. An empty result means the ledger is
consistent; do not "re-run" anything.

**Payment stuck pending** (alert above): verify the real status on Stripe's dashboard, then
**Mark resolved** — requires a note ("what Stripe showed"). Closing the row can never
double-credit the buyer late: fulfillment only moves PENDING → PAID → CREDITED, so a row
that was CLOSED is excluded from later fulfillment (`tests/test_webhook_retry.py` covers the
long-delayed-webhook case). Never manually grant credits for a stuck payment — that is what
the buyer's top-up form plus a verification note is for.

## 7. Ledger consistency and buyer balances

```
sqlite3 /data/leads.db "SELECT buyer_id, kind, COUNT(*), ROUND(SUM(amount),2) FROM buyer_ledger GROUP BY 1,2 ORDER BY 1"
sqlite3 /data/leads.db "SELECT buyer_id, ROUND(SUM(amount),2) FROM buyer_ledger WHERE is_test=0 GROUP BY 1"
```

Kinds (physical fact of the ledger, `engine/billing.py`): `PACKAGE_PURCHASE` (+, recorded
by operator after funds received), `LEAD_CHARGE` (−, at delivery), `LEAD_CREDIT` (+, on a
return), `ADJUSTMENT` (signed, always with a note). Rules to read it right:

- the buyer's portal shows the **same truth**: `/agent/<portal_token>/billing` shows the
  balance and this CSV — `/agent/<portal_token>/billing/statement.csv` (same data, no other
  source).
- TEST traffic lives in the same table with `is_test=1` — keep it out of real-money math.
- Every `ADJUSTMENT` row carries a note; one without should be investigated, not assumed
  (only the operator form can write these, and the form requires a note).

## 8. Diagnosing app/db failures and preserving evidence

- Availability first: `GET /healthz` must report — it returns `{"ok": true,
  "base_url": ..., "live_delivery": ...}`. `live_delivery` false is the staging-safe state;
  `"ok": false` (or a non-200) is the availability signal render.yaml and the Dockerfile
  HEALTHCHECK both poll.
- Came up dark this morning → check the host's process logs **first**: a startup fail-safe
  (`Refusing to start: ...`) aborts on a wrong/weak ADMIN_TOKEN in production, half-set
  Stripe keys, `INTEL_DB` set, or similar; the fix is the env var it names, not a code
  change (`tests/test_deployment_guards.py` pins each case).
- Wrong-token page visits to `/admin` return 403 and each attempt is logged by the host —
  verify the token from the secret store, not logs.
- SQLite/DB file state: with the app running, `leads.db-wal` / `leads.db-shm` side files
  exist *while connections are open*; a stale `-wal` long after stop usually means a second
  app instance is still attached somewhere (two writers on the same file is a real hazard).
- Evidence preservation before any restart: run §2 backup with the same env, and archive
  `outbox/` — it holds the delivered payloads' exact recorded bytes (`deliveries.
  payload_path` points here), which is what dispute or retry investigations need.
- Debug one lead: `/admin/lead/<lead_id>` shows the consent text (the exact SHA-256
  snapshot), the event timeline, all `deliveries`, and all `routing_decisions` rows, so the
  evidence package for a dispute or a failed-refund question is one page, not a query.

## 9. Safe restore — and when to stop instead

Restore only if the live DB is worse than a backup (corrupted, botched migration, data
loss). Stop and escalate rather than overwrite when:

- any **consent row** exists in the live DB that the candidate backup lacks — consents are
  the 10-year legal record; never delete-ledged or re-wrapped;
- `buyer_ledger` rows exist in the live DB that the candidate backup lacks (financial
  records you'd be destroying a truthful ledger to "restore");
- you don't yet know WHY things look wrong. **Rule: diagnose first — a restore that
  immediately re-corrupts tells you nothing.** Take a pre-restore backup (§2) and stop if
  the cause is unclear.

Restoring safely (this exact sequence was rehearsed by `tests/test_backup_restore.py`):

1. stop the application — no writer while you swap the file;
2. take a **pre-restore backup** of the current broken DB (don't destroy your only evidence);
3. `cp <chosen backup>/leads_YYYYMMDD_HHMMSS.db /data/leads.db`;
4. `PRAGMA integrity_check` = `ok`, then start and confirm `GET /healthz` returns
   `"ok": true` and one admin page loads.

## 10. End-of-day checklist

1. `GET /healthz` → the response must contain `"ok": true`.
2. `/admin` alerts table is **empty** (Stranded sale / Webhook out of retries / Negative
   balance / Payment stuck pending / Buyer email failed). Anything non-empty → §5/§6.
3. Ledger sums per §7 look sane; **no `LEAD_CHARGE` for a lead that isn't DELIVERED**, no
   orphan `LEAD_CREDIT` without a return reason.
4. `python backup_db.py` (with `LEADENGINE_DB` set, so no WARNING) → verify the new file
   with `PRAGMA integrity_check` = `ok`.
5. Any `TEST`-flagged fixture rows made during rehearsal stay flagged; the TEST vs real
   separation is what keeps staging traffic out of commercial metrics.

Staging guard rail for this whole document: `LEADENGINE_LIVE_DELIVERY` off (every delivery
is an `OUTBOX_DRY_RUN` under `outbox/`), `mode=off` payment rail, `ALLOW_INDEXING` 0 — the
defaults the app already ships with; nothing above says reliably "do X to go live" because
that is a decision documented in `docs/DEPLOYMENT.md` and `launch_check.py`.
