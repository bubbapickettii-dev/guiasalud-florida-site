# The seven pipeline states — what exists and what can never be confused (Oct 27, 2026)

The marketplace requirement defines seven distinct states. GS-allude implements each
one in **separate rows of separate tables / databases**, with the transitions human-
or user-gated. This file is the map; tests guard it (see §3).

| # | Required state | Where it lives | How it becomes the next state |
|---|---|---|---|
| 1 | **Public intent signal** | `data/signals.db` table `signals` (Reddit / HN / YouTube RSS / manual ingest) | NEVER automatic. Review at `/admin/intel/signals` (NEW → REVIEWED → ACTIONED/IRRELEVANT, evidence note required). An ACTIONED signal renders as a *content draft* at `/admin/content-ideas` — the only conversion is into a public content page, never into a contact. |
| 2 | **Consumer inquiry** | `leads.db` table `leads` row where the consumer submitted the form (`/check` / `/api/lead`), with `consents` row | Only the consumer's own typed phone + checkbox creates a row; provenance = first-party (UTM, experiment, partner) recorded on the same row. Public posts can never reach this table. |
| 3 | **Qualified inquiry** | same row, `qual_status='QUALIFIED'` (valid phone, served state, product fit; `NURTURE` if email-only) | Scoring at submit time only (`engine/qualify.py`); tier A/B/C from score; UNQUALIFIED/DUPLICATE are terminal, never sold. |
| 4 | **Routable lead** | routing decision row `routing_status='ROUTED'` with a genuinely eligible buyer | `engine/route.py route()` assigns ONE buyer server-side (caps, prepaid balance, buyer health, positive tier price) and sets `sale_type='EXCLUSIVE'`. NO_BUYER / NOT_ROUTABLE are dead ends — never "sold free". |
| 5 | **Assigned lead** | the ROUTED row, delivered state `routing_status='DELIVERED'` + one `deliveries` row | `deliver()` writes the exact payload (with `consent`, `provenance`, `what_they_saw`) — unique index enforces at most ONE successful delivery per lead, ever. |
| 6 | **Delivered lead (in buyer's hands)** | buyer desk shows the payload; agent statuses NEW → VIEWED → CONTACTED → APPOINTMENT → SOLD / NOT_QUALIFIED / BAD_INFO / DNC / OTHER | Returning is gated: in-window returns credit once (SQL-guarded `LEAD_CREDIT`), late returns hit the credit queue for operator decision. |
| 7 | **Paid / credited transaction** | `buyer_ledger` rows: `PACKAGE_PURCHASE`, `LEAD_CHARGE` (once per delivered lead), `LEAD_CREDIT` | Charges book only at delivery; retry/reposting never creates a second sale or a second charge; Stripe credits arrive only via signature-verified webhook. |

## Invariants (each guarded by a test named below)

1. **`signals.db` can never feed the pipeline.** `engine/signals.py` and
   `engine/content_intel.py` contain no imports of `engine.route` / `engine.notify` /
   `engine.billing` (AST test: `tests/test_signals.py`,
   `tests/test_sources.py::test_sources_module_never_touches_pipeline_or_network`).
   They can't even contact anyone — no contacting library is importable there.
2. **A signal's only exit is public content.** `/admin/content-ideas` drafts pages; the
   human pastes them into `config/content.json`; the page points visitors at `/check`.
   There is no code path — CLI, admin, or API — from a signal row to a leads row.
3. **Exclusive sale integrity.** `sale_type='EXCLUSIVE'` set by the platform, one
   successful delivery per lead (`ux_deliveries_lead`), `deliver()` refuses
   re-posting, `billing.reconcile_lead()` heals charge gaps at most once
   (`tests/test_exclusive.py`, `tests/test_lifecycle_reconcile.py`).
4. **Money is bookkeeping only.** Packages recorded by the operator after funds are
   confirmed; charges once per delivered lead; credits only per the return rules
   (`tests/test_billing.py`, `tests/test_payments.py`).
5. **No contact data from public sources.** Signals whitelist fields at ingest and
   verify the results (`tests/test_signals.py::test_ingest_dedupes_and_strips_people`,
   `tests/test_sources.py` YouTube fixture asserts no author fields survive).
6. **Consumer privacy inside leads.db.** Admin lead pages keep PII out of bulk
   exports; consents.csv is the only full-consent export and is token-gated.

## Video contrast (why the demonstrated product is not our frame)

The demo video (docs/VIDEO_RECON.md) shows peers' and consumers' phone/email visible
in shared team tables, with no consent UI anywhere. That is state inversion: they
start from "assigned lead" (bought) and never record an individual consent state of
their own. GS-allude's ladder runs the other direction: consumer choice first,
qualification second, single exclusive buyer third, payment last.
