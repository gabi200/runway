"""Deterministic authorization for every agent tool."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from common.config import load_country_pack
from common.db import Account, Document, Invoice, OutboxEmail, Payee, Payment, Setting, get_setting, get_today


@dataclass(frozen=True)
class Decision:
    outcome: str
    reason: str
    tier: str
    roles: tuple[str, ...] = ()


# Types and required fields mirror the tool contract without importing executor code.
_SCHEMAS: dict[str, tuple[str, dict[str, str]]] = {
    'get_balances': ('', {}),
    'list_transactions': ('', dict(account_id='s', from_date='d', to_date='d', category='s', limit='i')),
    'list_receivables': ('', dict(status='s', overdue_only='b', counterparty_id='s')),
    'list_payables': ('', dict(status='s', counterparty_id='s')),
    'list_documents': ('', dict(flagged_only='b')),
    'flag_document': ('document_id reason', dict(document_id='s', reason='s')),
    'get_forecast': ('', dict(weeks='i', include_lines='b')),
    'run_scenario': ('changes', dict(changes='l')),
    'compare_financing': ('amount_cents needed_by days_needed', dict(amount_cents='i', needed_by='d', days_needed='i')),
    'draft_email': ('purpose', dict(invoice_id='s', purpose='s', recipient_name='s', note='s')),
    'explain_figure': ('week', dict(week='i', field='s', scenario='l')),
    'send_payment_reminder': ('invoice_id', dict(invoice_id='s')),
    'schedule_payment': ('payee_id amount_cents execute_date', dict(payee_id='s', amount_cents='i', execute_date='d', invoice_id='s', document_id='s', idempotency_key='s', currency='s')),
    'reschedule_payment': ('payment_id new_date', dict(payment_id='s', new_date='d')),
    'prepare_financing_application': ('product_id amount_cents needed_by', dict(product_id='s', amount_cents='i', needed_by='d', days_needed='i')),
}


def _date(value: Any) -> date:
    if type(value) is date:
        return value
    if type(value) is str and len(value) == 10:
        return date.fromisoformat(value)
    raise ValueError('Expected an ISO date')


def _forbidden(value: Any) -> bool:
    if isinstance(value, dict):
        return any(type(k) is not str or 'iban' in k.lower() or k.lower() in {'bank_details', 'account_number'}
                   or _forbidden(v) for k, v in value.items())
    return isinstance(value, (list, tuple)) and any(_forbidden(v) for v in value)


def _validate(args: dict, required: str, fields: dict[str, str]) -> None:
    if set(args) - fields.keys() or any(k not in args for k in required.split()):
        raise ValueError('Missing or unexpected arguments')
    for key, value in args.items():
        kind = fields[key]
        if kind == 'd':
            _date(value)
        elif kind == 's' and (type(value) is not str or not value.strip()):
            raise ValueError(f'{key} must be a non-empty string')
        elif kind == 'i' and (type(value) is not int or value <= 0):
            raise ValueError(f'{key} must be a positive integer')
        elif kind == 'b' and type(value) is not bool:
            raise ValueError(f'{key} must be boolean')
        elif kind == 'l':
            if type(value) is not list or (key == 'changes' and not value):
                raise ValueError('Invalid scenario list')
            for change in value:
                if type(change) is not dict:
                    raise ValueError('Invalid scenario change')
                schemas = {
                    'delay_customer': ('type counterparty_id days', dict(type='s', counterparty_id='s', days='i')),
                    'inflow': ('type amount_cents week label', dict(type='s', amount_cents='i', week='i', label='s')),
                    'move_payment': ('type payment_id new_date', dict(type='s', payment_id='s', new_date='d')),
                }
                if change.get('type') not in schemas:
                    raise ValueError('Unknown scenario change')
                _validate(change, *schemas[change['type']])
    enums = {'status': {'open', 'paid', 'all'},
             'purpose': {'payment_reminder', 'payment_extension_request', 'general'},
             'field': {'opening_cents', 'receipts_cents', 'payments_cents', 'closing_cents'}}
    for key, choices in enums.items():
        if key in args and args[key] not in choices:
            raise ValueError(f'Invalid {key}')


def evaluate(session: Session, tool_name: str, args: dict, actor: str = 'agent',
             approvals: Sequence[str] = ()) -> Decision:
    """Recheck hard denials before considering authenticated approval roles."""
    tier = 'T4'
    try:
        with Path(__file__).with_name('rules.yaml').open(encoding='utf-8') as stream:
            rules = yaml.safe_load(stream)
        tiers = {name: tier_name for tier_name, names in rules['tiers'].items() for name in names}
        tier = tiers.get(tool_name, 'T4')
        if tier not in {'T0', 'T1', 'T2', 'T3', 'T4'}:
            raise ValueError('Invalid tool tier')
        if tool_name not in _SCHEMAS or tier == 'T4':
            return Decision('DENY', 'Unknown or forbidden tool', tier)
        if type(args) is not dict or _forbidden(args):
            return Decision('DENY', 'Invalid arguments or forbidden bank details', tier)
        _validate(args, *_SCHEMAS[tool_name])
        if actor not in {'agent', 'owner', 'accountant', 'system'}:
            return Decision('DENY', 'Unknown actor', tier)
        # Refresh runtime controls even when the caller retains an ORM identity cache.
        list(session.exec(select(Setting).where(Setting.key.in_(['today', 'kill_switch']))
                          .execution_options(populate_existing=True)))
        if get_setting(session, 'kill_switch', 'off') != 'off' and tier != 'T0':
            return Decision('DENY', 'Kill switch restricts tools to T0', tier)
        today = get_today(session)
        payment_rules = rules['payment']
        roles: tuple[str, ...] = ()
        reason = 'Tool allowed by policy'
        if tool_name == 'schedule_payment':
            payee = session.get(Payee, args['payee_id'], populate_existing=True)
            if payee is None or not payee.verified:
                return Decision('DENY', 'Payee is unknown or unverified', tier)
            account = session.get(Account, 'ACC-MAIN', populate_existing=True)
            currency = args.get('currency', load_country_pack().currency)
            if account is None or currency != account.currency or load_country_pack().currency != account.currency:
                return Decision('DENY', 'Currency must match the account', tier)
            if _date(args['execute_date']) < today:
                return Decision('DENY', 'Payment date is in the past', tier)
            invoice = session.get(Invoice, args['invoice_id'], populate_existing=True) if 'invoice_id' in args else None
            amount = args['amount_cents']
            if 'invoice_id' in args and (invoice is None or invoice.direction != 'AP' or invoice.status != 'open'
                    or invoice.counterparty_id != payee.counterparty_id or amount > invoice.amount_cents
                    or invoice.currency != account.currency):
                return Decision('DENY', 'Payment must match an open AP invoice and its amount', tier)
            doc_ids = {args.get('document_id'), invoice.document_id if invoice else None} - {None}
            for doc_id in doc_ids:
                doc = session.get(Document, doc_id, populate_existing=True)
                if doc is None or (doc.flagged and not doc.cleared):
                    return Decision('DENY', 'Linked document is missing or flagged and uncleared', tier)
            payments = session.exec(select(Payment).where(Payment.automatic == True,
                Payment.status != 'cancelled', Payment.created_sim_date == today)
                .execution_options(populate_existing=True)).all()
            payee_total = sum(p.amount_cents for p in payments if p.payee_id == payee.id)
            if (amount < payment_rules['automatic_amount_cents']
                    and amount + payee_total < payment_rules['daily_payee_cents']
                    and amount + sum(p.amount_cents for p in payments) < payment_rules['daily_global_cents']):
                reason = 'Automatic payment within amount and daily limits'
            else:
                tier = payment_rules['approval_tier']
                roles = tuple(payment_rules['material_roles'] if amount > payment_rules['accountant_above_cents']
                              else payment_rules['owner_roles'])
                reason = 'Payment exceeds automatic limits'
        elif tool_name == 'reschedule_payment':
            payment = session.get(Payment, args['payment_id'], populate_existing=True)
            invoice = session.get(Invoice, payment.invoice_id, populate_existing=True) if payment and payment.invoice_id else None
            new_date = _date(args['new_date'])
            if payment is None or payment.status != 'scheduled' or invoice is None or invoice.critical:
                return Decision('DENY', 'Requires a scheduled payment with a non-critical invoice', tier)
            if not today <= new_date <= invoice.due_date:
                return Decision('DENY', 'New date must be today or later and within the invoice due date', tier)
            roles = tuple(payment_rules['owner_roles'])
            reason = 'Rescheduling requires owner approval'
        elif tool_name == 'prepare_financing_application':
            roles = tuple(payment_rules['material_roles'] if args['amount_cents'] > payment_rules['accountant_above_cents']
                          else payment_rules['owner_roles'])
            reason = 'Financing application requires approval'
        elif tool_name == 'send_payment_reminder':
            invoice = session.get(Invoice, args['invoice_id'], populate_existing=True)
            if invoice is None or invoice.direction != 'AR' or invoice.status != 'open' or invoice.due_date >= today:
                return Decision('DENY', 'Requires an open overdue AR invoice', tier)
            mails = session.exec(select(OutboxEmail).where(OutboxEmail.kind == 'reminder')
                .execution_options(populate_existing=True)).all()
            if sum(m.sent_at.date() == today for m in mails) >= rules['reminder']['daily_limit']:
                return Decision('DENY', 'Daily reminder limit reached', tier)
            if any(m.invoice_id == invoice.id and m.sent_at.date() > today - timedelta(days=rules['reminder']['interval_days']) for m in mails):
                return Decision('DENY', 'Invoice reminder cooldown has not expired', tier)
        if roles:
            if not isinstance(approvals, (str, bytes)) and set(roles).issubset(set(approvals) - {'agent'}):
                return Decision('ALLOW', reason + '; approved by ' + ', '.join(roles), tier)
            return Decision('NEEDS_APPROVAL', reason, tier, roles)
        return Decision('ALLOW', reason, tier)
    except (ValueError, TypeError, KeyError, AttributeError, OSError, RecursionError, SQLAlchemyError, yaml.YAMLError):
        return Decision('DENY', 'Malformed arguments or unavailable policy configuration', tier)
