# Core product pass — consumer, marketplace, admin, domain (2026-11-02)

Scope: finish the remaining core product work that does **not** need the owner's personal
information or approval. Social profiles were explicitly out of scope and were not touched.
Nothing was deployed, published, emailed, messaged, charged, or DNS-changed; no live lead sale was
started; no account ownership, password or security setting was changed.

---

## 1. Consumer website (EN + ES)

### Opt-out is now actually enforced

Before this pass the opt-out form wrote a row and flagged existing leads, but a later visit to the
check could still create a fresh lead and sell it. The page promised "we'll stop all contact" and
the promise had a hole.

- `app._opted_out(phone, email)` reads the **same** `optouts` keys the opt-out form and the agents'
  DNC action write.
- `POST /api/lead` checks it after qualification. A match is recorded (nothing is hidden from the
  operator) but marked `UNQUALIFIED` / `consumer_opted_out`, so `route.route()` returns
  `NOT_ROUTABLE`: **no buyer is named, no delivery row, no ledger row, no charge.**
- The check's done screen has a dedicated, bilingual outcome ("You asked us not to contact you")
  instead of the generic "didn't meet the requirements" text, which would have been false.
- The audit trail keeps the reason: `routing_decisions.reason` contains `consumer_opted_out`.

### The opt-out and contact forms no longer lie about success

- `/optout` with no identifier used to render "Done. You won't be contacted again" while storing
  nothing. It now refuses with a bilingual message and records no success. `+1 (305) 555-0142`
  style numbers normalise correctly.
- `/contact` with an empty message had the same defect; it is refused now.

### Consumer inquiries reach a human

`contact_msgs` was write-only: stored in SQLite, rendered nowhere. Added:

- `engine/notify.consumer_inquiry()` — addressed to the configured support inbox, `Reply-To` the
  person, written to `outbox/inquiries/*.eml` when SMTP is off (never silently dropped).
- `GET /admin/inquiries` (`templates/admin_inquiries.html`) — the existing contact messages with
  reply addresses, plus a live warning when the legal identity is incomplete. Linked from the
  admin nav (Leads ▸ Inquiries) and from the walkthrough.
- An inquiry is never a lead: no routing, no sale, no list.

Preserved unchanged: no agent/buyer picker anywhere (the platform assigns), consent-first
submission, no-buyer safe mode, "availability is a race" honest outcomes, bilingual parity.

---

## 2. Lead marketplace

### An unfunded buyer can never be named in a consumer's consent on a live deployment

`enforce_credits` defaults to `false` (right for staging rehearsals). On production that meant a
real, verified, active buyer with a $0 balance could be named in the consent and delivered a lead
that could never be billed.

- `engine/billing.enforcement_on(marketplace, production)` — one rule: the config switch **or**
  running live.
- `billing.blocked_ids(..., production=...)` and `app._credit_enforcing()` use it. The REAL lane is
  always enforced in production; the TEST/dev lane keeps the switch, so demo rehearsals still run
  without credits.
- Wired through the two routing call sites (`/api/route-preview`, `/api/lead`), the buyer portal
  (`meta.enforcing`, the `blocked` / `low` flow messages), the operator buyer rows, `/for-agents`,
  and Admin ▸ Readiness — no surface says "ledger-only" on a live deployment any more.
- The buyer row's blocker list also names the production verification gate (licence/NPN not
  recorded), which previously excluded a buyer silently.

Unchanged and re-verified: exclusive assignment, consent naming, charge-once / credit-once /
deliver-once, returns and credit decisions, ledger adjustments, per-buyer isolation.

---

## 3. Admin / operating workflow

All of this uses the existing buyer form and table — no new dashboard.

- **Saved values are pre-filled.** The `tiers` input was hard-coded to `A,B`, so opening an
  existing buyer's form and saving silently reset a buyer who sells `A,C`. Prices, daily/monthly
  caps, age band and the return window are now pre-filled too, so an operator can *edit* rather
  than re-type blind. Blank fields still keep the stored value (the standalone Activate/Deactivate
  buttons post no fields and cannot reset anything).
- **Buyer contact details are editable.** `contact_name` / `contact_email` / `contact_phone` are
  saved into `buyer.contact` (validated email), shown on the row, and used by the copy-ready
  activation email when there is no public application snapshot. That closes the gap where an
  operator-created buyer had no recorded contactability.
- **Portal-link rotation is a dedicated action** on every non-demo row (confirmation dialog), in
  addition to the existing dropdown. It issues a fresh token, invalidates the old link immediately,
  and never changes activation, pricing, balance or delivery settings.
- The edit link reads "Review application → prefill onboarding form" for a pending application and
  "Edit / update this buyer (form pre-filled with saved values)" otherwise.

---

## 4. Domain and configuration readiness

- `app._site_origin()` — the one reading of a usable absolute origin. Canonical tags are omitted
  entirely when config has no absolute origin (a relative canonical is worse than none).
- `robots.txt` no longer appends `Sitemap:` while it says `Disallow: /` (a contradiction Search
  Console flags, and a way to advertise a staging host). The sitemap line appears only when
  indexing is allowed **and** an origin exists.
- `sitemap.xml` is 404 without a public origin instead of emitting relative `<loc>` values.
- Production startup prints a loud warning if `BASE_URL` is still a loopback host (not fatal — a
  first deploy often precedes DNS — but visible in the host's log).
- `/healthz` now reports `environment`, `indexing`, `credit_enforcement` and `public_origin`
  alongside `base_url`/`live_delivery`, so a staging host can never be mistaken for the live one.
- `assets/ads/google_rsa_kit.csv` and `meta_ad_kit.csv` shipped `http://localhost:8000` in every
  final URL (106 rows). Normalised to the existing `{BASE_URL}` placeholder convention; a test now
  fails if any shipped asset carries a loopback origin. `build_assets.py` honours `BASE_URL` like
  the app does.

### Exact remaining domain-dependent steps (owner input)

1. Buy/point the permanent consumer domain at the host.
2. Set `BASE_URL=https://<domain>` in the deployment environment (nothing is baked into assets).
3. Redeploy, then confirm `GET /healthz` shows that origin as `public_origin`.
4. Re-run the asset builders (`BASE_URL=https://<domain> python build_assets.py`,
   `python tools/distribution_pack.py`, `research/build_meta.py`) so QR codes, print pieces and ad
   kits carry the real domain.
5. Only then flip `ALLOW_INDEXING=1`, submit `/sitemap.xml` in Search Console/Bing, and put the
   domain in the social bios.

---

## 5. Legal / business configuration

- `config/site.json` now exposes `support_email` and `support_phone` as first-class keys (empty =
  not supplied; `contact_email` remains the documented alias). `SUPPORT_EMAIL` joins
  `LEGAL_ENTITY` / `MAILING_ADDRESS` / `CONTACT_EMAIL` / `SUPPORT_PHONE` as environment overrides,
  so real values never have to be committed.
- No placeholder can render publicly: `_real_value()` treats brackets, `CHANGE-ME` and the sample
  domain as "not supplied". Admin ▸ Inquiries and Readiness print the missing-field list.
- Technical corrections only. **No legal language was changed or presented as approved**
  (`consent_reviewed_by_counsel` is still `false`).

Still missing (owner input, cannot be invented): **legal entity name, registered mailing address,
support inbox**. Optional: public support phone.

---

## Verification (run on the final files)

| Check | Result |
|---|---|
| `pytest tests/ --ignore=tests/manual` | **598 passed** (19 pre-existing deprecation warnings) |
| `tests/test_launch_core.py` (new, 15 checks) | 15 passed |
| `tests/comprehensive/p1_consumer_journey.py` | 51/51 |
| `tests/comprehensive/p2_financial.py` | 76/76 |
| `tests/comprehensive/p3_routing_delivery.py` | 49/49 |
| `tests/smoke_routes.py` | 91/91 |
| Live staging (`127.0.0.1:5057`, temp DB) | `/healthz` reports staging/no-index/credit-enforcement; `robots.txt` has no Sitemap line; contact message stored, `.eml` written, visible on Admin ▸ Inquiries; empty opt-out refused; `/check` EN+ES 200; 0 tracebacks |
| Public page sweep (20 paths, EN+ES) | no dead `href="#"`, no empty form actions, no config placeholders (only embedded JSON and an option labelled "None") |

The two `test_testmode_integrity` fixtures now record a credit pack for the live buyer: a
production buyer that cannot pay is (correctly) no longer routable, so the fixture models a real
funded pilot. Their assertions were not weakened.

---

## Remaining blockers (all external)

| # | Blocker | Kind |
|---|---|---|
| 1 | Permanent consumer domain + `BASE_URL` | owner input |
| 2 | A real licensed FL agency (legal name, licence, NPN, first credit pack) | external |
| 3 | Legal entity name, mailing address, support inbox | owner input |
| 4 | Counsel review of consent/privacy/terms | external (recommended) |
| 5 | `ALLOW_INDEXING=1`, Stripe test rail, `LEADENGINE_LIVE_DELIVERY=1` | owner approval |
| 6 | Hosting with persistent DB and a strong `ADMIN_TOKEN` | external |
