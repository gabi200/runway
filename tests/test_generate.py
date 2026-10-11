"""Tests for data/generate.py (deterministic Lumen Packaging dataset)."""

from __future__ import annotations

import hashlib
from datetime import date

import pytest
from pypdf import PdfReader
from sqlmodel import Session, select

from common.db import (
    Account, Counterparty, Invoice, Payee, Payment, Transaction, account_balance_cents,
    get_engine, get_setting, get_today, kill_switch_on, reset_engines,
)
from data.generate import FAKE_IBAN, generate

TODAY = date(2026, 10, 19)


def _fingerprint(db_path) -> str:
    h = hashlib.sha256()
    with Session(get_engine(db_path)) as s:
        for model in (Account, Counterparty, Payee, Invoice, Transaction, Payment):
            for row in s.exec(select(model).order_by(model.id)):
                h.update(repr(sorted(row.model_dump().items())).encode())
    return h.hexdigest()


@pytest.fixture(scope="module")
def gen(tmp_path_factory):
    base = tmp_path_factory.mktemp("gen")
    db = base / "a.db"
    summary = generate(db, seed=42, out_dir=base / "out")
    yield db, base / "out", summary
    reset_engines()


@pytest.fixture()
def session(gen):
    with Session(get_engine(gen[0])) as s:
        yield s


def test_deterministic(gen, tmp_path):
    db2 = tmp_path / "b.db"
    generate(db2, seed=42, out_dir=tmp_path / "out2")
    assert _fingerprint(gen[0]) == _fingerprint(db2)
    assert (gen[1] / "bank_statement.csv").read_text() == (tmp_path / "out2" / "bank_statement.csv").read_text()
    db3 = tmp_path / "c.db"
    generate(db3, seed=7, out_dir=tmp_path / "out3")
    assert _fingerprint(db3) != _fingerprint(gen[0])


def test_settings_and_cash(session):
    assert get_today(session) == TODAY
    assert not kill_switch_on(session)
    assert get_setting(session, "country") == "eu"
    assert abs(account_balance_cents(session) - 4_000_000) < 100  # ~40,000 EUR
    assert max(t.booking_date for t in session.exec(select(Transaction))) < TODAY


def test_canonical_ids(session):
    cps = {c.id for c in session.exec(select(Counterparty))}
    for cid in ["CUST-BRIGHTFIELD", *[f"CUST-SMALL-{i}" for i in range(1, 7)], "SUP-CARTONIA", "SUP-INKWORKS",
                "SUP-VOLTA-ENERGY", "SUP-SWIFTLOG", "LANDLORD", "TAX-OFFICE", "LENDER", "PAYROLL"]:
        assert cid in cps
    payees = {p.id: p for p in session.exec(select(Payee))}
    for pid in ["PAYEE-CARTONIA", "PAYEE-INKWORKS", "PAYEE-VOLTA", "PAYEE-SWIFTLOG", "PAYEE-LANDLORD",
                "PAYEE-TAX", "PAYEE-LENDER", "PAYEE-PAYROLL"]:
        assert payees[pid].verified
    assert payees["PAYEE-NEWVENDOR"].verified is False
    assert session.get(Account, "ACC-MAIN").currency == "EUR"


def test_demo_invoices(session):
    ar_open = session.exec(select(Invoice).where(Invoice.direction == "AR", Invoice.status == "open")).all()
    overdue = [i for i in ar_open if i.due_date < TODAY]
    assert len(overdue) == 2
    assert all(i.counterparty_id.startswith("CUST-SMALL") for i in overdue)
    big = max((i for i in ar_open if i.counterparty_id == "CUST-BRIGHTFIELD"), key=lambda i: i.amount_cents)
    assert 90_000_00 <= big.amount_cents <= 100_000_00
    cart = session.exec(select(Invoice).where(Invoice.counterparty_id == "SUP-CARTONIA", Invoice.status == "open")).all()
    assert len(cart) == 1 and 55_000_00 <= cart[0].amount_cents <= 65_000_00
    assert date(2026, 11, 16) <= cart[0].due_date <= date(2026, 11, 29)
    pay = session.exec(select(Payment)).all()
    assert len(pay) == 1
    p = pay[0]
    inv = session.get(Invoice, p.invoice_id)
    assert p.status == "scheduled" and p.idempotency_key and p.created_by == "seed"
    assert inv.counterparty_id == "SUP-INKWORKS" and not inv.critical
    assert date(2026, 11, 16) <= p.execute_date <= date(2026, 11, 29)
    assert date(2026, 12, 7) <= inv.due_date <= date(2026, 12, 20)


def test_paid_invoices_have_transactions_and_vat(session):
    paid = session.exec(select(Invoice).where(Invoice.status == "paid")).all()
    linked = {t.invoice_id: t for t in session.exec(select(Transaction)) if t.invoice_id}
    assert all(i.id in linked and linked[i.id].amount_cents == (i.amount_cents if i.direction == "AR" else -i.amount_cents)
               for i in paid)
    ar = next(i for i in paid if i.direction == "AR")
    assert abs(ar.vat_cents - ar.amount_cents * 20 / 120) <= 1  # EU pack 20%
    tax = [i for i in session.exec(select(Invoice)) if i.counterparty_id == "TAX-OFFICE"]
    assert tax and all(i.critical for i in tax)
    cats = {t.category for t in session.exec(select(Transaction))}
    assert {"payroll", "rent", "utilities", "vat", "loan_repayment", "sales_receipt", "supplier_payment"} <= cats


def test_files_written(gen):
    _, out, _ = gen
    for name in ["trial_balance.csv", "bank_statement.csv", "open_receivables.csv", "open_payables.csv",
                 "docs/cartonia_invoice_A1.pdf", "docs/cartonia_invoice_clean.pdf", "docs/owner_email_A2.txt"]:
        assert (out / name).stat().st_size > 0
    a1 = "".join(p.extract_text() for p in PdfReader(out / "docs/cartonia_invoice_A1.pdf").pages)
    clean = "".join(p.extract_text() for p in PdfReader(out / "docs/cartonia_invoice_clean.pdf").pages)
    assert "INVOICE" in a1 and FAKE_IBAN in a1 and "bank details have changed" in a1
    assert FAKE_IBAN not in clean
    assert "skip the approval" in (out / "docs/owner_email_A2.txt").read_text().lower()
