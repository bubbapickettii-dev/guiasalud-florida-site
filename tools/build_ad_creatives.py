"""Renders the Meta ad creatives listed in config/meta_campaigns.json (feed 1080x1350, story 1080x1920).
Real type, exact words, no personal-attribute claims ("you are...") and no '$0'/'free insurance' claims.
    python tools/build_ad_creatives.py   -> assets/ads/meta/*.png"""
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "ads" / "meta"
OUT.mkdir(parents=True, exist_ok=True)
CR = {
 "es_exit_a": dict(k="SEGURO MÉDICO 2027 · FLORIDA", t="Cigna sale del Mercado", l=["Esos planes terminan el 31 de diciembre.", "Sin escoger antes del 15 de diciembre, el Mercado asigna un plan de otra compañía."], c="Fechas y opciones en 60 segundos", f="Información general con fuentes oficiales. No somos el gobierno ni una aseguradora."),
 "es_exit_b": dict(k="PLAN COMPRADO DIRECTO", t="Si el plan no es de CuidadoDeSalud.gov, nadie lo cambia por otro", l=["Simplemente termina el 31 de diciembre.", "Hay que escoger uno nuevo antes del 15 de diciembre."], c="Qué hacer, en español", f="Información general. Un agente con licencia ayuda solo si se le pide."),
 "es_exit_c": dict(k="ANTES DEL 15 DE DICIEMBRE", t="El plan asignado no revisa médicos ni medicinas", l=["Mismo nivel de plan, otra compañía y otra red.", "La lista de qué revisar en cada plan, gratis."], c="Chequeo gratis de 60 segundos", f="Información general. No somos el gobierno ni una aseguradora."),
 "en_life_a": dict(k="FLORIDA · LIFE INSURANCE", t="When a job ends, work life insurance usually ends too", l=["Florida law: 31 days to convert group coverage, no health exam.", "A new term policy can cost less. Compare before day 31."], c="Estimate the coverage needed", f="General information. A licensed life agent helps only if asked."),
 "en_life_b": dict(k="FLORIDA STATUTE 627.566", t="31 days.", l=["That's the window to convert group life insurance after employment ends.", "What to compare before it closes."], c="See the coverage a family needs", f="General information. Not an insurance company."),
 "en_exit_a": dict(k="2027 HEALTH PLANS · FLORIDA", t="Cigna is leaving Florida's Marketplace", l=["Those plans end December 31.", "Without a choice by December 15, HealthCare.gov assigns a plan from a different company."], c="Dates and options in 60 seconds", f="General information. Not the government or an insurance company."),
}
CSS = """*{box-sizing:border-box;margin:0}body{width:%dpx;height:%dpx;font-family:'Inter','Helvetica Neue',Arial,sans-serif;background:#0f2238;color:#fff;
display:flex;flex-direction:column;justify-content:space-between;padding:%dpx 80px 70px}.k{color:#9fe3c9;font-weight:800;letter-spacing:.08em;font-size:30px}
.t{font-size:%dpx;line-height:1.06;font-weight:900;letter-spacing:-.02em;margin:28px 0 36px}.l{font-size:40px;line-height:1.35;color:#dfe8f2;margin:0 0 22px}
.cta{background:#0b7a5a;border-radius:28px;padding:38px 40px;font-size:48px;font-weight:800}.f{font-size:24px;color:#9fb2c7;line-height:1.4;margin-top:26px}"""


def html(c, w, h):
    big = 96 if len(c["t"]) < 40 else 78 if len(c["t"]) < 70 else 66
    return (f"<html><head><style>{CSS % (w, h, 90 if h > 1500 else 70, big)}</style></head><body><div><div class='k'>{c['k']}</div>"
            f"<div class='t'>{c['t']}</div>" + "".join(f"<p class='l'>{x}</p>" for x in c["l"]) +
            f"</div><div><div class='cta'>{c['c']} →</div><div class='f'>{c['f']}</div></div></body></html>")


if __name__ == "__main__":
    with sync_playwright() as p:
        b = p.chromium.launch()
        for cid, c in CR.items():
            for kind, (w, h) in (("feed", (1080, 1350)), ("story", (1080, 1920))):
                pg = b.new_page(viewport={"width": w, "height": h})
                pg.set_content(html(c, w, h))
                pg.screenshot(path=str(OUT / f"{cid}_{kind}.png"))
                pg.close()
        b.close()
    print(len(CR) * 2, "creatives ->", OUT)
