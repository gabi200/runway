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
        session.add(Payee(id="PAYEE-TEST", name="Supplier", iban="DE89 3704 0044 0532 0130 00",
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
            executor.create_payee("New", "GB29 NWBK 6016 1331 9268 19", None, True)
        assert error.value.status_code == 403
    with BankClient(token="human-dev-token") as human:
        row = human.create_payee("New", "GB29 NWBK 6016 1331 9268 19", "SUP-TEST", True)
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


def test_tokens_default_to_random_private_files(tmp_path, monkeypatch):
    import os
    from bank.client import bank_token

    monkeypatch.delenv("RUNWAY_BANK_EXECUTOR_TOKEN")
    monkeypatch.delenv("RUNWAY_BANK_HUMAN_TOKEN")
    monkeypatch.setenv("RUNWAY_VAR_DIR", str(tmp_path / "var"))
    executor, human = bank_token("executor"), bank_token("human")
    assert len(executor) > 30 and executor != human and "dev-token" not in executor + human
    assert bank_token("executor") == executor  # stable: other processes on this var/ folder read the same file
    if os.name != "nt":
        assert (tmp_path / "var" / "secrets" / "bank-executor.token").stat().st_mode & 0o777 == 0o600
    with BankClient() as client:
        assert client.get_accounts()
    for public in ("executor-dev-token", "human-dev-token", ""):
        assert TestClient(app).get("/accounts", headers={"Authorization": f"Bearer {public}"}).status_code == 401


def test_idempotency_survives_rescheduling_and_execution():
    with BankClient(token="human-dev-token") as client:
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
        assert changed.status_code == 422
        assert client.patch(path, json={"execute_date": "2026-10-22"}).status_code == 200
        assert client.post("/simulate/advance?days=3", headers={"Authorization": "Bearer human-dev-token"}).json()["executed_count"] == 1
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
    with BankClient(token="human-dev-token") as client:
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
    with BankClient(token="human-dev-token") as client:
        client.create_payment("tax", "PAYEE-TEST", 100, "EUR", TODAY)
        cancelled = client.create_payment("cancelled", "PAYEE-TEST", 200, "EUR", TODAY)
    with session_scope() as session:
        payment = session.get(Payment, cancelled["id"])
        payment.status = "cancelled"
        session.add(payment)
        session.commit()
    def advance(_):
        with BankClient(token="human-dev-token") as client:
            return client.advance(0)
    with ThreadPoolExecutor(max_workers=2) as pool:
        summaries = list(pool.map(advance, range(2)))
    assert sum(row["executed_count"] for row in summaries) == 1
    with BankClient(token="human-dev-token") as client:
        assert client.list_transactions("ACC-MAIN")[0]["category"] == "other"
        assert len(client.list_payments("cancelled")) == 1


def _mandate(payee_id, approval_id="APR-T1", roles=("owner", "accountant")):
    """What the executor sends after owner and accountant approved verify_payee."""
    from agent import mandate
    body = {"type": mandate.MANDATE_TYPE, "company": mandate.COMPANY, "approval_id": approval_id,
            "tool": "verify_payee", "args": {"payee_id": payee_id}, "currency": "EUR", "required_roles": list(roles)}
    return mandate.document(body, [mandate.sign(body, role, f"{role}@test") for role in roles])


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
        with pytest.raises(BankError) as error:
            human.verify_payee("PAYEE-TEST")  # the human token alone cannot lift the block
        assert error.value.status_code == 403
        assert human.verify_payee("PAYEE-TEST", True, _mandate("PAYEE-TEST")) == before
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


@pytest.mark.parametrize("amount", [100000001, 2**62, 10**30])
def test_huge_payment_rejected(amount):
    with TestClient(app, headers=AUTH) as client:
        assert client.post("/payments", json=payload(amount_cents=amount)).status_code == 422


def test_invoice_cumulative_limit():
    with TestClient(app, headers=AUTH) as client:
        assert client.post("/payments", json=payload(amount_cents=1000)).status_code == 200
        assert client.post("/payments", json=payload(idempotency_key="second", amount_cents=201)).status_code == 422
        assert client.post("/payments", json=payload(idempotency_key="third", amount_cents=200)).status_code == 200


def test_executor_cannot_advance():
    with TestClient(app, headers=AUTH) as client:
        assert client.post("/simulate/advance?days=0").status_code == 403


def test_patch_cannot_pull_forward_or_exceed_invoice_due():
    with TestClient(app, headers=AUTH) as client:
        row = client.post("/payments", json=payload()).json()
        for day in (TODAY, TODAY + timedelta(days=11)):
            assert client.patch(f"/payments/{row['id']}", json={"execute_date": day.isoformat()}).status_code == 422


def test_unlinked_payment_is_other_and_identifiable():
    with BankClient(token="human-dev-token") as client:
        client.create_payment("unlinked", "PAYEE-TEST", 100, "EUR", TODAY)
        client.advance(0)
        tx = client.list_transactions("ACC-MAIN")[0]
        assert tx["category"] == "other"
        assert client.list_payments()[0]["transaction_id"] == tx["id"]


@pytest.mark.parametrize("amount", [100000001, 2**62, 10**30])
def test_service_and_model_enforce_amount_bound(amount):
    from bank import service
    from bank.app import PaymentCreate
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        PaymentCreate(**payload(amount_cents=amount))
    with session_scope() as session:
        body = payload(amount_cents=amount)
        body["execute_date"] = TODAY + timedelta(days=2)
        with pytest.raises(service.BankServiceError) as error:
            service.create_payment(session, body)
        assert error.value.status_code == 422


def test_service_invoice_limit_cancellation_and_paid_status():
    from bank import service

    def create(key, amount):
        with session_scope() as session:
            body = payload(idempotency_key=key, amount_cents=amount)
            body["execute_date"] = TODAY + timedelta(days=2)
            return service.create_payment(session, body)

    first = create("first", 1000)
    with pytest.raises(service.BankServiceError) as error:
        create("too-much", 201)
    assert error.value.status_code == 422
    with session_scope() as session:
        row = session.get(Payment, first["id"])
        row.status = "cancelled"
        session.add(row)
        session.commit()
    create("replacement", 1200)
    with session_scope() as session:
        invoice = session.get(Invoice, "AP-TEST")
        invoice.status = "paid"
        session.add(invoice)
        session.commit()
    with pytest.raises(service.BankServiceError) as error:
        create("paid", 1)
    assert error.value.status_code == 422


def test_service_reschedule_bounds_and_unlinked_booking():
    from bank import service

    with session_scope() as session:
        body = payload()
        body["execute_date"] = TODAY + timedelta(days=2)
        linked = service.create_payment(session, body)
    for day in (TODAY, TODAY + timedelta(days=11)):
        with session_scope() as session:
            with pytest.raises(service.BankServiceError) as error:
                service.reschedule_payment(session, linked["id"], day)
            assert error.value.status_code == 422
    with session_scope() as session:
        party = session.get(Counterparty, "SUP-TEST")
        party.kind = "landlord"
        session.add(party)
        session.commit()
    with session_scope() as session:
        body = payload(idempotency_key="unlinked", invoice_id=None)
        body["execute_date"] = TODAY
        unlinked = service.create_payment(session, body)
    with session_scope() as session:
        service.advance(session, 0)
    with session_scope() as session:
        payment = session.get(Payment, unlinked["id"])
        assert session.get(Transaction, payment.transaction_id).category == "other"


@pytest.mark.parametrize("scope", ["read", "payments", "payees", "simulate"])
def test_scope_dependency(scope):
    from bank.app import authorize
    from fastapi import HTTPException

    dependency = authorize(scope)
    assert dependency("Bearer human-dev-token") == "owner"
    if scope in {"read", "payments"}:
        assert dependency("Bearer executor-dev-token") == "agent"
    else:
        with pytest.raises(HTTPException) as error:
            dependency("Bearer executor-dev-token")
        assert error.value.status_code == 403


def test_service_maximum_payment_boundary_and_replay():
    from bank import service

    body = payload(amount_cents=100000000, invoice_id=None)
    body["execute_date"] = TODAY
    with session_scope() as session:
        first = service.create_payment(session, body)
    with session_scope() as session:
        replay = service.create_payment(session, body)
    assert replay["id"] == first["id"] and replay["idempotent_replay"]


def test_partial_execution_keeps_invoice_open_until_fully_paid():
    from bank.service import advance
    with session_scope() as s:
        for ident, amount in [("PART1", 100), ("PART2", 1100)]:
            s.add(Payment(id=ident, idempotency_key=ident, payee_id="PAYEE-TEST",
                          amount_cents=amount, currency="EUR", execute_date=TODAY,
                          invoice_id="AP-TEST", status="scheduled" if ident == "PART1" else "cancelled"))
        s.commit()
        assert advance(s, 0)["executed_count"] == 1
        assert s.get(Invoice, "AP-TEST").status == "open"
        p = s.get(Payment, "PART2")
        p.status = "scheduled"
        s.add(p)
        s.commit()
        advance(s, 0)
        assert s.get(Invoice, "AP-TEST").status == "paid"


@pytest.mark.parametrize("payee,amount", [("PAYEE-UNVERIFIED", 100), ("PAYEE-TEST", -1),
                                          ("PAYEE-TEST", 100000001)])
def test_advance_refuses_unsafe_stored_payment(payee, amount):
    from bank.service import advance
    from common.db import get_setting
    with session_scope() as s:
        s.add(Payment(id="UNSAFE", idempotency_key="unsafe", payee_id=payee,
                      amount_cents=amount, currency="EUR", execute_date=TODAY))
        s.commit()
        result = advance(s, 0)
        assert result["executed_count"] == result["total_executed_cents"] == 0
        assert s.get(Payment, "UNSAFE").status == "failed"
        assert get_setting(s, "bank.failure.UNSAFE")
        assert not s.exec(select(Transaction)).all()


def test_failed_invoice_payment_can_be_replaced():
    from bank.service import create_payment
    with session_scope() as s:
        s.add(Payment(id="FAILED", idempotency_key="failed", payee_id="PAYEE-TEST",
                      amount_cents=1200, currency="EUR", execute_date=TODAY,
                      invoice_id="AP-TEST", status="failed"))
        s.commit()
        body = payload(execute_date=TODAY)
        assert create_payment(s, body)["status"] == "scheduled"


def test_verify_requires_valid_iban():
    """A payee without a structurally valid IBAN (e.g. created by CSV import) cannot be verified."""
    from bank.service import BankServiceError, iban_is_valid, verify_payee
    from common.db import Payee, session_scope

    assert iban_is_valid("DE89 3704 0044 0532 0130 00")
    assert not iban_is_valid("")
    assert not iban_is_valid("DE00 3704 0044 0532 0130 00")
    with session_scope() as s:
        s.add(Payee(id="PAYEE-NOIBAN", name="Imported Co", iban="", verified=False))
        s.commit()
        try:
            verify_payee(s, "PAYEE-NOIBAN", True)
        except BankServiceError as exc:
            assert exc.status_code == 422
        else:
            raise AssertionError("verification without IBAN must fail")


def test_advance_refuses_payment_whose_document_was_flagged_later():
    from bank.service import advance
    from common.db import Document
    with session_scope() as s:
        s.add(Document(id="DOC-LATE", filename="inv.pdf", kind="invoice"))
        s.add(Payment(id="LINKED", idempotency_key="linked", payee_id="PAYEE-TEST",
                      amount_cents=100, currency="EUR", execute_date=TODAY, document_id="DOC-LATE"))
        s.commit()
        doc = s.get(Document, "DOC-LATE")
        doc.flagged = True
        s.add(doc)
        s.commit()
        assert advance(s, 0)["executed_count"] == 0
        assert s.get(Payment, "LINKED").status == "failed"
        doc.cleared = True
        s.add(doc)
        p = s.get(Payment, "LINKED")
        p.status = "scheduled"
        s.add(p)
        s.commit()
        assert advance(s, 0)["executed_payment_ids"] == ["LINKED"]


def test_missing_iban_can_be_set_once_by_a_human():
    with session_scope() as s:
        s.add(Payee(id="PAYEE-IMPORTED", name="Supplier", iban="", counterparty_id="SUP-TEST",
                    created_at=datetime.now(timezone.utc)))
        s.commit()
    client = TestClient(app)
    human = {"Authorization": "Bearer human-dev-token"}
    url = "/payees/PAYEE-IMPORTED/iban"
    assert client.put(url, json={"iban": "DE89 3704 0044 0532 0130 00"}, headers=AUTH).status_code == 403
    assert client.put(url, json={"iban": "DE00 1234"}, headers=human).status_code == 422
    row = client.put(url, json={"iban": "DE89 3704 0044 0532 0130 00"}, headers=human).json()
    assert row["iban"] == "DE89 3704 0044 0532 0130 00" and row["verified"] is False
    assert client.put(url, json={"iban": "GB82 WEST 1234 5698 7654 32"}, headers=human).status_code == 409
    body = {"verified": True, "mandate": _mandate("PAYEE-IMPORTED")}
    assert client.patch("/payees/PAYEE-IMPORTED", json=body, headers=human).json()["verified"]


def _forged(kind):
    if kind == "one role":
        return _mandate("PAYEE-TEST", roles=("owner",))
    if kind == "other payee":
        return _mandate("PAYEE-OTHER")
    m = _mandate("PAYEE-TEST")
    if kind == "edited after signing":
        m["mandate"]["args"]["payee_id"] = "PAYEE-TEST "
        return m
    m["signatures"] = [m["signatures"][0], {**m["signatures"][0], "role": "accountant"}]  # one key for both roles
    return m


@pytest.mark.parametrize("kind", ["one role", "other payee", "edited after signing", "one key for both"])
def test_bank_refuses_a_mandate_that_does_not_prove_two_people(kind):
    with BankClient(token="human-dev-token") as human:
        human.verify_payee("PAYEE-TEST", False)
        with pytest.raises(BankError) as error:
            human.verify_payee("PAYEE-TEST", True, _forged(kind))
        assert error.value.status_code == 403
        assert not next(p for p in human.list_payees() if p["id"] == "PAYEE-TEST")["verified"]


def test_a_mandate_verifies_only_once():
    with BankClient(token="human-dev-token") as human:
        human.verify_payee("PAYEE-TEST", False)
        human.verify_payee("PAYEE-TEST", True, _mandate("PAYEE-TEST"))
        human.verify_payee("PAYEE-TEST", False)
        with pytest.raises(BankError) as error:  # replaying the old approval does not re-verify
            human.verify_payee("PAYEE-TEST", True, _mandate("PAYEE-TEST"))
        assert error.value.status_code == 403 and "already used" in error.value.detail
