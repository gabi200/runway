"""Round-trip the generator's CSVs through data/importer.py."""

from __future__ import annotations

import csv
import os
import sqlite3
from contextlib import closing

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


def test_scheduled_import_rejects_unverified_payees(gen):
    base, out = gen
    before = closings(base / "src.db")
    with pytest.raises(ImportErrorReport, match="unverified"):
        import_files(base / "src.db", out / "trial_balance.csv", out / "bank_statement.csv",
                     out / "open_receivables.csv", out / "open_payables.csv",
                     paid_invoices_csv=base / "paid.csv", scheduled_payments_csv=base / "sched.csv")
    assert closings(base / "src.db") == before


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


TB = "account_code,account_name,debit,credit\n1000,Cash,50000,\n3000,Equity,,50000\n"
BS = "date,description,amount,category\n2026-09-01,Payment from Acme Ltd INV1,1000,sales_receipt\n"
AR = "number,counterparty,issue_date,due_date,amount,invoice_id\nA1,Acme Ltd,2026-09-01,2026-10-01,100,AR-1002\nA2,Acme Ltd,2026-09-01,2026-10-01,200,\n"


@pytest.mark.parametrize("kwargs,match", [
    ({"open_receivables_csv": AR}, "duplicate"),
    ({"open_receivables_csv": AR.replace(",AR-1002", ","),
      "scheduled_payments_csv": "counterparty,amount,execute_date\nAcme Ltd,500,2026-10-20\n"}, "supplier"),
    ({"scheduled_payments_csv": "counterparty,amount,execute_date\nEvil Corp,-5,2026-10-20\n"}, "amount"),
    ({"scheduled_payments_csv": "counterparty,amount,execute_date\nEvil Corp,99999999999,2026-10-20\n"}, "amount"),
    ({"scheduled_payments_csv": "counterparty,amount,execute_date\nEvil Corp,5,2099-99-99\n"}, "ISO date"),
])
def test_invalid_import_preserves_ledger_and_audit(tmp_path, kwargs, match):
    from audit.log import append, verify_chain
    target = tmp_path / "old.db"
    generate(target, out_dir=tmp_path / "out")
    with db.session_scope(target) as s:
        append(s, "owner", "get_balances", {}, "ALLOW")
    before = target.read_bytes()
    with pytest.raises(ImportErrorReport, match=match):
        import_files(target, TB, BS, **kwargs)
    assert target.read_bytes() == before
    with db.session_scope(target) as s:
        assert verify_chain(s).ok
        assert s.exec(select(db.AuditEntry)).first() is not None


@pytest.mark.parametrize("use_session", [False, True])
def test_write_failure_preserves_existing_db(tmp_path, monkeypatch, use_session):
    from audit.log import append, verify_chain
    target = tmp_path / "old.db"
    generate(target, out_dir=tmp_path / "out")
    with db.session_scope(target) as s:
        append(s, "owner", "get_balances", {}, "ALLOW")
    before = target.read_bytes()
    original = Session.commit
    def fail_import(s):
        if ".runway-import-" in str(s.get_bind().url):
            raise RuntimeError("injected write failure")
        original(s)
    monkeypatch.setattr(Session, "commit", fail_import)
    with db.session_scope(target) as s:
        with pytest.raises(ImportErrorReport, match="existing database preserved"):
            import_files(s if use_session else target, TB, BS)
        assert target.read_bytes() == before
        assert verify_chain(s).ok
        assert s.exec(select(db.AuditEntry)).first() is not None
    assert not list(tmp_path.glob(".runway-import-*"))


@pytest.mark.parametrize("use_session", [False, True])
def test_successful_atomic_import_reopens_database(tmp_path, use_session):
    target = tmp_path / "old.db"
    generate(target, out_dir=tmp_path / "out")
    with db.session_scope(target) as s:
        result = import_files(s if use_session else target, TB, BS)
        assert result["today"] == "2026-09-02"
        assert db.get_today(s).isoformat() == result["today"]
        assert s.get(db.Account, "ACC-MAIN").name == "Imported company main account"
    with db.session_scope(target) as s:
        assert db.account_balance_cents(s) == 5000000
    assert not list(tmp_path.glob(".runway-import-*"))


def test_import_keeps_the_live_file_for_other_connections(tmp_path):
    # A separately running bank server keeps its connection open; it must see the import,
    # and the file keeps its inode and permissions.
    target = tmp_path / "old.db"
    generate(target, out_dir=tmp_path / "out")
    db.reset_engines()
    before = os.stat(target)
    with closing(sqlite3.connect(target)) as other:
        assert other.execute("SELECT name FROM account").fetchall() != [("Imported company main account",)]
        import_files(target, TB, BS)
        assert other.execute("SELECT name FROM account").fetchall() == [("Imported company main account",)]
    after = os.stat(target)
    assert (after.st_ino, after.st_mode) == (before.st_ino, before.st_mode)
    assert not list(tmp_path.glob(".runway-import-*"))


def test_import_into_locked_database_fails_cleanly(tmp_path, monkeypatch):
    from data import importer
    monkeypatch.setattr(importer, "LOCK_WAIT_SECONDS", 0.1)
    target = tmp_path / "old.db"
    generate(target, out_dir=tmp_path / "out")
    db.reset_engines()
    before = target.read_bytes()
    with closing(sqlite3.connect(target, isolation_level=None)) as writer:
        writer.execute("BEGIN IMMEDIATE")  # another process is in the middle of a write
        with pytest.raises(ImportErrorReport, match="existing database preserved.*locked"):
            import_files(target, TB, BS)
        writer.execute("ROLLBACK")
    assert target.read_bytes() == before
    assert not list(tmp_path.glob(".runway-import-*"))


def test_bank_category_ignores_case_and_rejects_unknown_values(tmp_path):
    bs = "date,description,amount,category\n2026-09-01,Salaries,-1000,Payroll\n"
    import_files(tmp_path / "a.db", TB, bs)
    with db.session_scope(tmp_path / "a.db") as s:
        assert [t.category for t in s.exec(select(db.Transaction))] == ["payroll"]
    with pytest.raises(ImportErrorReport, match=r"bank statement TX-000001: unknown category 'Salaries'.*payroll"):
        import_files(tmp_path / "b.db", TB, bs.replace("Payroll", "Salaries"))
    assert not (tmp_path / "b.db").exists()


TB_NO_CASH = "account_code,account_name,debit,credit\n1100,Receivables,50000,\n3000,Equity,,50000\n"
NEWEST_FIRST = "date,description,amount,balance\n2026-09-03,Rent,-200,1300\n2026-09-01,Receipt,500,1500\n"


@pytest.mark.parametrize("bs", [
    NEWEST_FIRST,
    # same day, oldest first; "B10" sorts before "B9" as text
    "id,date,description,amount,balance\nB9,2026-09-01,Receipt,500,1500\nB10,2026-09-01,Rent,-200,1300\n",
])
def test_opening_balance_follows_statement_order(tmp_path, bs):
    result = import_files(tmp_path / "t.db", TB_NO_CASH, bs)
    assert result["opening_balance_cents"] == 100000
    with db.session_scope(tmp_path / "t.db") as s:
        assert db.account_balance_cents(s, as_of=db.get_today(s)) == 130000


def test_statement_end_is_compared_with_trial_balance_in_date_order(tmp_path):
    tb = "account_code,account_name,debit,credit\n1000,Cash,1300,\n3000,Equity,,1300\n"
    assert import_files(tmp_path / "t.db", tb, NEWEST_FIRST)["warnings"] == []
    assert "differs" in import_files(tmp_path / "u.db", tb.replace("1300", "1400"), NEWEST_FIRST)["warnings"][0]


def test_cash_account_is_not_any_account_with_cash_in_its_name(tmp_path):
    tb = ("account_code,account_name,debit,credit\n4900,Cash discounts granted,500,\n"
          "1010,Bank current account,50000,\n3000,Equity,,50500\n")
    result = import_files(tmp_path / "t.db", tb, "date,description,amount,balance\n2026-09-01,Receipt,100,50000\n")
    assert (result["opening_balance_cents"], result["warnings"]) == (4990000, [])
    with pytest.raises(ImportErrorReport, match="1020 Cash at bank, 1010 Bank current account.*code 1000"):
        import_files(tmp_path / "u.db", tb.replace("4900,Cash discounts granted", "1020,Cash at bank"), BS)


@pytest.mark.parametrize("amount", ["50000,00", "12,50", "1,2345.00", "1,000,0"])
def test_decimal_comma_is_rejected_not_read_as_thousands(tmp_path, amount):
    with pytest.raises(ImportErrorReport, match="is not a number"):
        import_files(tmp_path / "t.db", TB, f'date,description,amount\n2026-09-01,x,"{amount}"\n')


def test_thousands_separators_are_accepted(tmp_path):
    result = import_files(tmp_path / "t.db", TB, 'date,description,amount\n2026-09-01,x,"-1,000.50"\n')
    assert result["opening_balance_cents"] == 5000000 + 100050


@pytest.mark.parametrize("bs,match", [
    ("date,description,amount\n2026-09-01,Café,1\n".encode("latin-1"), "bank statement: not UTF-8"),
    ("date,description,amount\n2026-09-01,x,1,extra\n", "bank statement row 1: more fields than"),
    ("date,description,amount\n2026-09-01," + "x" * 200_000 + ",1\n", "bank statement line 2: field larger"),
    ("date,description,amount\n9999-12-31,x,1\n", "bank statement TX-000001 date: '9999-12-31' is outside"),
], ids=["latin-1", "extra-field", "huge-field", "year-9999"])
def test_malformed_bank_statement_is_reported_not_raised_raw(tmp_path, bs, match):
    with pytest.raises(ImportErrorReport, match=match):
        import_files(tmp_path / "t.db", TB, bs)
    assert not (tmp_path / "t.db").exists()


@pytest.mark.parametrize("days", ["99999999999", "²", "-3", "4.0"])
def test_days_overdue_must_be_whole_days(tmp_path, days):
    ar = f"number,counterparty,issue_date,due_date,amount,days_overdue\nA1,Acme Ltd,2026-09-01,2026-10-01,100,{days}\n"
    with pytest.raises(ImportErrorReport, match="open receivables row 1 days_overdue"):
        import_files(tmp_path / "t.db", TB, BS, ar)


def test_cr_only_line_endings_are_read(tmp_path):
    result = import_files(tmp_path / "t.db", TB.replace("\n", "\r"), "date,description,amount\r2026-09-01,x,1\r")
    assert (result["transactions"], result["opening_balance_cents"]) == (1, 4999900)
