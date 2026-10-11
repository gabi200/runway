"""Policy boundaries and adversarial authorization checks."""

from datetime import datetime, timedelta, timezone

import pytest

from common.config import load_country_pack
from common.db import (Account, Counterparty, Document, Invoice, OutboxEmail, Payee,
                       Payment, Setting, get_today, reset_engines, session_scope)
from policy.engine import evaluate


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setenv('RUNWAY_DB', str(tmp_path / 'policy.db'))
    monkeypatch.setenv('RUNWAY_COUNTRY', 'eu')
    reset_engines()
    with session_scope() as s:
        today = get_today(s)
        currency = load_country_pack().currency
        s.add(Account(id='ACC-MAIN', name='Main', currency=currency))
        s.add(Counterparty(id='SUP', name='Supplier', kind='supplier'))
        s.commit()
        s.add(Payee(id='P', name='Supplier', iban='test', counterparty_id='SUP', verified=True))
        s.add(Document(id='D', filename='test.txt', kind='invoice'))
        s.commit()
        for direction in ['AP', 'AR']:
            s.add(Invoice(id=direction, number=direction, direction=direction, counterparty_id='SUP',
                issue_date=today - timedelta(days=30), due_date=today + timedelta(days=10) if direction == 'AP' else today - timedelta(days=1),
                amount_cents=1000000, currency=currency))
        s.commit()
        yield s
    reset_engines()


def args(amount=95000, **extra):
    return dict(payee_id='P', amount_cents=amount, execute_date='2026-10-19', **extra)


def payment(s, ident, amount=95000, **extra):
    row = Payment(id=ident, idempotency_key=ident, payee_id='P', amount_cents=amount,
                  currency=load_country_pack().currency, execute_date=get_today(s),
                  created_sim_date=get_today(s), automatic=True, **extra)
    s.add(row)
    s.commit()
    return row


@pytest.mark.parametrize('amount,outcome,roles', [(1,'ALLOW',()),(99999,'ALLOW',()),
    (100000,'NEEDS_APPROVAL',('owner',)),(500000,'NEEDS_APPROVAL',('owner',)),
    (500001,'NEEDS_APPROVAL',('owner','accountant'))])
def test_thresholds(session, amount, outcome, roles):
    d = evaluate(session, 'schedule_payment', args(amount))
    assert (d.outcome, d.roles) == (outcome, roles)
    assert d.tier == ('T2' if outcome == 'ALLOW' else 'T3')


@pytest.mark.parametrize('value', [None, True, False, 0, -1, 1.5, '95000', [], {}])
def test_invalid_amount(session, value):
    assert evaluate(session, 'schedule_payment', args(value)).outcome == 'DENY'


@pytest.mark.parametrize('tool', ['add_payee','update_payee','change_limits','export_data','set_kill_switch','raise_limit','unknown'])
def test_a4_forbidden_tools(session, tool):
    d = evaluate(session, tool, {}, 'owner', ['owner','accountant'])
    assert (d.outcome, d.tier) == ('DENY', 'T4')


@pytest.mark.parametrize('key', ['iban', 'bank_details', 'account_number', 'new_IBAN_value', 'IBAN'])
def test_bank_details_any_depth(session, key):
    assert evaluate(session, 'schedule_payment', args(**{key:'x'})).outcome == 'DENY'
    assert evaluate(session, 'run_scenario', {'changes':[{'type':'inflow', key:'x'}]}).outcome == 'DENY'


def test_a3_split_payments(session):
    for index in range(4):
        d = evaluate(session, 'schedule_payment', args())
        assert d.outcome == ('ALLOW' if index == 0 else 'NEEDS_APPROVAL')
        if d.outcome == 'ALLOW':
            payment(session, f'P{index}')


def test_daily_totals_and_exclusions(session):
    payment(session, 'cancel', status='cancelled')
    manual = payment(session, 'manual')
    manual.automatic = False
    old = payment(session, 'old')
    old.created_sim_date -= timedelta(days=1)
    session.commit()
    assert evaluate(session, 'schedule_payment', args()).outcome == 'ALLOW'
    payment(session, 'edge', amount=5000, status='executed')
    assert evaluate(session, 'schedule_payment', args()).outcome == 'NEEDS_APPROVAL'
    session.add(Payee(id='P2', name='Other', iban='test', verified=True))
    session.commit()
    for i in range(3):
        p = payment(session, f'global{i}', amount=70000)
        p.payee_id = 'P2'
        session.commit()
    assert evaluate(session, 'schedule_payment', args(85000)).outcome == 'NEEDS_APPROVAL'


def test_payee_invoice_currency_and_documents(session):
    for bad in [args(payee_id_override='x'), args(invoice_id='missing'), args(document_id='missing'), args(currency='XXX'), args(execute_date_override='y')]:
        assert evaluate(session, 'schedule_payment', bad).outcome == 'DENY'
    p = session.get(Payee, 'P')
    p.verified = False
    session.commit()
    assert evaluate(session, 'schedule_payment', args()).outcome == 'DENY'
    p.verified = True
    inv = session.get(Invoice, 'AP')
    doc = session.get(Document, 'D')
    doc.flagged = True
    inv.document_id = 'D'
    session.commit()
    assert evaluate(session, 'schedule_payment', args(document_id='D')).outcome == 'DENY'
    assert evaluate(session, 'schedule_payment', args(invoice_id='AP')).outcome == 'DENY'
    doc.cleared = True
    session.commit()
    assert evaluate(session, 'schedule_payment', args(invoice_id='AP')).outcome == 'ALLOW'
    for field, value in [('status','paid'), ('direction','AR'), ('amount_cents',1), ('counterparty_id','OTHER')]:
        old = getattr(inv, field)
        if field == 'counterparty_id':
            session.add(Counterparty(id='OTHER', name='Other', kind='supplier'))
            session.commit()
        setattr(inv, field, value)
        session.commit()
        assert evaluate(session, 'schedule_payment', args(invoice_id='AP')).outcome == 'DENY'
        setattr(inv, field, old)
        session.commit()


def test_approvals_rechecked(session):
    a = args(600000)
    assert evaluate(session, 'schedule_payment', a, approvals=['agent','owner']).outcome == 'NEEDS_APPROVAL'
    d = evaluate(session, 'schedule_payment', a, approvals=['owner','accountant'])
    assert d.outcome == 'ALLOW' and 'owner, accountant' in d.reason
    session.get(Payee, 'P').verified = False
    session.commit()
    assert evaluate(session, 'schedule_payment', a, approvals=['owner','accountant']).outcome == 'DENY'


def test_reschedule(session):
    p = payment(session, 'reschedule', invoice_id='AP')
    a = dict(payment_id=p.id, new_date='2026-10-29')
    assert evaluate(session, 'reschedule_payment', a).roles == ('owner',)
    assert evaluate(session, 'reschedule_payment', a, approvals=['owner']).outcome == 'ALLOW'
    for value in ['2026-10-30','2026-10-18','invalid']:
        assert evaluate(session, 'reschedule_payment', dict(a, new_date=value), approvals=['owner']).outcome == 'DENY'
    inv = session.get(Invoice, 'AP')
    inv.critical = True
    session.commit()
    assert evaluate(session, 'reschedule_payment', a).outcome == 'DENY'
    inv.critical = False
    p.status = 'executed'
    session.commit()
    assert evaluate(session, 'reschedule_payment', a).outcome == 'DENY'
    p.status = 'scheduled'
    p.invoice_id = None
    session.commit()
    assert evaluate(session, 'reschedule_payment', a).outcome == 'DENY'


@pytest.mark.parametrize('amount,roles', [(500000,('owner',)),(500001,('owner','accountant'))])
def test_financing(session, amount, roles):
    a = dict(product_id='short_term_loan', amount_cents=amount, needed_by='2026-11-01')
    assert evaluate(session, 'prepare_financing_application', a).roles == roles
    assert evaluate(session, 'prepare_financing_application', a, approvals=roles).outcome == 'ALLOW'


def mail(s, ident, days=0, invoice='AR', kind='reminder'):
    s.add(OutboxEmail(id=ident, to='test', subject='test', body='test', invoice_id=invoice,
        kind=kind, sent_at=datetime.combine(get_today(s)-timedelta(days=days), datetime.min.time(), tzinfo=timezone.utc)))
    s.commit()


def test_reminder_limits(session):
    a = dict(invoice_id='AR')
    assert evaluate(session, 'send_payment_reminder', a).outcome == 'ALLOW'
    mail(session, 'old', days=7)
    mail(session, 'alert', kind='fraud_alert')
    assert evaluate(session, 'send_payment_reminder', a).outcome == 'ALLOW'
    mail(session, 'recent', days=6)
    assert evaluate(session, 'send_payment_reminder', a).outcome == 'DENY'


def test_reminder_global_and_invoice(session):
    a = dict(invoice_id='AR')
    for i in range(9):
        mail(session, str(i), invoice='other')
    assert evaluate(session, 'send_payment_reminder', a).outcome == 'ALLOW'
    mail(session, '10', invoice='other')
    assert evaluate(session, 'send_payment_reminder', a).outcome == 'DENY'


@pytest.mark.parametrize('field,value', [('status','paid'),('direction','AP'),('due_date',None)])
def test_reminder_requires_overdue_ar(session, field, value):
    inv = session.get(Invoice, 'AR')
    setattr(inv, field, value if value is not None else get_today(session))
    session.commit()
    assert evaluate(session, 'send_payment_reminder', {'invoice_id':'AR'}).outcome == 'DENY'


def test_tiers_kill_switch_and_argument_validation(session):
    cases = {'get_balances':({},'T0'), 'list_documents':({},'T0'),
        'flag_document':({'document_id':'D','reason':'Suspicious'},'T0'),
        'list_transactions':({},'T0'), 'list_receivables':({},'T0'), 'list_payables':({},'T0'),
        'get_forecast':({},'T1'), 'run_scenario':({'changes':[{'type':'delay_customer','counterparty_id':'SUP','days':30}]},'T1'),
        'compare_financing':({'amount_cents':100,'needed_by':'2026-11-01','days_needed':30},'T1'),
        'draft_email':({'purpose':'general'},'T1'), 'explain_figure':({'week':1},'T1')}
    for tool, (a, tier) in cases.items():
        assert evaluate(session, tool, a).outcome == 'ALLOW'
        assert evaluate(session, tool, a).tier == tier
    session.add(Setting(key='kill_switch', value='on'))
    session.commit()
    for tool, (a, tier) in cases.items():
        assert evaluate(session, tool, a).outcome == ('ALLOW' if tier == 'T0' else 'DENY')
    assert evaluate(session, 'schedule_payment', args(), approvals=['owner','accountant']).outcome == 'DENY'
    for tool, a in [('schedule_payment',{}),('get_balances',None),('get_forecast',{'weeks':True}),
                    ('list_documents',{'flagged_only':1}),('list_transactions',{'from_date':'bad'}),
                    ('run_scenario',{'changes':[{'type':'unknown'}]}),('list_payables',{'status':'bad'})]:
        assert evaluate(session, tool, a).outcome == 'DENY'


def test_approval_refreshes_external_changes(session):
    from common.db import session_scope
    session.get(Payee, 'P')
    with session_scope() as other:
        other.get(Payee, 'P').verified = False
        other.commit()
    assert evaluate(session, 'schedule_payment', args(600000), approvals=['owner','accountant']).outcome == 'DENY'


def test_unknown_payee_and_past_date(session):
    a = args()
    a['payee_id'] = 'missing'
    assert evaluate(session, 'schedule_payment', a).outcome == 'DENY'
    a = args()
    a['execute_date'] = '2026-10-18'
    assert evaluate(session, 'schedule_payment', a).outcome == 'DENY'


def test_kill_switch_refreshes_external_change(session):
    from common.db import session_scope
    session.add(Setting(key='kill_switch', value='off'))
    session.commit()
    assert evaluate(session, 'get_forecast', {}).outcome == 'ALLOW'
    with session_scope() as other:
        other.get(Setting, 'kill_switch').value = 'on'
        other.commit()
    assert evaluate(session, 'get_forecast', {}).outcome == 'DENY'


def test_malformed_policy_fails_closed(session, monkeypatch):
    from io import StringIO
    from pathlib import Path
    from policy.engine import load_rules
    load_rules.cache_clear()
    monkeypatch.setattr(Path, 'open', lambda *a, **kw: StringIO('tiers: ['))
    assert evaluate(session, 'get_balances', {}).outcome == 'DENY'
    load_rules.cache_clear()


@pytest.mark.parametrize("status", ["scheduled", "executed"])
def test_invoice_total_prevents_double_payment(session, status):
    payment(session, "existing", amount=950000, invoice_id="AP", status=status)
    assert evaluate(session, "schedule_payment", args(50001, invoice_id="AP")).outcome == "DENY"
    assert evaluate(session, "schedule_payment", args(50000, invoice_id="AP")).outcome != "DENY"


@pytest.mark.parametrize("amount", [100000001, 2**62, 10**30])
def test_payment_upper_bound(session, amount):
    assert evaluate(session, "schedule_payment", args(amount)).outcome == "DENY"


@pytest.mark.parametrize("product,amount,needed", [("missing", 100, "2026-11-01"),
    ("overdraft", 5000001, "2026-11-01"), ("invoice_financing", 800001, "2026-11-01"),
    ("supply_chain_finance", 100, "2026-11-01"), ("overdraft", 100, "2026-10-18"),
    ("invoice_financing", 100, "2026-10-20")])
def test_financing_hard_denials(session, product, amount, needed):
    assert evaluate(session, "prepare_financing_application", dict(product_id=product,
        amount_cents=amount, needed_by=needed), approvals=["owner", "accountant"]).outcome == "DENY"


def test_reschedule_cannot_pull_payment_forward(session):
    row = payment(session, "later", invoice_id="AP")
    row.execute_date = get_today(session) + timedelta(days=3)
    session.add(row)
    session.commit()
    assert evaluate(session, "reschedule_payment", dict(payment_id=row.id,
        new_date=get_today(session).isoformat()), approvals=["owner"]).outcome == "DENY"


def test_financing_need_tier(session):
    assert evaluate(session, "get_financing_need", {"scenario": []}).tier == "T1"
    assert evaluate(session, "get_financing_need", {"scenario": []}).outcome == "ALLOW"


def test_cancelled_invoice_payment_does_not_consume_amount(session):
    payment(session, "cancelled-invoice", amount=1000000, invoice_id="AP", status="cancelled")
    assert evaluate(session, "schedule_payment", args(1000000, invoice_id="AP")).outcome == "NEEDS_APPROVAL"


def test_rules_cached_and_explicitly_reloadable(session, monkeypatch):
    from io import StringIO
    from pathlib import Path
    from policy.engine import load_rules, reload_rules
    import yaml

    load_rules.cache_clear()
    assert evaluate(session, "get_balances", {}).outcome == "ALLOW"
    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", lambda *a, **kw: StringIO("tiers: ["))
        assert evaluate(session, "get_balances", {}).outcome == "ALLOW"
        with pytest.raises(yaml.YAMLError):
            reload_rules()
        assert evaluate(session, "get_balances", {}).outcome == "DENY"
    reload_rules()
    assert evaluate(session, "get_balances", {}).outcome == "ALLOW"


def test_maximum_payment_boundary(session):
    assert evaluate(session, "schedule_payment", args(100000000),
                    approvals=["owner", "accountant"]).outcome == "ALLOW"
