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
