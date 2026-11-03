# Video reconnaissance — the demonstrated agent-side insurance lead service (Oct 27, 2026)

Inspected from the stills in `video_ref/full/` and `video_ref/frames/` (the raw video
file is not in the workspace). Product: an agent-facing **insurance-lead distribution
workspace** white-labeled as "George Region" on `georgesusamarketplace.app`, footer
"Powered By **Lead Arena**", build tag `v1.0.76-prod`. Logged-in demo user: agent
**JHONATHAN W.** Each frame fact below cites the still it was read from. Anything a
still cannot prove is in §3 (unverified) and was NOT assumed anywhere in the code.

## 1. Observed facts

### 1.1 Product layout (all frames, sidebar + URL bar)
Navigation: **OVERVIEW** (Home/dashboard, Floor Dashboard, Submitted Apps) ·
**LEAD GENERATION** (Campaigns, Leads, Bulk Leads, Downloads) · **FINANCE** (Banking →
Balances / Agent Transfer / Transfer History) · **MANAGE** (Groups) ·
**CONFIGURATION** (Integrations) · Logout. Persistent top-right wallet strip:
**Balance $180.31 · Deposited $0.00 · Contributed $180.31** (`bulk_leads_85.png`,
`submitted_apps_57.png`).

### 1.2 Home dashboard — lead distribution overview (`groups_a_132.png`, `automatch_a_79.png`)
- "Welcome back, jhonathan! Here's your **lead distribution overview**."
- KPI tiles: **Leads Received Today 0** ("No leads today"), **Active Campaigns 0**
  ("0 campaigns received leads today"), **Leads This Week 1** ("−90.9% vs last
  week"), **Avg Cost Per Lead (This Week) $36.00** ("−5.7% vs prev week").
- **Financial Overview** cards: Available Balance $180.31, Today's Spending $0.00,
  Pending Allocation $0.00; Recent Transactions: **"Transfer from Grant England
  +$250.00 · 11 hours ago · completed"** — i.e. agent-to-agent money transfers.
- **Auto-Match Available**: "Groups you can still match against right now" —
  Left to Match $500.00 · Active Today 0 · Total Match $500.00; card *"Agents Group
  Matching — Friday – Monday · Not active today · Total: $500.00 · Rate: 50.00%"*.
- **Auto-Match Received**: "Recent matches credited to your account" — Total
  Received $1,675.00 · Recent Matches 10; rows "Agents Group Matching · 50% ·
  9d ago · +$150.00", "50% · 17d ago · +$300.00", "50% · 23d ago · +$150.00".
  Interpretation (inference): a **group-managed match bonus the platform credits at
  50% of a qualifying amount, in recurring Friday–Monday windows** — retention
  economics between agents, not lead pricing.

### 1.3 Leads store (`leads_table_63.png`; URL bar visible)
- Table columns: **Legacy, Segment, First Name, Last Name, Business Name,
  Employees, Date, Campaign, Tier**. Rows are *real purchased consumer leads*
  (e.g., Xavier Longoria — Oct 26, 7:39 PM — campaign "J split", tier
  "JUMBO SPlits"; Caleb Askew — Oct 31 — "OE / OPEN ENROLLMENT B…"; Rodney John­son —
  "Dynamite / Performance Mix (Dynamite)").
- Toolbar: search "by name, email, id, phone or **CRM ID**" · Date · Pinned ·
  Filters · **Columns · Download · Refresh**. URL shows the working params:
  `hiddenColumns=email,dateOfBirth,income,age,gender,household,zipCode,city`,
  `leads.dateRange=…2026-10-10…2026-10-27…`, **`leads.networkId=1474a`**.
  ⇒ **PII columns exist per lead and are hidden behind a Columns toggle**; leads
  are bought from **multiple networks** (networkId filter) and priced per campaign
  (the fixed $180.31 "Contributed" budget vs $36 avg cost implies purchases).
- No status column is visible in this view (statuses seen elsewhere as app pages,
  §1.4). No consent text or DNC/state shown anywhere in the stills.

### 1.4 Submitted Apps — the agent's production log (`submitted_apps_57.png`)
Caption exactly: *"Click a field to record the **Vcall date, estimated submitted AV,
family size, and source** for each app."* Table: **Submitted | Phone(s) | Email |
Vcall Date (Add date) | Est. Submitted AV (Add AV) | Family Size (Add size) |
Source (Select…)**, quick filters "Vcall today / Vcall tomorrow".
⇒ The platform tracks **outcomes after purchase** (submitted app = the agent's sale
entry), with the agent filling telephone/email/date/estimated annual value/family
size/source. Roughly a **higher-granularity version of our agent desk dispositions**
(their rows carry ~7 PII/prospect fields; ours carries outcome + timestamps only).

### 1.5 Floor Dashboard & milestones (management layer, `floor_a_45.png`)
"**SETH GARMANY · Field Training Agent** · #00044116", Year/Month/Week filter,
"11 Writing Agents of 12 on roster, Oct. 20**25**", "Issued Business **$0.00**",
"Submitted Sales **$0.00**", "Avg sale **$0.00** / writer", "Biggest deal **—**",
recently-won '**0 days streak**' strip yet the *Sales Goals* panel shows
"This Week $0 of 5K issued business · This Month $8,014 of 20K · This Quarter 105
of 63K · This Year 100% of 250K" with **paid /** target bands per period.
⇒ **Individual pipelines + leaderboards + written goals** — targets are set inside
the product (the "$7K/mo" goal framing reappears repeatedly). Relevant to us only
as an *upline management* pattern; out of scope for our own single-operator admin.

### 1.6 Integrations (`integrations_102.png`)
A grid of **CRM connector cards**, each with a gear "Manage" button and a status
pill: **VanillaSoft George · 1 destination**, **Email · 1 destination** (connected),
**OnlySales** (Not connected), **VanillaSoft Connett** (Not connected),
**Textdrop** (Not connected), **RINGY** (Not connected), **VanillaSoft
Team / Rahe / Gac / Finazzo** (Not connected; one card reads "Team · Contact Only").
⇒ The integration layer is **"where do leads/events go" — an outbound destinations
registry**: CRM per team + Email, with "1 destination" meaning one configured
sink. Our equivalents today are the buyer `delivery.method` (outbox / email /
CRM webhook) plus `/admin/lead/<id>/resend`; **we have no integrations registry
page**, which this session adds (see §4).

### 1.7 Bulk Leads (`bulk_leads_85.png`, `bulk3_125.png`)
"**Purchase Leads**" button; hint: "You can buy leads in bulk. Choose a date range
and download the leads you need as reports to shop monthly numbers?" — the page is
**empty in the demo** (no bulk orders yet); what is provable is the *motion* exists:
an agent can place a bulk lead order and download a completed report.

### 1.8 Groups (`groups_a_132.png` nav visible on GM; deeper screens not load-bearing)
Groups sidebar level named "John's Personal Groups". (Deeper group screens show
matching mechanics tied to §1.2's Auto-Match: group = pool with owner and members;
groups run their own matching windows; the demo group is Christian George's
"Agents Group Matching" pool of ~11 writers.) Treat details as **inference** —
captions there are partially illegible at 1280px.

## 2. Consistent picture (high-confidence synthesis)
1. The product's core loop is **buy leads in bulk ("networks"/campaigns) → call
   them → log submitted apps → keep money inside the platform's ledger**
   (auto-match bonuses, transfers between agents, "Add Credit" button).
2. **Consumer consent is not part of the demonstrated loop.** Nobody in these
   screens ever shows a consumer: the leads arrive purchased from a network
   (item 1.3 bought via some upstream network we never see), and all contactability
   evidence is implicit in the *purchase* itself (which in our model is exactly the
   thing we refuse to accept: buying is not consent).
3. The **"Avg Cost Per Lead"** tile proves the same economics our routing uses
   (price per lead), just applied agent-side.
4. The **app-of-record privacy posture** (peers' phones/emails in shared tables,
   real consumer emails in a team-visible "Submitted Apps" sheet) is the
   **opposite** of the GS-allude identity model: we store consumers' contact data
   only behind each consumer's own recorded consent, and our admin shows
   near-zero person-level data relative to these views.

## 3. Unverified / not shown (treated as hypotheses; NOTHING in our code relies on these)
- Where the purchased leads originate (upstream networks, their consent posture,
  returns/credits, filtering, real-time vs batch) — never shown.
- The platform's routing/assignment algorithm, caps, or exclusivity terms.
- Any consent wording, TCPA/DNC handling, phone verification, or compliance state.
- Whether "Downloads" CSV columns include consent evidence.
- Whether "Email · 1 destination" is IMAP-send or webhook; VanillaSoft scope.
- The Indian/Android demo duplication ("George Region" title vs "George Simon" hint
  at rebranding); irrelevant to architecture.
- Anything about pricing per segment beyond the single $36 CPL shown.

## 4. What this session builds in GS-allude because of these observations
| Video evidence | New/changed capability | Where |
|---|---|---|
| Integrations = per-sink destinations registry with honest per-connector status | `/admin/integrations` page listing our existing delivery rails + signal sources with machine-checkable status | `app.py`, `templates/admin_integrations.html` |
| "Submitted Apps" logging (post-sale outcome evidence per row) | exists (agent desk dispositions + `lead_outcomes`) — no change | — |
| Auto-Match-style **creditable balance previews** | exists (billing ledger + buyer balances) — no change | — |
| PII columns hidden behind a Columns toggle | we already store PII columns but never display them without consent evidence; admin lead pages stay minimal | — |
| Multi-network source ledger (`networkId`) | signals/leads schemas already carry `source`/`utm_network`-style fields; **unified "where did this lead-signal come from" view** | `engine/sources.py` (new): source registry + status + normalized event view |
| Real consumer email/phone visible to agents with no consent UI | **explicit anti-pattern note** in `docs/PHASE5_STATES.md`: PII never leaves `leads.db`, never displayed without consent, never copied from public posts | docs |

## 5. Impact on the plan (Phase 1 closure)
- The video shows **no consumer-side surface at all** — nothing exists to copy
  into the Check flow; *our* funnel remains strictly the differentiator.
- The video's strongest **transferable mechanic** is the *destinations/integrations
  registry with per-connector honest status* — implemented this session.
- Everything else demonstrated (bulk purchases without a consumer consent trail,
  team-visible consumer PII, auto-matching bonuses) is **deliberately not built**
  and now recorded here as a conscious product decision.
