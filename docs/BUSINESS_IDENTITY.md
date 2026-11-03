# Public business identity — one source, every surface

The public identity lives in `config/site.json` and can be supplied by environment instead, so
nothing has to be committed to the repo. Env always wins, including after an admin config reload
(`app._apply_env_site`).

| Field | `config/site.json` key | Env override | State today |
| --- | --- | --- | --- |
| Public brand name | `brand` | `BRAND` | **set: Gia Salud** |
| Business / legal name | `legal_entity` | `LEGAL_ENTITY` | **set: Gia Salud** (no invented suffix such as LLC/Inc.) |
| Public location | `location` | `LOCATION` | **set: Miami, Florida** |
| Founded | `founded` | `FOUNDED` | **set: Miami, Florida** |
| Street / mailing address | `mailing_address` | `MAILING_ADDRESS` | empty — **not required for this website**; never invented |
| Customer support inbox | `support_email` (alias `contact_email`) | `SUPPORT_EMAIL` / `CONTACT_EMAIL` | empty — **pending** until giasalud.support@gmail.com is verified |
| Public support phone | `support_phone` | `SUPPORT_PHONE` | empty (optional) |

`app.business_identity()` is the single reading of these values. It treats a value as **not supplied**
when it is empty, bracketed (`[...]`), `CHANGE-ME`, or the sample domain — so a placeholder can never
reach a public page even by accident. Every surface below reads that one helper.

What is deliberately **not** configured: a street address, a legal-entity suffix (LLC/Inc./Corp.),
a registration number, a phone number, and a support mailbox. None is invented anywhere. The business
name is printed as given; no page claims a registered entity form the owner has not supplied.

## Where each value is displayed

| Surface | Brand | Business name | Location / founded | Address | Support inbox | Phone |
| --- | --- | --- | --- | --- | --- | --- |
| Footer (all public pages, EN+ES) | ✓ wordmark | ✓ when different from brand | ✓ location | ✓ when set, else location | — (links to `/contact`) | — |
| `/privacy` and `/privacidad` | ✓ | ✓ "operated by" | ✓ | ✓ when set, else location | ✓ `contact_ref` | — |
| `/terms` and `/terminos` | ✓ | ✓ "operated by" | ✓ | ✓ when set, else location | — | — |
| `/contact` | ✓ | ✓ when set | ✓ | ✓ when set, else location | ✓ `mailto:` + form | ✓ when set |
| `/optout` | ✓ | — | — | — | ✓ `contact_ref` (privacy page) | — |
| `/for-agents` and `/para-agencias` (buyer side) | ✓ | — (the agency's own name is the subject) | — | — | operator reply-to comes from the inbox | — |
| Application + confirmation messages | — | — | — | — | reply-to is the applicant's own address (never ours) | — |
| Consumer consent text | ✓ platform phrase, platform-assigned agent | — | — | — | — | — |
| Admin → Readiness | ✓ | ✓ status | ✓ status | ✓ status ("optional") | ✓ status | ✓ status |

The footer therefore reads:

```
© Gia Salud · Miami, Florida · Contact · Not affiliated with HealthCare.gov, the Health Insurance Marketplace or any government agency.
```

No brackets, no "to be confirmed", no invented street address, no unverified email address.

## Compliance language (unchanged, verified)

`config/site.json` `disclosure` / `disclosure_es` / `gov_disclaimer` / `gov_disclaimer_es` state, in both
languages: not an insurance company, not an insurance agency, not a government site, does not sell
insurance, estimates are educational and not quotes/eligibility decisions/offers, and the consumer is
told that one licensed agency — assigned by the platform — would receive their details, with permission
asked before anything is shared. No surface claims government affiliation, carrier status, guaranteed
coverage, savings or eligibility. Managed-care/plan names are not used as brand marks.

## Turning on the support inbox (after it is verified)

One value, two possible places:

```bash
# host environment (preferred — nothing real lands in the repo)
SUPPORT_EMAIL=giasalud.support@gmail.com
```

or `config/site.json` → `"support_email": "giasalud.support@gmail.com"`.

That single change makes the contact page print the address next to the form, the privacy policy
(both languages) and the opt-out page point at it instead of the on-site form, and the readiness
screen stop listing it as missing. `python launch_check.py` confirms it.

Nothing prints the address before it exists: the contact page falls back to the on-site form, which is
real and already stores messages (on a persistent-disk deployment).

## Related

* `docs/RENDER_DEPLOY.md` — the free public URL and why it runs a read-only preview.
* `docs/LAUNCH_BLOCKERS.md` — the consolidated outstanding list.
* `python launch_check.py` / Admin ▸ Readiness — the same blockers, computed live.
