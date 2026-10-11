"""Test outbox: every email the system 'sends' is stored as OutboxEmail + var/outbox/<id>.eml."""

from __future__ import annotations

import os
import re
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from sqlmodel import Session, select

from common.config import load_country_pack
from common.db import REPO_ROOT, Counterparty, Invoice, OutboxEmail, get_setting, get_today, utcnow

DEFAULT_OWNER_EMAIL = "owner@lumen-packaging.example"
COMPANY = "Lumen Packaging"


def var_dir() -> Path:
    """Runtime output root (override with RUNWAY_VAR_DIR, used by tests)."""
    return Path(os.environ.get("RUNWAY_VAR_DIR", REPO_ROOT / "var"))


def next_id(session: Session, model, prefix: str, width: int = 4) -> str:
    """Next human-readable id like MAIL-0001 (max numeric suffix + 1)."""
    nums = [int(m.group(1)) for i in session.exec(select(model.id)).all() if (m := re.search(r"(\d+)$", i))]
    return f"{prefix}-{(max(nums) + 1 if nums else 1):0{width}d}"


def owner_email(session: Session) -> str:
    """Owner address from Setting `owner_email`, env RUNWAY_OWNER_EMAIL or a default."""
    return get_setting(session, "owner_email") or os.environ.get("RUNWAY_OWNER_EMAIL", DEFAULT_OWNER_EMAIL)


def tone_for(days_overdue: int, previous_reminders: int = 0) -> str:
    """Escalate by number of earlier reminders (0 friendly, 1 firm, 2+ final) and days overdue.

    Days overdue can only raise the tone (>7 days firm, >14 days final), and "final" needs at
    least one earlier reminder, so a first email is never a final notice.
    """
    by_count = 0 if previous_reminders <= 0 else 1 if previous_reminders == 1 else 2
    by_days = 0 if days_overdue <= 7 else 1 if days_overdue <= 14 else 2
    level = max(by_count, by_days)
    if previous_reminders <= 0:
        level = min(level, 1)
    return ("friendly", "firm", "final")[level]


def compose_reminder(name: str, number: str, amount: str, due: str, days_overdue: int,
                     tone: str | None = None, previous_reminders: int = 0) -> tuple[str, str, str]:
    """Return (tone, subject, body) of a payment reminder; `amount` is a display string."""
    tone = tone or tone_for(days_overdue, previous_reminders)
    if tone == "final" and previous_reminders <= 0:
        tone = "firm"
    if tone == "friendly":
        subject = f"Friendly reminder: invoice {number}"
        body = (f"Dear {name},\n\nA quick reminder that invoice {number} for {amount} was due on {due} "
                f"and is now {days_overdue} day(s) overdue. If you have already paid, please ignore this "
                f"message; otherwise we would be grateful for payment this week.\n\nKind regards,\n{COMPANY}")
    elif tone == "firm":
        subject = f"Payment overdue: invoice {number}"
        earlier = " We have reminded you before." if previous_reminders > 0 else ""
        body = (f"Dear {name},\n\nInvoice {number} for {amount}, due on {due}, is {days_overdue} days "
                f"overdue.{earlier} Please arrange payment within 5 working days or tell us the expected "
                f"payment date.\n\nRegards,\n{COMPANY}")
    else:
        subject = f"FINAL NOTICE: invoice {number}"
        body = (f"Dear {name},\n\nDespite earlier reminders, invoice {number} for {amount} (due {due}) "
                f"remains unpaid and is {days_overdue} days overdue. This is our final notice: please pay "
                f"immediately, otherwise we will have to escalate the matter.\n\nRegards,\n{COMPANY}")
    return tone, subject, body


def _sim_now(session: Session) -> datetime:
    """Wall-clock time of day on the simulated date (the rules engine counts mails per simulated day)."""
    now = utcnow()
    return datetime.combine(get_today(session), now.timetz())


def send_email(session: Session, to: str, subject: str, body: str, kind: str = "notification",
               invoice_id: str | None = None) -> OutboxEmail:
    """Store an email in the OutboxEmail table and as an .eml file. Nothing leaves the machine."""
    mail = OutboxEmail(id=next_id(session, OutboxEmail, "MAIL"), to=to, subject=subject, body=body,
                       invoice_id=invoice_id, kind=kind, sent_at=_sim_now(session))
    session.add(mail)
    session.commit()
    session.refresh(mail)
    msg = EmailMessage()
    msg["From"] = f"{COMPANY} <runway@lumen-packaging.example>"
    msg["To"], msg["Subject"] = to, subject
    msg["Date"] = mail.sent_at.strftime("%a, %d %b %Y %H:%M:%S +0000")
    msg["X-Runway-Kind"] = kind
    msg.set_content(body)
    out = var_dir() / "outbox"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{mail.id}.eml").write_bytes(bytes(msg))
    return mail


def reminder_for_invoice(session: Session, invoice: Invoice, today) -> tuple[str, str, str, str]:
    """Return (to, tone, subject, body) for an overdue AR invoice."""
    cp = session.get(Counterparty, invoice.counterparty_id)
    days = (today - invoice.due_date).days
    amount = load_country_pack().display(invoice.amount_cents)
    previous = len(session.exec(select(OutboxEmail).where(
        OutboxEmail.invoice_id == invoice.id, OutboxEmail.kind == "reminder")).all())
    tone, subject, body = compose_reminder(cp.name if cp else "customer", invoice.number, amount,
                                           invoice.due_date.isoformat(), days, previous_reminders=previous)
    to = (cp.email if cp and cp.email else None) or f"accounts@{invoice.counterparty_id.lower()}.example"
    return to, tone, subject, body
