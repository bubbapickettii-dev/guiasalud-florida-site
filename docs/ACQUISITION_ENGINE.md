# Consumer acquisition engine (Oct 2026)

> **CORRECTION (2026-10-31).** The conclusion below — *"there is no lawful scrape of consumer contact
> details next to an insurance need, so acquisition has to be inbound"* — was wrong, and it is what
> steered this project into referral partners, directories and content instead of consumer discovery.
> "No published contact" is not "no discoverable consumer": people ask for coverage in public every
> day, on threads where a reply is allowed. See `docs/WHY_CONSUMER_DISCOVERY_STALLED.md` for the
> diagnosis and `docs/DISCOVERY_RUN_2026-10-31.md` for the live run that replaced this assumption
> (365 accepted consumer posts from 2,447 pages, 153 live prospects, 152 with a public reply route).
> The referral-network work below is a **supplement**, not the engine.

Status: built, measured, not launched. Nothing here contacts anyone, publishes anything, charges
anyone or sells a lead.

## What the data settled

The public-profile pipeline answered one question definitively: people who are deciding on coverage
today do not publish contact details on their public profiles. 470 public items → 37 genuine
need posts → 36 profiles read → **0 published contacts**. There is no lawful scrape of consumer
contact details next to an insurance need, so acquisition has to be **inbound**: the consumer finds
us (or a card we placed) and submits their own details with consent.

That leaves exactly three levers, and only one of them is free:

| Lever | Status | Owner of the blocker |
|---|---|---|
| Search / inbound content | Assets built (`/free-help`, `/learn`), indexing OFF | needs domain + `ALLOW_INDEXING=1` |
| Referral network (clinics, pantries, staffing) | 837 locations ready with kits | needs operator-approved outreach |
| Paid social | researched, not funded | needs money |

## What was built

### `engine/network.py` — referral network builder
Turns the verified organization ledger (`data/org_contacts.db`: HRSA health-center site file, CMS
NPPES, seeded community orgs) into **activation-ready referral partners**:

- one partner row per verified location (`net-<name>-<hash>`), deterministic so rebuilds never
  double-count a source;
- **837 partners** created from 840 records (1 had no phone, 2 were out of state);
- every row starts at `IDENTIFIED` with `cobrand_approved=0`, so the intake page never carries an
  organization's name until that organization says yes;
- `kit()` produces the finished artifact per location: `/p/<id>` link, QR URL, aggregate stats page,
  EN + ES card copy, EN + ES outreach draft;
- `export_kits()` writes `out/network_kits.csv` — 837 rows, each stamped
  `APPROVAL REQUIRED — nothing sent by the platform`;
- `network_runs` records every build (considered / created / existing / skipped-by-reason / errors),
  so a thin run can never pass for a complete one.

Access basis: official public registries whose published purpose is public listing. Terms/robots for
each source were captured at collection and are copied into each partner's `evidence` field.

### Public directory (`/free-help`, `/ayuda-gratis`)
**795 verified locations across 193 Florida cities**, in both languages, with city filter, pagination
and `MedicalClinic` structured data. It is genuinely useful (that is what makes it rank) and it hands
the visitor two paths: call one of these organizations, or take the check. It is added to
`sitemap.xml`. Rows without a published phone or a published city are excluded — a listing you cannot call, or
cannot place in a town, is not help.

### `engine/leadgen.py` — the eight-stage ledger
One report, from recorded rows only, with the buyer-gate and blocker analysis:

```
visitors → started → completed → submitted → consented → contact_verified
         → qualified → eligible for buyer routing → purchased & delivered
```

Hard rules in the code: TEST traffic is excluded; a stage that did not happen is `0` and an unknown
metric is `-`; `consented` requires all three facts (consumer-submitted phone, valid phone, stored
consent row — text + timestamp); `delivered` excludes deliveries to demo placeholders and reports
them separately as suspect REAL rows; organization contacts are never summed into these stages.

Surfaces: `/admin/leadgen`, `/admin/leadgen.csv`, `python tools/leadgen.py report`.

## Measured on the staging database (2026-10-30)

| Stage | Count |
|---|---|
| Visitors | 8 |
| Started inquiries | 2 |
| Completed inquiries | 2 |
| Submitted inquiries | 1 |
| Consented leads | 1 |
| Verified contact details | 1 |
| Qualified leads | 1 |
| Eligible for buyer routing | **0** |
| Purchased & delivered | **0** |

Referral network: 837 partners, 0 activated, 0 tracked visits, 0 leads.
Directory: 795 locations, 193 cities.
Buyer gate: 4 buyers configured, 0 non-demo, 0 usable, 0 routable products.
Suspect: 1 delivery to a demo placeholder on a REAL-flagged row (staging QA leak, excluded from the
delivered count; production excludes demo buyers from routing).

## Blocked, in order

1. **A named licensed agency** for `health_aca` in FL (`config/buyers.json`). Without one the consent
   may not name a buyer, so the check stops at completion by design. This is the single dependency
   between "traffic" and "revenue".
2. **Counsel review** of the consent wording (`config/site.json → consent_reviewed_by_counsel`).
3. **Indexing on** a real domain (`ALLOW_INDEXING=1`) — the directory and guides cannot be found
   until this happens.
4. **Live delivery** (`LEADENGINE_LIVE_DELIVERY=1`) — currently dry-run to `outbox/`.
5. **Partner activation** — 837 kits ready, 0 approvals logged. Outreach is a human action.

## Operating it

```bash
python tools/leadgen.py report                  # the eight stages + blockers
python tools/leadgen.py network-build           # idempotent; safe to re-run
python tools/leadgen.py network-report          # activation + referral yield
python tools/leadgen.py network-export          # out/network_kits.csv (the work queue)
python tools/leadgen.py kit <partner_id>        # copy-ready card/outreach text
python tools/leadgen.py directory --city Miami  # what the public page shows
```

Focused checks: `python -m pytest tests/test_leadgen.py` (15 tests) plus the acquisition, partner
pipeline, render-defect and integrity suites.

## Corrected direction: active discovery (Oct 30, 2026)

The engine above answers "how do we convert a visitor". This section is the other half: finding
people who never visit us.

**`engine/prospector.py`** stores public prospects in `data/leads.db` (table `public_prospects`) with
the operator ladder `NEW_PROSPECT / CONTACT_INFORMATION_FOUND / CONTACT_PERMISSION_REQUIRED /
CONSENTED / VERIFIED / QUALIFIED / ROUTING_ELIGIBLE / REJECTED / DUPLICATE`. Rows never enter
`leads`: `ROUTING_ELIGIBLE` is set only when that same person submits `/check` and the row is linked
to a real consented lead (`link_lead` proves it by reading `leads` + `consents`).

**`engine/discovery.py`** reads permitted public pages, records a run per adapter in
`discovery_runs`, and stops on any access restriction:
- `citydata_forum` — City-Data's public Health Insurance board (robots-allowed; only
  `/forum/search.php`, `/forum/members` and reply endpoints are disallowed, none of which it touches).
- `discourse` — any public Discourse `/search.json` you list in `config/sources.json > discovery`.

**Operator surface**: `/admin/people` now lists these rows with the sentence that justified them, the
public contact and its evidence, the source URL, and one action form (VERIFIED / CONSENTED / QUALIFIED
/ REJECTED / DUPLICATE). Every action demands a note. Nothing on the page can send, call or DM.

**Continuous runs**: `python tools/discovery.py run --network` (cron example in the CLI docstring) or
`python tools/daily_run.py --discover`. `python tools/discovery.py signals` folds rows already
collected by `engine/signals.py` (Reddit public JSON, HN, Stack Exchange, fediverse) into the same
table without touching the network.

Measured (Oct 30, 2026): 50 City-Data thread pages → 4 accepted → 3 kept, 0 published contacts, 0
ACA-intent (the board's population is Medicare-age plan-cost discussion). Existing signal store: 400
INTEREST rows → 37 new prospects, 0 published contacts. Store total 95 rows (55 migrated legacy +
37 signal + 3 City-Data), 0 leads created. Two live false positives (informational thread, policy
op-ed) drove the classifier from page-scoped to sentence-window matching plus a commentary guard.
