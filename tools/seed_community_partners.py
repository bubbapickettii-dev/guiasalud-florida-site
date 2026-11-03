"""Seed the 6 community-referral partners (REFERRAL_PARTNERS.md) as partners + tracked /go links.

Idempotent and aligned with the CANONICAL partner ids the live /go/pt* links reference
(ngo-/edu-/biz- prefixes): INSERT OR IGNORE on both tables plus a default_angle backfill,
so re-running never overwrites operator edits and never creates duplicate rows.
Run:  python tools/seed_community_partners.py
"""
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.db import connect

DB = Path("data/leads.db")
NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")

# partner_id, display_name, type, audience, default_angle, language, evidence, contact_hint
PARTNERS = [
    ("ngo-branches-miami-vita", "Branches (Miami VITA)", "nonprofit", "tax-prep clients",
     "es_check", "es",
     "IRS VITA/TCE free tax prep site; clients bring 1095-A/subsidy questions at income changes (irs.gov; branchesfl.org/vita-tax-preparation)",
     "VITA site coordinator via branchesfl.org VITA program page (public email/phone)"),
    ("ngo-fchcinc-fl", "Florida Community Health Centers (FCHC Inc.)", "clinic", "FQHC patients",
     "es_check", "es",
     "FQHC serving uninsured/underinsured patients between coverage (fchcinc.org/patient-resources)",
     "Patient-resources / outreach coordinator via fchcinc.org"),
    ("edu-broward-esol", "Broward Community Schools ESOL", "education", "adult ESL students",
     "es_check", "es",
     "Adult ESOL students navigating US services for the first time (browardcommunityschools.com/esol)",
     "ESOL program office via browardcommunityschools.com"),
    ("ngo-cflcc-orlando", "Catholic Charities of Central Florida", "nonprofit", "housing/food relief clients",
     "es_check", "es",
     "Housing+food relief clients at income-disruption moments (cflcc.org/housing-counseling)",
     "Housing counseling program contact via cflcc.org"),
    ("ngo-echofl", "ECHO Florida (Emergency Care Help Organization)", "nonprofit", "food-aid clients",
     "health_check", "en",
     "Emergency food/aid clients with income-loss moments (echofl.org/pantries)",
     "Partner inquiries via echofl.org"),
    ("biz-laborfinders-fl", "Labor Finders Florida branches", "employer", "temp/seasonal workers",
     "self_employed", "en",
     "Staffing assignment gaps -> coverage lapses (laborfinders.com/contact-us)",
     "Local branch contact via laborfinders.com"),
]

# code, partner_id, campaign, channel, utm_medium, lang, utm_content, angle
LINKS = [
    ("ptbranchesmiamivita", "ngo-branches-miami-vita", "oep-2027-health-es", "partner_nonprofit", "nonprofit", "es",
     "Branches Miami VITA: bilingual handout + QR poster + co-branded referral link", "es_check"),
    ("ptfchcincfl", "ngo-fchcinc-fl", "oep-2027-health-es", "partner_clinic", "clinic", "es",
     "FCHC: patient handout + QR poster + co-branded referral link (ES-first)", "es_check"),
    ("ptbrowardesol", "edu-broward-esol", "oep-2027-health-es", "partner_education", "education", "es",
     "Broward ESOL: classroom handout + QR (ES)", "es_check"),
    ("ptcflccorlando", "ngo-cflcc-orlando", "oep-2027-health-es", "partner_nonprofit", "nonprofit", "es",
     "Catholic Charities CFL: handout + QR", "es_check"),
    ("ptechofl", "ngo-echofl", "oep-2027-health-es", "partner_nonprofit", "nonprofit", "es",
     "ECHO FL: pantry handout + QR", "es_check"),
    ("ptlaborfindersfl", "biz-laborfinders-fl", "oep-2027-health-es", "partner_staffing", "staffing", "en",
     "Labor Finders: break-room poster + QR (EN)", "self_employed"),
]

NOTE_EVERY = ("No per-enrollment or per-referral payment; consumer submits own info via /check; "
              "assisters receive materials only (Fla. Stat. 626.112(8); 45 CFR 155.210/155.225).")


def main() -> None:
    con = connect(DB)
    p_new = l_new = 0
    for pid, name, typ, aud, angle, lang, evidence, hint in PARTNERS:
        cur = con.execute(
            "INSERT OR IGNORE INTO partners (partner_id, display_name, type, audience, default_angle, language, "
            "status, token, cobrand_approved, evidence, contact_hint, created_at) "
            "VALUES (?,?,?,?,?,?, 'PROSPECT - not contacted', ?, 0, ?, ?, ?)",
            (pid, name, typ, aud, angle, lang, secrets.token_urlsafe(10), evidence, hint, NOW))
        p_new += cur.rowcount
        # keep /p/<partner_id> landings on the right check without touching operator-set values
        con.execute("UPDATE partners SET default_angle=? WHERE partner_id=? AND default_angle IS NULL",
                    (angle, pid))
    for code, pid, camp, chan, med, lang, content, angle in LINKS:
        cur = con.execute(
            "INSERT OR IGNORE INTO links (code, campaign, channel, utm_source, utm_medium, utm_campaign, "
            "utm_content, partner_id, angle, lang, note, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (code, camp, chan, "partner", med, camp, content, pid, angle, lang, NOTE_EVERY, NOW))
        l_new += cur.rowcount
    con.commit()
    print(f"partners added: {p_new}, links added: {l_new}")
    for r in con.execute("SELECT code, partner_id, angle, lang FROM links WHERE code LIKE 'pt%' ORDER BY code"):
        print(dict(r))
    con.close()


if __name__ == "__main__":
    main()
