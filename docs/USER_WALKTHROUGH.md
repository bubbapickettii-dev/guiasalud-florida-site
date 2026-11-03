# GS Allude — Owner's Walkthrough

Open the app at **http://127.0.0.1:8000** (started locally; nothing is published to the internet).

Admin: **http://127.0.0.1:8000/admin?token=DEVLOCAL** (dev token; a guided version of everything
below is built into the app under **Admin ▸ Walkthrough**).

## Safety on this machine

- Every consumer submission recorded so far is synthetic (`is_test=1`); there are **no real consumers**
  in the data. The rule is enforced server-side: a QA browser (`/qa?on=1`) or `?test=1` marks the row
  TEST, and TEST rows can never be sold, routed to a real buyer or counted as market evidence.
- 4 **internal test desks** (`Internal Test Desk A/F/B/H — demo, not a customer`); their **portals are
  clickable** from the walkthrough page on this machine only. They are never a customer: in production
  (`LEADENGINE_ENV=production`) they are excluded from real-traffic routing *and* their portal tokens are
  refused (404), so a demo link cannot be a working login once real buyers exist.
  The 2 real buyer records (agent-health-1, agent-life-1) are inactive drafts with `CHANGE-ME-…` tokens;
  a draft has no working portal in any environment.
- **Off by design:** live payments (no Stripe keys), real SMS/email/webhooks (deliveries are dry runs
  into `out\outbox\`), ad spend, any public collection sweep, any contact with prospects or consumers.

## 1 · Consumer experience

| Where | What happens | Then |
|---|---|---|
| `/` (Spanish-first) or `/?lang=en` | Public home; health-insurance check is the entry point | Click the Check |
| `/check` (EN) · `/check?lang=es&a=es_check` (ES) | Qualification questions → consent (required) → submission. Campaign: `oep-2027-health` / `...es` | Submission lands in admin queue with consent evidence |
| `/learn` `/aprende`, `/layoffs` `/despidos`, `/open-enrollment-2027` `/inscripcion-abierta-2027` | Content pages; each CTA feeds the Check | Content = the acquisition surfaces for the researched opportunities |
| `/for-agents` | Buyer-acquisition pitch + application | Application lands in buyer pipeline; never auto-activated |

**Join:** consumer journey → admin **Overview** (`/admin`) shows the funnel and unmatched queue.

## 2 · AI & data collection (all manual; never automatic)

| Where | What happens | Join |
|---|---|---|
| `/admin/integrations` | Honest status of every sink/source + Local-AI panel (Ollama URL/models/health), per-collector last results, prospect-acquisition paths | Tells you what actually runs |
| `/admin/intel/signals` | Review queue of **public posts** (Reddit, HN, YouTube, Google News, webreader). No usernames, no contacting | Mark REVIEWED with a note |
| `/admin/intel/clusters` | **Local Ollama** distills signals into prioritized clusters; counts/titles are facts, AI text is labeled inference | Cluster links → ladder |
| CLI `tools/collect.py` | `status`, `run --network` (overlap-locked), `clusters-build`, `ai-health`, `ai-smoke`, `report` | Everything the pages show |

**Join:** signals → (REVIEW) → clusters → opportunities → content ideas/guides pages → visitors.

## 3 · Opportunities & prospects

| Where | What happens |
|---|---|
| `/admin/opportunities` | 27 researched theses (OPP-xx/HID-xx): status, evidence, recorded results |
| `/admin/channels` | **Create a tracked link** for each place a person sees it (a subreddit answer, a library flyer QR, an org newsletter) → gets the URL + QR. Every visit and request it produces is grouped under that source, so a channel that works can never hide inside "direct" |
| `/admin/sources` | **Arrival report**: visitors → check started → completed → contact submitted → valid contact → consent+phone → qualified → routing-eligible → delivered, per source, per channel, per campaign, with the biggest drop named. Real and TEST are separate tabs; loopback/private-LAN traffic is never counted as a consumer |
| `/admin/prospects` | **Business-buyer** registry (separate from consumer leads): stages, verification provenance, decision history, safe import/export, gated handoff to onboarding |
| `/admin/organic` | Publishing/outreach work queue with cost/time logging |
| `/admin/people` | **Consumer-side discovery** (scrapers: signals, Bluesky, Lemmy, forums) with measured per-source yield — fetched/candidates/accepted/skipped. Discovery finds people already asking for help; it never contacts them and never creates a lead |
| `/admin/content-ideas`, `/admin/intel/briefs` | Signals → draft EN/ES briefs; county briefs |

**Irreversible vs reversible:** review statuses/notes are just history entries (reversible);
handoff creating a buyer draft is guarded (refuses duplicates) but visible & traceable.

## 4 · Buyer onboarding

| Where | What happens |
|---|---|
| `/admin/buyer-pipeline` (+ per-buyer detail) | Follow-up queue; shows the SAME blockers the routing engine computes |
| `/admin/readiness` | Launch blockers vs operator decisions vs integration state |
| `/admin/pipeline` | Sign-up → activation workflow |
| `/admin/lab` | Experiments: what we tried/cost/produced |

**Join:** prospect handoff → buyer draft (inactive) → operator verification/activation → eligible.

## 5 · Marketplace & delivery

| Where | What happens |
|---|---|
| `/admin` Overview | Funnel counts, unmatched queue with routing explanations |
| Lead detail page | Qualification, consent, routing blockers, exclusive delivery (dry-run), ledger trail |
| `/admin/economics` | Recorded money split TEST vs real + cross-checks + scenario calculator |
| `/admin/credits` | Return-credit dispute queue (evidence-based) |
| Internal test desk portals (local/staging only) | `/agent/demo-health-token`, `/agent/demo-agency-a-token`, `/agent/demo-agency-b-token`, `/agent/demo-fe-specialist-token` (+ `/billing`, `/leads`). These tokens are **fixtures, documented here, and refused in production**; a real buyer gets a random token generated at activation and handed over privately. |

**The rule the app enforces:** a consumer never chooses their buyer. Routing picks the one eligible
buyer (territory, pricing, credits, exclusivity) or explains why nobody is eligible.

## 6 · Administration

Partners (`/admin/partners`) · Content (`/admin/content`) · Audiences (`/admin/audiences`) ·
Funnel (`/admin/funnel`) · exports (`/admin/export.csv`, `/admin/consents.csv`) — plus the
**Walkthrough** tab that mirrors this guide inside the app.

## Requires credentials / your explicit action (currently blocked, honestly reported)

- **Google Places** prospect sweep: needs `GOOGLE_PLACES_API_KEY` + your decision
  (`config/sources.json → places → approved: true`, per-request billing).
- **Meta/TikTok** official adapters: platform credentials/approval required.
- **Stripe** live payments: keys unset; top-ups stay the operator-recorded path.
- **All outbound contact** (email/SMS/webhooks): intentionally off; deliveries dry-run to `outbox/`.

## How the data flows (one sentence each)

1. Public posts → minimized signals (`signals.db`) → local-AI clusters → thesis evidence → content.
2. Consumer visits a Check → qualification + consent → lead record (isolated `leads.db`).
3. Lead → exclusive eligibility → buyer purchase (balance/ledger) → dry-run delivery → buyer desk.
4. Businesses → prospect registry → qualified/verified evidence → buyer draft → operator activation.
