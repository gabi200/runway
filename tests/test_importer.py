"""Round-trip the generator's CSVs through data/importer.py."""

from __future__ import annotations

import csv

import pytest
from sqlmodel import Session, select

from common import db
from data.generate import generate
from data.importer import ImportErrorReport, import_files
from forecast.engine import build_forecast


@pytest.fixture(scope="module")
def gen(tmp_path_factory):
    base = tmp_path_factory.mktemp("imp")
    generate(base / "src.db", out_dir=base / "out")
    out = base / "out"
    with Session(db.get_engine(base / "src.db")) as s:
        names = {c.id: c.name for c in s.exec(select(db.Counterparty))}
        payees = {p.id: p.name for p in s.exec(select(db.Payee))}
        invs = {i.id: i for i in s.exec(select(db.Invoice))}
        with open(base / "paid.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["number", "counterparty", "due_date", "paid_date", "amount"])
            for i in invs.values():
                if i.status == "paid" and i.direction == "AR":
                    w.writerow([i.number, names[i.counterparty_id], i.due_date, i.paid_date, i.amount_cents / 100])
        with open(base / "sched.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["counterparty", "amount", "execute_date", "invoice_number"])
            for p in s.exec(select(db.Payment)):
                w.writerow([payees[p.payee_id], p.amount_cents / 100, p.execute_date, invs[p.invoice_id].number])
    yield base, out
    db.reset_engines()


def closings(path):
    with Session(db.get_engine(path)) as s:
        return [w.closing_cents for w in build_forecast(s).weeks]


def test_full_roundtrip_gives_same_forecast(gen):
    base, out = gen
    summary = import_files(base / "full.db", out / "trial_balance.csv", out / "bank_statement.csv",
                           out / "open_receivables.csv", out / "open_payables.csv",
                           paid_invoices_csv=base / "paid.csv", scheduled_payments_csv=base / "sched.csv")
    assert summary["warnings"] == [] and summary["today"] == "2026-10-19"
    assert closings(base / "full.db") == closings(base / "src.db")
    assert closings(base / "full.db")[5] < 0  # the week-6 shortfall survives the round trip


def test_basic_import_builds_forecast_and_payees_unverified(gen):
    base, out = gen
    import_files(base / "basic.db", out / "trial_balance.csv", out / "bank_statement.csv",
                 out / "open_receivables.csv", out / "open_payables.csv")
    assert len(closings(base / "basic.db")) == 13
    with Session(db.get_engine(base / "basic.db")) as s:
        payees = s.exec(select(db.Payee)).all()
        assert payees and not any(p.verified for p in payees)
        assert db.account_balance_cents(s, as_of=db.get_today(s)) == 4_000_000


def test_unbalanced_trial_balance_rejected_and_db_untouched(gen, tmp_path):
    base, out = gen
    bad = (out / "trial_balance.csv").read_text().replace("1000,Cash at bank,40000.00", "1000,Cash at bank,40001.00")
    target = tmp_path / "t.db"
    with pytest.raises(ImportErrorReport, match="not balanced"):
        import_files(target, bad, out / "bank_statement.csv")
    assert not target.exists() or not _has_rows(target)


def _has_rows(path):
    with Session(db.get_engine(path)) as s:
        try:
            return s.exec(select(db.Account)).first() is not None
        except Exception:
            return False


def test_missing_columns_rejected(gen):
    base, out = gen
    with pytest.raises(ImportErrorReport, match="missing required column"):
        import_files(base / "x.db", "account_code,debit,credit\n1000,1.00,\n", out / "bank_statement.csv")
    with pytest.raises(ImportErrorReport, match="missing required column"):
        import_files(base / "x.db", out / "trial_balance.csv", "date,amount\n2026-01-01,1\n")
