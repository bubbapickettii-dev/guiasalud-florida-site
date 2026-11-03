"""Launch readiness: prints every remaining blocker, grouped by the dependency it belongs to.
   python launch_check.py            (reads config/ + current environment variables)
Nothing here contacts anyone or spends anything; it only reads config and env.

The same computation is exposed as launch_check.collect(cfg_dir=None) so the operator
readiness view (/admin/readiness) can render these blockers live from the app (against the
same config copy the running app uses) — the CLI report and the web view can never drift.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from engine import hosting

B = Path(__file__).resolve().parent


def collect(cfg_dir: Path | None = None):
    """Compute the launch blockers + their category labels against the given config dir
    (defaults to this module's own config/). Read-only: reads config JSON + os.environ."""
    cdir = Path(cfg_dir) if cfg_dir else B / "config"
    cfg = lambda n: json.loads((cdir / f"{n}.json").read_text())  # noqa: E731
    S, buyers, consent = cfg("site"), cfg("buyers"), cfg("consent")
    env = os.environ.get
    # ONE origin resolver (engine/hosting.py): BASE_URL > the host-assigned URL > config/site.json.
    base = hosting.public_origin(S).rstrip("/")
    base_src = hosting.origin_source(S)
    storage = hosting.storage_mode(env("LEADENGINE_DB") or "")

    block = {k: [] for k in ["1 Domain", "2 Hosting", "3 Buyer activation (commercial)", "4 Legal review", "5 Tracking", "6 Ad account", "7 Partner", "8 Organic",
                             "9 Payment rail", "10 Lead delivery"]}
    CATEGORY = {   # how to read this report (deploy checklist = docs/DEPLOYMENT.md)
        "1 Domain": "REQUIRES CONFIGURATION", "2 Hosting": "REQUIRES CONFIGURATION",
        # the licensed agency itself is owner-confirmed (2026-11-02): what remains is the commercial
        # activation step the operator controls, not a supplier to find.
        "3 Buyer activation (commercial)": "REQUIRES MY APPROVAL",
        "4 Legal review": "REQUIRES EXTERNAL VERIFICATION", "5 Tracking": "REQUIRES CONFIGURATION",
        "6 Ad account": "REQUIRES MY APPROVAL", "7 Partner": "REQUIRES MY APPROVAL", "8 Organic": "REQUIRES MY APPROVAL",
        "9 Payment rail": "REQUIRES MY APPROVAL", "10 Lead delivery": "REQUIRES MY APPROVAL", }
    if not hosting.is_absolute_origin(base) or re.search(r"localhost|127\.0\.0\.1|example\.", base, re.I):
        block["1 Domain"].append(f"The public origin is '{base or '(unset)'}'. A free Render web service publishes its assigned "
                               "origin (https://<service>.onrender.com) in RENDER_EXTERNAL_URL, and the app now uses it "
                               "automatically — so this clears as soon as the service exists, before any domain is bought. "
                               "Canonical URLs, sitemap, robots, QR codes and prepared links all read that one value; a "
                               "custom domain later is just BASE_URL. See docs/RENDER_DEPLOY.md.")
    # An https origin — including the one a host assigns itself (RENDER_EXTERNAL_URL) — is READY: this
    # group only exists to catch a deployment whose links would point somewhere unreachable. Which
    # origin is in force, and where it came from, is shown on /healthz and on Admin > Readiness.
    del base_src
    if env("LEADENGINE_ENV") != "production":
        block["2 Hosting"].append("Not running with LEADENGINE_ENV=production (guards: strong admin token, demo buyers hidden from real traffic).")
    if not env("ADMIN_TOKEN") or len(env("ADMIN_TOKEN", "")) < 16:
        block["2 Hosting"].append("ADMIN_TOKEN missing or shorter than 16 characters.")
    if storage == "ephemeral" and hosting.storage_declared() == "ephemeral":
        block["2 Hosting"].append(
            "READ-ONLY PUBLIC PREVIEW (LEADENGINE_STORAGE=ephemeral, as the free Render blueprint sets it): this "
            "host keeps nothing, so consumer submissions, buyer applications and contact messages are refused "
            "instead of stored and lost. The WEBSITE is fully served (pages, check in EN+ES, canonicals, sitemap, "
            "HTTPS); collecting leads, consent evidence or payments needs a persistent disk (render.yaml: starter "
            "+ /var/data, or PythonAnywhere /home/<user>/data) declared as LEADENGINE_STORAGE=persistent. "
            "See docs/RENDER_DEPLOY.md.")
    elif storage == "ephemeral":
        block["2 Hosting"].append(
            "Database storage is EPHEMERAL for LEADENGINE_DB and LEADENGINE_STORAGE is unset: consent evidence "
            "must survive 10 years and buyer/payment rows are money, so this host cannot keep what it collects. "
            "Declare it — LEADENGINE_STORAGE=persistent when the path is a mounted volume (paid Render /var/data, "
            "PythonAnywhere /home/<user>/data), or LEADENGINE_STORAGE=ephemeral on a host with no disk (the app "
            "then runs a read-only public preview and refuses submissions instead of storing and losing them). "
            "See docs/RENDER_DEPLOY.md.")
    if env("INTEL_DB"):
        block["2 Hosting"].append("INTEL_DB is set. Do NOT upload the public-record prospect DB to the server; briefs ship as config/regional_briefs.json.")
    # Identity: the business name and public location are configured (Gia Salud, Miami, Florida). A
    # registered STREET address is deliberately not required for this website configuration, and an
    # unsupplied value is never printed (see app.business_identity).
    if not (S.get("legal_entity") or env("LEGAL_ENTITY")) or str(S.get("legal_entity") or "").startswith("["):
        block["4 Legal review"].append("LEGAL_ENTITY missing (the business name printed in the footer, privacy and terms).")
    if not (S.get("location") or env("LOCATION")):
        block["4 Legal review"].append("LOCATION missing (the public 'Miami, Florida' line in the footer and legal pages).")
    if not ((S.get("support_email") or S.get("contact_email")) or env("SUPPORT_EMAIL") or env("CONTACT_EMAIL")):
        block["4 Legal review"].append("Support inbox not configured yet (SUPPORT_EMAIL). Tracked until a real inbox is verified: "
                                       "until then /contact uses the on-site form and no address is displayed — never a fabricated one.")
    real_health = [b for b in buyers if "health_aca" in b.get("products", []) and not b.get("is_demo") and b.get("active")]
    if not real_health:
        block["3 Buyer activation (commercial)"].append(
            "Owner-confirmed: a licensed Florida agency is in place, so this is no longer a supplier blocker. "
            "To START LEAD SALES, enter that agency's row in config/buyers.json (exact legal name as it must appear in consent, "
            "FL 2-15/2-40 licence #, NPN, the operator's recorded licence + NPN checks, prices per tier, cap, delivery). "
            "Until then the site runs no-buyer mode and collects nothing — the WEBSITE is not blocked by it.")
    if env("LEADENGINE_LIVE_DELIVERY") != "1" or not env("SMTP_HOST"):
        note = "Note: email delivery off; agents read leads in their portal /agent/<portal_token> (fine to start)."
        block["10 Lead delivery"].append(note + " (this is the safe default - turning delivery on is an operator approval, not a config error).")
    if not S.get("consent_reviewed_by_counsel"):
        block["4 Legal review"].append(f"RECOMMENDED, not technically blocking: consent EN+ES (version {consent['version']}), privacy and terms not reviewed by counsel. "
                                       "Questions for counsel: docs/GO_LIVE.md section 'Legal review'.")
    if not (S.get("meta_pixel_id") or env("META_PIXEL_ID")) and not (S.get("ga4_id") or env("GA4_ID")):
        block["5 Tracking"].append("Optional. First-party tracking (events, /go links, experiment ids) already works without any pixel. Add GA4_ID / META_PIXEL_ID only when a paid test is approved.")
    block["6 Ad account"].append("Only for paid tests: Meta/Google ad account with insurance Special Ad Category acknowledged, payment method, your approval to spend. Kit: assets/ads/oep_meta_kit.csv (UNTESTED).")
    block["7 Partner"].append("Partners are status 'PROSPECT - not contacted'. You approve and send the first pitch (partner_kit/pitches.md). Admin > Partners creates link + QR + guide instantly.")
    if env("ALLOW_INDEXING") != "1":
        block["8 Organic"].append("ALLOW_INDEXING=0: pages are hidden from Google, which switches off the search engine. Set ALLOW_INDEXING=1 at launch.")
    block["8 Organic"].append("Work the queue at /admin/organic: approve, post/send, log minutes; results show per item.")

    # payment rail + delivery state (read-only, no external contact)
    try:
        import engine.payments as _pay
        _mk_path = cdir / "marketplace.json"
        _rail_mode = _pay.mode()
        _rail_ok = _pay.rail_approved(json.loads(_mk_path.read_text(encoding="utf-8")) if _mk_path.exists() else {})
        _wh_ok = bool(env("STRIPE_WEBHOOK_SECRET"))
        block["9 Payment rail"].append(f"mode={_rail_mode} | approved={_rail_ok} | webhook secret set={_wh_ok}. "
                                       + ("TEST rail - safe, but bookings land in the Stripe TEST dashboard, not your bank. " if _rail_mode == "test" else "")
                                       + ("LIVE rail: real money. Turning it on is an explicit operator approval. " if _rail_mode == "live" and _rail_ok else "")
                                       + "Live processing stays OFF until STRIPE keys + marketplace.payments.live_payments + LEADENGINE_ENV=production are all set deliberately.")
        if _rail_mode == "off":
            block["9 Payment rail"].append("Rail off: buyers use the operator top-up path (recorded, audited, no card payment).")
    except Exception as e_:  # config/env malformed: report instead of crashing the read-only check
        block["9 Payment rail"].append(f"could not read payment config ({e_})")
    block["10 Lead delivery"].append(
        ("LEADENGINE_LIVE_DELIVERY=1: consumer data WILL be emailed/posted to configured buyers when sold. "
         "Approved by whom? Only flip with a signed agent + legal sign-off.") if env("LEADENGINE_LIVE_DELIVERY") == "1"
        else "LEADENGINE_LIVE_DELIVERY is off (default): every delivery is a dry run to outbox/. Never sold contact data leaves the server.")

    return block, CATEGORY, base


def main() -> None:
    block, CATEGORY, base = collect()
    print(f"Base URL: {base}")
    for k, v in block.items():
        if not v:
            print(f"\n== {k}: READY - verified in code/tests (nothing to configure)")
        else:
            print(f"\n== {k}: [{CATEGORY[k]}] {len(v)} item(s)")
        for x in v:
            print("  -", x)


if __name__ == "__main__":
    main()
