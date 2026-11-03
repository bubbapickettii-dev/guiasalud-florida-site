"""Ad-platform conversion tracking.

Browser: Meta Pixel / GA4 tags injected only when IDs are set in config/site.json.
Server:  Meta Conversions API 'Lead' event, de-duplicated with the browser event via event_id = lead_id.
Privacy: only hashed email/phone + standard event; NO answers, health info, product or campaign details
         are sent to Meta (Meta restricts sharing health-related data; insurance is a special ad category).
Dry-run unless META_CAPI_TOKEN is set - the payload is written to outbox/capi/ so you can inspect it.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path


def _h(v: str) -> str:
    return hashlib.sha256(v.strip().lower().encode()).hexdigest() if v else ""


def meta_capi_lead(lead: dict, request, site: dict, outbox: Path) -> str:
    pixel = site.get("meta_pixel_id")
    payload = {"data": [{
        "event_name": "Lead", "event_time": int(time.time()), "event_id": lead["lead_id"], "action_source": "website",
        "event_source_url": (lead.get("landing_url") or "").split("?")[0],
        "user_data": {"em": [_h(lead.get("email", ""))], "ph": [_h("1" + lead["phone"]) if lead.get("phone") else ""],
                      "client_ip_address": request.headers.get("X-Forwarded-For", request.remote_addr),
                      "client_user_agent": request.headers.get("User-Agent", ""),
                      "fbc": request.cookies.get("_fbc", ""), "fbp": request.cookies.get("_fbp", "")}}]}
    d = outbox / "capi"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{lead['lead_id']}.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")
    token = os.environ.get("META_CAPI_TOKEN")
    if not (token and pixel and os.environ.get("LEADENGINE_LIVE_DELIVERY") == "1"):
        return "DRY_RUN"
    import requests
    r = requests.post(f"https://graph.facebook.com/v19.0/{pixel}/events", params={"access_token": token}, json=payload, timeout=10)
    return "SENT" if r.ok else f"FAILED {r.status_code}"
