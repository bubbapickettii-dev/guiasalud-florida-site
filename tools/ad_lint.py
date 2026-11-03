"""Compliance lint for every piece of acquisition copy before it goes out.
Checks paid ads (config/meta_campaigns.json) strictly and organic copy (config/distribution.json) for the hard rules.
  python tools/ad_lint.py          -> exit 1 if any ERROR
Rules:
- never: '$0', 'free insurance/health insurance/coverage', 'guaranteed', 'government program', 'official', 'Obamacare plan from the government'
- paid only (Meta personal-attributes policy): no assertions about the viewer's health, finances, job loss, age, etc.
  e.g. 'Are you...', 'Lost your job', 'Laid off?', 'your diabetes', 'low income?', 'over 50?'
- paid: primary text <= 300 chars, headline <= 45 chars; url must carry utm_source/utm_medium/utm_campaign/x
- everything: must not ask people to share personal details in comments/DMs
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NEVER = [r"\$\s?0\b", r"\bfree (health )?(insurance|coverage)\b", r"\bseguro (médico )?gratis\b", r"\bguarantee", r"\bgarantizad", r"\bofficial\b",
         r"\boficial del gobierno\b", r"\bgovernment program\b", r"\bprograma del gobierno\b"]
ATTR = [r"\bare you\b", r"\b¿?eres\b", r"\blost your job\b", r"\blaid off\?", r"\b¿?te despidieron\b", r"\byour (diabetes|condition|illness|debt|credit)\b",
        r"\blow income\?", r"\bover (50|60|65)\?", r"\bunemployed\?", r"\bbroke\?", r"\bsin trabajo\?"]
ASK = [r"\b(comment|dm|message) (me|us) your\b", r"\bdéjanos tu (número|teléfono)\b", r"\bsend (me|us) your (number|phone)\b"]


def lint() -> list[tuple[str, str, str]]:
    out = []
    meta = json.loads((ROOT / "config" / "meta_campaigns.json").read_text())
    for c in meta["campaigns"]:
        if c.get("special_ad_category") != "FINANCIAL_PRODUCTS_SERVICES":
            out.append(("ERROR", c["name"], "special ad category must be FINANCIAL_PRODUCTS_SERVICES"))
        for a in c["ads"]:
            txt = " ".join([a["primary_text"], a["headline"], a["description"]])
            for rx in NEVER + ATTR + ASK:
                if re.search(rx, txt, re.I):
                    out.append(("ERROR", a["ad_id"], f"matches /{rx}/"))
            if len(a["primary_text"]) > 300:
                out.append(("ERROR", a["ad_id"], "primary text > 300 chars"))
            if len(a["headline"]) > 45:
                out.append(("WARN", a["ad_id"], f"headline {len(a['headline'])} chars (>45 truncates)"))
            for k in ("utm_source=", "utm_medium=", "utm_campaign=", "x="):
                if k not in a["url"]:
                    out.append(("ERROR", a["ad_id"], f"url missing {k}"))
            for f_ in (a["creative_feed"], a["creative_story"]):
                if not (ROOT / f_).exists():
                    out.append(("WARN", a["ad_id"], f"creative not rendered yet: {f_} (run tools/build_ad_creatives.py)"))
    for p in json.loads((ROOT / "config" / "distribution.json").read_text())["placements"]:
        for k in ("copy_en", "copy_es"):
            t = p.get(k) or ""
            for rx in NEVER + ASK:
                if re.search(rx, t, re.I):
                    out.append(("ERROR", p["id"], f"{k} matches /{rx}/"))
    return out


if __name__ == "__main__":
    res = lint()
    for r in res:
        print(*r, sep=" | ")
    print(f"{sum(1 for r in res if r[0] == 'ERROR')} errors, {sum(1 for r in res if r[0] == 'WARN')} warnings")
    sys.exit(1 if any(r[0] == "ERROR" for r in res) else 0)
