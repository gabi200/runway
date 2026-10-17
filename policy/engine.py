"""Deterministic authorization for every agent tool."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from common.db import (Account, Invoice, OutboxEmail, Payee, Payment, Setting, blocking_documents,
                       get_setting, get_today, set_setting)
from llm.pseudonym import contains_iban


@dataclass(frozen=True)
class Decision:
    outcome: str
    reason: str
    tier: str
    roles: tuple[str, ...] = ()


STRING = "string"
DATE = "date"
POSITIVE_INTEGER = "positive_integer"
BOOLEAN = "boolean"
SCENARIO_LIST = "scenario_list"


# Types and required fields mirror the tool contract without importing executor code.
_SCHEMAS: dict[str, tuple[str, dict[str, str]]] = {
    "get_balances": ("", {}),
    "list_transactions": ("", dict(account_id=STRING, from_date=DATE, to_date=DATE, category=STRING, limit=POSITIVE_INTEGER)),
    "list_receivables": ("", dict(status=STRING, overdue_only=BOOLEAN, counterparty_id=STRING)),
    "list_payables": ("", dict(status=STRING, counterparty_id=STRING)),
    "list_documents": ("", dict(flagged_only=BOOLEAN)),
    "flag_document": ("document_id reason", dict(document_id=STRING, reason=STRING)),
    "withdraw_request": ("approval_id reason", dict(approval_id=STRING, reason=STRING)),
    "get_financing_need": ("", dict(scenario=SCENARIO_LIST)),
    "get_forecast": ("", dict(weeks=POSITIVE_INTEGER, include_lines=BOOLEAN)),
    "run_scenario": ("changes", dict(changes=SCENARIO_LIST)),
    "compare_financing": ("amount_cents needed_by days_needed", dict(amount_cents=POSITIVE_INTEGER, needed_by=DATE, days_needed=POSITIVE_INTEGER)),
    "draft_email": ("purpose", dict(invoice_id=STRING, purpose=STRING, recipient_name=STRING, note=STRING)),
    "explain_figure": ("week", dict(week=POSITIVE_INTEGER, field=STRING, scenario=SCENARIO_LIST)),
    "make_chart": ("dataset", dict(dataset=STRING, scenario=SCENARIO_LIST, overdue_only=BOOLEAN)),
    "send_payment_reminder": ("invoice_id", dict(invoice_id=STRING)),
    "schedule_payment": ("payee_id amount_cents execute_date", dict(payee_id=STRING, amount_cents=POSITIVE_INTEGER, execute_date=DATE, invoice_id=STRING, document_id=STRING, idempotency_key=STRING, currency=STRING)),
    "reschedule_payment": ("payment_id new_date", dict(payment_id=STRING, new_date=DATE)),
    "prepare_financing_application": ("product_id amount_cents needed_by", dict(product_id=STRING, amount_cents=POSITIVE_INTEGER, needed_by=DATE, days_needed=POSITIVE_INTEGER)),
}


_SCENARIO_SCHEMAS = {  # a label is optional everywhere: a model that names its change is not refused for it
    "delay_customer": ("type counterparty_id days", dict(type=STRING, counterparty_id=STRING, days=POSITIVE_INTEGER, label=STRING)),
    "inflow": ("type amount_cents week label", dict(type=STRING, amount_cents=POSITIVE_INTEGER, week=POSITIVE_INTEGER, label=STRING)),
    "move_payment": ("type payment_id new_date", dict(type=STRING, payment_id=STRING, new_date=DATE, label=STRING)),
    "move_invoice": ("type invoice_id new_date", dict(type=STRING, invoice_id=STRING, new_date=DATE, label=STRING)),
}

_ENUMS = {"status": {"open", "paid", "all"},
         "purpose": {"payment_reminder", "payment_extension_request", "general"},
         "field": {"opening_cents", "receipts_cents", "payments_cents", "closing_cents"},
         "dataset": {"forecast_balance", "forecast_cashflow", "receivables", "receivables_ageing", "payables",
                     "transactions_monthly", "financing_options"}}


@lru_cache(maxsize=1)
def load_rules() -> dict:
    """Load policy configuration once; invalid configuration fails closed."""
    raw = Path(__file__).with_name("rules.yaml").read_bytes()
    rules = yaml.safe_load(raw)
    rules["sha256"] = hashlib.sha256(raw).hexdigest()  # which file is in force; see record_rules
    rules["tool_tiers"] = {name: tier for tier, names in rules["tiers"].items() for name in names}
    return rules


def record_rules(session: Session) -> None:
    """Audit the rules file in force whenever it differs from the one recorded last.

    ponytail: detection, not prevention. Someone who can edit rules.yaml before a process starts can also
    edit this code; the entry makes the change visible in the hash-chained log (and in a published head).
    """
    digest = load_rules()["sha256"]
    previous = get_setting(session, "policy_sha256")
    if previous != digest:
        from audit import log as audit

        audit.append(session, "system", "policy_loaded", {"sha256": digest, "previous": previous}, "INFO",
                     "Rules file in force" + (" changed since the last record" if previous else ""))
        set_setting(session, "policy_sha256", digest)


def reload_rules() -> dict:
    """Reload configuration after an intentional policy change or in tests."""
    load_rules.cache_clear()
    return load_rules()


def _date(value: Any) -> date:
    if type(value) is date:
        return value
    if type(value) is str and len(value) == 10:
        return date.fromisoformat(value)
    raise ValueError("Expected an ISO date")


def _forbidden(value: Any) -> bool:
    if isinstance(value, dict):
        return any(type(k) is not str or "iban" in k.lower() or k.lower() in {"bank_details", "account_number"}
                   or _forbidden(v) for k, v in value.items())
    return isinstance(value, (list, tuple)) and any(_forbidden(v) for v in value)


def _iban_in_values(value: Any) -> bool:
    """An IBAN smuggled into a free-text value (a note, an id) rather than an argument named for it."""
    if isinstance(value, dict):
        return any(_iban_in_values(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_iban_in_values(v) for v in value)
    return isinstance(value, str) and contains_iban(value)


def _validate(args: dict, required: str, fields: dict[str, str]) -> None:
    if set(args) - fields.keys() or any(k not in args for k in required.split()):
        raise ValueError("Missing or unexpected arguments")
    for key, value in args.items():
        kind = fields[key]
        if kind == DATE:
            _date(value)
        elif kind == STRING and (type(value) is not str or not value.strip()):
            raise ValueError(f"{key} must be a non-empty string")
        elif kind == POSITIVE_INTEGER and (type(value) is not int or value <= 0):
            raise ValueError(f"{key} must be a positive integer")
        elif kind == BOOLEAN and type(value) is not bool:
            raise ValueError(f"{key} must be boolean")
        elif kind == SCENARIO_LIST:
            if type(value) is not list or (key == "changes" and not value):
                raise ValueError("Invalid scenario list")
            for change in value:
                if type(change) is not dict:
                    raise ValueError("Invalid scenario change")
                if change.get("type") not in _SCENARIO_SCHEMAS:
                    raise ValueError("Unknown scenario change")
                _validate(change, *_SCENARIO_SCHEMAS[change["type"]])
    for key, choices in _ENUMS.items():
        if key in args and args[key] not in choices:
            raise ValueError(f"Invalid {key}")


def validate_call(tool_name: str, args: dict) -> None:
    """Stateless schema and amount checks, for idempotent replays that skip `evaluate`."""
    _validate(args, *_SCHEMAS[tool_name])
    if tool_name == "schedule_payment" and args["amount_cents"] > load_rules()["payment"]["max_payment_cents"]:
        raise ValueError("Payment exceeds maximum amount")


def evaluate(session: Session, tool_name: str, args: dict, actor: str = "agent",
             approvals: Sequence[str] = ()) -> Decision:
    """Evaluate fail-closed validation, forbidden capabilities, then the kill switch.

    Tool-specific DENY rules precede tier/approval logic. Approval re-checks run
    this same sequence, so recorded human roles never bypass a hard denial.
    """
    tier = "T4"
    try:
        rules = load_rules()
        tiers = rules["tool_tiers"]
        tier = tiers.get(tool_name, "T4")
        if tier not in {"T0", "T1", "T2", "T3", "T4"}:
            raise ValueError("Invalid tool tier")
        if tool_name not in _SCHEMAS or tier == "T4":
            return Decision("DENY", "Unknown or forbidden tool", tier)
        if type(args) is not dict or _forbidden(args):
            return Decision("DENY", "Invalid arguments or forbidden bank details", tier)
        if tier != "T0" and _iban_in_values(args):  # T0 may quote one: a flag reason citing the fraud IBAN
            return Decision("DENY", "Bank details are not allowed in tool arguments", tier)
        _validate(args, *_SCHEMAS[tool_name])
        if tool_name == "schedule_payment" and args["amount_cents"] > rules["payment"]["max_payment_cents"]:
            return Decision("DENY", "Payment exceeds maximum amount", tier)
        if actor not in {"agent", "owner", "accountant", "system"}:
            return Decision("DENY", "Unknown actor", tier)
        # Refresh runtime controls even when the caller retains an ORM identity cache.
        list(session.exec(select(Setting).where(Setting.key.in_(["today", "kill_switch"]))
                          .execution_options(populate_existing=True)))
        if get_setting(session, "kill_switch", "off") != "off" and tier != "T0":
            return Decision("DENY", "Kill switch restricts tools to T0", tier)
        today = get_today(session)
        if tool_name in {"compare_financing", "prepare_financing_application"}:
            bounds = rules["financing"]
            days = args.get("days_needed", bounds["min_days_needed"])
            if not bounds["min_days_needed"] <= days <= bounds["max_days_needed"]:
                return Decision("DENY", "days_needed must be within 1..365 days", tier)
            if not today <= _date(args["needed_by"]) <= today + timedelta(days=bounds["max_needed_by_days"]):
                return Decision("DENY", "needed_by must be within today..today+120 days", tier)
        payment_rules = rules["payment"]
        roles: tuple[str, ...] = ()
        reason = "Tool allowed by policy"
        if tool_name == "schedule_payment":
            payee = session.get(Payee, args["payee_id"], populate_existing=True)
            if payee is None:  # two reasons, so the model fixes a wrong id instead of sending the user to verify
                return Decision("DENY", "Unknown payee id: use the payee_id listed with the supplier's invoice", tier)
            if not payee.verified:
                return Decision("DENY", "Payee is not verified yet: a person verifies it on the Payees page", tier)
            account = session.get(Account, "ACC-MAIN", populate_existing=True)
            currency = args.get("currency", account.currency if account else None)
            if account is None or currency != account.currency:
                return Decision("DENY", "Currency must match the account", tier)
            if _date(args["execute_date"]) < today:
                return Decision("DENY", "Payment date is in the past", tier)
            invoice = session.get(Invoice, args["invoice_id"], populate_existing=True) if "invoice_id" in args else None
            amount = args["amount_cents"]
            if "invoice_id" in args and (invoice is None or invoice.direction != "AP" or invoice.status != "open"
                    or invoice.counterparty_id != payee.counterparty_id or amount > invoice.amount_cents
                    or invoice.currency != account.currency):
                return Decision("DENY", "Payment must match an open AP invoice and its amount", tier)
            if invoice:
                committed = session.exec(select(Payment).where(Payment.invoice_id == invoice.id,
                    Payment.status.in_(["scheduled", "executed"])).execution_options(populate_existing=True)).all()
                remaining = invoice.amount_cents - sum(p.amount_cents for p in committed)
                if amount != remaining:
                    return Decision("DENY", "Payment amount must equal the remaining unpaid and unscheduled invoice balance", tier)
            if blocking_documents(session, payee.id, args.get("invoice_id"), args.get("document_id")):
                return Decision("DENY", "Linked document is missing or flagged and uncleared", tier)
            payments = session.exec(select(Payment).where(Payment.automatic == True,
                Payment.status != "cancelled", Payment.created_sim_date == today)
                .execution_options(populate_existing=True)).all()
            payee_total = sum(p.amount_cents for p in payments if p.payee_id == payee.id)
            week_total = sum(p.amount_cents for p in session.exec(select(Payment).where(
                Payment.automatic == True, Payment.status != "cancelled",  # noqa: E712
                Payment.created_sim_date > today - timedelta(days=7), Payment.created_sim_date <= today)
                .execution_options(populate_existing=True)))
            # Only payments backed by an open AP invoice may run without a human.
            backed = invoice is not None or not payment_rules["automatic_requires_invoice"]
            if (backed and amount < payment_rules["automatic_amount_cents"]
                    and amount + payee_total < payment_rules["daily_payee_cents"]
                    and amount + sum(p.amount_cents for p in payments) < payment_rules["daily_global_cents"]
                    and amount + week_total < payment_rules["weekly_global_cents"]):
                reason = "Automatic payment within amount, daily and weekly limits"
            else:
                tier = payment_rules["approval_tier"]
                roles = tuple(payment_rules["material_roles"] if amount > payment_rules["accountant_above_cents"]
                              else payment_rules["owner_roles"])
                reason = "Payment exceeds automatic limits" if backed else "Payment without an open AP invoice needs approval"
        elif tool_name == "reschedule_payment":
            payment = session.get(Payment, args["payment_id"], populate_existing=True)
            invoice = session.get(Invoice, payment.invoice_id, populate_existing=True) if payment and payment.invoice_id else None
            new_date = _date(args["new_date"])
            if payment is None or payment.status != "scheduled" or invoice is None or invoice.critical:
                return Decision("DENY", "Requires a scheduled payment with a non-critical invoice", tier)
            if not max(today, payment.execute_date) <= new_date <= invoice.due_date:
                return Decision("DENY", "New date must not precede today or the current payment date, or exceed invoice due date", tier)
            roles = tuple(payment_rules["owner_roles"])
            reason = "Rescheduling requires owner approval"
        elif tool_name == "prepare_financing_application":
            from agent.financing import get_product, _eligibility

            product = get_product(args["product_id"])
            if product is None:
                return Decision("DENY", "Unknown financing product", tier)
            needed_by = _date(args["needed_by"])
            if needed_by < today or today + timedelta(days=product["time_to_cash_days"]) > needed_by:
                return Decision("DENY", "Financing cannot arrive by the needed date", tier)
            exclusion, _ = _eligibility(session, product, args["amount_cents"], args.get("days_needed"))
            if exclusion:
                return Decision("DENY", exclusion, tier)
            roles = tuple(payment_rules["material_roles"] if args["amount_cents"] > payment_rules["accountant_above_cents"]
                          else payment_rules["owner_roles"])
            reason = "Financing application requires approval"
        elif tool_name == "send_payment_reminder":
            invoice = session.get(Invoice, args["invoice_id"], populate_existing=True)
            if invoice is None or invoice.direction != "AR" or invoice.status != "open" or invoice.due_date >= today:
                return Decision("DENY", "Requires an open overdue AR invoice", tier)
            mails = session.exec(select(OutboxEmail).where(OutboxEmail.kind == "reminder")
                .execution_options(populate_existing=True)).all()
            if sum(m.sent_at.date() == today for m in mails) >= rules["reminder"]["daily_limit"]:
                return Decision("DENY", "Daily reminder limit reached", tier)
            if any(m.invoice_id == invoice.id and m.sent_at.date() > today - timedelta(days=rules["reminder"]["interval_days"]) for m in mails):
                return Decision("DENY", "Invoice reminder cooldown has not expired", tier)
        if roles:
            if not isinstance(approvals, (str, bytes)) and set(roles).issubset(set(approvals) - {"agent"}):
                return Decision("ALLOW", reason + "; approved by " + ", ".join(roles), tier)
            return Decision("NEEDS_APPROVAL", reason, tier, roles)
        return Decision("ALLOW", reason, tier)
    except (ValueError, TypeError, KeyError, AttributeError, OSError, RecursionError, SQLAlchemyError, yaml.YAMLError):
        return Decision("DENY", "Malformed arguments or unavailable policy configuration", tier)
