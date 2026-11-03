"""Consumer-submitted phone validation.

The phone on a lead is ALWAYS the number the consumer typed into the Health Coverage Check, together with
their consent for the named agent to call/text it. It is never looked up, appended or swapped for a public
record number (enforced by DB triggers in engine/db.py).

Checks here are free and structural (North American Numbering Plan rules + obvious junk). Line type
(mobile / landline / VoIP) needs a paid carrier-lookup API; none is connected, so line_type stays
UNKNOWN and says why. Plug a provider into `line_type()` only after you approve the cost.
"""
from __future__ import annotations

import os
import re

PHONE_SOURCE = "CONSUMER_SUBMITTED"
TOLL_FREE = {"800", "833", "844", "855", "866", "877", "888", "900"}
# Florida geographic area codes. An out-of-state area code is a FLAG, never a rejection (people keep old numbers).
# 324 (904 overlay) and 645 (305/786 overlay) are newer overlays; verify against NANPA if it ever matters.
FL_AREA_CODES = {"239", "305", "321", "324", "352", "386", "407", "448", "561", "645", "656", "689", "727", "754",
                 "772", "786", "813", "850", "863", "904", "941", "954"}
JUNK = {"1234567890", "0123456789", "9876543210"}


def digits(raw: str) -> str:
    d = re.sub(r"\D", "", raw or "")
    return d[1:] if len(d) == 11 and d.startswith("1") else d


def validate(raw: str, is_test: bool = False) -> dict:
    d = digits(raw)
    out = {"national": "", "e164": "", "valid": False, "reason": "", "area_code": "", "area_region": "",
           "line_type": "UNKNOWN", "line_type_source": "not checked - no carrier lookup connected (paid API, not approved)",
           "source": PHONE_SOURCE}
    if not d:
        out["reason"] = "missing"
        return out
    if len(d) != 10:
        out["reason"] = "not_10_digits"
        return out
    npa, nxx, line = d[:3], d[3:6], d[6:]
    out["area_code"] = npa
    fictional = nxx == "555" and line.startswith("01")
    if fictional and not is_test:
        out["reason"] = "fictional_555_01xx"
    elif npa[0] in "01" or nxx[0] in "01":
        out["reason"] = "invalid_nanp_format"
    elif npa[1:] == "11" or nxx[1:] == "11":
        out["reason"] = "n11_service_code"
    elif npa in TOLL_FREE:
        out["reason"] = "toll_free_not_personal"
    elif len(set(d)) <= 2 or d in JUNK:
        out["reason"] = "junk_pattern"
    elif nxx == "555" and not fictional:
        out["reason"] = "directory_555"
    else:
        out["valid"] = True
        out["reason"] = "ok_test_fictional" if fictional else "ok"
    if out["valid"]:
        out["national"], out["e164"] = d, "+1" + d
        out["area_region"] = "FL" if npa in FL_AREA_CODES else "US_OTHER"
        lt = line_type(out["e164"])
        if lt:
            out["line_type"], out["line_type_source"] = lt
    return out


def line_type(e164: str):
    """Hook for a carrier lookup provider. Returns (type, source) or None. Nothing is called unless
    PHONE_LOOKUP_PROVIDER is set AND an implementation is added here - none is, by design."""
    if not os.environ.get("PHONE_LOOKUP_PROVIDER"):
        return None
    return None


def pretty(national: str) -> str:
    return f"({national[:3]}) {national[3:6]}-{national[6:]}" if len(national or "") == 10 else (national or "")
