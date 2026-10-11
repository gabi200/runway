"""Tests for forecast/engine.py on the generated Lumen Packaging data."""

from __future__ import annotations

from datetime import date

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
    assert abs(fc.min_closing_cents - (-18_000_00)) <= 2_500_00
    assert all(fc.weeks[i].closing_cents >= 0 for i in range(4))  # weeks 1-4
    assert 6 in fc.shortfall_weeks


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
