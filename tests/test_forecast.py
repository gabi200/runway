"""Tests for forecast/engine.py on the generated Lumen Packaging data."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlmodel import Session

from common.db import Invoice, Payment, Transaction, get_engine, reset_engines
from data.generate import generate
from forecast.engine import build_forecast, explain_figure


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    path = tmp_path_factory.mktemp("fc") / "f.db"
    generate(path, seed=42, out_dir=path.parent / "out")
    yield path
    reset_engines()


@pytest.fixture()
def s(db):
    with Session(get_engine(db)) as session:
        yield session


def test_shape(s):
    fc = build_forecast(s)
    assert len(fc.weeks) == 13 and fc.start_date == date(2026, 10, 19) and fc.currency == "EUR"
    assert fc.opening_balance_cents == 4_000_000
    for prev, b in zip(fc.weeks, fc.weeks[1:]):
        assert b.opening_cents == prev.closing_cents
    for b in fc.weeks:
        assert b.closing_cents == b.opening_cents + b.receipts_cents + b.payments_cents


def test_week6_trough(s):
    fc = build_forecast(s)
    assert fc.min_week == 6
    assert fc.min_closing_cents == -1_830_332
    assert all(fc.weeks[i].closing_cents >= 0 for i in range(4))  # weeks 1-4
    assert 6 in fc.shortfall_weeks


def test_running_balance_dips_inside_weeks_are_shortfalls(s):
    """Regression (B1): a week whose running balance goes below zero is a shortfall, even if it closes higher."""
    fc = build_forecast(s)
    w6, w7 = fc.weeks[5], fc.weeks[6]
    # 11-26: the Inkworks payment (12,000) is counted before the same-day receipt (11,000)
    assert (w6.min_running_cents, w6.min_running_date) == (-2_930_332, date(2026, 11, 26))
    # 12-01: rent (8,000) before the 90,000 Brightfield receipt of 12-03; the week closes positive
    assert (w7.min_running_cents, w7.min_running_date) == (-2_630_332, date(2026, 12, 1))
    assert w6.closing_cents == -1_830_332 and w7.closing_cents == 6_369_668  # closings unchanged
    assert fc.shortfall_weeks == [6, 7, 10, 11]  # week 11 opens below zero until the 12-31 receipt
    assert (fc.min_running_cents, fc.min_running_date, fc.min_running_week) == (-2_930_332, date(2026, 11, 26), 6)
    for b in fc.weeks:
        assert b.min_running_cents <= min(b.opening_cents, b.closing_cents)


def test_brightfield_lands_week_11_and_week6_pressure(s):
    fc = build_forecast(s)
    bf = [(b.index, ln) for b in fc.weeks for ln in b.lines if ln.amount_cents >= 90_000_00]
    assert any(i == 11 for i, ln in bf if ln.kind == "receipt" and ln.amount_cents == 95_000_00)
    w5, w6 = fc.weeks[4], fc.weeks[5]
    assert any("CM-2026-0919" in ln.label for ln in w5.lines + w6.lines)
    assert any(ln.label.startswith("Payroll") for ln in w6.lines)
    assert any(ln.label.startswith("VAT") for ln in w6.lines)


def test_traceability(s):
    fc = build_forecast(s)
    for b in fc.weeks:
        for ln in b.lines:
            assert ln.source_refs, ln
            for ref in ln.source_refs:
                model = Transaction if ref.startswith("TX-") else Payment if ref.startswith("PAY-") else Invoice
                assert s.get(model, ref) is not None, ref


def test_scenario_move_payment_improves_week6(s):
    base = build_forecast(s).weeks[5].closing_cents
    inv = s.get(Payment, "PAY-0001")
    due = s.get(Invoice, inv.invoice_id).due_date
    moved = build_forecast(s, scenario=[{"type": "move_payment", "payment_id": "PAY-0001", "new_date": due.isoformat()}])
    assert moved.weeks[5].closing_cents == base + inv.amount_cents
    assert moved.scenario


def test_scenario_delay_brightfield_worsens(s):
    base = build_forecast(s)
    late = build_forecast(s, scenario=[{"type": "delay_customer", "counterparty_id": "CUST-BRIGHTFIELD", "days": 30}])
    assert late.min_closing_cents < base.min_closing_cents
    assert late.weeks[5].closing_cents <= base.weeks[5].closing_cents


def test_scenario_accepts_customer_name(s):
    """Regression (judge task T03): a live model passed the customer's name instead of its id."""
    by_id = build_forecast(s, scenario=[{"type": "delay_customer", "counterparty_id": "CUST-BRIGHTFIELD", "days": 30}])
    by_name = build_forecast(s, scenario=[{"type": "delay_customer", "counterparty_id": "Brightfield Appliances",
                                           "days": 30}])
    assert by_name.min_closing_cents == by_id.min_closing_cents


@pytest.mark.parametrize("days", [7, 10])
def test_delay_of_overdue_customer_moves_receipt_later(s, days):
    """Regression (B3): the scenario delay is added after the overdue clamp, so it never moves a receipt earlier."""
    def ar1091(fc):
        return next(ln for w in fc.weeks for ln in w.lines if ln.source_refs == ["AR-1091"])
    base = ar1091(build_forecast(s))
    late = ar1091(build_forecast(s, scenario=[{"type": "delay_customer", "counterparty_id": "CUST-SMALL-3",
                                               "days": days}]))
    assert base.expected_date == date(2026, 10, 22)  # due 10-04, overdue: today + 3
    assert late.expected_date == base.expected_date + timedelta(days=days)
    assert f"+{days} days scenario delay" in late.note


@pytest.mark.parametrize("change, message", [
    ({"type": "move_payment", "payment_id": "PAY-9999", "new_date": "2026-12-01"}, "unknown payment"),
    ({"type": "move_payment", "payment_id": "PAY-0001", "new_date": "2026-12-15"}, "due date"),  # AP-2052 due 12-14
    ({"type": "move_payment", "payment_id": "PAY-0001", "new_date": "2026-11-20"}, "earlier"),  # scheduled 11-26
    ({"type": "delay_customer", "counterparty_id": "CUST-NOPE", "days": 5}, "unknown customer"),
    ({"type": "delay_customer", "counterparty_id": "SUP-CARTONIA", "days": 5}, "unknown customer"),
    ({"type": "inflow", "amount_cents": 100, "week": 20, "label": "x"}, "week must be 1..13"),
    ({"type": "inflow", "amount_cents": 100, "week": 0, "label": "x"}, "week must be 1..13"),
    ({"type": "bogus"}, "unknown scenario change"),
])
def test_invalid_scenario_changes_are_rejected(s, change, message):
    """Regression (B4): an invalid what-if change raises instead of silently returning the baseline."""
    with pytest.raises(ValueError, match=message):
        build_forecast(s, scenario=[change])


def test_only_scheduled_payments_can_be_moved(s):
    s.get(Payment, "PAY-0001").status = "executed"
    s.flush()
    with pytest.raises(ValueError, match="only scheduled payments"):
        build_forecast(s, scenario=[{"type": "move_payment", "payment_id": "PAY-0001", "new_date": "2026-12-01"}])
    s.rollback()


@pytest.mark.parametrize("weeks", [0, -1, 53, 10**6, 20_000])
def test_weeks_is_bounded(s, weeks):
    """Regression (B5): weeks was unbounded (20,000 weeks took 42 s, 10**6 overflowed the date)."""
    with pytest.raises(ValueError, match=r"weeks must be 1\.\.52"):
        build_forecast(s, weeks=weeks)


def test_52_weeks_allowed(s):
    fc = build_forecast(s, weeks=52)
    assert len(fc.weeks) == 52 and fc.weeks[-1].closing_cents == fc.weeks[-1].opening_cents + \
        fc.weeks[-1].receipts_cents + fc.weeks[-1].payments_cents


def test_scenario_inflow(s):
    base = build_forecast(s)
    fin = build_forecast(s, scenario=[{"type": "inflow", "amount_cents": 40_000_00, "week": 5, "label": "financing"}])
    assert fin.weeks[5].closing_cents == base.weeks[5].closing_cents + 40_000_00
    assert fin.weeks[3].closing_cents == base.weeks[3].closing_cents
    assert any(ln.label == "financing" for ln in fin.weeks[4].lines)


def test_explain_figure(s):
    ex = explain_figure(s, 6)
    fc = build_forecast(s)
    assert ex["value_cents"] == fc.weeks[5].closing_cents
    assert ex["lines"] and ex["sources"]["transactions"] and ex["sources"]["invoices"]
    ids = {r["id"] for k in ex["sources"].values() for r in k}
    assert all(ref in ids for ln in ex["lines"] for ref in ln["source_refs"])
    pay = explain_figure(s, 6, "payments_cents")
    assert all(ln["amount_cents"] < 0 for ln in pay["lines"])
    with pytest.raises(ValueError):
        explain_figure(s, 99)


def test_payment_originated_transactions_do_not_change_estimates(db):
    """Regression (H2): small automatic payments booked as payroll/rent must not move the estimates."""
    from datetime import datetime, timezone
    from common.db import Payee
    with Session(get_engine(db)) as s:
        base = build_forecast(s)
        before = [w.closing_cents for w in base.weeks]
        s.add(Payee(id="PAYEE-PROBE", name="Probe", counterparty_id="LANDLORD", iban="DE00", verified=True,
                    created_at=datetime.now(timezone.utc)))
        for n, (cat, cp) in enumerate([("rent", "LANDLORD"), ("payroll", "PAYROLL")], 1):
            s.add(Transaction(id=f"TX-PROBE{n}", account_id="ACC-MAIN", booking_date=date(2026, 10, 18),
                              amount_cents=-95_000, currency="EUR", description="auto", category=cat,
                              counterparty_id=cp))
            s.add(Payment(id=f"PAY-PROBE{n}", idempotency_key=f"probe-{n}", payee_id="PAYEE-PROBE",
                          amount_cents=95_000, currency="EUR", execute_date=date(2026, 10, 18),
                          status="executed", automatic=True, transaction_id=f"TX-PROBE{n}"))
        s.commit()
        after = build_forecast(s)
        # opening balance drops by the two real payments; the medians stay the same. The payroll one only
        # counts toward October's payroll (B2: booked this month), so that estimate is 950.00 smaller.
        assert after.opening_balance_cents == base.opening_balance_cents - 190_000
        assert [w.closing_cents for w in after.weeks] == [c - 95_000 for c in before]
        payroll = [ln.amount_cents for w in after.weeks for ln in w.lines if ln.label == "Payroll (estimate)"]
        assert payroll == [-5_506_000 + 95_000, -5_506_000, -5_506_000]
        refs = {r for w in after.weeks for ln in w.lines for r in ln.source_refs}
        assert not refs & {"TX-PROBE1", "TX-PROBE2"}
        s.rollback()
        for model, ids in ((Payment, ["PAY-PROBE1", "PAY-PROBE2"]), (Transaction, ["TX-PROBE1", "TX-PROBE2"]),
                           (Payee, ["PAYEE-PROBE"])):
            for i in ids:
                s.delete(s.get(model, i))
        s.commit()


def _estimate_dates(fc, label):
    return [ln.expected_date for w in fc.weeks for ln in w.lines if ln.label == f"{label} (estimate)"]


@pytest.mark.parametrize("from_payment", [False, True])
def test_recurring_estimate_not_repeated_when_booked_this_month(s, from_payment):
    """Regression (B2a): payroll already booked this month (bank row or bank-executed payment) is not estimated again."""
    s.add(Transaction(id="TX-EARLYPAY", account_id="ACC-MAIN", booking_date=date(2026, 10, 16), amount_cents=-5_506_000,
                      currency="EUR", description="payroll paid early", category="payroll", counterparty_id="PAYROLL"))
    if from_payment:
        s.add(Payment(id="PAY-EARLYPAY", idempotency_key="early-pay", payee_id="PAYEE-PAYROLL", amount_cents=5_506_000,
                      currency="EUR", execute_date=date(2026, 10, 16), status="executed", transaction_id="TX-EARLYPAY"))
    s.flush()
    assert _estimate_dates(build_forecast(s), "Payroll") == [date(2026, 11, 25), date(2026, 12, 25)]
    s.rollback()


def test_scheduled_tax_payment_replaces_vat_estimate(s):
    """Regression (B2b): a scheduled payment to the tax office covers that month's VAT estimate."""
    s.add(Payment(id="PAY-VATPROBE", idempotency_key="vat-probe", payee_id="PAYEE-TAX", amount_cents=1_149_166,
                  currency="EUR", execute_date=date(2026, 10, 23), status="scheduled"))
    s.flush()
    fc = build_forecast(s)
    assert _estimate_dates(fc, "VAT payment") == [date(2026, 11, 25), date(2026, 12, 25)]
    assert any("PAY-VATPROBE" in ln.source_refs for ln in fc.weeks[0].lines)
    s.rollback()


def test_open_tax_payable_replaces_vat_estimate(s):
    """Regression (B2c): an open payable to the tax office due in a month covers that month's VAT estimate."""
    s.add(Invoice(id="AP-TAXPROBE", number="VAT-2026-11", direction="AP", counterparty_id="TAX-OFFICE",
                  issue_date=date(2026, 10, 15), due_date=date(2026, 11, 25), amount_cents=1_200_000,
                  currency="EUR", critical=True))
    s.flush()
    fc = build_forecast(s)
    assert _estimate_dates(fc, "VAT payment") == [date(2026, 10, 25), date(2026, 12, 25)]
    assert any("AP-TAXPROBE" in ln.source_refs for ln in fc.weeks[5].lines)
    s.rollback()


def test_small_known_item_only_tops_up_the_estimate(s):
    """A 10.00 payment to the tax office must not wipe out the 11,491.66 VAT estimate of its month."""
    before = [w.closing_cents for w in build_forecast(s).weeks]
    s.add(Payment(id="PAY-TINY", idempotency_key="tiny", payee_id="PAYEE-TAX", amount_cents=1_000,
                  currency="EUR", execute_date=date(2026, 11, 25), status="scheduled"))
    s.flush()
    fc = build_forecast(s)
    assert [w.closing_cents for w in fc.weeks] == before
    vat = [ln for ln in fc.weeks[5].lines if ln.label == "VAT payment (estimate)"]
    assert [ln.amount_cents for ln in vat] == [-1_148_166]
    s.rollback()


def test_unbooked_recurring_item_carried_forward_after_its_day(s):
    """Regression (B2d): once the clock passes the 25th with nothing booked, payroll and VAT are still due."""
    from common.db import Setting
    s.get(Setting, "today").value = "2026-10-27"
    s.flush()
    fc = build_forecast(s)
    assert fc.start_date == date(2026, 10, 27)
    for label in ("Payroll", "VAT payment"):
        assert _estimate_dates(fc, label)[:2] == [date(2026, 10, 30), date(2026, 11, 25)]
    assert _estimate_dates(fc, "Rent")[0] == date(2026, 11, 1)  # October rent is booked
    s.rollback()


def test_explain_opening_balance_has_sources(s):
    ex = explain_figure(s, 1, "opening_cents")
    summ = ex["transactions_summary"]
    assert summ["count"] == len(summ["transaction_ids"]) > 0
    assert ex["account"]["opening_balance_cents"] + summ["sum_cents"] == ex["value_cents"]


def test_ap_remaining_balance_includes_partial_payment_refs(s):
    from sqlmodel import select
    inv = next(i for i in s.exec(select(Invoice)) if i.direction == "AP" and i.status == "open"
               and not s.exec(select(Payment).where(Payment.invoice_id == i.id)).all())
    existing = s.exec(select(Payment)).first()
    for ident, amount, status in [("PART-EXEC", 100, "executed"), ("PART-SCHED", 200, "scheduled"),
                                  ("PART-FAIL", 400, "failed"), ("PART-CANCEL", 500, "cancelled")]:
        s.add(Payment(id=ident, idempotency_key=ident, payee_id=existing.payee_id,
                      amount_cents=amount, currency="EUR", execute_date=inv.due_date,
                      invoice_id=inv.id, status=status))
    s.flush()
    lines = [ln for w in build_forecast(s).weeks for ln in w.lines if inv.id in ln.source_refs]
    residual = next(ln for ln in lines if ln.label.startswith("Payable"))
    assert residual.amount_cents == -(inv.amount_cents - 300)
    assert set(residual.source_refs) == {inv.id, "PART-EXEC", "PART-SCHED"}
    assert sum(ln.amount_cents for ln in lines) == -(inv.amount_cents - 100)
    s.rollback()
