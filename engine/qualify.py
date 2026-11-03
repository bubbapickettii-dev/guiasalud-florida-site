"""Lead validation, qualification, scoring and tiering.

Status:
  QUALIFIED   - PHONE_LEAD: consumer-submitted valid phone + consent naming the agent, served state, fits product -> sellable
  NURTURE     - EMAIL_ONLY_INQUIRY (no phone given) or 'just researching' -> not sold, not routed; the consumer gets only
                the one summary email they asked for. No follow-up campaigns.
  DUPLICATE   - same phone/email submitted in the last 30 days
  UNQUALIFIED - fails a hard rule (invalid phone, unserved state, age out of range, junk)
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from . import phone as phone_mod

AGE_LOW = {"18-29": 18, "30-39": 30, "40-49": 40, "50-59": 50, "60-69": 60, "70-85": 70,
           "18-25": 18, "26-34": 26, "35-44": 35, "45-54": 45, "55-64": 55, "65+": 65}
AGE_HIGH = {"18-29": 29, "30-39": 39, "40-49": 49, "50-59": 59, "60-69": 69, "70-85": 85,
            "18-25": 25, "26-34": 34, "35-44": 44, "45-54": 54, "55-64": 64, "65+": 120}
JUNK_NAMES = {"test", "asdf", "qwerty", "none", "na", "n/a", "fake", "aaa", "xxx", "mickey", "mouse"}
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def phone10(p: str, allow_fictional: bool = False) -> str:
    d = re.sub(r"\D", "", p or "")
    if allow_fictional and len(d) == 10 and d[3:7] == "5550" and d[7] == "1":
        return d  # 555-01XX is reserved for fiction - only accepted on TEST submissions
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) != 10 or d[0] in "01" or d[3] in "01" or len(set(d)) <= 2 or d[3:6] == "555":
        return ""
    return d


def validate(form: dict, campaign: dict, site: dict) -> list[str]:
    errs = []
    if not (form.get("first_name") or "").strip() or not (form.get("last_name") or "").strip():
        errs.append("name_missing")
    if (form.get("first_name") or "").strip().lower() in JUNK_NAMES or (form.get("last_name") or "").strip().lower() in JUNK_NAMES:
        errs.append("junk_name")
    em = (form.get("email") or "").strip()
    if (form.get("contact_scope") != "PHONE_AND_EMAIL" or em) and not EMAIL_RE.match(em):   # email optional on phone leads
        errs.append("email_invalid")
    if form.get("contact_scope") == "PHONE_AND_EMAIL":
        pv = phone_mod.validate(form.get("phone", ""), bool(form.get("is_test")))
        if not pv["valid"]:
            errs.append("phone_invalid:" + pv["reason"])
    if not re.match(r"^\d{5}$", (form.get("zip") or "").strip()):
        errs.append("zip_invalid")
    if form.get("state") not in site["served_states"]:
        errs.append("state_not_served")
    ar = form.get("age_range")
    if ar in AGE_LOW and (AGE_HIGH[ar] < campaign["age_min"] or AGE_LOW[ar] > campaign["age_max"]):
        errs.append("age_out_of_range")
    return errs


def is_duplicate(con, phone: str, email: str, days=30) -> bool:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    r = con.execute("SELECT 1 FROM leads WHERE created_at >= ? AND ((phone != '' AND phone = ?) OR (email != '' AND lower(email) = lower(?))) "
                    "AND qual_status != 'UNQUALIFIED' LIMIT 1", (since, phone or "~", email or "~")).fetchone()
    return bool(r)


TIMEFRAME = {"asap": 30, "30d": 22, "jan1": 26, "renewal": 18, "1-3m": 12, "research": 3}
AMOUNT = {"lt100k": 6, "100-250k": 10, "250-500k": 14, "500k-1m": 16, "1m+": 18, "unsure": 8,
          "5-10k": 8, "10-15k": 10, "15-25k": 12, "25k+": 12, "lt150k": 10, "150-300k": 13, "300-500k": 15, "500k+": 16}
HEALTH = {"excellent": 8, "good": 6, "fair": 3, "conditions": 2}


HEALTH_URGENCY = {"HIGH": 30, "MEDIUM": 20, "LOW": 5}
HEALTH_INCOME = {"100-150": 12, "150-250": 12, "250-400": 12, "gt400": 9, "unsure": 7, "lt100": 2}


def score_health(form: dict, events: list[dict], intel: dict | None) -> tuple[float, dict]:
    """Health (ACA) scoring: urgency + placeability + contactability + engagement. Weights are starting
    hypotheses - re-fit them against agent outcomes (appointment/sold) once real leads exist."""
    b = {"urgency": HEALTH_URGENCY.get(form.get("urgency"), 8),
         "income_band": HEALTH_INCOME.get(form.get("income_band"), 6),
         "household": 6 if int(form.get("household_size") or 1) > 1 else 3,
         "consented_phone": 12 if form.get("contact_scope") == "PHONE_AND_EMAIL" else 0,
         "phone_in_state": 3 if form.get("_phone_region") == "FL" else 0,
         "prefers_phone": 4 if form.get("preferred_contact") in ("phone", "text") else 0,
         "completed_check": 10 if (form.get("tool_result") or {}).get("headline") else 0,
         "sep_or_oep": 6 if form.get("enrollment_situation") in ("SEP_LIKELY", "OEP_OPEN", "OEP_UPCOMING") else 0,
         "public_signal_match": 10 if intel else 0,
         "coverage_gap_risk": -12 if "coverage_gap_risk" in (form.get("flags") or []) else 0}
    return max(0.0, min(100.0, float(sum(b.values())))), b


def score(form: dict, events: list[dict], intel: dict | None) -> tuple[float, dict]:
    if form.get("urgency"):
        return score_health(form, events, intel)
    b = {}
    b["timeframe"] = TIMEFRAME.get(form.get("timeframe") or form.get("health_timeframe"), 5)
    b["amount"] = AMOUNT.get(form.get("coverage_amount") or form.get("fe_amount") or form.get("mortgage_balance"), 6)
    b["health"] = HEALTH.get(form.get("health_rating"), 5)
    b["tobacco"] = 4 if form.get("tobacco") == "no" else 0
    b["contact_scope"] = 12 if form.get("contact_scope") == "PHONE_AND_EMAIL" else 0
    b["prefers_phone"] = 4 if form.get("preferred_contact") in ("phone", "text") else 0
    names = {e.get("event") for e in events}
    tr = form.get("tool_result") or {}
    b["used_tool"] = 10 if ("tool_complete" in names or (isinstance(tr, dict) and tr.get("headline"))) else (5 if "tool_start" in names else 0)
    b["dependents"] = 6 if ((form.get("coverage_for") or "").startswith("me_") or form.get("dependents") == "yes"
                            or form.get("health_who") in ("me_spouse", "me_kids", "family")) else 0
    b["public_signal_match"] = 10 if intel else 0
    total = max(0.0, min(100.0, float(sum(b.values()))))
    return total, b


def tier(score_: float) -> str:
    return "A" if score_ >= 70 else ("B" if score_ >= 50 else "C")


def qualify(con, form: dict, campaign: dict, site: dict, events: list[dict], intel: dict | None) -> dict:
    errs = validate(form, campaign, site)
    pv = phone_mod.validate(form.get("phone", ""), bool(form.get("is_test"))) if form.get("contact_scope") == "PHONE_AND_EMAIL" \
        else phone_mod.validate("")      # no consent to be called -> the phone (if typed) is not kept at all
    phone = pv["national"]
    form["_phone_region"] = pv["area_region"]
    s, br = score(form, events, intel)
    t = tier(s)
    if errs:
        status = "UNQUALIFIED"
    elif is_duplicate(con, phone, form.get("email", "")):
        status = "DUPLICATE"
    elif form.get("contact_scope") != "PHONE_AND_EMAIL" or "research" in (form.get("timeframe"), form.get("health_timeframe")) \
            or form.get("health_timeframe") == "exploring" and form.get("urgency") == "LOW":
        status = "NURTURE"
    else:
        status = "QUALIFIED"
    lead_type = "PHONE_LEAD" if form.get("contact_scope") == "PHONE_AND_EMAIL" else "EMAIL_ONLY_INQUIRY"
    return {"qual_status": status, "qual_reasons": errs, "score": s, "tier": t, "score_breakdown": br, "phone": phone,
            "phone_check": pv, "lead_type": lead_type}
