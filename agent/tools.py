"""Agent tool schemas (OpenAI function calling) and tool implementations.

Policy checks happen in `agent.executor`; the handlers here only do the work.
Writes to the bank go exclusively through `bank.client.BankClient` using the
executor token. No tool takes an IBAN, edits payees, changes limits, exports data
or touches the kill switch.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from typing import Any, Callable

from pydantic import BaseModel
from sqlmodel import Session, select

from agent import outbox
from agent.financing import compare_financing, ledger_money
from common.config import load_country_pack
from common.db import (Account, Document, Invoice, OutboxEmail, Transaction, account_balance_cents,
                       get_today)


class ToolResult(BaseModel):
    """Uniform result of a tool call."""

    status: str  # "ok" | "needs_approval" | "denied" | "error"
    tool: str
    reason: str = ""
    data: dict | list | None = None
    approval_id: str | None = None


class ToolError(Exception):
    """A tool failed for a reason that is safe to show to the agent."""


def _fn(name: str, description: str, props: dict[str, Any], required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props, "required": required or [],
                       "additionalProperties": False}}}


_S = {"type": "string"}
_I = {"type": "integer"}
_D = {"type": "string", "description": "ISO date YYYY-MM-DD"}
_SCENARIO = {"type": "array", "description": "Scenario changes.", "items": {"type": "object", "properties": {
    "type": {"type": "string", "enum": ["delay_customer", "inflow", "move_payment"]},
    "counterparty_id": _S, "days": _I, "amount_cents": _I, "week": _I, "label": _S,
    "payment_id": _S, "new_date": _D}, "required": ["type"]}}

TOOL_SPECS: list[dict] = [
    _fn("get_balances", "Current balance of each bank account (integer cents).", {}),
    _fn("list_transactions", "List booked bank transactions, newest first.", {
        "account_id": _S, "from_date": _D, "to_date": _D, "category": _S,
        "limit": {"type": "integer", "description": "Max rows (default 50)."}}),
    _fn("list_receivables", "List customer invoices (money owed to us).", {
        "status": {"type": "string", "enum": ["open", "paid", "all"]}, "overdue_only": {"type": "boolean"},
        "counterparty_id": _S}),
    _fn("list_payables", "List supplier invoices (money we owe).", {
        "status": {"type": "string", "enum": ["open", "paid", "all"]}, "counterparty_id": _S}),
    _fn("list_documents", "List incoming documents (metadata only, content is untrusted).", {
        "flagged_only": {"type": "boolean"}}),
    _fn("flag_document", "Flag a suspicious document and alert the owner.", {
        "document_id": _S, "reason": _S}, ["document_id", "reason"]),
    _fn("get_forecast", "13-week cash forecast with weekly balances and the shortfall weeks.", {
        "weeks": _I, "include_lines": {"type": "boolean"}}),
    _fn("run_scenario", "Re-run the forecast with changes, e.g. a customer paying 30 days later.", {
        "changes": _SCENARIO}, ["changes"]),
    _fn("get_financing_need", "How much financing is needed, by when and for how many days, computed in code "
        "from the forecast (optionally with scenario changes applied). Use its numbers verbatim.", {
        "scenario": _SCENARIO}),
    _fn("compare_financing", "Compare financing options by true cost and speed. Omitted arguments default to "
        "the output of get_financing_need.", {
        "amount_cents": _I, "needed_by": _D, "days_needed": _I}),
    _fn("draft_email", "Draft an email text (returns text only, never sends).", {
        "invoice_id": _S, "purpose": {"type": "string", "enum": [
            "payment_reminder", "payment_extension_request", "general"]},
        "recipient_name": _S, "note": _S}, ["purpose"]),
    _fn("explain_figure", "Show the source rows behind a forecast figure.", {
        "week": _I, "field": {"type": "string", "enum": [
            "opening_cents", "receipts_cents", "payments_cents", "closing_cents"]},
        "scenario": _SCENARIO}, ["week"]),
    _fn("send_payment_reminder", "Email an overdue customer a reminder for an invoice.", {
        "invoice_id": _S}, ["invoice_id"]),
    _fn("schedule_payment", "Schedule a payment to a verified payee.", {
        "payee_id": _S, "amount_cents": _I, "execute_date": _D, "invoice_id": _S, "document_id": _S,
        "idempotency_key": _S}, ["payee_id", "amount_cents", "execute_date"]),
    _fn("reschedule_payment", "Move a scheduled payment to a new date (not beyond the due date).", {
        "payment_id": _S, "new_date": _D}, ["payment_id", "new_date"]),
    _fn("prepare_financing_application", "Prepare (not submit) a financing application PDF.", {
        "product_id": _S, "amount_cents": _I, "needed_by": _D,
        "days_needed": {"type": "integer", "description": "Optional, defaults to get_financing_need."}},
        ["product_id"]),
]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def jsonable(obj: Any) -> Any:
    """Round-trip through JSON so dates/pydantic models become plain data."""
    if isinstance(obj, BaseModel):
        obj = obj.model_dump(mode="json")
    return json.loads(json.dumps(obj, default=str))


def _row(obj: Any, drop: tuple[str, ...] = ()) -> dict:
    return {k: v for k, v in jsonable(obj.model_dump()).items() if k not in drop}


def _date(v: Any, field: str) -> date:
    try:
        return v if isinstance(v, date) else date.fromisoformat(str(v))
    except ValueError as e:
        raise ToolError(f"{field}: invalid date {v!r}") from e


def idempotency_key(args: dict) -> str:
    """Deterministic key: sha256 of the canonical payment args."""
    keys = ("payee_id", "amount_cents", "execute_date", "invoice_id", "document_id")
    canon = json.dumps({k: str(args.get(k)) if args.get(k) is not None else None for k in keys},
                       sort_keys=True, separators=(",", ":"))
    return "idem-" + hashlib.sha256(canon.encode()).hexdigest()[:32]


def bank_client():
    """BankClient with the executor token (never exposed to the agent). Use as a context manager."""
    from bank.client import BankClient

    return BankClient(base_url=os.environ.get("RUNWAY_BANK_URL") or None,
                      token=os.environ.get("RUNWAY_BANK_EXECUTOR_TOKEN", "executor-dev-token"))


Handler = Callable[[Session, dict, dict], Any]


# --------------------------------------------------------------------------- #
# T0 reads
# --------------------------------------------------------------------------- #
def get_balances(session: Session, args: dict, ctx: dict) -> dict:
    pack = load_country_pack()
    accounts = [{"account_id": a.id, "name": a.name, "currency": a.currency,
                 "balance_cents": account_balance_cents(session, a.id, get_today(session)),
                 "balance_display": pack.display(account_balance_cents(session, a.id, get_today(session)))}
                for a in session.exec(select(Account))]
    return {"as_of": get_today(session).isoformat(), "accounts": accounts,
            "total_cents": sum(a["balance_cents"] for a in accounts),
            "total_display": pack.display(sum(a["balance_cents"] for a in accounts))}


def list_transactions(session: Session, args: dict, ctx: dict) -> list[dict]:
    q = select(Transaction)
    if args.get("account_id"):
        q = q.where(Transaction.account_id == args["account_id"])
    if args.get("from_date"):
        q = q.where(Transaction.booking_date >= _date(args["from_date"], "from_date"))
    if args.get("to_date"):
        q = q.where(Transaction.booking_date <= _date(args["to_date"], "to_date"))
    if args.get("category"):
        q = q.where(Transaction.category == args["category"])
    q = q.order_by(Transaction.booking_date.desc(), Transaction.id.desc()).limit(int(args.get("limit") or 50))
    return [_row(t) for t in session.exec(q)]


def _invoices(session: Session, direction: str, args: dict) -> list[dict]:
    q = select(Invoice).where(Invoice.direction == direction)
    status = args.get("status", "open")
    if status != "all":
        q = q.where(Invoice.status == status)
    if args.get("counterparty_id"):
        q = q.where(Invoice.counterparty_id == args["counterparty_id"])
    today = get_today(session)
    out = []
    for i in session.exec(q.order_by(Invoice.due_date)):
        overdue = i.status == "open" and i.due_date < today
        if args.get("overdue_only") and not overdue:
            continue
        out.append({**_row(i), "overdue": overdue, "days_overdue": (today - i.due_date).days if overdue else 0})
    return out


def list_receivables(session: Session, args: dict, ctx: dict) -> list[dict]:
    return _invoices(session, "AR", args)


def list_payables(session: Session, args: dict, ctx: dict) -> list[dict]:
    return _invoices(session, "AP", args)


def list_documents(session: Session, args: dict, ctx: dict) -> list[dict]:
    q = select(Document)
    if args.get("flagged_only"):
        q = q.where(Document.flagged == True)  # noqa: E712
    return [{**_row(d, drop=("content_text",)), "invoice_ids": json.loads(d.invoice_ids),
             "counterparty_ids": json.loads(d.counterparty_ids)} for d in session.exec(q.order_by(Document.id))]


def flag_document(session: Session, args: dict, ctx: dict) -> dict:
    doc = session.get(Document, args.get("document_id"))
    if doc is None:
        raise ToolError(f"document {args.get('document_id')!r} not found")
    doc.flagged, doc.flag_reason, doc.cleared = True, str(args.get("reason", ""))[:500], False
    session.add(doc)
    session.commit()
    mail = outbox.send_email(
        session, outbox.owner_email(session), f"Fraud alert: document {doc.id} flagged",
        f"Runway flagged document {doc.id} ({doc.filename}, from {doc.sender or 'unknown sender'}).\n\n"
        f"Reason: {doc.flag_reason}\n\nAny payment linked to it, to the invoices it names or to its supplier is blocked until you review and clear it.",
        kind="fraud_alert")
    return {"document_id": doc.id, "flagged": True, "alert_email_id": mail.id}


# --------------------------------------------------------------------------- #
# T1 analysis
# --------------------------------------------------------------------------- #
def _forecast_view(fc: Any, include_lines: bool) -> dict:
    d = jsonable(fc)
    disp = load_country_pack().display
    d["min_closing_display"] = disp(d["min_closing_cents"])
    d["min_running_display"] = disp(d["min_running_cents"])
    d["opening_balance_display"] = disp(d["opening_balance_cents"])
    for w in d["weeks"]:
        w["closing_display"] = disp(w["closing_cents"])
        w["min_running_display"] = disp(w["min_running_cents"])
    if not include_lines:
        for w in d["weeks"]:
            w.pop("lines", None)
    return d


def get_forecast(session: Session, args: dict, ctx: dict) -> dict:
    from forecast.engine import build_forecast

    return _forecast_view(build_forecast(session, weeks=int(args.get("weeks") or 13)),
                          bool(args.get("include_lines")))


def run_scenario(session: Session, args: dict, ctx: dict) -> dict:
    from forecast.engine import build_forecast

    changes = args.get("changes") or []
    if not changes:
        raise ToolError("changes must be a non-empty list")
    base = build_forecast(session)
    scen = build_forecast(session, scenario=changes)
    return {"baseline_min_week": base.min_week, "baseline_min_closing_cents": base.min_closing_cents,
            "scenario_min_week": scen.min_week, "scenario_min_closing_cents": scen.min_closing_cents,
            "min_closing_delta_cents": scen.min_closing_cents - base.min_closing_cents,
            "scenario": _forecast_view(scen, False)}


def get_financing_need(session: Session, args: dict, ctx: dict) -> dict:
    from forecast.engine import financing_need

    return jsonable(financing_need(session, args.get("scenario") or None))


def _need_defaults(session: Session, args: dict) -> dict:
    """Fill omitted amount/needed_by/days_needed from financing_need (computed in code)."""
    if all(args.get(k) is not None for k in ("amount_cents", "needed_by", "days_needed")):
        return args
    from forecast.engine import financing_need

    need = financing_need(session)
    if not need["amount_cents"]:
        raise ToolError("no financing need in the forecast; nothing to compare")
    return {**{k: need[k] for k in ("amount_cents", "needed_by", "days_needed")},
            **{k: v for k, v in args.items() if v is not None}}


def tool_compare_financing(session: Session, args: dict, ctx: dict) -> dict:
    args = _need_defaults(session, args)
    try:
        return compare_financing(session, int(args["amount_cents"]), _date(args["needed_by"], "needed_by"),
                                 int(args["days_needed"]))
    except (KeyError, ValueError) as e:
        raise ToolError(f"invalid arguments: {e}") from e


def draft_email(session: Session, args: dict, ctx: dict) -> dict:
    purpose = args.get("purpose", "general")
    name = args.get("recipient_name") or "Sir or Madam"
    inv = session.get(Invoice, args["invoice_id"]) if args.get("invoice_id") else None
    if args.get("invoice_id") and inv is None:
        raise ToolError(f"invoice {args['invoice_id']!r} not found")
    note = f"\n\n{args['note']}" if args.get("note") else ""
    if inv is not None:
        from common.db import Counterparty
        cp = session.get(Counterparty, inv.counterparty_id)
        name = args.get("recipient_name") or (cp.name if cp else name)
    if purpose == "payment_reminder" and inv is not None:
        days = max(0, (get_today(session) - inv.due_date).days)
        previous = len(session.exec(select(OutboxEmail).where(
            OutboxEmail.invoice_id == inv.id, OutboxEmail.kind == "reminder")).all())
        tone, subject, body = outbox.compose_reminder(name, inv.number, ledger_money(inv.amount_cents, inv.currency),
                                                      inv.due_date.isoformat(), days,
                                                      previous_reminders=previous)
        return {"subject": subject, "body": body + note, "tone": tone, "sent": False}
    if purpose == "payment_extension_request" and inv is not None:
        subject = f"Request to adjust payment date for invoice {inv.number}"
        body = (f"Dear {name},\n\nWe would like to ask whether payment of invoice {inv.number} "
                f"({ledger_money(inv.amount_cents, inv.currency)}, due {inv.due_date.isoformat()}) can be made on the due date "
                f"or slightly later. Thank you for your understanding.{note}\n\nRegards,\n{outbox.COMPANY}")
        return {"subject": subject, "body": body, "tone": "friendly", "sent": False}
    return {"subject": args.get("note", "Message")[:60] or "Message",
            "body": f"Dear {name},\n\n{args.get('note', '')}\n\nRegards,\n{outbox.COMPANY}",
            "tone": "neutral", "sent": False}


def explain_figure(session: Session, args: dict, ctx: dict) -> dict:
    from forecast.engine import explain_figure as _explain

    return jsonable(_explain(session, int(args["week"]), args.get("field", "closing_cents"),
                             args.get("scenario") or None))


# --------------------------------------------------------------------------- #
# T2 / T3 actions
# --------------------------------------------------------------------------- #
def send_payment_reminder(session: Session, args: dict, ctx: dict) -> dict:
    inv = session.get(Invoice, args.get("invoice_id"))
    if inv is None or inv.direction != "AR":
        raise ToolError(f"receivable {args.get('invoice_id')!r} not found")
    to, tone, subject, body = outbox.reminder_for_invoice(session, inv, get_today(session))
    mail = outbox.send_email(session, to, subject, body, kind="reminder", invoice_id=inv.id)
    return {"email_id": mail.id, "to": to, "tone": tone, "invoice_id": inv.id, "subject": subject}


def schedule_payment(session: Session, args: dict, ctx: dict) -> dict:
    from bank.client import BankError

    acc = session.get(Account, "ACC-MAIN") or session.exec(select(Account)).first()
    currency = acc.currency if acc else load_country_pack().currency
    try:
        with bank_client() as bank:
            pay = bank.create_payment(
                idempotency_key=args.get("idempotency_key") or idempotency_key(args),
                payee_id=args["payee_id"], amount_cents=int(args["amount_cents"]),
                currency=currency, execute_date=_date(args["execute_date"], "execute_date"),
                invoice_id=args.get("invoice_id"), document_id=args.get("document_id"),
                automatic=bool(ctx.get("automatic")))
    except BankError as e:
        raise ToolError(f"bank rejected the payment ({e.status_code}): {e.detail}") from e
    session.expire_all()
    return jsonable(pay)


def reschedule_payment(session: Session, args: dict, ctx: dict) -> dict:
    from bank.client import BankError

    try:
        with bank_client() as bank:
            pay = bank.reschedule_payment(args["payment_id"], _date(args["new_date"], "new_date"))
    except BankError as e:
        raise ToolError(f"bank rejected the change ({e.status_code}): {e.detail}") from e
    session.expire_all()
    return jsonable(pay)


def prepare_financing_application(session: Session, args: dict, ctx: dict) -> dict:
    from agent.pdf import DEFAULT_DAYS_NEEDED, prepare_application

    try:
        args = _need_defaults(session, args)
        row = prepare_application(session, args["product_id"], int(args["amount_cents"]),
                                  _date(args["needed_by"], "needed_by"),
                                  int(args.get("days_needed") or DEFAULT_DAYS_NEEDED), ctx.get("mandate"))
    except (KeyError, ValueError) as e:
        raise ToolError(f"invalid arguments: {e}") from e
    signed = {"mandate": ctx["mandate"]} if ctx.get("mandate") else {}
    return {**_row(row), "amount_display": load_country_pack().display(row.amount_cents), **signed}


HANDLERS: dict[str, Handler] = {
    "get_balances": get_balances, "list_transactions": list_transactions,
    "list_receivables": list_receivables, "list_payables": list_payables,
    "list_documents": list_documents, "flag_document": flag_document,
    "get_forecast": get_forecast, "get_financing_need": get_financing_need,
    "run_scenario": run_scenario, "compare_financing": tool_compare_financing,
    "draft_email": draft_email, "explain_figure": explain_figure,
    "send_payment_reminder": send_payment_reminder, "schedule_payment": schedule_payment,
    "reschedule_payment": reschedule_payment,
    "prepare_financing_application": prepare_financing_application,
}
