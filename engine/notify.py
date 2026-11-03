"""Speed-to-lead: tell the buyer the second a lead lands (minutes matter for contact rates).

Dry-run by default: writes the exact email to outbox/notifications/. Real sending only when
SMTP_HOST/SMTP_USER/SMTP_PASS/SMTP_FROM are set AND LEADENGINE_LIVE_DELIVERY=1 - i.e. when you turn it on.
"""
from __future__ import annotations

import os
import smtplib
import time
from email.message import EmailMessage
from pathlib import Path

from .db import now


def buyer_new_lead(con, buyer: dict, lead: dict, site: dict, outbox: Path) -> str:
    portal = f"{site['base_url']}/agent/{buyer.get('portal_token', '')}"
    body = (f"New {lead['product'].replace('_', ' ')} lead - tier {lead['tier']} (score {lead['score']:.0f})\n\n"
            f"Name: {lead['first_name']} {lead['last_name']}\n"
            f"Phone: {lead.get('phone_e164') or lead['phone']}  (typed by the consumer, consented {lead.get('consent_ts') or ''} UTC, "
            f"consent v{lead.get('consent_version') or ''}; line type {lead.get('phone_line_type') or 'unknown'})\n"
            f"Email: {lead.get('email') or '(not given)'}\n"
            f"ZIP/State: {lead['zip']} {lead['state']}   Age range: {lead['age_range']}\n"
            f"Prefers: {lead.get('preferred_contact')} ({lead.get('best_time')})\n"
            f"Wants: {lead.get('coverage_amount') or ''}  Timeframe: {lead.get('timeframe') or (lead.get('answers') or {}).get('health_timeframe', '')}\n"
            f"Their tool result: {(lead.get('tool_result') or {}).get('headline', '')}\n\n"
            f"This consumer consented to be contacted by {buyer['name']} only. Consent record + outcome buttons: {portal}\n"
            f"Please mark the outcome (contacted / appointment / sold / bad info) - it's how we send you better leads.\n")
    if lead["product"] == "health_aca":
        body += (f"\nSituation: {lead.get('need_path') or '-'} | household {lead.get('household_size') or '-'} | income band {lead.get('income_band') or '-'}"
                 f" | {lead.get('enrollment_situation') or '-'} | urgency {lead.get('urgency') or '-'} | language {(lead.get('lang') or 'en').upper()}\n"
                 "\nCompliance reminders:\n"
                 "- Florida: call 8am-8pm consumer local time, max 3 calls per 24h on the same subject (Fla. Stat. 501.616); honor STOP/opt-out immediately.\n"
                 "- Before helping with a Marketplace application, document the consumer's consent per 45 CFR 155.220(j)(2)(iii) and keep it 10 years.\n"
                 "- Special Enrollment Periods: CMS requires pre-enrollment verification for most SEPs in 2027 - ask for documents early.\n")
    msg = EmailMessage()
    msg["Subject"] = f"[New lead] {lead['first_name']} {lead['last_name'][:1]}. - {lead['campaign']} - tier {lead['tier']}"
    msg["From"] = os.environ.get("SMTP_FROM", "leads@example.invalid")
    msg["To"] = buyer.get("delivery", {}).get("email") or "buyer@example.invalid"
    msg.set_content(body)
    d = outbox / "notifications"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{lead['lead_id']}.eml").write_bytes(bytes(msg))
    status = "DRY_RUN"
    if os.environ.get("LEADENGINE_LIVE_DELIVERY") == "1" and os.environ.get("SMTP_HOST") and buyer.get("delivery", {}).get("email"):
        with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", 587))) as s:
            s.starttls(); s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"]); s.send_message(msg)
        status = "SENT"
    con.execute("INSERT INTO deliveries (lead_id,buyer_id,ts,method,status,payload_path,response) VALUES (?,?,?,?,?,?,?)",
                (lead["lead_id"], buyer["id"], now(), "email_notification", status, str(d / f"{lead['lead_id']}.eml"), ""))
    return status


def buyer_application(buyer_id: str, application: dict, agency_name: str, site: dict, outbox: Path) -> str:
    """Tell the operator an agency applied to become a buyer (via the public /for-agents page).
    DRY-RUN ONLY (always an .eml in outbox/applications/): the buyer row it created is an INACTIVE draft,
    so this email can never cause contact with a consumer. Reply-to points at the applicant so the
    operator can answer from their own mailbox without exposing tokens or PII beyond what they sent."""
    msg = EmailMessage()
    msg["Subject"] = f"[Buyer application] {agency_name} ({buyer_id})"
    msg["From"] = os.environ.get("SMTP_FROM", "hello@example.invalid")
    msg["To"] = os.environ.get("SMTP_FROM", "hello@example.invalid")
    msg["Reply-To"] = application.get("contact_email", "")
    body = (f"An agency applied to buy leads: {agency_name} (draft buyer id: {buyer_id})\n\n"
            f"Review + activate on Admin > Buyers (draft is inactive; activation still runs the full\n"
            "field check: license, NPN, prices per tier, delivery method).\n\n"
            f"Contact: {application.get('contact_name', '')} <{application.get('contact_email', '')}"
            f"> {application.get('contact_phone', '')}\n"
            f"Licensed states: {application.get('licensed_states') or '-'}\n"
            f"Products: {application.get('products') or '-'}\n"
            f"License #: {application.get('license_number') or '-'}   NPN: {application.get('npn') or '-'}\n"
            f"Expected volume: {application.get('expected_volume') or '-'}\n"
            f"Delivery preference: {application.get('delivery_preference') or 'outbox'}"
            f"{(' → ' + application['delivery_webhook']) if application.get('delivery_webhook') else ''}\n"
            f"Applied from: {application.get('source', '/for-agents')} at {application.get('ts', '')}\n\n"
            f"Note: {application.get('note', '') or '-'}\n")
    msg.set_content(body)
    d = outbox / "applications"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{buyer_id}.eml").write_bytes(bytes(msg))
    return "DRY_RUN"


def consumer_inquiry(message: str, reply_to: str, site: dict, outbox: Path) -> str:
    """A message a consumer typed into the public contact form. It is a SUPPORT request, not a lead:
    it is never routed, never sold and never added to any list. Addressed to the configured support
    inbox; without one it still lands as a reviewable .eml (and the admin Inquiries page lists every
    row), so an inquiry can never disappear into a database table nobody reads."""
    to = (site.get("support_email") or site.get("contact_email") or "").strip()
    msg = EmailMessage()
    msg["Subject"] = f"[Consumer inquiry] {site.get('brand', 'website')} contact form"
    msg["From"] = os.environ.get("SMTP_FROM", "hello@example.invalid")
    msg["To"] = to or os.environ.get("SMTP_FROM", "hello@example.invalid")
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content("A consumer wrote through the public contact form. Reply only if they asked for one,\n"
                    "and never add this address to marketing lists.\n\n"
                    f"Reply to: {reply_to or '(not given - do not attempt to identify them)'}\n\n{message}\n")
    d = outbox / "inquiries"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{now().replace(':', '').replace('-', '')}-{time.time_ns()}.eml"
    p.write_bytes(bytes(msg))
    return _send(msg) if (to and os.environ.get("LEADENGINE_LIVE_DELIVERY") == "1") else "DRY_RUN"


def _send(msg: EmailMessage) -> str:
    if os.environ.get("LEADENGINE_LIVE_DELIVERY") == "1" and os.environ.get("SMTP_HOST"):
        with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", 587))) as s:
            s.starttls(); s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"]); s.send_message(msg)
        return "SENT"
    return "DRY_RUN"


def consumer_summary(con, lead: dict, site: dict, outbox: Path, seller: str | None) -> str:
    """The summary the consumer was promised on the done screen. Transactional (they asked for it), with
    unsubscribe + physical address (CAN-SPAM). Never contains anything they didn't type or see on screen."""
    es = (lead.get("lang") or "en") == "es"
    r = lead.get("tool_result") or {}
    unsub = f"{site['base_url']}/optout?email={lead['email']}"
    if es:
        subj = "Tu resumen de cobertura médica 2027"
        body = (f"Hola {lead['first_name']}:\n\nAquí está el resumen que viste en el chequeo:\n\n{r.get('headline', '')}\n\n"
                "Fechas clave: la Inscripción Abierta va del 1 de noviembre de 2026 al 15 de enero de 2027. Inscríbete antes del 15 de diciembre "
                "para que la cobertura empiece el 1 de enero. Si perdiste la cobertura de tu trabajo, normalmente tienes 60 días.\n\n"
                + (f"{seller}, una agencia de seguros con licencia, te contactará como pediste (8am-8pm).\n\n" if seller else
                   "Nadie te va a llamar. Si quieres ayuda de un agente con licencia, vuelve a hacer el chequeo y elige \"Sí, ayúdame a comparar\".\n\n")
                + "Esto es un estimado educativo, no una determinación de elegibilidad. Fuente de fechas: HealthCare.gov. "
                f"{site.get('disclosure_es', '')} {site.get('gov_disclaimer_es', '')}\n\n"
                f"Darte de baja: {unsub}\n{site['legal_entity']} · {site['mailing_address']}\n")
    else:
        subj = "Your 2027 health coverage summary"
        body = (f"Hi {lead['first_name']},\n\nHere's the summary you saw in the check:\n\n{r.get('headline', '')}\n\n"
                "Key dates: Open Enrollment runs Nov 1, 2026 to Jan 15, 2027. Enroll by Dec 15 for coverage that starts Jan 1. "
                "If you lost job-based coverage, you usually have 60 days.\n\n"
                + (f"{seller}, a licensed insurance agency, will contact you the way you asked (8am-8pm).\n\n" if seller else
                   "Nobody will call you. If you want help from a licensed agent, take the check again and choose \"Yes, help me compare\".\n\n")
                + "This is an educational estimate, not an eligibility determination. Dates: HealthCare.gov. "
                f"{site.get('disclosure', '')} {site.get('gov_disclaimer', '')}\n\n"
                f"Unsubscribe: {unsub}\n{site['legal_entity']} · {site['mailing_address']}\n")
    msg = EmailMessage()
    msg["Subject"] = subj
    msg["From"] = os.environ.get("SMTP_FROM", "hello@example.invalid")
    msg["To"] = lead["email"]
    msg["List-Unsubscribe"] = f"<{unsub}>"
    msg.set_content(body)
    d = outbox / "consumer"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{lead['lead_id']}.eml").write_bytes(bytes(msg))
    status = "DRY_RUN" if lead.get("is_test") else _send(msg)   # test leads are never emailed
    con.execute("INSERT INTO deliveries (lead_id,buyer_id,ts,method,status,payload_path,response) VALUES (?,?,?,?,?,?,?)",
                (lead["lead_id"], None, now(), "consumer_summary", status, str(d / f"{lead['lead_id']}.eml"), ""))
    return status
