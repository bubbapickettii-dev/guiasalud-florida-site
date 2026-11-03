"""Builds the ready-to-use Engine 1 pack from config/distribution.json + config/content.json:
  out/organic_pack/POSTING_PACK.md     every placement: status, where, how to find it, rules, exact copy + its tracked link
  out/organic_pack/shortform.md        20-30s scripts per platform with on-screen text, caption and tracked link
  assets/print/handout_job_{en,es}.pdf  CareerSource handout (health 60 days + life 31 days) with tracked QR
  assets/print/counter_jan_es.pdf       pharmacy counter card for the January window (one QR per store via ?c=)
Run after BASE_URL is set so links/QRs point at the live domain:
  BASE_URL=https://yourdomain.com python tools/distribution_pack.py"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = (os.environ.get("BASE_URL") or "https://TUDOMINIO.com").rstrip("/")
OUT = ROOT / "out" / "organic_pack"
PRINT = ROOT / "assets" / "print"

STATUS_NOTE = {"READY": "can go now", "NEEDS_DOMAIN": "needs the live domain", "NEEDS_BRAND_ACCOUNT": "needs the brand account on this platform",
               "NEEDS_MOD_OK": "needs moderator approval first", "NEEDS_ADMIN_OK": "needs each group admin's OK first",
               "NEEDS_APPROVAL_TO_SEND": "needs Bubba's OK before we send it", "SCHEDULED_JAN": "post Jan 2-14 only"}

SHORTS = [
 dict(id="exit-es", placement_suffix="exit-es", lang="es", title="Cigna se va de Florida",
      hook="¿Tu plan del Mercado es con Cigna? Esto te va a pasar el 1 de enero.",
      beats=["Cigna sale del Mercado de Florida en 2027.", "Si no haces nada, CuidadoDeSalud.gov te pasa a OTRA compañía.",
             "Esa compañía no revisa si tu médico o tus medicinas están en el plan.", "Escoge tú antes del 15 de diciembre."],
      onscreen=["CIGNA SE VA DE FLORIDA", "Te pasan a OTRA compañía", "No revisan tus médicos", "Escoge antes del 15/12"],
      cta="Guía gratis con fuentes oficiales: link en el perfil."),
 dict(id="offx-es", placement_suffix="exit-es", lang="es", title="Compraste directo con la aseguradora",
      hook="Si compraste tu plan directo con Cigna, nadie te va a pasar a otro plan.",
      beats=["Si tu plan no es de CuidadoDeSalud.gov, nadie te pasa a otro plan.", "El plan simplemente termina el 31 de diciembre.",
             "Tienes que escoger uno nuevo tú mismo, antes del 15 de diciembre."],
      onscreen=["¿Plan comprado DIRECTO?", "Termina el 31/12", "Nadie te pasa a otro", "Escoge antes del 15/12"],
      cta="Revisa tu caso en 60 segundos: link en el perfil."),
 dict(id="jan-es", placement_suffix="exit-es", lang="es", title="Te cambiaron de seguro (enero)",
      hook="¿Fuiste a la farmacia y tu seguro ya no es el mismo? No estás atrapado.",
      beats=["Si tu compañía salió de Florida (como Cigna), el Mercado te pasó a otra.", "Todavía puedes escoger tu propio plan hasta el 15 de enero.",
             "El plan nuevo empieza el 1 de febrero."],
      onscreen=["¿TE CAMBIARON DE SEGURO?", "Hasta el 15 de enero", "Empieza el 1 de febrero"], cta="Link en el perfil (publicar del 2 al 14 de enero)."),
 dict(id="job-en", placement_suffix="job-en", lang="en", title="Laid off: two deadlines",
      hook="Laid off in Florida? You have two insurance deadlines and nobody tells you about the second one.",
      beats=["Health: losing job coverage opens a 60-day window. COBRA is usually full price.", "Dropping COBRA later does not reopen that window.",
             "Life: work life insurance usually ends with the job. Florida gives you 31 days to convert it, no health exam."],
      onscreen=["LAID OFF IN FLORIDA?", "Health: 60 days", "Life: 31 days", "Check your HR packet"], cta="Free guide with both deadlines: link in bio."),
 dict(id="life-en", placement_suffix="job-en", lang="en", title="Your work life insurance ended",
      hook="Your life insurance probably ended the day your job did.",
      beats=["Most work life insurance is group coverage. It stops with the job.", "Florida law gives you 31 days to convert it without a health exam.",
             "But a new term policy can cost a lot less if you're healthy. Compare before day 31."],
      onscreen=["WORK LIFE INSURANCE", "Ends with the job", "31 days to convert", "Compare first"], cta="See how much coverage you need: link in bio."),
]


def link(code, c=None):
    return f"{BASE}/go/{code}" + (f"?c={c}" if c else "")


def pack():
    D = json.loads((ROOT / "config" / "distribution.json").read_text())["placements"]
    OUT.mkdir(parents=True, exist_ok=True)
    L = ["# Engine 1 posting pack", "", f"Links point at {BASE}. Every link is tracked to its own placement; leads from it show up under channel + engine in the funnel and `tools/engine_report.py`.",
         "Post only from the BRAND account (never a personal account, never posing as a consumer). Never ask anyone for personal details in a group or comment: the only place a person gives info is our form.", ""]
    L += ["| Placement | Platform | Status | Category |", "|---|---|---|---|"]
    for p in D:
        L.append(f"| {p['id']} | {p['platform']} | {p['status']} ({STATUS_NOTE.get(p['status'], '')}) | {p['category']} |")
    for p in D:
        L += ["", f"## {p['id']}: {p['platform']}", f"- **Where:** {p['where']}", f"- **Status:** {p['status']}: {STATUS_NOTE.get(p['status'], '')}",
              f"- **Who posts:** {p['owner']}"]
        if p.get("rules"):
            L.append(f"- **Rules:** {p['rules']}")
        if p.get("how_to_find"):
            L.append(f"- **How to find / message:** {p['how_to_find']}")
        if p.get("kind") == "setup":
            continue
        L.append(f"- **Tracked link:** {link(p['code'])}")
        for k, lab in (("copy_es", "Copy (ES)"), ("copy_en", "Copy (EN)")):
            if p.get(k):
                L += ["", f"**{lab}:**", "", "```", p[k] + ("\n" + link(p["code"]) if "reddit" not in p["utm_source"] else "\n[link only where allowed: " + link(p["code"]) + "]"), "```"]
    (OUT / "POSTING_PACK.md").write_text("\n".join(L) + "\n")
    S = ["# Short-form scripts (20-30s, brand accounts)", "", "Tracked link goes in the profile/bio for each platform. If an AI presenter is used, label it on screen ('Presentadora generada con IA' / 'AI-generated presenter').", ""]
    for s in SHORTS:
        S += [f"## {s['title']} ({s['lang'].upper()})", f"- **Hook (0-3s):** {s['hook']}"] + [f"- **Beat {i + 1}:** {b}" for i, b in enumerate(s["beats"])] + \
             [f"- **On-screen text:** {' / '.join(s['onscreen'])}", f"- **CTA:** {s['cta']}"]
        for plat in ("tiktok", "instagram", "youtube"):
            S.append(f"- **{plat} bio link:** {link('sf' + plat + s['placement_suffix'].replace('-', ''))}")
        S.append("")
    (OUT / "shortform.md").write_text("\n".join(S) + "\n")


def _qr(url):
    import qrcode
    b = io.BytesIO()
    qrcode.make(url).save(b, format="PNG")
    b.seek(0)
    return b


def handout(lang):
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    es = lang == "es"
    PRINT.mkdir(parents=True, exist_ok=True)
    f = PRINT / f"handout_job_{lang}.pdf"
    c = canvas.Canvas(str(f), pagesize=letter)
    W, H = letter
    c.setFillColorRGB(0.06, 0.13, 0.22); c.rect(0, H - 130, W, 130, fill=1, stroke=0)
    c.setFillColorRGB(1, 1, 1); c.setFont("Helvetica-Bold", 24)
    c.drawString(40, H - 70, "¿Perdiste tu trabajo en Florida?" if es else "Lost your job in Florida?")
    c.setFont("Helvetica", 14)
    c.drawString(40, H - 98, "Dos fechas límite de seguro que casi nadie explica" if es else "Two insurance deadlines most people never hear about")
    c.setFillColorRGB(0, 0, 0)
    y = H - 175
    blocks = ([("1. Seguro médico: 60 días", ["Perder el seguro del trabajo abre 60 días para inscribirte en un plan del Mercado.",
               "COBRA normalmente es el precio completo. Dejar COBRA después NO reabre el periodo.", "Si la empresa cerró, normalmente no hay COBRA."]),
               ("2. Seguro de vida: 31 días", ["El seguro de vida del trabajo normalmente termina con el empleo.",
               "La ley de Florida (s. 627.566) da 31 días para convertirlo sin examen médico.", "Una póliza nueva puede salir más barata si estás saludable. Compara antes del día 31."])]
              if es else
              [("1. Health insurance: 60 days", ["Losing job-based coverage opens a 60-day window to enroll in a Marketplace plan.",
                "COBRA is usually the full price. Dropping COBRA later does NOT reopen the window.", "If the company closed completely, COBRA usually isn't available."]),
               ("2. Life insurance: 31 days", ["Work life insurance usually ends with the job.",
                "Florida law (s. 627.566) gives you 31 days to convert it with no health exam.", "A new term policy can cost less if you're healthy. Compare before day 31."])])
    for head, lines in blocks:
        c.setFont("Helvetica-Bold", 16); c.drawString(40, y, head); y -= 24
        c.setFont("Helvetica", 12)
        for ln in lines:
            c.drawString(52, y, "• " + ln); y -= 18
        y -= 14
    url = link("ptcareersourcesfl")
    c.drawImage(ImageReader(_qr(url)), 40, y - 170, 160, 160)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(220, y - 40, "Revisa tus fechas y opciones gratis:" if es else "Check your dates and options, free:")
    c.setFont("Helvetica", 12); c.drawString(220, y - 62, url)
    c.drawString(220, y - 84, "Un agente con licencia te ayuda solo si lo pides." if es else "A licensed agent helps only if you ask.")
    c.setFont("Helvetica", 8); c.setFillColorRGB(0.35, 0.35, 0.35)
    c.drawString(40, 40, ("Información general. No somos el gobierno, tu empleador ni una aseguradora." if es else
                          "General information. Not the government, your employer or an insurance company.") + " Fla. Stat. 627.566 / 627.567; HealthCare.gov.")
    c.save()
    return f


def handout_separating():
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    PRINT.mkdir(parents=True, exist_ok=True)
    f = PRINT / "handout_separating_en.pdf"
    c = canvas.Canvas(str(f), pagesize=letter)
    W, H = letter
    c.setFillColorRGB(0.06, 0.13, 0.22); c.rect(0, H - 130, W, 130, fill=1, stroke=0)
    c.setFillColorRGB(1, 1, 1); c.setFont("Helvetica-Bold", 24); c.drawString(40, H - 70, "Leaving the military in Florida?")
    c.setFont("Helvetica", 14); c.drawString(40, H - 98, "Three insurance dates that start the day you separate")
    c.setFillColorRGB(0, 0, 0)
    y = H - 175
    for head, lines in [("1. Life insurance: 120 days", ["The VA says you can convert SGLI to a private permanent policy if you apply within 120 days,", "with no proof of good health needed for standard premium rates."]),
                        ("2. Life insurance: 240 days", ["Apply for VGLI within 240 days and the VA does not consider your health conditions.", "After that you must show good health. Outside limit: 1 year and 120 days."]),
                        ("3. Health insurance: 60 days", ["TRICARE ends when you separate; TAMP adds 180 days for certain groups (check milConnect).", "CHCBP: apply within 60 days of losing TRICARE/TAMP. Marketplace: enroll before coverage ends to avoid a gap."])]:
        c.setFont("Helvetica-Bold", 16); c.drawString(40, y, head); y -= 22
        c.setFont("Helvetica", 11)
        for ln in lines:
            c.drawString(52, y, ln); y -= 16
        y -= 14
    url = link("ptmilitaryen")
    c.drawImage(ImageReader(_qr(url)), 40, y - 170, 160, 160)
    c.setFont("Helvetica-Bold", 14); c.drawString(220, y - 40, "Check your dates and options, free:")
    c.setFont("Helvetica", 12); c.drawString(220, y - 62, url); c.drawString(220, y - 84, "A licensed agent helps only if you ask.")
    c.setFont("Helvetica", 8); c.setFillColorRGB(0.35, 0.35, 0.35)
    c.drawString(40, 40, "General information. Not affiliated with the VA, DoD or TRICARE. Sources: va.gov (VGLI, SGLI conversion), tricare.mil (TAMP, CHCBP).")
    c.save()
    return f


def counter_card():
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    f = PRINT / "counter_jan_es.pdf"
    W, H = 432, 576   # 6x8 in
    c = canvas.Canvas(str(f), pagesize=(W, H))
    c.setFillColorRGB(0.04, 0.48, 0.35); c.rect(0, H - 150, W, 150, fill=1, stroke=0)
    c.setFillColorRGB(1, 1, 1); c.setFont("Helvetica-Bold", 26); c.drawCentredString(W / 2, H - 70, "¿Te cambiaron de seguro?")
    c.setFont("Helvetica", 14); c.drawCentredString(W / 2, H - 100, "Si tu compañía salió de Florida (como Cigna), el Mercado te pasó a otra.")
    c.drawCentredString(W / 2, H - 120, "Todavía puedes escoger tu plan hasta el 15 de enero.")
    url = link("ptpharmacyjan", "STORE")
    c.drawImage(ImageReader(_qr(url)), W / 2 - 110, 150, 220, 220)
    c.setFillColorRGB(0, 0, 0); c.setFont("Helvetica-Bold", 14); c.drawCentredString(W / 2, 125, "Escanea: revisa tus opciones en 60 segundos")
    c.setFont("Helvetica", 8); c.drawCentredString(W / 2, 60, "Información general. No somos el gobierno ni una aseguradora. Un agente con licencia solo si lo pides.")
    c.drawCentredString(W / 2, 48, "Imprimir una por farmacia: reemplazar STORE en el enlace por el nombre corto de la tienda.")
    c.save()
    return f


def outreach():
    """Send-ready B2B messages (employer HR, CareerSource, community page owners), highest priority first.
    Nothing is sent from here: each one goes out only after approval, then gets logged in /admin/organic."""
    f = ROOT / "config" / "outreach.json"
    if not f.exists():
        return
    items = json.loads(f.read_text())["items"]
    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    items.sort(key=lambda i: (order.get(i["priority"].split(" ")[0], 3), i.get("life_window_closes") or "9999"))
    L = ["# Outreach queue: send-ready, approval required", "", f"Links point at {BASE}. After sending, log it in /admin/organic (action SENT + minutes) so results are tracked per message.", ""]
    for i in items:
        L += [f"## {i['id']} ({i['priority']})", f"- **To:** {i['target']}", f"- **Find the contact:** {i['how_to_find']}"]
        if i.get("life_window_closes"):
            from datetime import date as _d
            if i["life_window_closes"] >= _d.today().isoformat():
                L.append(f"- **Send before:** {i['life_window_closes']} (31-day life window); health window closes {i['sep_window_closes']}")
            else:
                L.append(f"- **Send before:** {i['sep_window_closes']} (60-day health window; the life window already closed)")
        L += ["", "```", i["message"].replace("{LINK}", BASE + i["link"]).replace("{PDF}", " and ".join(BASE + x.strip() for x in i.get("pdf", "").split(" and ")) if i.get("pdf", "").startswith("/") else i.get("pdf", "")).replace("{SENDER}", "[Name], [Brand] ([email])"), "```", ""]
    (OUT / "OUTREACH.md").write_text("\n".join(L) + "\n")


if __name__ == "__main__":
    pack()
    outreach()
    print(handout("en"), handout("es"), handout_separating(), counter_card(), OUT / "POSTING_PACK.md", OUT / "shortform.md")
