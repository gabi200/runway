"""Bank contracts against an isolated SQLite ledger."""
from datetime import date, datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from bank.app import app
from bank.client import BankClient, BankError
from common.db import (Account, Counterparty, Invoice, Payee, Payment, Transaction,
                       get_today, reset_engines, session_scope, set_setting)

TODAY = date(2026, 10, 19)
AUTH = {"Authorization": "Bearer executor-dev-token"}


@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNWAY_DB", str(tmp_path / "bank.db"))
    monkeypatch.setenv("RUNWAY_BANK_EXECUTOR_TOKEN", "executor-dev-token")
    monkeypatch.setenv("RUNWAY_BANK_HUMAN_TOKEN", "human-dev-token")
    reset_engines()
    with session_scope() as session:
        session.add(Account(id="ACC-MAIN", name="Main", currency="EUR", opening_balance_cents=10000))
        session.add(Counterparty(id="SUP-TEST", name="Supplier", kind="supplier"))
        session.commit()
        session.add(Payee(id="PAYEE-TEST", name="Supplier", iban="TEST-IBAN",
                          counterparty_id="SUP-TEST", verified=True, created_at=datetime.now(timezone.utc)))
        session.add(Payee(id="PAYEE-UNVERIFIED", name="Unknown", iban="OTHER", created_at=datetime.now(timezone.utc)))
        session.add(Invoice(id="AP-TEST", number="1", direction="AP", counterparty_id="SUP-TEST",
                            issue_date=TODAY, due_date=TODAY + timedelta(days=10),
                            amount_cents=1200, currency="EUR"))
        session.commit()
        set_setting(session, "today", TODAY.isoformat())
    yield
    reset_engines()


def payload(**changes):
    return {"idempotency_key": "request-1", "payee_id": "PAYEE-TEST",
            "amount_cents": 1200, "currency": "EUR",
            "execute_date": (TODAY + timedelta(days=2)).isoformat(),
            "invoice_id": "AP-TEST", **changes}


def test_auth_scopes():
    with TestClient(app) as client:
        for path in ("/accounts", "/payees", "/payments", "/accounts/ACC-MAIN/transactions"):
            assert client.get(path).status_code == 401
            assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
            assert client.get(path, headers=AUTH).status_code == 200
        for path in ("/payments", "/simulate/advance", "/payees"):
            assert client.post(path, json=payload()).status_code == 401
        assert client.patch("/payments/PAY-0001", json={"execute_date": TODAY.isoformat()}).status_code == 401
    with BankClient() as executor:
        with pytest.raises(BankError) as error:
            executor.create_payee("New", "NEW-IBAN", None, True)
        assert error.value.status_code == 403
    with BankClient(token="human-dev-token") as human:
        row = human.create_payee("New", "NEW-IBAN", "SUP-TEST", True)
        assert row["verified"] is True
        assert row["created_by"] == "owner"
        assert len(human.list_payees()) == 3
        assert human.create_payment("human", row["id"], 10, "EUR", TODAY)["created_by"] == "owner"
        assert human.advance(0)["executed_count"] == 1


def test_environment_tokens(monkeypatch):
    monkeypatch.setenv("RUNWAY_BANK_EXECUTOR_TOKEN", "custom-executor")
    monkeypatch.setenv("RUNWAY_BANK_HUMAN_TOKEN", "custom-human")
    with BankClient() as client:
        assert client.get_accounts()[0]["balance_cents"] == 10000
    with BankClient(token="executor-dev-token") as client:
        with pytest.raises(BankError) as error:
            client.get_accounts()
        assert error.value.status_code == 401


def test_idempotency_survives_rescheduling_and_execution():
    with BankClient() as client:
        args = ("once", "PAYEE-TEST", 1200, "EUR", TODAY + timedelta(days=2))
        first = client.create_payment(*args, invoice_id="AP-TEST", automatic=True)
        assert first["id"] == "PAY-0001"
        assert first["created_sim_date"] == TODAY.isoformat()
        assert first["idempotent_replay"] is False
        replay = client.create_payment(*args, invoice_id="AP-TEST", automatic=True)
        assert replay == {**first, "idempotent_replay": True}
        with pytest.raises(BankError) as error:
            client.create_payment("once", "PAYEE-TEST", 1201, "EUR", args[-1], invoice_id="AP-TEST", automatic=True)
        assert error.value.status_code == 409
        client.reschedule_payment(first["id"], TODAY + timedelta(days=3))
        client.advance(3)
        replay = client.create_payment(*args, invoice_id="AP-TEST", automatic=True)
        assert replay["status"] == "executed"
        assert replay["idempotent_replay"] is True
        assert len(client.list_payments()) == 1
        assert len(client.list_transactions("ACC-MAIN")) == 1


@pytest.mark.parametrize("changes", [
    {"payee_id": "PAYEE-UNVERIFIED"}, {"payee_id": "MISSING"},
    {"amount_cents": 0}, {"amount_cents": -1}, {"amount_cents": 1.5},
    {"amount_cents": True}, {"currency": "CNY"},
    {"execute_date": "2026-10-18"}, {"invoice_id": "MISSING"},
    {"document_id": "MISSING"}, {"iban": "ILLEGAL"},
])
def test_rejected_payments(changes):
    with TestClient(app, headers=AUTH) as client:
        assert client.post("/payments", json=payload(**changes)).status_code == 422
        assert client.get("/payments").json() == []


def test_rescheduling_rules():
    with TestClient(app, headers=AUTH) as client:
        payment = client.post("/payments", json=payload()).json()
        path = f"/payments/{payment['id']}"
        assert client.patch(path, json={"execute_date": "2026-10-18"}).status_code == 422
        assert client.patch(path, json={"execute_date": TODAY.isoformat(), "amount_cents": 2}).status_code == 422
        assert client.patch(path, json={}).status_code == 422
        assert client.patch("/payments/MISSING", json={"execute_date": TODAY.isoformat()}).status_code == 404
        changed = client.patch(path, json={"execute_date": TODAY.isoformat()})
        assert changed.status_code == 200
        assert changed.json()["amount_cents"] == 1200
        assert client.post("/simulate/advance?days=0").json()["executed_count"] == 1
        assert client.patch(path, json={"execute_date": "2026-10-22"}).status_code == 409
    with session_scope() as session:
        row = session.get(Payment, payment["id"])
        row.status = "cancelled"
        session.add(row)
        session.commit()
    with BankClient() as client:
        with pytest.raises(BankError) as error:
            client.reschedule_payment(payment["id"], TODAY)
        assert error.value.status_code == 409


def test_advance_books_due_payments_once():
    with BankClient() as client:
        due = client.create_payment("due", "PAYEE-TEST", 1200, "EUR", TODAY + timedelta(days=2), invoice_id="AP-TEST")
        future = client.create_payment("future", "PAYEE-TEST", 500, "EUR", TODAY + timedelta(days=10))
        assert client.advance(1)["executed_count"] == 0
        summary = client.advance(3)
        assert summary["today"] == "2026-10-23"
        assert summary["executed_payment_ids"] == [due["id"]]
        assert summary["total_executed_cents"] == 1200
        tx = client.list_transactions("ACC-MAIN", TODAY + timedelta(days=2), TODAY + timedelta(days=2))
        assert len(tx) == 1
        assert tx[0]["amount_cents"] == -1200
        assert tx[0]["category"] == "supplier_payment"
        assert tx[0]["invoice_id"] == "AP-TEST"
        assert client.get_accounts()[0]["balance_cents"] == 8800
        assert client.list_payments("scheduled")[0]["id"] == future["id"]
        assert client.list_payments("executed")[0]["transaction_id"] == tx[0]["id"]
        assert client.advance(0)["executed_count"] == 0
        assert len(client.list_transactions("ACC-MAIN")) == 1
        with pytest.raises(BankError) as error:
            client.advance(-1)
        assert error.value.status_code == 422
    with session_scope() as session:
        assert get_today(session) == date(2026, 10, 23)
        invoice = session.get(Invoice, "AP-TEST")
        assert invoice.status == "paid"
        assert invoice.paid_date == date(2026, 10, 21)


def test_concurrent_duplicate_requests():
    def create(_):
        with BankClient() as client:
            return client.create_payment("concurrent", "PAYEE-TEST", 100, "EUR", TODAY)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(create, range(4)))
    assert {row["id"] for row in results} == {"PAY-0001"}
    assert sum(not row["idempotent_replay"] for row in results) == 1
    with session_scope() as session:
        assert len(session.exec(select(Payment)).all()) == 1


def test_http_transport(monkeypatch):
    original = httpx.Client
    def client_factory(**kwargs):
        return original(**kwargs, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=[{"authorization": request.headers["Authorization"]}])))
    monkeypatch.setattr("bank.client.httpx.Client", client_factory)
    with BankClient(base_url="http://bank.example/", token="remote-token") as client:
        assert client.get_accounts() == [{"authorization": "Bearer remote-token"}]


def test_history_filters_and_errors():
    with session_scope() as session:
        for index, day in enumerate((TODAY - timedelta(days=1), TODAY, TODAY + timedelta(days=1))):
            session.add(Transaction(id=f"TX-{index:06d}", account_id="ACC-MAIN",
                booking_date=day, amount_cents=100, currency="EUR", description="Receipt", category="sales_receipt"))
        session.commit()
    with BankClient() as client:
        assert client.get_accounts()[0]["balance_cents"] == 10200
        assert len(client.list_transactions("ACC-MAIN", TODAY, TODAY)) == 1
        for account_id, start, end, code in (
            ("MISSING", None, None, 404), ("ACC-MAIN", TODAY + timedelta(days=1), TODAY, 422)):
            with pytest.raises(BankError) as error:
                client.list_transactions(account_id, start, end)
            assert error.value.status_code == code


def test_advance_matching_category_and_cancelled_payment():
    with session_scope() as session:
        party = session.get(Counterparty, "SUP-TEST")
        party.kind = "tax"
        session.add(party)
        session.commit()
    with BankClient() as client:
        client.create_payment("tax", "PAYEE-TEST", 100, "EUR", TODAY)
        cancelled = client.create_payment("cancelled", "PAYEE-TEST", 200, "EUR", TODAY)
    with session_scope() as session:
        payment = session.get(Payment, cancelled["id"])
        payment.status = "cancelled"
        session.add(payment)
        session.commit()
    def advance(_):
        with BankClient() as client:
            return client.advance(0)
    with ThreadPoolExecutor(max_workers=2) as pool:
        summaries = list(pool.map(advance, range(2)))
    assert sum(row["executed_count"] for row in summaries) == 1
    with BankClient() as client:
        assert client.list_transactions("ACC-MAIN")[0]["category"] == "vat"
        assert len(client.list_payments("cancelled")) == 1


def test_verify_payee_requires_human_and_updates_in_place():
    with TestClient(app) as client:
        assert client.patch("/payees/PAYEE-TEST", json={"verified": False}).status_code == 401
    with BankClient() as executor:
        with pytest.raises(BankError) as error:
            executor.verify_payee("PAYEE-TEST", False)
        assert error.value.status_code == 403
    with BankClient(token="human-dev-token") as human:
        before = next(row for row in human.list_payees() if row["id"] == "PAYEE-TEST")
        assert human.verify_payee("PAYEE-TEST", False) == {**before, "verified": False}
        with pytest.raises(BankError) as error:
            human.create_payment("unverified", "PAYEE-TEST", 100, "EUR", TODAY)
        assert error.value.status_code == 422
        assert human.verify_payee("PAYEE-TEST") == before
        assert len(human.list_payees()) == 2
        assert human.create_payment("verified", "PAYEE-TEST", 100, "EUR", TODAY)["status"] == "scheduled"
        with pytest.raises(BankError) as error:
            human.verify_payee("MISSING")
        assert error.value.status_code == 404


@pytest.mark.parametrize("body", [{}, {"verified": True, "iban": "NEW"},
                                  {"verified": True, "name": "Changed"},
                                  {"verified": True, "counterparty_id": None},
                                  {"verified": "true"}, {"verified": 1}, {"verified": None}])
def test_verify_payee_only_accepts_verified_boolean(body):
    with TestClient(app, headers={"Authorization": "Bearer human-dev-token"}) as client:
        before = client.get("/payees").json()
        assert client.patch("/payees/PAYEE-TEST", json=body).status_code == 422
        assert client.get("/payees").json() == before
