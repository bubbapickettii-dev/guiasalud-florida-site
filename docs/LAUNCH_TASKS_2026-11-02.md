# Launch tasks 1 / 2 / 6 — what changed, what is verified, what is blocked (2026-11-02)

Nothing was deployed, published, posted, messaged, charged or DNS-changed.

## Task 1 — Instagram / TikTok / Facebook

**Done (no login needed):**

- `config/social.json` — single source of truth for all three profiles: display name, handle +
  fallbacks, category, ES/EN bio, Facebook About, per-platform UTM set, asset paths.
- `out/social_kit/PROFILE_KIT.md` — the exact copy to paste, per platform, in order.
- `out/social_kit/images/` — profile 1080×1080, Facebook cover 1640×624, five story-highlight tiles.
  Generated from the site palette by `tools/social_assets.py` (re-runnable; no stock art).
- `tools/social_links.py` — builds and validates the three links, refuses http, loopback, tunnels,
  `*staging*`/`*preview*` hosts, `/for-agents`, `/para-agencias`, `/agent/…`, `/admin`, `/p/…`.
- `out/social_kit/links.json` + `links.md` — status **BLOCKED**: `config/social.json` has no
  permanent domain, so each entry carries the exact query string as a template, no publishable URL.
- `tests/test_social_links.py` — 15 checks pinning the tagging and the destination refusals.

**Verified:** the three UTM sets reach the consumer home and are captured verbatim
(`localStorage le_attr` → `utm_source=instagram`, `utm_medium=organic_social`,
`utm_campaign=gs_allude_launch`) with no redirect to any internal page.

**Blocked:** all three platforms opened logged out (Instagram → `/accounts/login`,
Facebook → `/login.php`, TikTok → `/foryou`). No profile can be edited without the owner's login + 2FA.
Also blocked: the website field itself, until the permanent consumer domain exists.

## Task 2 — legal identity and support configuration

**Done:**

- `app.business_identity()` — one reading of `legal_entity`, `mailing_address`, `support_email`
  (alias of `contact_email`), `support_phone`; a bracketed/`CHANGE-ME`/sample value counts as
  *not supplied*, so no placeholder can reach a page.
- Env overrides: `LEGAL_ENTITY`, `MAILING_ADDRESS`, `CONTACT_EMAIL`, `SUPPORT_PHONE` (nothing real
  needs committing).
- Surfaced consistently: footer, `/privacy`, `/privacidad`, `/terms`, `/terminos`, `/contact`
  (mailto + phone + address, form still the fallback), `/for-agents`, `/para-agencias`.
- Admin ▸ Readiness now shows each field with configured/MISSING and where it appears.
- `docs/BUSINESS_IDENTITY.md` — field → surface map and the compliance sentences that stay untouched.
- Tests: placeholders never render on ten public paths; configured values render identically on six.

**Still missing (real values only; nothing invented):** legal entity name, mailing address, support inbox.
Optional: public support phone.

## Task 6 — demo buyers and credentials

**Done:**

- Demo desks renamed from `[Demo Agency A - placeholder]` to `Internal Test Desk A (demo - not a
  customer)`; each carries a `_demo_notice`. Buyer desk shows `TEST ACCOUNT` + "not a real account".
- `_buyer()` gate: a draft (`active:false`, tokens like `CHANGE-ME-…`) has no desk in any environment;
  on `LEADENGINE_ENV=production` a demo token is 404.
- Production routing (`_buyers(False)`) now excludes demo buyers **and** buyers whose licence/NPN checks
  are not recorded — an unverified agency cannot buy real consumer data. Staging keeps both, labelled,
  so rehearsals and the test suite still work.
- Belt-and-braces in the submission path: in production a real lead can never be booked to a demo buyer.
- Admin shows a `DEMO` pill; walkthrough hides demo portal links in production.
- Staging admin token rotated (new value in `.staging/ADMIN_TOKEN`, git-ignored, 0600); both previously
  disclosed tokens now return 403, the current one 200. Two rotations were performed — the interim one
  was invalidated in the same session.
- Tests: production-mode 404s for demo + draft portals, staging 200 for the labelled demo, and the
  verified-buyer eligibility split; the test fixture now records licence/NPN checks the way the
  operator does.

**Unchanged:** `data/prospects.db` (25,818 records), `data/leads.db` real ledger, `config/marketplace.json`
(`live_payments: false`), no real buyer exists, no buyer balance or ownership touched.

## Checks run (all on the final files)

- `pytest tests/ --ignore=tests/manual` → **582 passed**
- `tests/comprehensive/p1_consumer_journey.py` → 51/51, `p2_financial.py` → 76/76, `p3_routing_delivery.py` → 49/49
- `tests/smoke_routes.py` → 91/91
- staging (`127.0.0.1:5055`): consumer pages, admin overview/readiness/walkthrough/prospects all 200, 0 tracebacks
- `tools/social_links.py` → BLOCKED without a domain, READY with one; staging and buyer-app destinations refused

`test_collect.py::test_run_lock_refuses_fresh_owner_and_allows_stale` failed once during an intermediate
full run and passes standalone and in the final full run — it depends on the shared cooldown log, a
pre-existing order-sensitivity unrelated to these tasks.
