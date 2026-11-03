# Free public URL on Render — what it can host, what it cannot

Goal: a real, public `https://<name>.onrender.com` address serving the existing application on
Render's **free** Web Service tier, with the staging environment untouched, without collecting
consumer data on a filesystem that gets wiped.

The repository now contains two blueprints and one resolver:

| File | Purpose |
| --- | --- |
| `render-free.yaml` | **The free public website.** `plan: free`, no disk, `LEADENGINE_STORAGE=ephemeral` → read-only public preview. |
| `render.yaml` | The paid production service (`starter` + 1 GB disk at `/var/data`) for when the site must hold leads, consent evidence and buyer credits. |
| `engine/hosting.py` | One resolver for the public origin and the storage mode. Nothing else builds an origin. |

## 1. Render free-tier limits (checked against Render's docs, 2026)

| Limit | Value | Source |
| --- | --- | --- |
| Compute | 0.1 CPU / 512 MB (`free` plan id) | [blueprint-spec](https://render.com/docs/blueprint-spec) |
| Instance hours | 750 per workspace per month; exceeding it suspends all free services for the month | [free](https://render.com/docs/free) |
| Spin-down | after **15 minutes** with no inbound traffic; wake-up about **1 minute** | [free](https://render.com/docs/free) |
| **Persistent disk** | **not available on free** — "Paid services can preserve local filesystem changes by attaching a persistent disk, but Free web services cannot" | [free](https://render.com/docs/free), [disks](https://render.com/docs/disks) |
| Outbound mail | blocked on ports 25 / 465 / 587 | [free](https://render.com/docs/free) |
| Assigned origin | injected as `RENDER_EXTERNAL_URL` = `https://<service>.onrender.com` | [environment-variables](https://render.com/docs/environment-variables) |
| Build minutes / bandwidth | monthly allowances; exhausting them disables new builds / suspends free services | [free](https://render.com/docs/free), [build-pipeline](https://render.com/docs/build-pipeline) |
| Free Postgres | exists but **expires 30 days after creation**, then is deleted after a 14-day grace period | [free](https://render.com/docs/free) |

Two consequences worth stating plainly:

* Renaming a service to change its `onrender.com` subdomain is **not documented either way** — pick the
  name you want when you create the service. `giasalud` is the preferred name; if it is taken Render
  appends a suffix, and the blueprint/service name decides it.
* The free Postgres tier is not usable as this app's database: it expires in 30 days and the app's
  store is a single SQLite file. Nothing here uses it.

## 2. Is the existing application compatible?

| Requirement | Verdict |
| --- | --- |
| Python web app + WSGI | **Yes.** `gunicorn app:app --bind 0.0.0.0:$PORT` with `requirements-prod.txt` (flask, gunicorn, qrcode, reportlab, requests). Render injects `$PORT`. |
| Memory / CPU | **Yes.** One worker, 4 threads, 512 MB. The app is a thin Flask front over SQLite; page dumps and PDF/QR generation are the heaviest paths and are admin/partner-only. |
| Health check | **Yes.** `healthCheckPath: /healthz` answers `{"ok": true, …}` plus the origin, storage mode and safety switches. It is cheap (one `SELECT 1`). |
| Cold starts | Acceptable for a preview, poor for paid traffic: the first request after 15 idle minutes takes about a minute. |
| Outbound email | Not needed: `LEADENGINE_LIVE_DELIVERY=0`, so notifications are dry-run `.eml` files (which also means they are lost on restart — fine, they are dry runs). |
| **Database durability** | **No.** SQLite on the ephemeral filesystem loses every row on restart/redeploy/spin-down. This is the one real incompatibility, and it is handled explicitly below rather than papered over. |

### The storage decision (no silent substitution)

The app will not pretend an ephemeral filesystem is a database. The mode is **declared**, never
silently inferred, because a guessed path can be wrong either way (a volume mounted under `/app`, a
Windows server on a `D:` drive): `LEADENGINE_STORAGE=ephemeral` is what puts the app in the
read-only preview, and `render-free.yaml` sets exactly that. `LEADENGINE_STORAGE=persistent` declares
a mounted volume. An **undeclared** path is never a silent third option — it is printed as a startup
warning, reported on `/healthz`, shown on **Admin ▸ Readiness** and kept as an open blocker by
`launch_check.py`, so nobody collects data on a host they never thought about.

In the declared-ephemeral preview:

* `engine/hosting.py` reports `storage=ephemeral`, and the app sets `READONLY_PREVIEW` — logged at
  startup, shown on `/healthz` (`"storage": "ephemeral"`, `"accepts_submissions": false`,
  `"read_only_preview": true`) and on **Admin ▸ Readiness**.
* In that mode the public write paths **refuse instead of losing data**:
  * `POST /api/lead` → `503 {"error": "preview_read_only"}`;
  * `POST /contact` → "this preview deployment does not store messages yet, so nothing was sent";
  * `POST /optout` → "nothing was recorded … and it does not contact anyone either";
  * `POST /for-agents/apply` → "this preview deployment does not store applications yet".
* Everything read-only works normally: home, `/check` (EN + ES), guides, layoffs hub, open-enrollment,
  privacy/terms in both languages, `/for-agents`, `/robots.txt`, `/sitemap.xml`, `/healthz`,
  partner stats and the admin screens behind the token.

Existing data is **not** touched: nothing is uploaded from `data/` (`.gitignore` now excludes
`data/`, `outbox/`, `backups/`, `*.db`, `*.eml`, `.env`), and the free service creates its own empty
database in `/tmp`. Your local staging database, buyer rows, consent records and any payment rows stay
on this machine.

When real collection begins, move to the paid service (`render.yaml`: starter + disk at `/var/data`,
`LEADENGINE_STORAGE=persistent`) or PythonAnywhere (`/home/<user>/data`). The env flip is all that
changes; `READONLY_PREVIEW` turns itself off when the path is persistent.

## 3. The public origin, configured once

`engine/hosting.py` resolves, in order:

1. `BASE_URL` — the operator's own value (also what a custom domain later sets);
2. `RENDER_EXTERNAL_URL` — Render's injected `https://<name>.onrender.com`;
3. `config/site.json` → `base_url` (staging default `http://localhost:8000`).

Everything reads that one value: canonical tags, hreflang alternates, `og:url`, `sitemap.xml`,
the `Sitemap:` line in `robots.txt`, QR codes, guide PDFs, partner links, admin link builders.
`/healthz` reports `base_url` **and** `base_url_source`, so a deployment can never be mistaken for
another one.

HTTPS and redirects:

* Render terminates TLS and forwards `X-Forwarded-Proto`. A request that actually arrived as `http`
  while the public origin is `https` gets a `301` to the https URL (same path and query).
* Render adds `www.` → root (or the reverse) automatically for custom domains; the app does not
  second-guess hosts, so a request to the `onrender.com` name and to a future custom domain both work
  while `BASE_URL` says which one is canonical.
* `robots.txt` returns `Disallow: /` while `ALLOW_INDEXING=0`; the sitemap line appears only when
  crawling is allowed, and `/sitemap.xml` is already valid at the assigned URL (it returns 404 only if
  no absolute origin is configured at all).

## 4. What you must authorize (I cannot create accounts)

1. **A Render account** (GitHub/Google/email sign-in, no card required for the free plan). Do not add a
   payment method for this step.
2. **A Git repository Render can read** containing this app folder, connected to that account. If the
   folder lives inside a larger repo, that is fine: set the Blueprint Path to
   `<folder>/render-free.yaml` and uncomment `rootDir`.
3. **Click Deploy** (New ▸ Blueprint ▸ repo ▸ Blueprint Path ▸ Apply). Render will prompt for
   `SUPPORT_EMAIL` because it is `sync: false` — **leave it blank** until the inbox below exists;
   blank means the contact page shows the on-site form and no address.
4. Nothing else. No card, no domain purchase, no DNS change.

After it deploys, Render shows the URL and the generated `ADMIN_TOKEN` under
Dashboard ▸ service ▸ Environment. Treat that token like a password: it is not in this repo, and it
should not be pasted into chat or a document.

## 5. Verification (after the deploy exists)

Replace `<url>` with the assigned origin.

```bash
curl -s <url>/healthz                 # ok, environment=production, storage=ephemeral,
                                      # base_url=<url>, base_url_source=RENDER_EXTERNAL_URL,
                                      # accepts_submissions=false, indexing=false
curl -sI <url>/                       # 200, text/html
curl -s <url>/robots.txt              # Disallow: /  (indexing off)
curl -s <url>/sitemap.xml | head -c 400   # loc entries start with <url>
curl -s "<url>/check"                 # English check
curl -s "<url>/check?a=es_check"      # Spanish check
curl -s "<url>/aprende" <url>/privacidad <url>/terminos   # 200 each
curl -sI <url>/agent/demo-health-token    # 404: demo portal tokens are refused in production
curl -sI "<url>/admin"                    # 403: refused without the admin token
curl -s -o /dev/null -w '%{http_code}' -X POST <url>/api/lead -d '{}' -H 'Content-Type: application/json'   # 503 preview_read_only
```

On the page itself: the footer must read `© Gia Salud · Miami, Florida · Contact · Not affiliated with
HealthCare.gov …` — no bracketed placeholder, no invented street address, no unverified email.

## 6. What the free URL does not give you

* No lead, consent, buyer, contact or payment records (refused by design, above).
* No SEO/organic engine: `ALLOW_INDEXING=0` until you deliberately launch, and a cold-starting free
  service is a poor search/serving target anyway.
* No uptime guarantee: 750 instance hours/month across the workspace, spin-down after 15 idle minutes.
* Nothing commercial: no buyer configured, live delivery off, payment rail off, no ads, no outreach.

## 7. Cost summary

| Item | Cost |
| --- | --- |
| Render free web service + `onrender.com` subdomain (HTTPS included) | $0 |
| Render starter + 1 GB disk (the moment records must persist) | ~$7/mo + disk (verify on render.com/pricing) |
| Gmail support inbox | $0 |
| Custom domain (optional, not done here) | ~$10–15/yr |

## 8. Support inbox

Done: **giasaludellc@gmail.com**, created through Google's normal sign-up (the earlier preferred
`giasalud.support@gmail.com` was not the address actually registered, so it is not used anywhere).

* It is set as `SUPPORT_EMAIL` on the live `giasalud` service (Dashboard > Environment). The value
  stays out of the repository, as `render-free.yaml` declares it `sync: false`.
* Google asks for its own device/phone check the first time the new account is opened in a browser.
  That is the owner signing in, not a site setting; until it is done the mailbox may not be read.
* Once you have it, set the value in one place — `SUPPORT_EMAIL` in the host environment, or
  `support_email` in `config/site.json` — and the contact page, privacy "write to", the opt-out path
  and the readiness screen all pick it up (env wins, including after an admin config reload).
* Until then: `support_email` is empty, `/contact` uses the on-site form, and `launch_check.py` keeps
  the item visible. No fabricated address is ever printed.
