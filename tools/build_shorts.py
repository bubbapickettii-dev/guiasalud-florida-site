"""Render the wave-2 Spanish short-form videos (text-on-screen, 1080x1920, ~22s, silent) from the scripts in
out/organic_pack/shortform_wave2.md. Faceless on purpose: nothing here pretends to be a person or an agent.
Output: out/shorts/V1..V7.mp4 + out/shorts/contact_sheet.png.   Run: python tools/build_shorts.py
Add a voice-over or music in the platform editor if wanted (silent here so there are no licensing issues)."""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out" / "shorts"
TMP = OUT / "_cards"
W, H = 1080, 1920
BG, INK, ACC, SOFT = (11, 43, 38), (255, 255, 255), (96, 214, 160), (190, 214, 206)
FB = "/usr/share/fonts/opentype/inter/Inter-Bold.otf"
FR = "/usr/share/fonts/opentype/inter/Inter-Regular.otf"
SOURCE_LINE = "Información educativa. No somos aseguradora ni agencia."

VIDEOS = {
    "V1": dict(hook="¿Abren las inscripciones el 15 de octubre?\nDepende de cuál seguro.",
               beats=[("MEDICARE", "15 de octubre al 7 de diciembre."),
                      ("SEGURO DEL MERCADO (OBAMACARE)", "Empieza el 1 de noviembre."),
                      ("PLAN EL 1 DE ENERO", "Escoge antes del 15 de diciembre. Cierra el 15 de enero.")],
               cta="Fechas con fuentes oficiales:\nlink en el perfil", src="Fuente: HealthCare.gov"),
    "V2": dict(hook="¿Tu plan del Mercado es de Cigna?\nHaz estas 3 cosas.",
               beats=[("1  CONFIRMA TU PLAN", "Entra a tu cuenta en CuidadoDeSalud.gov."),
                      ("2  CIGNA SE VA EN 2027", "Cigna sale del Mercado de Florida. Si no escoges, te pueden pasar a otra compañía."),
                      ("3  ESCOGE ANTES DEL 15/12", "Para tener cobertura desde el 1 de enero.")],
               cta="Guía gratis con fuentes oficiales:\nlink en el perfil", src="Fuente: HealthCare.gov y anuncio de Cigna"),
    "V3": dict(hook="Antes de escoger plan,\nhaz esto con tu médico.",
               beats=[("1  MÉDICO", "Busca su nombre en el directorio del plan."),
                      ("2  MEDICINAS", "Busca tus medicinas en la lista de medicamentos del plan."),
                      ("3  LLAMA AL CONSULTORIO", "Confirma. Los directorios a veces no están al día.")],
               cta="Qué revisar antes del 15 de diciembre:\nlink en el perfil", src="Fuente: HealthCare.gov"),
    "V4": dict(hook="El plan más barato por mes\npuede salir caro el año\nque lo necesitas.",
               beats=[("PRIMA", "Lo que pagas cada mes."),
                      ("DEDUCIBLE", "Lo que pagas tú antes de que el plan ayude."),
                      ("COMPARA LAS DOS", "Y el máximo que pagarías al año.")],
               cta="Cómo comparar planes:\nlink en el perfil", src="Fuente: HealthCare.gov"),
    "V5": dict(hook="¿Perdiste tu trabajo?\nTu seguro médico tiene un reloj.",
               beats=[("60 DÍAS: MERCADO", "Perder el seguro del trabajo te da un período especial para escoger plan."),
                      ("COBRA: HASTA 18 MESES", "Normalmente puedes quedarte con el plan del trabajo, pero pagas el costo completo."),
                      ("COMPARA LAS DOS", "Antes de decidir.")],
               cta="Guía gratis:\nlink en el perfil", src="Fuente: HealthCare.gov y Departamento de Trabajo"),
    "V6": dict(hook="¿Te quitaron Medicaid?\nPuede que tengas 90 días.",
               beats=[("PERDISTE MEDICAID O CHIP", "Puedes tener un período especial de 90 días para escoger plan del Mercado."),
                      ("TUS INGRESOS IMPORTAN", "Cambian lo que te ofrece el Mercado. Revisa tu caso con fuentes oficiales."),
                      ("MIRA TU CARTA", "Ahí está tu fecha límite.")],
               cta="Link en el perfil", src="Fuente: HealthCare.gov"),
    "V7": dict(hook="¿Se te pasó la inscripción?\nNo significa que no tengas opciones.",
               beats=[("PRÓXIMA INSCRIPCIÓN", "Empieza el 1 de noviembre."),
                      ("PERDISTE COBERTURA", "Puedes tener un período especial de 60 días."),
                      ("MEDICAID Y CHIP", "Se pueden solicitar durante todo el año si calificas.")],
               cta="Fechas con fuentes oficiales:\nlink en el perfil", src="Fuente: HealthCare.gov"),

    "V8": dict(hook="¿Te llegó una carta de Cigna?\nNo la botes.",
               beats=[("CIGNA SALE DEL MERCADO DE FLORIDA", "Los planes individuales de Cigna terminan el 31 de diciembre de 2026."),
                      ("SI NO HACES NADA", "Si tu plan es de CuidadoDeSalud.gov, te pueden inscribir en un plan de otra compañía."),
                      ("OTRO PLAN, OTRAS REGLAS", "Puede tener otros médicos, otras medicinas y otro precio.")],
               cta="Fechas oficiales y qué revisar:\nlink en el perfil", src="Fuente: HealthCare.gov"),
    "V9": dict(hook="¿Compraste tu plan de Cigna\ndirecto con la aseguradora?\nNadie te pasa a otro.",
               beats=[("COMPRADO DIRECTO", "Si lo compraste directo con la aseguradora, no hay cambio automático."),
                      ("EL PLAN TERMINA EL 31/12", "Tienes que escoger tú un plan nuevo."),
                      ("NO ESPERES AL ÚLTIMO DÍA", "Escoge antes del 15 de diciembre para tener cobertura el 1 de enero.")],
               cta="Revisa cuál es tu caso:\nlink en el perfil", src="Fuente: HealthCare.gov"),
    "V10": dict(hook="Antes de aceptar un plan nuevo:\n3 minutos con tu médico.",
               beats=[("1  TU MÉDICO", "Búscalo por nombre en el directorio del plan."),
                      ("2  TUS MEDICINAS", "Búscalas en la lista de medicamentos del plan."),
                      ("3  LLAMA AL CONSULTORIO", "Confirma que acepta el plan. Los directorios no siempre están al día.")],
               cta="Qué revisar antes del 15 de diciembre:\nlink en el perfil", src="Fuente: HealthCare.gov"),
}


def font(path, size):
    return ImageFont.truetype(path, size)


def wrap(draw, text, fnt, maxw):
    lines = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split(" "):
            t = (cur + " " + word).strip()
            if draw.textlength(t, font=fnt) <= maxw:
                cur = t
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    return lines


def card(path, head, sub=None, foot=None, big=False, idx=None, total=None):
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, W, 14], fill=ACC)
    maxw = W - 2 * 100 - 60            # leave the right rail free for platform buttons
    hs = 96 if big else 70
    fh = font(FB, hs)
    hl = wrap(d, head, fh, maxw)
    while len(hl) * hs * 1.18 > 640 and hs > 56:
        hs -= 6
        fh = font(FB, hs)
        hl = wrap(d, head, fh, maxw)
    fs = font(FR, 50)
    sl = wrap(d, sub, fs, maxw) if sub else []
    total_h = len(hl) * hs * 1.18 + (40 + len(sl) * 50 * 1.35 if sl else 0)
    y = max(330, (H - total_h) / 2 - 80)
    for ln in hl:
        d.text((100, y), ln, font=fh, fill=ACC if not big else INK)
        y += hs * 1.18
    if sl:
        y += 40
        for ln in sl:
            d.text((100, y), ln, font=fs, fill=INK)
            y += 50 * 1.35
    if idx:
        d.text((100, 250), f"{idx} / {total}", font=font(FR, 38), fill=SOFT)
    f2 = font(FR, 34)
    d.text((100, 1420), SOURCE_LINE, font=f2, fill=SOFT)
    if foot:
        d.text((100, 1470), foot, font=f2, fill=SOFT)
    im.save(path)
    return im


def clip(png, out, secs):
    fade = 0.35
    vf = (f"zoompan=z='min(zoom+0.0007,1.07)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={int(secs*30)}:s={W}x{H}:fps=30,"
          f"fade=t=in:st=0:d={fade},fade=t=out:st={secs-fade}:d={fade},format=yuv420p")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-t", str(secs), "-i", str(png),
                    "-f", "lavfi", "-t", str(secs), "-i", "anullsrc=r=44100:cl=stereo", "-vf", vf,
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-shortest", str(out)], check=True)


def build(key, v):
    TMP.mkdir(parents=True, exist_ok=True)
    cards = [(card(TMP / f"{key}_0.png", v["hook"], big=True), 3.8)]
    n = len(v["beats"])
    for i, (h, s) in enumerate(v["beats"], 1):
        cards.append((card(TMP / f"{key}_{i}.png", h, s, idx=i, total=n), 4.8))
    cards.append((card(TMP / f"{key}_cta.png", v["cta"], None, foot=v["src"], big=True), 4.2))
    parts = []
    for i, (im, secs) in enumerate(cards):
        p = TMP / f"{key}_{i if i < len(cards)-1 else 'cta'}.png"
        o = TMP / f"{key}_part{i}.mp4"
        clip(p, o, secs)
        parts.append(o)
    lst = TMP / f"{key}_list.txt"
    lst.write_text("".join(f"file '{p.name}'\n" for p in parts))
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(OUT / f"{key}.mp4")], check=True)
    return cards


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    thumbs = []
    import sys
    only = sys.argv[1:]
    for k, v in VIDEOS.items():
        if only and k not in only:
            continue
        cs = build(k, v)
        thumbs.append(cs[0][0].resize((270, 480)))
        print("built", k)
    sheet = Image.new("RGB", (280 * len(thumbs) + 20, 500), (30, 30, 30))
    for i, t in enumerate(thumbs):
        sheet.paste(t, (10 + i * 280, 10))
    sheet.save(OUT / "contact_sheet.png")


if __name__ == "__main__":
    main()
