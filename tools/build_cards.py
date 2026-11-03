"""Renders share cards (WhatsApp / IG feed 1080x1350, IG story 1080x1920) for wave-2 experiments.
Text is drawn as real type (not AI image text), so every word is exact. Re-run after BASE_URL is set:
    BASE_URL=https://yourdomain.com python tools/build_cards.py      -> assets/cards/*.png"""
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parents[1] / "assets" / "cards"
OUT.mkdir(parents=True, exist_ok=True)
base = os.environ.get("BASE_URL", "")
HOST = base.split("//")[-1].strip("/") if base and "localhost" not in base else "TUDOMINIO.com"
CARDS = [
 dict(id="es_plan_change", lang="es", link="/go/x016?c=wa-card", kicker="SEGURO MÉDICO 2027 · FLORIDA", title="¿Tu plan del Mercado es con Cigna?",
      lines=["Esas compañías salen del Mercado de Florida en 2027. Tu plan termina el 31 de diciembre.", "Elige un plan nuevo antes del 15 de diciembre para no quedarte sin cobertura."],
      cta="Chequeo gratis de 60 segundos", foot="Información general. No somos el gobierno ni una aseguradora. Un agente con licencia te ayuda solo si lo pides."),
 dict(id="en_plan_change", lang="en", link="/go/x017?c=card", kicker="2027 HEALTH PLANS · FLORIDA", title="Is your Marketplace plan with Cigna?",
      lines=["Those companies are leaving Florida's Marketplace for 2027. Your plan ends December 31.", "Pick a new plan by December 15 so there's no gap on January 1."],
      cta="Free 60-second check", foot="General information. Not the government or an insurance company. A licensed agent helps only if you ask."),
 dict(id="es_florida_blue", lang="es", link="/go/x016?c=wa-card-fb", kicker="RENOVACIÓN 2027 · FLORIDA", title="¿Te subió la renovación de Florida Blue?",
      lines=["Florida Blue pidió cerca de 26% más en sus planes PPO individuales para 2027.", "Si no haces nada, renuevas con el precio nuevo. Puedes comparar del 1 de noviembre al 15 de diciembre."],
      cta="Revisa tus opciones en 60 segundos", foot="Aumento solicitado (propuesto). Tu carta muestra tu precio real. Información general."),
 dict(id="en_job_loss", lang="en", link="/go/x018?c=card", kicker="LAID OFF IN FLORIDA?", title="You have 60 days to sort out health insurance.",
      lines=["Losing job-based coverage opens a 60-day window to enroll in a Marketplace plan.", "COBRA is usually the full price, and dropping it later doesn't reopen the window. Compare first."],
      cta="See my 60-day options", foot="General information. Not the government, your employer or an insurance company."),
 dict(id="es_job_loss", lang="es", link="/go/x018?c=card-es&a=es_life_change", kicker="¿TE DESPIDIERON EN FLORIDA?", title="Tienes 60 días para resolver tu seguro médico.",
      lines=["Perder el seguro del trabajo abre un periodo de 60 días para inscribirte en un plan del Mercado.", "COBRA normalmente es el precio completo, y dejarla después no reabre el periodo. Compara primero."],
      cta="Revisa tus opciones de 60 días", foot="Información general. No somos el gobierno, tu empleador ni una aseguradora."),
 dict(id="es_oep_late", lang="es", link="/inscripcion-abierta-2027", kicker="INSCRIPCIÓN ABIERTA 2027", title="¿Se te pasó la inscripción? No.",
      lines=["Sigue abierta hasta el 15 de enero de 2027.", "Si eliges plan entre el 16 de diciembre y el 15 de enero, la cobertura empieza el 1 de febrero."],
      cta="Revisa tus opciones", foot="Fechas de CuidadoDeSalud.gov. Información general."),
]
CSS = """*{box-sizing:border-box;margin:0}body{width:%dpx;height:%dpx;font-family:'Inter','Helvetica Neue',Arial,sans-serif;background:#0f2238;color:#fff;
display:flex;flex-direction:column;justify-content:space-between;padding:%dpx 80px 70px}
.k{color:#9fe3c9;font-weight:800;letter-spacing:.08em;font-size:30px}.t{font-size:%dpx;line-height:1.06;font-weight:900;letter-spacing:-.02em;margin:28px 0 36px}
.l{font-size:38px;line-height:1.35;color:#dfe8f2;margin:0 0 22px}.cta{background:#0b7a5a;border-radius:28px;padding:36px 40px;margin-top:20px}
.cta b{display:block;font-size:46px}.cta span{display:block;font-size:34px;color:#cff3e5;margin-top:10px;word-break:break-all}.f{font-size:24px;color:#9fb2c7;line-height:1.4}"""


def html(c, w, h):
    big = 92 if len(c["title"]) < 60 else 80
    return f"""<html><head><style>{CSS % (w, h, 110 if h > 1500 else 80, big)}</style></head><body><div><div class="k">{c['kicker']}</div>
<div class="t">{c['title']}</div>{''.join(f'<p class="l">{x}</p>' for x in c['lines'])}</div>
<div><div class="cta"><b>{c['cta']} →</b><span>{HOST}{c['link']}</span></div><p class="f" style="margin-top:28px">{c['foot']}</p></div></body></html>"""


with sync_playwright() as pw:
    b = pw.chromium.launch()
    for c in CARDS:
        for name, (w, h) in {"feed": (1080, 1350), "story": (1080, 1920)}.items():
            p = b.new_page(viewport={"width": w, "height": h})
            p.set_content(html(c, w, h)); p.wait_for_timeout(150)
            p.screenshot(path=str(OUT / f"{c['id']}_{name}.png")); p.close()
    b.close()
print("cards:", sorted(x.name for x in OUT.glob("*.png")))
