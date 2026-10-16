"""Mock bank operations on the shared ledger."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlmodel import Session, select

from policy.engine import load_rules

from common.db import (Account, Counterparty, Document, Invoice, Payee, Payment,
                       Setting, Transaction, account_balance_cents, blocking_documents, get_today, set_setting)


class BankServiceError(Exception):
    """A rejected bank operation."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _lock(session: Session) -> None:
    # Acquire the SQLite writer lock before reading IDs or idempotency state.
    session.execute(text("BEGIN IMMEDIATE"))


def _next_id(session: Session, model: Any, prefix: str, width: int = 4) -> str:
    ids = session.exec(select(model.id)).all()
    numbers = [int(value[len(prefix):]) for value in ids
               if value.startswith(prefix) and value[len(prefix):].isdigit()]
    return f"{prefix}{max(numbers, default=0) + 1:0{width}d}"


def get_accounts(session: Session) -> list[dict]:
    """Return accounts with balances as of simulated today."""
    return [dict(account.model_dump(mode="json"), balance_cents=account_balance_cents(
        session, account.id, get_today(session)))
        for account in session.exec(select(Account).order_by(Account.id))]


def list_transactions(session: Session, account_id: str, from_: date | None = None,
                      to: date | None = None) -> list[dict]:
    """Return inclusive booking-date history."""
    if session.get(Account, account_id) is None:
        raise BankServiceError(404, "Account not found")
    if from_ and to and from_ > to:
        raise BankServiceError(422, "from must not be after to")
    query = select(Transaction).where(Transaction.account_id == account_id)
    if from_:
        query = query.where(Transaction.booking_date >= from_)
    if to:
        query = query.where(Transaction.booking_date <= to)
    return [row.model_dump(mode="json") for row in session.exec(
        query.order_by(Transaction.booking_date, Transaction.id))]


def list_payees(session: Session) -> list[dict]:
    """Return registered payees."""
    return [row.model_dump(mode="json") for row in session.exec(select(Payee).order_by(Payee.id))]


def iban_is_valid(iban: str) -> bool:
    """Structural IBAN check: country code, check digits and ISO 13616 mod-97 checksum."""
    compact = (iban or "").replace(" ", "").upper()
    if not (15 <= len(compact) <= 34) or not compact[:2].isalpha() or not compact[2:4].isdigit():
        return False
    if not compact.isalnum():
        return False
    rearranged = compact[4:] + compact[:4]
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(digits) % 97 == 1


def create_payee(session: Session, name: str, iban: str,
                 counterparty_id: str | None, verified: bool) -> dict:
    """Register a payee after human authorization."""
    _lock(session)
    if counterparty_id and session.get(Counterparty, counterparty_id) is None:
        raise BankServiceError(422, "Unknown counterparty")
    if verified and not iban_is_valid(iban):
        raise BankServiceError(422, "A payee can only be verified with a valid IBAN")
    row = Payee(id=_next_id(session, Payee, "PAYEE-"), name=name, iban=iban,
                counterparty_id=counterparty_id, verified=verified, created_by="owner", created_at=datetime.now(timezone.utc))
    session.add(row)
    session.commit()
    session.refresh(row)
    return row.model_dump(mode="json")


def _check_mandate(session: Session, mandate: dict | None, tool: str, args: dict) -> str:
    """Approval id of a valid, unused mandate for exactly this action, signed with both roles' registered keys."""
    from agent.mandate import registered_keys, verify_mandate

    problems = verify_mandate(mandate, registered_keys()) if mandate else ["no signed mandate was sent"]
    body = (mandate or {}).get("mandate") or {}
    if not problems and (body.get("tool") != tool or body.get("args") != args
                         or not {"owner", "accountant"} <= set(body.get("required_roles") or [])):
        problems = ["the mandate is for a different action"]
    if not problems and session.get(Setting, f"bank.mandate.{body.get('approval_id')}") is not None:
        problems = ["the mandate was already used"]
    if problems:
        raise BankServiceError(403, "Verifying a payee needs a mandate signed by owner and accountant: "
                               + "; ".join(problems))
    return str(body["approval_id"])


def verify_payee(session: Session, payee_id: str, verified: bool, mandate: dict | None = None) -> dict:
    """Set a payee's verification status after human authorization.

    Unverifying only adds a block, so the human token is enough. Verifying lifts one, so the bank
    itself also needs a mandate that owner and accountant signed for this payee (used once).
    """
    _lock(session)
    row = session.get(Payee, payee_id)
    if row is None:
        raise BankServiceError(404, "Payee not found")
    if verified and not iban_is_valid(row.iban):
        raise BankServiceError(422, "A payee can only be verified with a valid IBAN")
    if verified:
        used = _check_mandate(session, mandate, "verify_payee", {"payee_id": payee_id})
        session.add(Setting(key=f"bank.mandate.{used}", value=payee_id))
    row.verified = verified
    session.add(row)
    session.commit()
    session.refresh(row)
    return row.model_dump(mode="json")


def set_payee_iban(session: Session, payee_id: str, iban: str) -> dict:
    """Fill in the IBAN of a payee that has none (e.g. from CSV import); it stays unverified."""
    _lock(session)
    row = session.get(Payee, payee_id)
    if row is None:
        raise BankServiceError(404, "Payee not found")
    if row.iban.strip():
        raise BankServiceError(409, "Bank details already set; create a new payee to use a different IBAN")
    if not iban_is_valid(iban):
        raise BankServiceError(422, "Invalid IBAN")
    row.iban, row.verified = iban, False
    session.add(row)
    session.commit()
    session.refresh(row)
    return row.model_dump(mode="json")


def create_payment(session: Session, body: dict, actor: str = "agent") -> dict:
    """Schedule a payment, retaining its original idempotency payload."""
    _lock(session)
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    existing = session.exec(select(Payment).where(
        Payment.idempotency_key == body["idempotency_key"])).first()
    if existing:
        stored = session.get(Setting, f"bank.request.{existing.id}")
        original = stored.value if stored else json.dumps(
            {key: getattr(existing, key) for key in body}, sort_keys=True,
            separators=(",", ":"), default=str)
        if canonical != original:
            raise BankServiceError(409, "Idempotency key already used with a different body")
        return dict(existing.model_dump(mode="json"), idempotent_replay=True)
    payee = session.get(Payee, body["payee_id"], populate_existing=True)
    if payee is None or not payee.verified:
        raise BankServiceError(422, "Payee must exist and be verified")
    account = session.get(Account, "ACC-MAIN")
    if account is None:
        raise BankServiceError(422, "Main account not found")
    if type(body["amount_cents"]) is not int or not 0 < body["amount_cents"] <= load_rules()["payment"]["max_payment_cents"]:
        raise BankServiceError(422, "amount_cents must be a positive integer within the payment maximum")
    if body["currency"] != account.currency:
        raise BankServiceError(422, "Currency must match account currency")
    if body["execute_date"] < get_today(session):
        raise BankServiceError(422, "execute_date must not be before today")
    if body.get("invoice_id"):
        invoice = session.get(Invoice, body["invoice_id"], populate_existing=True)
        if (invoice is None or invoice.direction != "AP" or invoice.status != "open"
                or invoice.counterparty_id != payee.counterparty_id
                or invoice.currency != account.currency):
            raise BankServiceError(422, "Invoice must be an open AP invoice for this payee and currency")
        committed = session.exec(select(Payment).where(Payment.invoice_id == invoice.id,
            Payment.status.in_(["scheduled", "executed"]))).all()
        if sum(p.amount_cents for p in committed) + body["amount_cents"] > invoice.amount_cents:
            raise BankServiceError(422, "Payments exceed invoice amount")
    if body.get("document_id") and session.get(Document, body["document_id"]) is None:
        raise BankServiceError(422, "Unknown document")
    if blocking_documents(session, payee.id, body.get("invoice_id"), body.get("document_id")):
        raise BankServiceError(422, "Linked document is missing or flagged and uncleared")
    payment = Payment(id=_next_id(session, Payment, "PAY-"), **body,
                      created_sim_date=get_today(session), created_by=actor,
                      created_at=datetime.now(timezone.utc))
    session.add(payment)
    session.add(Setting(key=f"bank.request.{payment.id}", value=canonical))
    session.commit()
    session.refresh(payment)
    return dict(payment.model_dump(mode="json"), idempotent_replay=False)


def list_payments(session: Session, status: str | None = None) -> list[dict]:
    """Return payments, optionally filtered by status."""
    query = select(Payment)
    if status:
        query = query.where(Payment.status == status)
    return [row.model_dump(mode="json") for row in session.exec(query.order_by(Payment.id))]


def reschedule_payment(session: Session, payment_id: str, execute_date: date) -> dict:
    """Move a scheduled payment to today or a later date."""
    _lock(session)
    row = session.get(Payment, payment_id, populate_existing=True)
    if row is None:
        raise BankServiceError(404, "Payment not found")
    if row.status != "scheduled":
        raise BankServiceError(409, "Only scheduled payments can be rescheduled")
    if execute_date < get_today(session):
        raise BankServiceError(422, "execute_date must not be before today")
    if execute_date < row.execute_date:
        raise BankServiceError(422, "Cannot move a payment earlier")
    if row.invoice_id:
        invoice = session.get(Invoice, row.invoice_id, populate_existing=True)
        if invoice is None or execute_date > invoice.due_date:
            raise BankServiceError(422, "Payment must remain within invoice due date")
    row.execute_date = execute_date
    session.add(row)
    session.commit()
    session.refresh(row)
    return row.model_dump(mode="json")


def advance(session: Session, days: int) -> dict:
    """Advance the clock and book all due scheduled payments atomically."""
    if type(days) is not int or days < 0:
        raise BankServiceError(422, "days must be a non-negative integer")
    _lock(session)
    previous = get_today(session)
    try:
        today = previous + timedelta(days=days)
    except OverflowError:
        raise BankServiceError(422, "Advance exceeds supported date range") from None
    payments = session.exec(select(Payment).where(
        Payment.status == "scheduled", Payment.execute_date <= today
    ).order_by(Payment.execute_date, Payment.id)).all()
    executed = []
    for payment in payments:
        payee = session.get(Payee, payment.payee_id, populate_existing=True)
        reason = None
        if payee is None or not payee.verified:
            reason = "Payee is unknown or unverified"
        elif not 0 < payment.amount_cents <= load_rules()["payment"]["max_payment_cents"]:
            reason = "Payment amount is outside allowed bounds"
        else:
            # A document flagged after scheduling must still stop the payment.
            if blocking_documents(session, payment.payee_id, payment.invoice_id, payment.document_id):
                reason = "Linked document is missing or flagged and uncleared"
        if reason:
            payment.status = "failed"
            session.add(payment)
            session.add(Setting(key=f"bank.failure.{payment.id}", value=reason))
            continue
        counterparty = session.get(Counterparty, payee.counterparty_id) if payee.counterparty_id else None
        category = {"payroll": "payroll", "landlord": "rent", "utility": "utilities",
                    "tax": "vat", "lender": "loan_repayment"}.get(
                        counterparty.kind if counterparty else "", "supplier_payment")
        if payment.invoice_id is None:
            category = "other"
        transaction = Transaction(id=_next_id(session, Transaction, "TX-", 6),
            account_id=payment.account_id, booking_date=payment.execute_date,
            amount_cents=-payment.amount_cents, currency=payment.currency,
            description=f"Payment {payment.id} to {payee.name}", category=category,
            counterparty_id=payee.counterparty_id, invoice_id=payment.invoice_id)
        session.add(transaction)
        session.flush()
        payment.status = "executed"
        payment.transaction_id = transaction.id
        session.add(payment)
        if payment.invoice_id:
            invoice = session.get(Invoice, payment.invoice_id)
            session.flush()
            settled = sum(p.amount_cents for p in session.exec(select(Payment).where(
                Payment.invoice_id == payment.invoice_id, Payment.status == "executed")))
            if invoice and invoice.direction == "AP" and settled >= invoice.amount_cents:
                invoice.status = "paid"
                invoice.paid_date = payment.execute_date
                session.add(invoice)
        executed.append(payment.id)
    # set_setting commits the clock and all ledger changes in one transaction.
    set_setting(session, "today", today.isoformat())
    return {"previous_today": previous.isoformat(), "today": today.isoformat(),
            "days": days, "executed_count": len(executed), "executed_payment_ids": executed,
            "total_executed_cents": sum(payment.amount_cents for payment in payments if payment.id in executed)}
