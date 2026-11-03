"""Bridge to the public-record MARKET INTELLIGENCE layer (../leadgen/data/leadgen.db).

Public records are AUDIENCE SIGNALS, not leads and not a contact list. This module never reads a public
phone number or email address (tests/test_engine.py greps for it). Uses:
  1. audience sizing   - how many signals per campaign, how fast new ones appear, where (ZIP/city)
  2. geo packs         - top ZIPs/cities per campaign -> ad geo-targets, EDDM routes, local pages, flyer drops
  3. partner targets   - businesses that sit in front of each audience (brokerages onboarding the most
                         new agents, salons where many new beauty pros work) -> partnership channel
  4. lead context      - when a consumer submits on their own, check whether they match a public
                         trigger (e.g. newly licensed realtor) -> score context only. The lead's phone is
                         ALWAYS the one the consumer typed; nothing from the public record is copied onto a lead.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

SOUTH_FL = ("330", "331", "332", "333", "334")


def _con(path: Path):
    if not path.exists():
        return None
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def audience(intel_db: Path, campaigns: list[dict]) -> list[dict]:
    con = _con(intel_db)
    out = []
    d30 = (date.today() - timedelta(days=30)).isoformat()
    d90 = (date.today() - timedelta(days=90)).isoformat()
    for c in campaigns:
        trig = c.get("source_signals") or []
        row = {"campaign": c["id"], "headline": c["headline"], "signals": ", ".join(t.replace("_", " ").title() for t in trig) or "(no public signal - content/partner driven)",
               "audience_signals": 0, "new_last_30d": 0, "per_week_90d": 0.0, "south_fl": 0, "top_zips": "", "top_cities": ""}
        if con and trig:
            ph = ",".join("?" * len(trig))
            # count distinct prospects whose trigger list contains one of the campaign signals
            like = " OR ".join("triggers LIKE ?" for _ in trig)
            args = [f"%{t}%" for t in trig]
            r = con.execute(f"SELECT COUNT(*) n, SUM(CASE WHEN substr(zip5,1,3) IN ('330','331','332','333','334') THEN 1 ELSE 0 END) sfl "
                            f"FROM prospects WHERE {like}", args).fetchone()
            row.update(audience_signals=r["n"] or 0, south_fl=r["sfl"] or 0)
            row["new_last_30d"] = con.execute(f"SELECT COUNT(*) FROM signals WHERE trigger IN ({ph}) AND event_date >= ?",
                                              trig + [d30]).fetchone()[0]
            row["per_week_90d"] = round(con.execute(f"SELECT COUNT(*) FROM signals WHERE trigger IN ({ph}) AND event_date >= ?",
                                                    trig + [d90]).fetchone()[0] / 12.86, 1)
            row["top_zips"] = ", ".join(f"{z} ({n})" for z, n in con.execute(
                f"SELECT zip5, COUNT(*) n FROM prospects WHERE ({like}) AND zip5 != '' GROUP BY 1 ORDER BY 2 DESC LIMIT 10", args))
            row["top_cities"] = ", ".join(f"{z.title()} ({n})" for z, n in con.execute(
                f"SELECT city, COUNT(*) n FROM prospects WHERE ({like}) AND city != '' GROUP BY 1 ORDER BY 2 DESC LIMIT 8", args))
        out.append(row)
    return out


def geo_pack(intel_db: Path, campaign: dict, limit=50) -> list[dict]:
    """ZIP-level targeting pack for one campaign (ads geo-targets, EDDM routes, flyer drops, local pages)."""
    con = _con(intel_db)
    trig = campaign.get("source_signals") or []
    if not con or not trig:
        return []
    like = " OR ".join("triggers LIKE ?" for _ in trig)
    rows = con.execute(f"SELECT zip5, MAX(city) city, COUNT(*) audience_signals, MAX(latest_event) latest FROM prospects "
                       f"WHERE ({like}) AND zip5 != '' GROUP BY zip5 ORDER BY audience_signals DESC LIMIT ?",
                       [f"%{t}%" for t in trig] + [limit]).fetchall()
    return [dict(r) | {"campaign": campaign["id"]} for r in rows]


def partner_targets(intel_db: Path) -> dict[str, list[dict]]:
    """Businesses that stand in front of our audiences. Derived from public licensing data."""
    con = _con(intel_db)
    if not con:
        return {}
    since = (date.today() - timedelta(days=365)).isoformat()
    # 1) brokerages onboarding the most newly licensed agents
    brokers = Counter()
    sfl = Counter()
    for r in con.execute("SELECT details, zip5 FROM signals WHERE trigger='NEW_REALTOR_LICENSE' AND event_date >= ?", (since,)):
        b = (json.loads(r["details"] or "{}").get("brokerage") or "").strip()
        if b:
            brokers[b] += 1
            if (r["zip5"] or "")[:3] in SOUTH_FL:
                sfl[b] += 1
    brokerages = [{"brokerage": b, "new_agents_12mo": n, "new_agents_south_fl": sfl.get(b, 0),
                   "partner_offer": "Co-branded 'New Agent Benefits Guide' in onboarding packet + QR to self-employed checklist",
                   "campaign": "newly-self-employed"} for b, n in brokers.most_common(100)]
    # 2) booth-rental hubs: addresses where 3+ different newly licensed beauty/barber pros receive mail
    hub = defaultdict(set)
    for r in con.execute("SELECT addr1, zip5, city, first_name, last_name FROM signals WHERE trigger IN "
                         "('NEW_BEAUTY_LICENSE','NEW_BARBER_LICENSE') AND event_date >= ? AND addr1 != ''", (since,)):
        a = re.sub(r"\b(STE|SUITE|UNIT|APT|#)\s*\S+", "", (r["addr1"] or "").replace(".", "")).strip()
        hub[(a, r["zip5"], r["city"])].add((r["first_name"], r["last_name"]))
    shops = {}
    for r in con.execute("SELECT addr1, zip5, business_name FROM signals WHERE trigger='NEW_SALON_OWNER' AND business_name != ''"):
        a = re.sub(r"\b(STE|SUITE|UNIT|APT|#)\s*\S+", "", (r["addr1"] or "").replace(".", "")).strip()
        shops.setdefault((a, r["zip5"]), r["business_name"])
    salons = sorted(({"address": a, "zip5": z, "city": c, "new_licensees_at_address": len(p),
                      "licensed_shop_name": shops.get((a, z), ""),
                      "verify": "licensed shop at this address" if shops.get((a, z)) else "UNVERIFIED - could be a school, salon suite or apartment complex; check before pitching",
                      "partner_offer": "Booth-renter benefits checklist flyer at the station + QR",
                      "campaign": "newly-self-employed"}
                     for (a, z, c), p in hub.items() if len(p) >= 3 and shops.get((a, z))), key=lambda x: -x["new_licensees_at_address"])[:150]
    # only addresses that are LICENSED SHOPS (businesses). Unverified addresses could be homes/apartments -> excluded.
    # 3) where new owner-operators concentrate (for truck stops / CDL schools / dispatch partners)
    trucking = [{"city": r["city"], "zip5": r["zip5"], "new_owner_operators": r["n"],
                 "partner_offer": "Flyer + QR at truck stops / repair shops / CDL schools nearby", "campaign": "owner-operator"}
                for r in con.execute("SELECT city, zip5, COUNT(*) n FROM signals WHERE trigger='NEW_TRUCKING_OWNER_OPERATOR' "
                                     "GROUP BY city, zip5 ORDER BY n DESC LIMIT 60")]
    return {"brokerages": brokerages, "salon_hubs": salons, "trucking_hotspots": trucking}


def match_lead(intel_db: Path, first: str, last: str, zip5: str) -> dict | None:
    """Did a consumer who raised their hand ALSO appear in a public trigger? (name + ZIP)"""
    con = _con(intel_db)
    if not con or not (first and last and zip5):
        return None
    f, l = first.strip().upper(), last.strip().upper()
    hits = []
    for r in con.execute("SELECT trigger, event_date, first_name, last_name, source FROM signals WHERE zip5=? AND last_name LIKE ?",
                         (zip5, f"%{l.split()[-1]}%")):
        if (r["first_name"] or "").upper().startswith(f[:3]):
            hits.append({"trigger": r["trigger"], "date": r["event_date"], "source": r["source"]})
    return {"signals": hits} if hits else None


# --------------------------------------------------------------------------- regional briefs
SEG = {"NEW_REALTOR_LICENSE": "realtor", "NEW_BEAUTY_LICENSE": "beauty", "NEW_BARBER_LICENSE": "beauty",
       "NEW_SALON_OWNER": "beauty", "NEW_TRUCKING_OWNER_OPERATOR": "trucking", "NEW_TRUCKING_FLEET": "trucking",
       "NEW_SELF_EMPLOYED_PROVIDER": "health_1099", "NEW_HEALTHCARE_PROVIDER": "health_1099",
       "NEW_HEALTHCARE_PRACTICE_OWNER": "business", "NEW_LOCAL_BUSINESS": "business", "NEW_BUSINESS_LOCATION": "business"}
SEG_PLAY = {
    "realtor": {"angle": "self_employed", "partners": "RE schools, brokerages onboarding new agents, local Realtor boards",
                "content": ["health-insurance-self-employed", "1099-worker-health-insurance"],
                "channels": "Partner guide in onboarding packet; RE-school class handout QR; organic short video for new agents"},
    "beauty": {"angle": "self_employed", "partners": "Salon-suite operators, beauty schools, supply stores",
               "content": ["freelancer-health-insurance-options", "how-much-does-health-insurance-cost"],
               "channels": "Counter card QR at suites/supply stores; IG/TikTok booth-renter content; Spanish creator collabs"},
    "trucking": {"angle": "self_employed", "partners": "CDL schools, freight factoring, dispatch services, truck stops",
                 "content": ["1099-worker-health-insurance", "health-insurance-self-employed"],
                 "channels": "Facebook trucking groups (value posts, no DMs); CDL-school handout; factoring-company newsletter"},
    "health_1099": {"angle": "self_employed", "partners": "ABA agencies, home-health and therapy staffing agencies",
                    "content": ["1099-worker-health-insurance", "health-insurance-between-jobs"],
                    "channels": "Agency onboarding guide; Behavior Technician communities; Google search on 1099 terms"},
    "business": {"angle": "self_employed", "partners": "CPAs/bookkeepers, chambers, SBDC, coworking spaces",
                 "content": ["health-insurance-self-employed", "do-i-qualify-for-help-paying-health-insurance"],
                 "channels": "CPA client newsletter insert; chamber newsletters; local search page"},
}
ES_FIRST_COUNTIES = {"MIAMI-DADE", "DADE"}


def _county_from_zip(z: str) -> str:
    try:
        n = int(z)
    except (TypeError, ValueError):
        return ""
    if 33010 <= n <= 33018 or 33030 <= n <= 33035 or n == 33039 or 33054 <= n <= 33056 or 33101 <= n <= 33299:
        return "MIAMI-DADE"
    if n in (33004, 33009) or 33019 <= n <= 33029 or 33060 <= n <= 33097 or 33301 <= n <= 33394 or n in (33441, 33442):
        return "BROWARD"
    if 33401 <= n <= 33499:
        return "PALM BEACH"
    return ""


def regional_briefs(intel_db: Path, top_n: int = 12) -> list[dict]:
    """Per-city brief: WHERE to put the Check, WHO the partners are, WHICH angle/language/content.
    Built from aggregate counts only - never a contact list."""
    con = _con(intel_db)
    if not con:
        return []
    since = (date.today() - timedelta(days=365)).isoformat()
    city_seg = defaultdict(Counter)
    county_of = {}
    zips = defaultdict(Counter)
    for r in con.execute("SELECT trigger, city, county, zip5 FROM signals WHERE event_date >= ? AND substr(zip5,1,3) IN "
                         "('330','331','332','333','334') AND city != ''", (since,)):
        seg = SEG.get(r["trigger"])
        if not seg:
            continue
        c = r["city"].upper().strip()
        city_seg[c][seg] += 1
        zips[c][r["zip5"]] += 1
        if not county_of.get(c):
            county_of[c] = (r["county"] or "").upper() or _county_from_zip(r["zip5"])
    out = []
    for city, segs in sorted(city_seg.items(), key=lambda kv: -sum(kv[1].values()))[:top_n]:
        total = sum(segs.values())
        lead_seg, _ = segs.most_common(1)[0]
        play = SEG_PLAY[lead_seg]
        es_first = county_of.get(city, "") in ES_FIRST_COUNTIES
        out.append({
            "city": city.title(), "county": county_of.get(city, "").title(), "new_self_employed_12mo": total,
            "segments": [(s, n, round(100 * n / total)) for s, n in segs.most_common()],
            "top_zips": [z for z, _ in zips[city].most_common(5)],
            "lead_segment": lead_seg,
            "language": "ES-first test + EN" if es_first else "EN-first, ES variant",
            "language_basis": ("INFERENCE: Miami-Dade county; verify with Census ACS table C16001 before spending"
                               if es_first else "INFERENCE: default; verify with Census ACS table C16001"),
            "angle": play["angle"], "angle_es": "es_" + play["angle"],
            "partners": play["partners"], "content": play["content"], "channels": play["channels"],
            "paid_geo": f"Meta: {city.title()} city pin, 15-mile minimum radius (Special Ad Category). Google: city/ZIP geo allowed.",
        })
    return out
