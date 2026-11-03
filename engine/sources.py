"""SOURCE & SINK REGISTRY — what actually connects, checked at runtime.

Two honest registries gathered into one module so the admin integrations console can
NEVER show a connector as live when it is not.

  SINKS    - where consumer records / notifications / money bookkeeping go (delivery
             rails, email, Stripe, tracking pixels, phone lookup). Status is derived
             from the SAME configuration the running code reads, not from hope.
  SOURCES  - where public intent signals are collected from. Each row states its
             feasible integration path and its honest status:
               COLLECTS    — a collector exists and runs offline-safe (injected tests).
               NEEDS_SETUP — the path is feasible but needs operator config (no cost).
               NEEDS_KEY   — feasible only behind a paid/credential gate (NOT installed).
               TOS_BLOCKED — platform terms disallow the kind of collection we would
                             need; manual research + offline ingest only.
This module is pure metadata + status derivation: it performs NO network requests and
never imports the lead pipeline (route/notify/billing unreachable by design — see
tests/test_sources.py for the AST guard, same discipline as engine/signals.py).
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------- sources
SIGNAL_SOURCES: list[dict] = [
    {"id": "reddit", "label": "Reddit (r/HealthInsurance + all-Reddit ES/EN search)",
     "kind": "public_discussion", "status": "COLLECTS",
     "auth": "none — public search JSON endpoints", "cost": "$0",
     "collector": "python tools/signals_watch.py fetch --network   (explicit operator action; "
                  "or tools/collect.py run --network for ALL eligible sources at once)",
     "notes": "Rate-limited to one request per 2 s, ≤100 items/query, polite UA. Author usernames are dropped before storage.",
     "evidence": "engine/signals.py: fetch_reddit()"},
    {"id": "hackernews", "label": "Hacker News (Algolia public search API)",
     "kind": "public_discussion", "status": "COLLECTS",
     "auth": "none — public API", "cost": "$0",
     "collector": "same fetch command; runs with the Reddit sweep (or tools/collect.py run --network)",
     "notes": "Small volume; affordability/insurtech threads only occasionally consumer-intent.",
     "evidence": "engine/signals.py: fetch_hn()"},
    {"id": "reddit_comments", "label": "Reddit COMMENTS (PullPush public mirror, comment search)",
     "kind": "public_discussion", "status": "COLLECTS",
     "auth": "none — free public mirror comment endpoint (api.pullpush.io); config gate > sources.json > reddit_comments > enabled",
     "cost": "$0",
     "collector": "runs inside the sweep (tools/collect.py run --network) and in the discovery pack",
     "notes": "COMMENT depth: the actual personal question often sits in a replies, not the post title. Same author-dropping + taxonomy + review gates; comments classify as kind=INTEREST when a topic runs.",
     "evidence": "engine/signals.py: fetch_reddit_comments()"},
    {"id": "stackexchange", "label": "Stack Exchange Q&A (health / money / workplace, public API)",
     "kind": "public_discussion", "status": "COLLECTS",
     "auth": "none — api.stackexchange.com search/excerpts at five-sites-per-low volume", "cost": "$0 (quota-tracked per-IP)",
     "collector": "tools/collect.py run --network or discovery (sites/queries in config/sources.json > stackexchange)",
     "notes": "Documented API, no key needed at this volume; remaining per-IP quota recorded in the fetch log. Genuine individual coverage questions (COBRA, job loss, self-employment).",
     "evidence": "engine/signals.py: fetch_stackexchange()"},
    {"id": "hn_comments", "label": "Hacker News comments (Algolia tags=comment)",
     "kind": "public_discussion", "status": "COLLECTS",
     "auth": "none — public Algolia API", "cost": "$0",
     "collector": "tools/collect.py run --network / discovery",
     "notes": "The comment layer where personal job-loss/coverage/affordability threads happen; TOPICS-matching comments classify kind=INTEREST (same rule as reddit rows).",
     "evidence": "engine/signals.py: fetch_hn_comments()"},
    {"id": "apify", "label": "Apify — licensed actors for IG/TikTok/Facebook public items (PAID)",
     "kind": "social_platform", "status": "NEEDS_APPROVAL",
     "auth": "APIFY_TOKEN env + paid_social.apify.approved=true (operator budget record)",
     "cost": "> $0 — paid per actor run + dataset items; REFUSED silently-free",
     "collector": "python tools/collect.py social-paid --network  (the double gate prints its own refusal row until both open)",
     "notes": "Adapters ARE implemented (engine/social_scrapers.py, run-sync per actor with operator-tuned payloads in config > paid_social > apify). Until you set approved=true AND put APIFY_TOKEN in the env: zero network, refusal row, run own sweep unaffected. Instagram/TikTok/Facebook/SERP-forum collection becomes real the day both gates open.",
     "evidence": "engine/social_scrapers.py: run_apify_actors() + tests/test_discovery_people.py"},
    {"id": "brightdata", "label": "Bright Data — licensed web-data collection for IG/TikTok/FB (PAID)",
     "kind": "social_platform", "status": "NEEDS_APPROVAL",
     "auth": "BD_DATA_API_TOKEN env + paid_social.brightdata.approved=true (operator budget record)",
     "cost": "> $0 — paid per dataset snapshot; REFUSED silently-free",
     "collector": "python tools/collect.py social-paid --network  (same double gate, trigger+poll snapshot flow)",
     "notes": "Adapters ARE implemented (engine/social_scrapers.py). Dataset IDs are operator-pasted into config > paid_social > brightdata > datasets once a subscription exists. Until then: zero network, refusal row.",
     "evidence": "engine/social_scrapers.py: run_brightdata_datasets() + tests/test_discovery_people.py"},
    {"id": "mastodon", "label": "Mastodon public hashtag timelines (credential-free)",
     "kind": "public_discussion", "status": "COLLECTS",
     "auth": "none — public instance timeline JSON (default instance: mastodon.social); config gate > sources.json > mastodon",
     "cost": "$0",
     "collector": "tools/collect.py run --network (uses config/sources.json > mastodon > tags)",
     "notes": "Sparse but genuine person-shaped membership: hashtag public posts about coverage/healthcare. Same author-dropping + accept_filter + review gates; a topic match classifies kind=INTEREST (INTEREST_SOURCES rule).",
     "evidence": "engine/signals.py: fetch_mastodon()"},
    {"id": "bluesky", "label": "Bluesky public post search (credential-free, anonymous)",
     "kind": "public_discussion", "status": "COLLECTS",
     "auth": "none — public AppView app.bsky.feed.searchPosts answers anonymous GETs (public.api.bsky.app); config gate > sources.json > bluesky",
     "cost": "$0",
     "collector": "tools/collect.py run --network (uses config/sources.json > bluesky > queries; quoted phrases + lang honoured)",
     "notes": "Freshest page per query (anonymous cursor paging is unreliable per Bluesky docs) + since=<date> recency at the source. Poster identity dropped by construction (permalink built from the DID, not the handle); same accept_filter + review gates, topic match => kind=INTEREST.",
     "evidence": "engine/signals.py: fetch_bluesky() — endpoint per docs.bsky.app + 05-31-2024 search guide"},
    {"id": "lemmy", "label": "Lemmy anonymous comment search (credential-free)",
     "kind": "public_discussion", "status": "COLLECTS",
     "auth": "none — public /api/v3/search?type_=Comments&sort=New anonymous reads on configured instances (default lemmy.world); config gate > sources.json > lemmy",
     "cost": "$0",
     "collector": "tools/collect.py run --network (uses config/sources.json > lemmy > instances + queries)",
     "notes": "Comment layer of Lemmy communities (the person-shaped parts of v3 API docs, live-verified Oct 2026). Comment author/creator dropped; community NAME stays the community label. Same accept_filter + review gates.",
     "evidence": "engine/signals.py: fetch_lemmy()"},
    {"id": "reddit_fallback", "label": "Reddit FALLBACK (PullPush public mirror API)",
     "kind": "public_discussion", "status": "COLLECTS",
     "auth": "none — free public mirror endpoint (api.pullpush.io); config gate > sources.json > reddit_fallback > enabled",
     "cost": "$0",
     "collector": "runs in the same sweep after reddit.com (tools/collect.py run --network); fills what the official search denied",
     "notes": "Reachable from THIS network where reddit.com 403-blocks its datacenter IP. Community-run mirror: data is an unverified copy, same author-dropping + dedupe + review gates, source label reddit_fallback for provenance.",
     "evidence": "engine/signals.py: fetch_reddit_fallback() — verified live from this machine Oct 27"},
    {"id": "youtube", "label": "YouTube channel uploads (public Atom feeds)",
     "kind": "public_video", "status": "NEEDS_SETUP",
     "auth": "none — public feeds; requires channel_ids pasted by the operator into config/sources.json",
     "cost": "$0",
     "collector": "tools/signals_watch.py fetch --network   (after config/sources.json lists channel ids; "
                  "or tools/collect.py run --network)",
     "notes": "feed: https://www.youtube.com/feeds/videos.xml?channel_id=<ID>. Search API needs a Google key = NOT installed. No author handles stored; channel NAME is the community label.",
     "evidence": "engine/signals.py: fetch_youtube()",
     "setup": "1) Open a channel page → share → copy its UC… channel-id (or use the channel URL's /channel/UC… part). 2) Paste the ID into config/sources.json > youtube_channels. 3) Run tools/collect.py run --network. No key, no cost."},
    {"id": "forums", "label": "Public forums & Q&A sites (per-site manual ingest)",
     "kind": "public_discussion", "status": "NEEDS_SETUP",
     "auth": "varies per site; no universal public API exists",
     "cost": "$0",
     "collector": "offline ingest: tools/signals_watch.py ingest file.json (durable URL required)",
     "notes": "Each forum is a one-off: some have public JSON, most require human browsing. The ingest path records hand-found posts with the same whitelist + review gates.",
     "evidence": "docs/SOURCES.md",
     "setup": "For each forum YOU pick: browse the public thread, save the post JSON as {\"signals\":[{title,url,snippet,...}]}, then tools/signals_watch.py ingest file.json. Durable URL required; same review gates."},
    {"id": "facebook", "label": "Facebook (public pages, groups, discussions)",
     "kind": "social_platform", "status": "TOS_BLOCKED",
     "auth": "Meta Graph API app + business verification; page/group tokens; App Review",
     "cost": "$0 (API) but requires a signed platform Term Addendum + review cycle",
     "collector": "NOT implemented — Graph endpoints we'd need are username-heavy and the platform bars bulk public-group collection",
     "notes": "Until credentialed + reviewed: Manual research, record finds with offline ingest.",
     "evidence": "docs/SOURCES.md",
     "setup": "Meta developer app → business verification → platform Term Addendum → App Review for the page/group permission. Only then does an adapter make sense — the access method is the blocker, not the code."},
    {"id": "instagram", "label": "Instagram (public posts, reels, comments)",
     "kind": "social_platform", "status": "TOS_BLOCKED",
     "auth": "Meta Graph API (Instagram Basic Display / Business), App Review",
     "cost": "$0 API / requires review; scraping violates platform ToS",
     "collector": "NOT implemented — hashtag/comment collection is ToS-restricted; hashtag search of public business accounts is only allowed to the account owner",
     "notes": "Watch: owner-account hashtag dashboards; creators collabs come later via the partner channel.",
     "evidence": "docs/SOURCES.md",
     "setup": "Operator path: IG Business account we own + Meta App Review for instagram_business scopes → set IG_GRAPH_ACCESS_TOKEN + IG_OWNED_USER_ID. engine/platforms.py activates via env, no rewrite."},
    {"id": "tiktok", "label": "TikTok (public videos, hashtag/search)",
     "kind": "social_platform", "status": "TOS_BLOCKED",
     "auth": "Commercial Content API / Research API — application + approval required",
     "cost": "$0 to apply; not applied for",
     "collector": "NOT implemented — no application has been filed yet",
     "notes": "When/if the Research API is granted: add fetch_tiktok() here, same whitelist/review loop.",
     "evidence": "docs/SOURCES.md",
     "setup": "Operator actions: apply at developers.tiktok.com → Research API (requires company verification + approval). Nothing in code can do this; engine/platforms.py adapter activates the day credentials land."},
    {"id": "google_news", "label": "Google News RSS (carrier-exit / market news headlines)",
     "kind": "search_results", "status": "COLLECTS",
     "auth": "none — public RSS for news.google.com searches",
     "cost": "$0",
     "collector": "tools/signals_watch.py fetch --network   (query terms in config/sources.json > google_news_queries; "
                  "or tools/collect.py run --network)",
     "notes": "News ≠ consumer intent: validates context for carrier-exit content, never a person. Headlines survive only the taxonomy filter.",
     "evidence": "engine/signals.py: fetch_google_news()"},
    {"id": "webreader", "label": "Public web pages + ScrapeGraphAI extraction (AI-structured reader)",
     "kind": "public_web", "status": "COLLECTS",
     "auth": "none — public pages, robots.txt respected; AI extraction needs a LOCAL model (Ollama) or an approved billed provider",
     "cost": "$0 in fallback mode; SGA mode needs a locally-hosted model we run",
     "collector": "tools/signals_watch.py web --network   or tools/collect.py run --network (sweep) "
                  "— seeds in config/sources.json > webreader",
     "notes": "Working now with the stdlib reader (fallback_reader mode). When scrapegraphai is installed with a local Ollama model, the SAME loop upgrades to instruction-driven structured extraction — no schema/review changes. OpenAI-backed graphs never run without explicit operator approval (billed).",
     "evidence": "engine/web_extract.py: sga_status(), collect_site(), extract_sga()"},
    {"id": "official_cms", "label": "CMS official open-data catalog (Marketplace enrollment + plan datasets)",
     "kind": "official_data", "status": "COLLECTS",
     "auth": "none — data.cms.gov/data.json public DCAT catalog (verified live from this machine Oct 27)", "cost": "$0",
     "collector": "python tools/collect.py official-data   (explicit operator action; also runs inside tools/collect.py run --network)",
     "notes": "THE robots-safe healthcare.gov alternative: one catalog request per run, relevance-filtered to Marketplace enrollment / plan-year / subsidy datasets, ingested as kind=MARKET market intelligence. No crawler, no access-control bypass.",
     "evidence": "engine/official_data.py: fetch_cms_open_data()"},
    {"id": "hcgov", "label": "Healthcare.gov official content API (EN + ES help pages, machine-readable)",
     "kind": "official_data", "status": "COLLECTS",
     "auth": "none — healthcare.gov/api/index.json, the site's OWN published reuse feed (link-attribution terms per endpoint)", "cost": "$0",
     "collector": "python tools/collect.py official-data   (explicit operator action; also runs inside tools/collect.py run --network)",
     "notes": "Blocks the DISALLOWED /api/ crawling path by using the deliberate machine-readable catalog instead: official help pages, Spanish titles included, taxonomy-filtered, kind=MARKET. Generic crawling of healthcare.gov stays off.",
     "evidence": "engine/official_data.py: fetch_healthcare_gov_content()"},
    {"id": "search_web", "label": "Web search results pages",
     "kind": "search_results", "status": "NEEDS_KEY",
     "auth": "SERP API key (paid, gated) — NOT INSTALLED, not requested",
     "cost": "> $0 — refused without operator approval",
     "collector": "NOT implemented",
     "notes": "No scraping of Google/Bing results; a licensed SERP API is the only compliant path and requires a purchase decision.",
     "evidence": "docs/SOURCES.md",
     "setup": "Purchase decision REQUIRED (this is a paid API): pick a SERP provider, get a key, record approval, then add an adapter here. Deliberately not built until you approve spending."},
    {"id": "publishers", "label": "Insurance publishers, lead providers, partner feeds",
     "kind": "lead_marketplace", "status": "AGREEMENT_REQUIRED",
     "auth": "per-vendor signed agreement + consent-certificate requirement (TrustedForm/Jornaya)",
     "cost": "per-lead fees; recorded in config/lead_supply.json",
     "collector": "NOT implemented — every external row stays supply-DISABLED until a signed agreement lands",
     "notes": "Resale is NOT AUTHORIZED by default; buying a lead is not permission to redistribute it.",
     "evidence": "config/lead_supply.json + /admin/intel/supply"},
    {"id": "first_party", "label": "GS-allude first-party (Check funnel, tracked links, QR)",
     "kind": "own_funnel", "status": "COLLECTS",
     "auth": "n/a — our own app", "cost": "hosting only",
     "collector": "always on: /check submissions + events table (self-hosted analytics)",
     "notes": "The ONLY source that produces consumer inquiries, because the consumer chose us. Signals never feed it.",
     "evidence": "engine/funnel.py, engine/db.py"},
]

SOURCE_STATUS_ORDER = ["COLLECTS", "NEEDS_SETUP", "NEEDS_KEY", "NEEDS_CREDENTIALS", "TOS_BLOCKED", "AGREEMENT_REQUIRED"]


# This module is pure metadata + status derivation: it performs NO network requests and
# never imports the lead pipeline (route/notify/billing unreachable by design — see
# tests/test_sources.py for the AST guard, same discipline as engine/signals.py).

# ------------------------------------------------------------------ prospect sources
PROSPECT_SOURCES: list[dict] = [
    {"id": "manual_csv", "label": "Operator research + CSV import (public directories, license boards, chambers, referral partners)",
     "kind": "business_discovery", "status": "COLLECTS", "auth": "none — manually researched public business records", "cost": "$0",
     "collector": "Admin > Prospects (add/import) or tools/prospects.py import csv --file X --source manual",
     "notes": "Working today: rows land in data/prospects.db as decision NEW, verify_state UNVERIFIED. No contact action exists anywhere in this loop.",
     "evidence": "engine/prospects.py: ingest/import_csv"},
    {"id": "google_places", "label": "Google Places API (official, billed per request)",
     "kind": "business_discovery", "status": "NEEDS_KEY", "auth": "GOOGLE_PLACES_API_KEY + explicit budget approval recorded in config/sources.json > places > approved:true", "cost": "per-request billing — refused without approval",
     "collector": "engine/prospects.py fetch_places()",
     "notes": "The ToS-clean path once a paid budget is approved. Adapter exists and is gated: without key+approval it refuses and names the missing dependency. NO purchase happens from code.",
     "evidence": "engine/prospects.py: fetch_places, _places_approved"},
    {"id": "maps_scraper_kit", "label": "google-maps-scraper-kit (MIT) — headless browser over google.com/maps",
     "kind": "business_discovery", "status": "TOS_BLOCKED", "auth": "not applicable — access method itself violates Google's ToS (its own README states this)", "cost": "$0 but access risk",
     "collector": "NOT integrated — MIT license does not change the access method",
     "notes": "Blocked by our access boundary; documented, not implemented. Permitted alternative: the official Places API above.",
     "evidence": "docs/SOURCES.md + engine/prospects.py docstring"},
]


def sources_catalog() -> list[dict]:
    return [dict(s) for s in SIGNAL_SOURCES]


# --------------------------------------------------------------------------- sinks
def _env_ok(name: str) -> bool:
    return bool((os.environ.get(name) or "").strip())


def sink_registry(con=None, marketplace_cfg: dict | None = None, buyers: list[dict] | None = None) -> list[dict]:
    """Delivery/notification rails with runtime-derived status. `con` is the leads DB
    connection (read-only use) when available so sinks can show their ACTIVITY counts."""
    live = (os.environ.get("LEADENGINE_LIVE_DELIVERY") == "1")

    def delivery_counts():
        if con is None:
            return {}
        try:
            rows = {r[0]: (r[1], r[2]) for r in con.execute(
                "SELECT method, COUNT(*), SUM(status='SENT') FROM deliveries GROUP BY 1")}
            return rows
        except Exception:                      # noqa: BLE001 - schema not migrated yet
            return {}

    dc = delivery_counts()
    outbox_n = int(dc.get("outbox", (0, 0))[0])
    webhook_rows = dc.get("webhook", (0, 0))
    sinks: list[dict] = [
        {"id": "outbox", "label": "Outbox delivery (dry-run JSON payment payload files)",
         "status": ("COLLECTS" if outbox_n else "NEEDS_SETUP"),
         "auth": "none (filesystem)", "cost": "$0",
         "detail": f"{outbox_n} delivery payload(s) written",
         "notes": "Default rail. Nothing leaves the machine; the buyer portal serves the same payload.",},
        {"id": "email", "label": "Email delivery (SMTP → buyer desk / consumer summary)",
         "status": ("COLLECTS" if (_env_ok("SMTP_HOST") and live) else
                    ("NEEDS_SETUP" if _env_ok("SMTP_HOST") else "NOT_CONFIGURED")),
         "auth": "SMTP_HOST/SMTP_PORT/SMTP_FROM + LEADENGINE_LIVE_DELIVERY=1",
         "cost": " SMTP provider cost when live",
         "detail": f"live_delivery={live}",
         "notes": "Dry-runs print the email body to the outbox log instead of sending.",},
        {"id": "webhook", "label": "Buyer CRM webhook delivery (HMAC-signed POST)",
         "status": "COLLECTS" if (buyers and any(b.get("delivery", {}).get("method") == "webhook"
                                                 and b["delivery"].get("webhook_url") for b in buyers))
                    else "NEEDS_SETUP",
         "auth": "per-buyer webhook_url + optional webhook_secret (HMAC-SHA256)",
         "cost": "$0",
         "detail": f"{webhook_rows[0]} webhook delivery row(s), {webhook_rows[1]} sent live",
         "notes": "Live POSTs only when LEADENGINE_LIVE_DELIVERY=1; the sale payload always lands in outbox first.",},
        {"id": "stripe", "label": "Stripe billing rail (buyer credit packs, webhook-verified)",
         "status": ("COLLECTS" if _env_ok("STRIPE_SECRET_KEY") else "NOT_CONFIGURED"),
         "auth": "STRIPE_SECRET_KEY + STRIPE_WEBHOOK_SECRET",
         "cost": "Stripe per-transaction fees",
         "detail": ("live payments ON (production)" if (marketplace_cfg or {}).get("payments", {}).get("live_payments")
                    and os.environ.get("LEADENGINE_ENV") == "production" else "test-mode / sandbox only"),
         "notes": "Credits are granted ONLY by the signature-verified webhook; no code moves money.",},
        {"id": "sandbox_checkout", "label": "Sandbox checkout (rehearsal, BOOKS TO TEST LANE ONLY)",
         "status": "COLLECTS" if (marketplace_cfg or {}) else "NEEDS_SETUP",
         "auth": "built-in", "cost": "$0",
         "detail": "in production this rail refuses",
         "notes": "Lets a buyer rehearse purchase flows with zero real money.",},
        {"id": "otp", "label": "Phone verification (Twilio Verify)",
         "status": ("COLLECTS" if os.environ.get("OTP_PROVIDER") == "twilio_verify"
                    and _env_ok("TWILIO_ACCOUNT_SID") else "NOT_CONFIGURED"),
         "auth": "OTP_PROVIDER=twilio_verify + TWILIO_ACCOUNT_SID/AUTH_TOKEN/VERIFY_SID",
         "cost": "Twilio per-verification",
         "detail": "off by default; enable via otp_step in site.json",
         "notes": "Optional lead-quality gate; TEST traffic skips real SMS.",},
        {"id": "meta_capi", "label": "Meta Conversions API (own ad campaigns ONLY)",
         "status": ("COLLECTS" if _env_ok("META_CAPI_TOKEN") else "NOT_CONFIGURED"),
         "auth": "META_CAPI_TOKEN + META_PIXEL_ID + LEADENGINE_LIVE_DELIVERY=1",
         "cost": "ad spend",
         "detail": "no health data ever sent",
         "notes": "Used only for our own traffic campaigns; disabled by default.",},
        {"id": "ga4", "label": "GA4 analytics tag (own pages)",
         "status": ("COLLECTS" if _env_ok("GA4_ID") else "NOT_CONFIGURED"),
         "auth": "GA4_ID", "cost": "$0",
         "detail": "first-party analytics only",
         "notes": "Own-site events; never another company's click data.",},
        {"id": "phone_lookup", "label": "Phone line-type lookup provider",
         "status": ("COLLECTS" if _env_ok("PHONE_LOOKUP_PROVIDER") else "NOT_CONFIGURED"),
         "auth": "PHONE_LOOKUP_PROVIDER=<vendor> + vendor keys",
         "cost": "vendor pricing",
         "detail": "off by default",
         "notes": "Lead-quality enrichment only; never used for contact decisions.",},
    ]
    return sinks


def signals_counts(con=None) -> dict:
    """Per-source signal counts for the console (read-only over data/signals.db)."""
    if con is None:
        return {}
    try:
        rows = {r[0]: {"signals": r[1], "actioned": r[2]} for r in con.execute(
            "SELECT source, COUNT(*), SUM(status='ACTIONED') FROM signals GROUP BY 1")}
        return rows
    except Exception:                          # noqa: BLE001
        return {}
