"""Partner distribution kit: the people who ALREADY reach our audiences hand out a genuinely useful
resource with a QR code that's tracked to them. Nothing here contacts anyone - Bubba sends the pitches.

Outputs (partner_kit/):
  partners.json (also copied to config/)   - partner targets derived from public data + their tracked links/tokens
  guides/<audience>_<partner>.pdf          - 2-page guides with partner-specific QR codes (EN + ES for realtors)
  pitches.md                               - outreach scripts per partner type (email, DM, follow-up, objections)
Facts used in guides (verified Oct 2026): OEP 2027 = Nov 1 2026 - Jan 15 2027, enroll by Dec 15 for Jan 1;
enhanced credits expired after 2025 -> 100-400% FPL band; 2026 HHS poverty guidelines (48 states) used for 2027 coverage.
"""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
from collections import Counter
from pathlib import Path

import qrcode

def _reportlab():
    """PDF/QR layout deps are imported lazily (same reasoning as engine/partners.py: the app imports this
    module for GUIDE copy + fpl_rows only, and on some Windows hosts loading the reportlab->PIL DLL chain
    fails while the rest of the app runs fine)."""
    from reportlab.lib.colors import HexColor, white
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    return HexColor, white, letter, canvas


B = Path(__file__).resolve().parent
OUT = B / "partner_kit"
SITE = json.loads((B / "config/site.json").read_text())
INTEL = B.parent / "leadgen/data/leadgen.db"
INK, ACC, MUTED = "#0f2238", "#0b7a5a", "#5a6b7d"   # hex strings; resolved to reportlab colors inside the PDF builders
FPL = {1: 15960, 2: 21640, 3: 27320, 4: 33000}


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:32]


# ------------------------------------------------------------------ partner targets (from public data)
def targets():
    con = sqlite3.connect(INTEL)
    sfl = Counter()
    for d, z in con.execute("SELECT details, zip5 FROM signals WHERE trigger='NEW_REALTOR_LICENSE'"):
        b = json.loads(d or "{}").get("brokerage", "").strip()
        if b and (z or "")[:3] in ("330", "331", "332", "333", "334"):
            sfl[b] += 1
    P = []
    for b, n in sfl.most_common(12):
        P.append(dict(id="brk-" + slug(b), display_name=b.title(), type="brokerage", audience="realtor",
                      evidence=f"{n} newly licensed agents with South FL addresses joined in the last 12 months (DBPR)",
                      contact_hint="Managing broker / onboarding or recruiting lead of the South FL office; for cloud brokerages (eXp, LPT, Real) target large TEAM leaders who recruit - they want perks for recruits"))
    schools = [("FLORIDA REALTORS SCHOOL OF REAL ESTATE", "Orlando (64 locations statewide)", "Run by the state Realtor association"),
               ("GOLD COAST PROFESSIONAL SCHOOLS", "Tamarac (12 locations)", "Big South FL pre-license school"),
               ("SOUTH FLORIDA REAL ESTATE SCHOOL", "Miami (6 locations)", "Miami pre-license school"),
               ("BROWARD, PALM BEACHES AND ST. LUCIE REALTORS", "Fort Lauderdale (8 locations)", "Realtor association school - new-member orientation"),
               ("APRENDIENDO REAL ESTATE USA", "Miami (3 locations)", "Spanish-language pre-license school - use ES guide"),
               ("SOCIETY REAL ESTATE SCHOOL", "Palmetto Bay (4 locations)", "Miami-Dade school"),
               ("1A SCHOOL OF REAL ESTATE", "Plantation (4 locations)", "Broward school"),
               ("MIAMI DADE COLLEGE - CONTINUING ED", "Miami", "College pre-license program")]
    for n, where, why in schools:
        P.append(dict(id="sch-" + slug(n), display_name=n.title(), type="re_school", audience="realtor", evidence=f"Licensed RE school, {where} (DBPR school file). {why}.",
                      contact_hint="School director / owner: offer the guide as a free graduation-packet resource for students about to go 1099",
                      lang="es" if "APRENDIENDO" in n else "en"))
    generic = [("salon-suites", "Salon suite operators (e.g. suite franchise locations in Miami-Dade/Broward)", "salon_suites", "beauty",
                "25,543 new beauty + 2,268 barber licenses statewide in 12 months; suite tenants are all 1099",
                "Location owner/manager: a free 'Suite Owner Benefits Checklist' they can give tenants"),
               ("freight-factoring", "Freight factoring & dispatch services for new carriers", "trucking_services", "trucking",
                "8,074 new FL carriers since April (FMCSA), 1,502 in Miami-Dade; factoring/dispatch onboard new carriers every week",
                "Onboarding / carrier-success team: add the owner-operator protection guide to their welcome kit"),
               ("cdl-schools", "CDL schools in South FL", "cdl_school", "trucking", "Graduates who go owner-operator lose company benefits",
                "School director: graduation packet"),
               ("aba-agencies", "ABA therapy agencies using 1099 Behavior Technicians", "aba_agency", "health_1099",
                "612 new sole-proprietor Behavior Technician NPIs in FL in ~4 months (NPPES) - the largest self-employed provider group",
                "HR/onboarding lead: a benefits resource for 1099 RBTs (most fall in the subsidy range)"),
               ("cpa-bookkeepers", "CPAs / bookkeepers / LLC formation services", "cpa", "business",
                "7,600+ new Miami-Dade businesses in 2026 (LBT/CU)", "Owner: embed the calculator or hand clients the business-owner checklist during OEP")]
    for pid, name, typ, aud, ev, hint in generic:
        P.append(dict(id=pid, display_name=name, type=typ, audience=aud, evidence=ev, contact_hint=hint, generic=True))
    default_campaign = {"realtor": "oep-2027-health", "beauty": "oep-2027-health", "trucking": "oep-2027-health",
                        "health_1099": "oep-2027-health", "business": "oep-2027-health"}
    old = {}
    p_old = B / "config/partners.json"
    if p_old.exists():
        old = {p["id"]: p for p in json.loads(p_old.read_text() or "[]")}
    for p in P:
        p["default_campaign"] = default_campaign[p["audience"]]
        p["status"] = old.get(p["id"], {}).get("status", "PROSPECT - not contacted")
        p["token"] = old.get(p["id"], {}).get("token") or secrets.token_urlsafe(10)
        p["cobrand_approved"] = old.get(p["id"], {}).get("cobrand_approved", False)
        p["link"] = f"{SITE['base_url']}/p/{p['id']}"
        p["stats_page"] = f"{SITE['base_url']}/partner/{p['token']}"
    return P


# ------------------------------------------------------------------ guide content (accurate, non-salesy)
def fpl_rows(es=False):
    return [(f"{n} {'persona' if es and n == 1 else 'personas' if es else 'person' if n == 1 else 'people'}",
             f"${FPL[n]:,} - ${FPL[n] * 4:,}") for n in (1, 2, 3, 4)]


GUIDES = {
 "realtor": {"title": "New Agent Benefits & Protection Guide", "kicker": "FLORIDA - 2027 EDITION",
   "intro": "Congratulations on your license. As an independent contractor you're now your own benefits department: no employer health plan, no group life insurance, no sick pay. Here's what to set up, in order.",
   "sections": [("1. Health coverage (do this first)", ["2027 Open Enrollment: Nov 1, 2026 - Jan 15, 2027. Enroll by Dec 15 for coverage starting Jan 1.",
                  "Lost a job's health plan when you went full-time in real estate? That usually opens a 60-day Special Enrollment Period.",
                  "Commission income is uneven: estimate it honestly and update your Marketplace application if it changes during the year.",
                  "If you're self-employed and not eligible for an employer plan (yours or a spouse's), you may be able to deduct premiums - ask your tax pro."]),
                ("2. Income protection", ["Your first closings can take months. Disability insurance replaces part of your income if illness or injury stops you from working.",
                  "Build a cash cushion of 3-6 months of expenses before spending on marketing."]),
                ("3. Life insurance", ["If anyone depends on your income (kids, partner, a co-signed loan), ask a licensed agent how life insurance could replace that income.",
                  "Your brokerage doesn't provide it - you're independent."]),
                ("4. Business basics", ["Errors & omissions coverage (check what your brokerage requires).", "Set aside money for quarterly estimated taxes."])],
   "table_title": "2027 premium tax credit income range (100%-400% of the poverty level)",
   "cta1": ("2027 Health Coverage Check", "oep-2027-health"), "cta2": ("Self-Employed Protection Checklist", "newly-self-employed")},
 "trucking": {"title": "Owner-Operator Family & Health Protection Guide", "kicker": "FLORIDA - 2027 EDITION",
   "intro": "Getting your own authority means the company's benefits stayed with the company. Your truck has insurance - here's how to cover you and your family.",
   "sections": [("1. Health coverage", ["2027 Open Enrollment: Nov 1, 2026 - Jan 15, 2027. Enroll by Dec 15 for coverage starting Jan 1.",
                  "Left a company driving job? Losing that health plan usually opens a 60-day Special Enrollment Period.",
                  "Estimate your 2027 net profit (after fuel, truck payment and expenses) - that's what the Marketplace uses."]),
                ("2. If you can't drive", ["Occupational accident and disability coverage are different from your truck's liability policy - ask what each pays and when.",
                  "Keep 3-6 months of expenses (including the truck note) in reserve."]),
                ("3. Your family", ["If something happened to you, who pays the truck note, the house and the bills? A licensed agent can explain your options.",
                  "Driving occupations are underwritten differently - work with an agent who places truckers."])],
   "table_title": "2027 premium tax credit income range (100%-400% of the poverty level)",
   "cta1": ("2027 Health Coverage Check", "oep-2027-health"), "cta2": ("Family Protection Gap Check", "owner-operator")},
 "beauty": {"title": "Booth Renter & Suite Owner Benefits Checklist", "kicker": "FLORIDA - 2027 EDITION",
   "intro": "Renting a chair or a suite makes you a business owner. Nobody else is covering your health, your hands or your income now.",
   "sections": [("1. Health coverage", ["2027 Open Enrollment: Nov 1, 2026 - Jan 15, 2027. Enroll by Dec 15 for coverage starting Jan 1.",
                  "See whether your 2027 income falls in the range that gets help paying premiums (table on page 2)."]),
                ("2. Your hands are your income", ["Disability coverage pays part of your income if an injury keeps you off the chair.", "Professional liability coverage protects you if a client claims harm."]),
                ("3. Family protection", ["If people depend on your income, ask a licensed agent about life insurance options."])],
   "table_title": "2027 premium tax credit income range (100%-400% of the poverty level)",
   "cta1": ("2027 Health Coverage Check", "oep-2027-health"), "cta2": ("Self-Employed Protection Checklist", "newly-self-employed")},
 "health_1099": {"title": "1099 Provider Benefits Guide", "kicker": "FOR INDEPENDENT BEHAVIOR TECHNICIANS, THERAPISTS & CLINICIANS - FLORIDA 2027",
   "intro": "Working as a 1099 provider means no employer health plan, no paid sick time and no group life insurance. Here's the short list.",
   "sections": [("1. Health coverage", ["2027 Open Enrollment: Nov 1, 2026 - Jan 15, 2027. Enroll by Dec 15 for coverage starting Jan 1.",
                  "Estimate 2027 income honestly; update the Marketplace if your hours change."]),
                ("2. Income protection", ["Disability coverage replaces part of your income if you can't work.", "Professional liability: check what your agency covers for contractors."]),
                ("3. Life insurance", ["If someone depends on your income, ask a licensed agent about life insurance options."])],
   "table_title": "2027 premium tax credit income range (100%-400% of the poverty level)",
   "cta1": ("2027 Health Coverage Check", "oep-2027-health"), "cta2": ("Self-Employed Protection Checklist", "newly-self-employed")},
 "business": {"title": "New Business Owner Protection Checklist", "kicker": "FLORIDA - 2027 EDITION",
   "intro": "Starting a business usually means leaving employer benefits behind. Before Open Enrollment, check these five things.",
   "sections": [("1. Your health coverage", ["2027 Open Enrollment: Nov 1, 2026 - Jan 15, 2027. Enroll by Dec 15 for coverage starting Jan 1.",
                  "Solo owner? Individual Marketplace coverage. With employees? Ask about small-group options."]),
                ("2. If you couldn't work", ["Who pays the lease and loans you personally guaranteed? Disability and key-person coverage exist for this."]),
                ("3. Your family", ["Term life sized to your debts + income + family needs."]),
                ("4. Partners", ["Have a co-owner? A funded buy-sell agreement keeps the business running if one of you dies."])],
   "table_title": "2027 premium tax credit income range (100%-400% of the poverty level)",
   "cta1": ("2027 Health Coverage Check", "oep-2027-health"), "cta2": ("Business Owner Protection Check", "new-business-owner")},
}
GUIDE_ES = {"title": "Guía de Beneficios y Protección para Nuevos Agentes", "kicker": "FLORIDA - EDICIÓN 2027",
   "intro": "Felicidades por su licencia. Como contratista independiente, usted ahora es su propio departamento de beneficios: sin plan de salud del empleador, sin seguro de vida grupal, sin días pagados por enfermedad. Esto es lo que debe preparar, en orden.",
   "sections": [("1. Seguro médico (primero)", ["Inscripción Abierta 2027: 1 de noviembre de 2026 - 15 de enero de 2027. Inscríbase antes del 15 de diciembre para cobertura desde el 1 de enero.",
                  "¿Perdió el seguro de su trabajo al dedicarse a bienes raíces? Normalmente eso abre un Periodo Especial de Inscripción de 60 días.",
                  "Sus comisiones varían: estime con honestidad y actualice su solicitud del Mercado si cambian.",
                  "Si trabaja por su cuenta y no tiene acceso a un plan de empleador (suyo o de su pareja), podría deducir las primas. Pregúntele a su contador."]),
                ("2. Protección de ingresos", ["Los primeros cierres pueden tardar meses. Un seguro de incapacidad reemplaza parte de su ingreso si una enfermedad o lesión le impide trabajar.",
                  "Ahorre de 3 a 6 meses de gastos antes de gastar en mercadeo."]),
                ("3. Seguro de vida", ["Si alguien depende de su ingreso, un seguro de vida a término suele ser la forma más económica de protegerle.", "Su corredora no se lo da: usted es independiente."]),
                ("4. Su negocio", ["Seguro de errores y omisiones (verifique lo que exige su corredora).", "Aparte dinero para los impuestos estimados trimestrales."])],
   "table_title": "Rango de ingreso para crédito fiscal 2027 (100%-400% del nivel de pobreza)",
   "cta1": ("Chequeo de Seguro Médico 2027", "oep-2027-health-es"), "cta2": ("Lista de Protección para Independientes (en inglés)", "newly-self-employed")}


def wrap(c, text, x, y, w, font="Helvetica", size=10.5, lead=14, color=INK):
    c.setFont(font, size); c.setFillColor(color)
    line = ""
    for word in text.split():
        t = (line + " " + word).strip()
        if c.stringWidth(t, font, size) > w:
            c.drawString(x, y, line); y -= lead; line = word
        else:
            line = t
    if line:
        c.drawString(x, y, line); y -= lead
    return y


def guide_pdf(g, partner, path, es=False):
    HexColor, white, letter, canvas = _reportlab()
    W, H = letter
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setTitle(g["title"])
    # page 1
    c.setFillColor(INK); c.rect(0, H - 150, W, 150, fill=1, stroke=0)
    c.setFillColor(HexColor("#9fe3c9")); c.setFont("Helvetica-Bold", 10); c.drawString(48, H - 48, g["kicker"])
    wrap(c, g["title"], 48, H - 80, W - 96, "Helvetica-Bold", 24, 28, white)
    if partner and partner.get("cobrand_approved"):
        c.setFillColor(white); c.setFont("Helvetica", 10); c.drawString(48, H - 140, ("Cortesía de " if es else "Provided by ") + partner["display_name"])
    y = wrap(c, g["intro"], 48, H - 180, W - 96, "Helvetica", 11.5, 16)
    y -= 6
    for head, bullets in g["sections"]:
        if y < 120:
            c.showPage(); y = H - 60
        y = wrap(c, head, 48, y - 6, W - 96, "Helvetica-Bold", 13, 18)
        for b_ in bullets:
            c.setFillColor(ACC); c.circle(54, y + 3.5, 2.6, fill=1, stroke=0)
            y = wrap(c, b_, 64, y, W - 112, "Helvetica", 10.5, 14) - 3
    # page 2: subsidy table + QR codes + checklist
    c.showPage()
    y = wrap(c, g["table_title"], 48, H - 60, W - 96, "Helvetica-Bold", 13, 18)
    for k, v in fpl_rows(es):
        c.setFont("Helvetica", 11); c.setFillColor(INK); c.drawString(64, y, k); c.drawRightString(W - 64, y, v)
        c.setStrokeColor(HexColor("#e3e8ee")); c.line(64, y - 5, W - 64, y - 5); y -= 20
    y = wrap(c, ("Basado en las guías de pobreza HHS 2026 (48 estados) que se usan para la cobertura 2027. Los subsidios extra terminaron después de 2025. La elegibilidad real la determina el Mercado." if es else
                 "Based on the 2026 HHS poverty guidelines (48 states) used for 2027 coverage. The extra subsidies ended after 2025. Actual eligibility is determined by the Marketplace."),
             64, y - 2, W - 128, "Helvetica", 8.5, 11, MUTED)
    y -= 14
    pid = partner["id"] if partner else "guide"
    for i, (label, cid) in enumerate((g["cta1"], g["cta2"])):
        url = f"{SITE['base_url']}/p/{pid}?c={cid}&src=guide" if partner else f"{SITE['base_url']}/c/{cid}"
        qp = OUT / "qr" / f"{pid}_{cid}.png"
        qrcode.make(url, box_size=8, border=1).save(qp)
        x = 64 + i * ((W - 128) / 2)
        c.drawImage(str(qp), x, y - 150, 140, 140)
        yy = wrap(c, label, x, y - 166, 200, "Helvetica-Bold", 11, 14, ACC)
        wrap(c, ("Escanee - 60 segundos, gratis" if es else "Scan - free, 60 seconds"), x, yy - 2, 200, "Helvetica", 9, 12, MUTED)
    y -= 220
    c.setFont("Helvetica-Bold", 13); c.setFillColor(INK); c.drawString(48, y, "Checklist" if not es else "Lista")
    y -= 20
    items = ([h.split(". ", 1)[1] for h, _ in g["sections"]] + (["Fondo de emergencia de 3-6 meses"] if es else ["3-6 month emergency fund"]))
    for it in items:
        c.setStrokeColor(INK); c.rect(52, y - 2, 10, 10, stroke=1, fill=0); c.setFont("Helvetica", 11); c.drawString(70, y, it); y -= 20
    wrap(c, (SITE.get("disclosure_es") if es else SITE["disclosure"]) + " " + (SITE.get("gov_disclaimer_es") if es else SITE["gov_disclaimer"]), 48, 60, W - 96, "Helvetica", 7.5, 9.5, MUTED)
    c.save()


PITCHES = """# Partner outreach scripts (Bubba sends these himself; nothing is sent automatically)

Rules for every partner: no money changes hands for referrals (keeps it simple and compliant), we never share consumer data with the partner,
nobody in their audience gets called unless THEY ask on our page. The partner gets a stats page (visits / tool uses / requests - counts only).

## Pre-license schools & Realtor associations (highest-leverage realtor channel)
**Subject:** Free benefits guide for your graduates (going 1099 in the middle of Open Enrollment)
Hi {name} - your students are about to become independent contractors right as 2027 health Open Enrollment opens (Nov 1 - Jan 15). Most don't realize they just lost
employer benefits. I put together a free 2-page "New Agent Benefits & Protection Guide" (English + Spanish) with a 60-second coverage check. Nothing to sell in class -
it's a handout or a link in your graduation email. Want me to send the PDF so you can look it over? You'd get a private page showing how many students used it.
**Follow-up (3-4 days):** Attaching the guide - the only thing that changes for your school is a QR code that lets us show you usage. Happy to add your logo if you want it co-branded.

## Brokerage onboarding / recruiting team leaders
**DM:** Quick one - do you give new agents anything on benefits? Most onboard right into Open Enrollment with no health plan. I made a free "New Agent Benefits Guide" +
60-sec coverage check you can drop in your onboarding packet or new-agent group chat. Zero cost, no spam to your agents. Want the PDF?
**Angle for team leaders who recruit (eXp / LPT / Real):** "a benefits resource you can offer recruits" = a recruiting perk at zero cost.

## Salon suite operators / beauty schools
**DM:** Your suite owners are all 1099 - nobody's covering their health or their hands. I made a free "Booth Renter & Suite Owner Benefits Checklist" with a 60-second
coverage check. Want a stack of QR cards for the front desk or the PDF for your tenant newsletter?

## Freight factoring, dispatch services, CDL schools
**Email:** New carriers you onboard just lost their company health and life coverage. Free 2-page "Owner-Operator Family & Health Protection Guide" for your welcome kit,
with a QR to a 2-minute check. It helps carriers stay on the road (a sick driver with no coverage is a lost customer). PDF attached if useful.

## ABA agencies (1099 Behavior Technicians)
**Email to HR/onboarding:** Your 1099 RBTs don't get your benefits, and Open Enrollment starts Nov 1. Free "1099 Provider Benefits Guide" with a 60-second coverage check
you can include in onboarding. Helps retention, costs nothing, no contact unless the tech asks.

## CPAs / bookkeepers / LLC formation services
**Email:** Your new-business clients are leaving employer coverage. Free "New Business Owner Protection Checklist" + a calculator you can embed on your site
(one line of code, tracked to you) for Open Enrollment season.

## Objections
- "Are you going to spam my people?" No. Nobody gets contacted unless they fill out the check and ask for options themselves. You can see the page first.
- "Do you pay referral fees?" No - it's a free resource. (We keep it that way on purpose.)
- "Is this a government site?" No, and the page says so clearly; it connects people with licensed agents if they ask.
"""


def main():
    (OUT / "qr").mkdir(parents=True, exist_ok=True); (OUT / "guides").mkdir(exist_ok=True)
    P = targets()
    (OUT / "partners.json").write_text(json.dumps(P, indent=1))
    (B / "config/partners.json").write_text(json.dumps(P, indent=1))
    made = []
    for aud, g in GUIDES.items():
        guide_pdf(g, None, OUT / "guides" / f"{aud}_GENERIC.pdf"); made.append(f"{aud}_GENERIC.pdf")
    guide_pdf(GUIDE_ES, None, OUT / "guides" / "realtor_GENERIC_ES.pdf", es=True); made.append("realtor_GENERIC_ES.pdf")
    for p in P:
        g = GUIDES[p["audience"]]
        if p.get("lang") == "es":
            guide_pdf(GUIDE_ES, p, OUT / "guides" / f"realtor_ES_{p['id']}.pdf", es=True); made.append(f"realtor_ES_{p['id']}.pdf")
        else:
            guide_pdf(g, p, OUT / "guides" / f"{p['audience']}_{p['id']}.pdf"); made.append(f"{p['audience']}_{p['id']}.pdf")
    (OUT / "pitches.md").write_text(PITCHES)
    print(json.dumps({"partners": len(P), "guides": len(made)}, indent=1))


if __name__ == "__main__":
    main()
