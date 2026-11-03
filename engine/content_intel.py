"""CONTENT INTELLIGENCE — turn reviewed public-intent signals into draft EN/ES content ideas.

The loop: PUBLIC POSTS (signals.db) are reviewed by hand with evidence notes. When a
topic keeps recurring, that is DEMAND EVIDENCE for a content answer. This module
produces a draft brief per topic: what to say (EN + ES), the hook, a 3-beat script,
and the CTA pointing at the Check angle that already serves that need. It NEVER drafts
outreach to a person and never touches the leads DB — the only output is content to
publish public-facing, directing interested consumers to our own coverage-check page
(the first-party path docs/SOURCES.md marks as the only fully authorized one).

Inputs are REVIEWED/ACTIONED rows only: an unreviewed post is un-inspected evidence
and cannot become a content draft. Drafts are proposals; a human still edits before
any page is added to config/content.json (the guardrail there: education only, ends
at the Check, no plan/carrier solicitation).
"""
from __future__ import annotations

import json
from pathlib import Path

from .signals import TOPICS

ROOT = Path(__file__).resolve().parents[1]
CONTENT_CFG = ROOT / "config" / "content.json"

# Per-topic draft copy (EN/ES). Generic sentence frames, no invented facts.
_COPY: dict[str, dict] = {
    "job_loss": {
        "en_title": "Lost your job? Your 60-day health insurance window",
        "es_title": "¿Te despidieron? Tus 60 días para asegurar tu salud",
        "en_hook": "Lost your job and your insurance? You have 60 days — here's what to do first.",
        "es_hook": "¿Perdiste tu trabajo y tu seguro? Tienes 60 días — esto es lo primero que debes hacer.",
        "beats_en": ["Losing job coverage is a Special Enrollment Period — you don't wait for Open Enrollment.",
                     "You usually get 60 days from the loss to choose a Marketplace plan.",
                     "The Check shows your exact window and subsidy range in 60 seconds."],
        "beats_es": ["Perder cobertura del trabajo abren un Periodo de Inscripción Especial: no esperas a la inscripción abierta.",
                     "Normalmente tienes 60 días desde la pérdida para elegir un plan.",
                     "El Check muestra tu fecha límite y tu rango de subsidio en un minuto."],
    },
    "cobra": {
        "en_title": "COBRA ending? What comes after your 18 months",
        "es_title": "¿Se te acaba el COBRA? Qué sigue después",
        "en_hook": "COBRA ends in a few months. Here's the date that decides your next move.",
        "es_hook": "Tu COBRA se acaba pronto. Compara los costes — esto es lo que sigue.",
        "beats_en": ["COBRA can be far more expensive than a Marketplace plan when you compare first.",
                     "Losing COBRA is a Special Enrollment Period — enroll BEFORE the last month ends if you can.",
                     "Free Check: your deadline, plan window and subsidy range."],
        "beats_es": ["Compara costes: COBRA suele ser más caro que un plan del Mercado.",
                     "Perder COBRA es un Periodo de Inscripción Especial — inscríbete antes de que termine el último mes.",
                     "Check gratis: tu fecha límite, ventana y rango de subsidio."],
    },
    "carrier_exit": {
        "en_title": "Your 2027 plan is going away — the December 15 date to know",
        "es_title": "Tu plan 2027 se termina — la fecha límite del 15 de diciembre",
        "en_hook": "If your plan was discontinued, December 15 decides when coverage starts.",
        "es_hook": "Si tu plan fue cancelado, el 15 de diciembre decide cuándo empieza tu cobertura.",
        "beats_en": ["Carriers exiting the Marketplace give you Special Enrollment rights.",
                     "Same rules as Open Enrollment: enroll by Dec 15 for Jan 1 coverage.",
                     "Check your new-plan window and subsidy range — free."],
        "beats_es": ["Si tu aseguradora sale del Mercado, tienes derechos de inscripción especial.",
                     "Mismas reglas: inscríbete antes del 15 de diciembre para cobertura el 1 de enero.",
                     "Revisa tu ventana y rango de subsidio — gratis."],
    },
    "auto_moved": {
        "en_title": "Enrolled in a plan you didn't choose? You can still switch",
        "es_title": "¿Te inscribieron en un plan que no elegiste? Aún puedes cambiarlo",
        "en_hook": "Re-enrolled automatically? Don't assume it's final — check your options.",
        "es_hook": "¿Te pasaron a otro plan sin pedirlo? No lo des por hecho — revisa tus opciones.",
        "beats_en": ["Auto-enrolled plans aren't final: you can pick another plan.",
                     "Switching may change your premium and subsidy.",
                     "Run the 60-second Check, then call a licensed agent if you need help."],
        "beats_es": ["Los planes automáticos no son finales: puedes elegir otro plan.",
                     "Cambiar puede cambiar tu prima y subsidio.",
                     "Haz el Check gratis y llama a un agente con licencia si necesitas ayuda."],
    },
    "self_employed": {
        "en_title": "Self-employed and no employer plan? Start here",
        "es_title": "¿Trabajas por tu cuenta y no tienes seguro? Empieza aquí",
        "en_hook": "1099 or freelance? You can buy Marketplace coverage — income decides the help.",
        "es_hook": "¿1099 o independiente? Puedes comprar en el Mercado — el ingreso decide la ayuda.",
        "beats_en": ["You don't need an employer to get covered.",
                     "Net income (not gross hopes) drives your premium tax credit.",
                     "SE self-employed health premium deduction: ask a tax professional."],
        "beats_es": ["No necesitas un empleador para tener cobertura.",
                     "Tu ingreso neto determina tu crédito fiscal.",
                     "Deducción de primas para trabajadores independientes: consulta a tu contador."],
    },
    "timing": {
        "en_title": "Insurance deadlines: the two dates that matter for 2027",
        "es_title": "Dos fechas que deciden tu seguro para 2027",
        "en_hook": "Two dates decide your 2027 coverage. Miss them and you wait a year.",
        "es_hook": "Dos fechas deciden tu cobertura 2027.",
        "beats_en": ["Open Enrollment: Nov 1 – Jan 15; enroll by Dec 15 for Jan 1.",
                     "Life changes (job loss, aging off, marriage) reopen a 60-day window.",
                     "Free Check: which window applies to YOU right now."],
        "beats_es": ["Inscripción abierta: 1 nov – 15 ene; inscríbete antes del 15 dic para el 1 ene.",
                     "Cambios de vida reabren una ventana de 60 días.",
                     "Check gratis: qué ventana te aplica HOY."],
    },
    "cost_savings": {
        "en_title": "Paying too much for health coverage? Check what you actually qualify for",
        "es_title": "¿Muy caro tu seguro? Averigua para qué ayuda califica de verdad",
        "en_hook": "Premium tax credits lower the bill — most shoppers never check their range.",
        "es_hook": "Los créditos fiscales bajan la prima — casi nadie revisa su rango.",
        "beats_en": ["Subsidies are based on estimated yearly income and household size.",
                     "Know your range before comparing plans — it changes the price you see.",
                     "Free Check: your subsidy range in one minute."],
        "beats_es": ["El subsidio depende de tu ingreso estimado y tamaño del hogar.",
                     "Conoce tu rango ANTES de comparar planes — cambia el precio que ves.",
                     "Check gratis: tu rango de subsidio en un minuto."],
    },
    "family_aging": {
        "en_title": "Turning 26? What losing your parents' plan really means",
        "es_title": "¿Cumples 26? Qué significa perder el plan de tus padres",
        "en_hook": "Aging off the family plan is a 60-day moment, not a crisis.",
        "es_hook": "Salir del plan familiar es una ventana de 60 días, no una crisis.",
        "beats_en": ["You age out at 26 — that qualifies you for a Special Enrollment Period.",
                     "Plans for one person are usually cheaper than family premiums.",
                     "Run the Check free: your window and first-month costs."],
        "beats_es": ["Sales a los 26 — califica para un Periodo de Inscripción Especial.",
                     "Un plan individual suele costar menos que el familiar.",
                     "Haz el Check gratis: tu ventana y tu primer mes."],
    },
    "enroll_help": {
        "en_title": "Who can actually help you enroll (and it's free)",
        "es_title": "Quién te puede ayudar a inscribirte (y es gratis)",
        "en_hook": "You don't have to figure out enrollment alone — and help shouldn't cost you.",
        "es_hook": "No tienes que inscribirte solo — y la ayuda no debe costarte.",
        "beats_en": ["Licensed agents and Navigators: free to you, with your permission.",
                     "Avoid anyone asking for a fee to 'enroll you.'",
                     "Our Check gives the facts first; an agent calls only if you ask."],
        "beats_es": ["Agentes con licencia y Navigators: gratis para ti, con tu permiso.",
                     "Evita cobros por 'inscribirte'.",
                     "Nuestro Check te da los datos primero; un agente llama solo si tú pides."],
    },
}


def _content_pages() -> list[dict]:
    try:
        return json.loads(CONTENT_CFG.read_text(encoding="utf-8")).get("pages", [])
    except (OSError, ValueError):
        return []


def ideas(con, min_score: int = 1) -> list[dict]:
    """Draft briefs per topic from REVIEWED/ACTIONED signals. `min_score` = minimum number
    of evidence signals below which the brief is flagged NEEDS_MORE_EVIDENCE (still shown:
    review queues work best when the operator sees the margin)."""
    rows = [dict(r) for r in con.execute(
        "SELECT topic, lang, title, url, observed_at FROM signals WHERE status IN ('REVIEWED','ACTIONED') "
        "ORDER BY intent DESC")]
    by_topic: dict[str, list[dict]] = {}
    for r in rows:
        if r["topic"] in TOPICS:
            by_topic.setdefault(r["topic"], []).append(r)
    pages = _content_pages()
    out = []
    for topic, sigs in by_topic.items():
        if len(sigs) < min_score:
            continue
        copy = _COPY[topic]
        angle = TOPICS[topic]["angle"]
        langs = {s["lang"] for s in sigs}
        # existing pages that already answer this angle (EN and ES variants)
        existing = [p["slug"] for p in pages if p.get("angle") == angle and p.get("lang") == "en"]
        existing_es = [p["slug"] for p in pages if p.get("angle") == angle and p.get("lang") == "es"]
        new_slug = (
            topic.replace("_", "-") + "-" + (angle.replace("_", "-")))
        out.append({
            "topic": topic,
            "topic_label": TOPICS[topic]["label"],
            "angle": angle,
            "evidence": {"count": len(sigs), "langs": sorted(langs),
                         "newest": max((s["observed_at"] or "") for s in sigs),
                         "sample": [{"title": s["title"][:110], "url": s["url"]} for s in sigs[:3]]},
            "draft_en": {"title": copy["en_title"], "hook": copy["en_hook"], "beats": copy["beats_en"],
                         "cta": "Free 60-second check: /check?a=" + angle},
            "draft_es": {"title": copy["es_title"], "hook": copy["es_hook"], "beats": copy["beats_es"],
                         "cta": "Chequeo gratis de 60 segundos: /check?a=" + angle},
            "existing_en_pages": existing,
            "existing_es_pages": existing_es,
            "proposed_slug": new_slug,
            "state": "READY_TO_DRAFT" if len(sigs) >= 3 else "NEEDS_MORE_EVIDENCE",
        })
    out.sort(key=lambda b: (-b["evidence"]["count"], b["topic"]))
    return out
