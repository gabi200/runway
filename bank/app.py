"""Authenticated FastAPI interface for the mock bank."""
from __future__ import annotations

import os
import secrets
from datetime import date
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session

from bank import service
from common.db import iter_session

app = FastAPI(title="Runway mock bank")
DB = Annotated[Session, Depends(iter_session)]


def authorize(scope: str):
    """Require a recognized bearer token with the requested scope."""
    def dependency(authorization: Annotated[str | None, Header()] = None) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Bearer token required", headers={"WWW-Authenticate": "Bearer"})
        token = authorization[7:]
        human = os.getenv("RUNWAY_BANK_HUMAN_TOKEN", "human-dev-token")
        executor = os.getenv("RUNWAY_BANK_EXECUTOR_TOKEN", "executor-dev-token")
        # Ambiguous configuration must not grant executor credentials human scope.
        if secrets.compare_digest(token, executor):
            actor = "agent"
        elif secrets.compare_digest(token, human):
            actor = "owner"
        else:
            raise HTTPException(401, "Invalid token", headers={"WWW-Authenticate": "Bearer"})
        if scope == "payees" and actor != "owner":
            raise HTTPException(403, "Human token required")
        return actor
    return dependency


@app.exception_handler(service.BankServiceError)
async def bank_error(request: Request, exc: service.BankServiceError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


class PayeeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    iban: str = Field(min_length=1)
    counterparty_id: str | None = None
    verified: bool = False


class PaymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1)
    payee_id: str
    amount_cents: int = Field(gt=0, strict=True)
    currency: str
    execute_date: date
    invoice_id: str | None = None
    document_id: str | None = None
    automatic: bool = False


class PaymentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    execute_date: date


@app.get("/accounts", dependencies=[Depends(authorize("read"))])
def accounts(session: DB) -> list[dict]:
    return service.get_accounts(session)


@app.get("/accounts/{account_id}/transactions", dependencies=[Depends(authorize("read"))])
def transactions(account_id: str, session: DB,
                 from_: Annotated[date | None, Query(alias="from")] = None,
                 to: date | None = None) -> list[dict]:
    return service.list_transactions(session, account_id, from_, to)


@app.get("/payees", dependencies=[Depends(authorize("read"))])
def payees(session: DB) -> list[dict]:
    return service.list_payees(session)


@app.post("/payees", dependencies=[Depends(authorize("payees"))])
def add_payee(body: PayeeCreate, session: DB) -> dict:
    return service.create_payee(session, **body.model_dump())


@app.post("/payments")
def add_payment(body: PaymentCreate, session: DB,
                actor: str = Depends(authorize("payments"))) -> dict:
    return service.create_payment(session, body.model_dump(), actor)


@app.get("/payments", dependencies=[Depends(authorize("read"))])
def payments(session: DB, status: str | None = None) -> list[dict]:
    return service.list_payments(session, status)


@app.patch("/payments/{payment_id}", dependencies=[Depends(authorize("payments"))])
def patch_payment(payment_id: str, body: PaymentPatch, session: DB) -> dict:
    return service.reschedule_payment(session, payment_id, body.execute_date)


@app.post("/simulate/advance", dependencies=[Depends(authorize("simulate"))])
def simulate_advance(session: DB, days: Annotated[int, Query(ge=0)] = 1) -> dict:
    return service.advance(session, days)
