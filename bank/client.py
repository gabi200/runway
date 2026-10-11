"""HTTP and in-process client for authorized bank callers."""
from __future__ import annotations

import json
import os
from datetime import date
from typing import Any

import httpx
from fastapi.testclient import TestClient


class BankError(Exception):
    """A failed bank request with its HTTP status and detail."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class BankClient:
    """Use the local bank by default, or a remote HTTP bank."""

    def __init__(self, base_url: str | None = None, token: str | None = None):
        headers = {"Authorization": f"Bearer {token if token is not None else os.getenv('RUNWAY_BANK_EXECUTOR_TOKEN', 'executor-dev-token')}"}
        if base_url is None:
            from bank.app import app
            self._client = TestClient(app, headers=headers)
        else:
            self._client = httpx.Client(base_url=base_url.rstrip("/"), headers=headers)

    def close(self) -> None:
        """Release transport resources."""
        self._client.close()

    def __enter__(self) -> BankClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            raise BankError(503, str(exc)) from exc
        if response.is_error:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise BankError(response.status_code, detail if isinstance(detail, str) else json.dumps(detail))
        return response.json()

    def get_accounts(self) -> list[dict]:
        return self._request("GET", "/accounts")

    def list_transactions(self, account_id: str, from_: date | None = None,
                          to: date | None = None) -> list[dict]:
        params = {}
        if from_ is not None:
            params["from"] = from_.isoformat()
        if to is not None:
            params["to"] = to.isoformat()
        return self._request("GET", f"/accounts/{account_id}/transactions", params=params)

    def list_payees(self) -> list[dict]:
        return self._request("GET", "/payees")

    def create_payee(self, name: str, iban: str, counterparty_id: str | None,
                     verified: bool) -> dict:
        return self._request("POST", "/payees", json=dict(name=name, iban=iban,
            counterparty_id=counterparty_id, verified=verified))

    def create_payment(self, idempotency_key: str, payee_id: str, amount_cents: int,
                       currency: str, execute_date: date, invoice_id: str | None = None,
                       document_id: str | None = None, automatic: bool = False) -> dict:
        return self._request("POST", "/payments", json=dict(idempotency_key=idempotency_key,
            payee_id=payee_id, amount_cents=amount_cents, currency=currency,
            execute_date=execute_date.isoformat(), invoice_id=invoice_id,
            document_id=document_id, automatic=automatic))

    def list_payments(self, status: str | None = None) -> list[dict]:
        return self._request("GET", "/payments", params={"status": status} if status else {})

    def reschedule_payment(self, payment_id: str, execute_date: date) -> dict:
        return self._request("PATCH", f"/payments/{payment_id}",
                             json={"execute_date": execute_date.isoformat()})

    def advance(self, days: int) -> dict:
        return self._request("POST", "/simulate/advance", params={"days": days})
