"""One classification for every acquisition engine, so every visit and every lead lands in the same buckets.

channel (how the person arrived) -> engine (which of our acquisition engines it belongs to):
  search            organic search click              -> ORGANIC
  ai_answer         ChatGPT / Perplexity / Gemini ...  -> ORGANIC
  social_community  Reddit, Facebook groups/pages, IG, TikTok, YouTube, WhatsApp shares, Nextdoor -> ORGANIC
  partner           partner link / QR / printed piece / embed -> PARTNER
  paid_meta         Facebook / Instagram ads            -> PAID
  paid_other        any other paid click (Google, Reddit ads, ...) -> PAID
  referral          another website linked to us        -> ORGANIC
  direct            typed / unknown                     -> DIRECT

category: health vs life is decided by the product of the lead (exact) and, for visits, by campaign or page.
The rules live in config/engines.json so a new channel never needs a code change.
"""
from __future__ import annotations

import json
from pathlib import Path

CFG = json.loads((Path(__file__).resolve().parents[1] / "config" / "engines.json").read_text(encoding="utf-8"))


def _q(items):
    return ",".join("'" + i.replace("'", "''") + "'" for i in items)


def channel_sql(src: str, med: str, partner: str) -> str:
    c = CFG["channels"]
    return f"""(CASE
      WHEN {partner} IS NOT NULL AND {partner} != '' THEN 'partner'
      WHEN lower(COALESCE({med},'')) IN ({_q(c['partner_mediums'])}) THEN 'partner'
      WHEN lower(COALESCE({med},'')) IN ({_q(c['paid_mediums'])}) AND lower(COALESCE({src},'')) IN ({_q(c['meta_sources'])}) THEN 'paid_meta'
      WHEN lower(COALESCE({med},'')) IN ({_q(c['paid_mediums'])}) THEN 'paid_other'
      WHEN lower(COALESCE({med},'')) = 'ai_answer' THEN 'ai_answer'
      WHEN lower(COALESCE({src},'')) IN ({_q(c['search_sources'])}) THEN 'search'
      WHEN lower(COALESCE({src},'')) IN ({_q(c['social_sources'])}) OR lower(COALESCE({med},'')) IN ({_q(c['social_mediums'])}) THEN 'social_community'
      WHEN lower(COALESCE({med},'')) = 'referral' THEN 'referral'
      WHEN lower(COALESCE({src},'')) IN ('', '(direct)', 'direct', 'none') THEN 'direct'
      ELSE 'referral' END)"""


def engine_sql(channel_expr: str) -> str:
    m = CFG["engine_of_channel"]
    whens = " ".join(f"WHEN {channel_expr} = '{ch}' THEN '{eng}'" for ch, eng in m.items())
    return f"(CASE {whens} ELSE 'DIRECT' END)"


def category_event_sql(campaign: str, entry: str) -> str:
    life_c, life_p = CFG["category"]["life_campaigns"], CFG["category"]["life_page_prefixes"]
    health_p = CFG["category"]["health_page_prefixes"]
    life_pages = " OR ".join(f"COALESCE({entry},'') LIKE '{p}%'" for p in life_p) or "0"
    health_pages = " OR ".join(f"COALESCE({entry},'') LIKE '{p}%'" for p in health_p) or "0"
    return f"""(CASE WHEN {campaign} IN ({_q(life_c)}) THEN 'life' WHEN {campaign} LIKE 'oep-2027-health%' THEN 'health'
      WHEN {life_pages} THEN 'life' WHEN {health_pages} THEN 'health' ELSE '-' END)"""


def category_lead_sql() -> str:
    return "(CASE WHEN product = 'health_aca' THEN 'health' WHEN product IS NULL THEN '-' ELSE 'life' END)"


EV_CHANNEL = channel_sql("COALESCE(e.utm_source, v.first_utm_source)", "COALESCE(e.utm_medium, v.first_utm_medium)", "COALESCE(e.partner_id, v.partner_id)")
LD_CHANNEL = channel_sql("utm_source", "utm_medium", "partner_id")
