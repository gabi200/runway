"""HTTP and in-process client for authorized bank callers."""
from __future__ import annotations

import json
import os
from datetime import date
from typing import Any

import secrets
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from common.db import REPO_ROOT


def bank_token(kind: str) -> str:
    """Bearer token for "executor" or "human": RUNWAY_BANK_<KIND>_TOKEN, else a random one kept in var/secrets/.

    The random token is made on first use (mode 0600) and shared by every process that uses the same
    var/ folder, so no token is ever public. Set the variables when the bank runs on another machine.
    """
    value = os.environ.get(f"RUNWAY_BANK_{kind.upper()}_TOKEN", "").strip()
    if value:
        return value
    path = Path(os.environ.get("RUNWAY_VAR_DIR", REPO_ROOT / "var")) / "secrets" / f"bank-{kind}.token"
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as stream:
            stream.write(secrets.token_urlsafe(32))
        try:
            os.link(tmp, path)  # atomic: the first process wins and nobody reads a half-written file
        except FileExistsError:
            pass
        finally:
            tmp.unlink()
    return path.read_text(encoding="utf-8").strip()


class BankError(Exception):
    """A failed bank request with its HTTP status and detail."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class BankClient:
    """Use the local bank by default, or a remote HTTP bank."""

    def __init__(self, base_url: str | None = None, token: str | None = None):
        headers = {"Authorization": f"Bearer {token if token is not None else bank_token('executor')}"}
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

    def verify_payee(self, payee_id: str, verified: bool = True) -> dict:
        """Set verification status using a human bank token."""
        return self._request("PATCH", f"/payees/{payee_id}", json={"verified": verified})

    def set_payee_iban(self, payee_id: str, iban: str) -> dict:
        """Fill in a missing IBAN using a human bank token."""
        return self._request("PUT", f"/payees/{payee_id}/iban", json={"iban": iban})

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
