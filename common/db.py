"""Shared SQLite schema for Runway.

Every module (mock bank, forecast, rules engine, tools, audit log, UI) reads and
writes the same database through these models. Amounts are always stored as
integer minor units (cents) to avoid floating-point rounding errors.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Field, Session, SQLModel, create_engine, select

def utcnow() -> datetime:
    """Timezone-aware current UTC time (this SQLModel version rejects naive datetimes)."""
    return datetime.now(timezone.utc)


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "var" / "runway.db"


# --------------------------------------------------------------------------- #
# Reference data
# --------------------------------------------------------------------------- #
class Account(SQLModel, table=True):
    """A bank account of the company. Balance is derived from transactions."""

    id: str = Field(primary_key=True)  # e.g. "ACC-MAIN"
    name: str
    currency: str
    opening_balance_cents: int = 0  # balance before the first stored transaction


class Counterparty(SQLModel, table=True):
    """A customer, supplier or other party (landlord, tax office, lender, staff)."""

    id: str = Field(primary_key=True)  # e.g. "CUST-BRIGHTFIELD", "SUP-CARTONIA"
    name: str
    kind: str  # "customer" | "supplier" | "landlord" | "tax" | "lender" | "payroll" | "utility"
    payment_terms_days: int = 30
    email: Optional[str] = None


class Payee(SQLModel, table=True):
    """Someone the company can pay. Only humans (UI) may create or edit payees."""

    id: str = Field(primary_key=True)  # e.g. "PAYEE-CARTONIA"
    name: str
    counterparty_id: Optional[str] = Field(default=None, foreign_key="counterparty.id")
    iban: str
    verified: bool = False
    created_by: str = "owner"  # "owner" | "accountant" | "seed"
    created_at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #
class Invoice(SQLModel, table=True):
    """An invoice we sent (AR, money owed to us) or received (AP, money we owe)."""

    id: str = Field(primary_key=True)  # e.g. "AR-1042", "AP-2210"
    number: str
    direction: str  # "AR" | "AP"
    counterparty_id: str = Field(foreign_key="counterparty.id")
    issue_date: date
    due_date: date
    amount_cents: int  # gross amount incl. VAT, always positive
    vat_cents: int = 0
    currency: str
    status: str = "open"  # "open" | "paid"
    paid_date: Optional[date] = None
    critical: bool = False  # AP only: True = must not be rescheduled (e.g. tax)
    document_id: Optional[str] = Field(default=None, foreign_key="document.id")


class Transaction(SQLModel, table=True):
    """A booked bank transaction. Positive = money in, negative = money out."""

    id: str = Field(primary_key=True)  # e.g. "TX-000123"
    account_id: str = Field(foreign_key="account.id")
    booking_date: date = Field(index=True)
    amount_cents: int
    currency: str
    description: str
    category: str  # "sales_receipt" | "supplier_payment" | "payroll" | "rent" | "utilities" | "vat" | "loan_repayment" | "financing" | "other"
    counterparty_id: Optional[str] = Field(default=None, foreign_key="counterparty.id")
    invoice_id: Optional[str] = Field(default=None, foreign_key="invoice.id")


class Payment(SQLModel, table=True):
    """A payment scheduled or executed through the mock bank."""

    id: str = Field(primary_key=True)  # e.g. "PAY-0001"
    idempotency_key: str = Field(unique=True, index=True)
    account_id: str = Field(default="ACC-MAIN", foreign_key="account.id")
    payee_id: str = Field(foreign_key="payee.id")
    amount_cents: int  # positive
    currency: str
    execute_date: date
    status: str = "scheduled"  # "scheduled" | "executed" | "cancelled"
    invoice_id: Optional[str] = Field(default=None, foreign_key="invoice.id")
    document_id: Optional[str] = Field(default=None, foreign_key="document.id")
    automatic: bool = False  # True when allowed at tier T2 without human approval
    created_by: str = "agent"
    created_at: datetime = Field(default_factory=utcnow)
    created_sim_date: Optional[date] = None  # simulated 'today' when created (daily caps)
    transaction_id: Optional[str] = None  # set once executed


# --------------------------------------------------------------------------- #
# Documents, approvals, outputs
# --------------------------------------------------------------------------- #
class Document(SQLModel, table=True):
    """An incoming document (invoice PDF, email). Its text is UNTRUSTED data."""

    id: str = Field(primary_key=True)  # e.g. "DOC-0007"
    filename: str
    kind: str  # "invoice" | "email" | "other"
    sender: Optional[str] = None
    content_text: str = ""
    received_at: datetime = Field(default_factory=utcnow)
    flagged: bool = False
    flag_reason: Optional[str] = None
    cleared: bool = False  # a human reviewed the flag and cleared it


class ApprovalRequest(SQLModel, table=True):
    """A tool call that the rules engine parked for human approval."""

    id: str = Field(primary_key=True)  # e.g. "APR-0003"
    tool_name: str
    args_json: str
    required_roles_json: str  # JSON list, e.g. ["owner", "accountant"]
    approvals_json: str = "[]"  # JSON list of roles that approved so far
    status: str = "pending"  # "pending" | "approved" | "rejected" | "executed" | "failed"
    reason: str = ""
    result_json: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    decided_at: Optional[datetime] = None


class OutboxEmail(SQLModel, table=True):
    """Test inbox: every email the system 'sends' lands here (and in var/outbox/)."""

    id: str = Field(primary_key=True)  # e.g. "MAIL-0001"
    to: str
    subject: str
    body: str
    invoice_id: Optional[str] = None
    kind: str = "reminder"  # "reminder" | "fraud_alert" | "notification"
    sent_at: datetime = Field(default_factory=utcnow)


class FinancingApplication(SQLModel, table=True):
    """A prepared (not submitted) financing application with its PDF."""

    id: str = Field(primary_key=True)  # e.g. "FIN-0001"
    product_id: str
    amount_cents: int
    currency: str
    needed_by: date
    pdf_path: str
    status: str = "prepared"
    created_at: datetime = Field(default_factory=utcnow)


class AuditEntry(SQLModel, table=True):
    """Append-only, hash-chained audit log entry. See audit/log.py."""

    id: Optional[int] = Field(default=None, primary_key=True)
    timestamp: str  # ISO-8601 UTC string, part of the hashed content
    actor: str  # "agent" | "owner" | "accountant" | "system"
    action: str  # tool name or human action, e.g. "schedule_payment", "approve"
    args_json: str = "{}"  # full args, for display; covered by args_hash
    args_hash: str
    decision: str  # "ALLOW" | "NEEDS_APPROVAL" | "DENY" | "EXECUTED" | "INFO" | ...
    reason: str = ""
    prev_hash: str
    hash: str


class Setting(SQLModel, table=True):
    """Key/value runtime settings: simulated clock, kill switch, country pack."""

    key: str = Field(primary_key=True)
    value: str


# --------------------------------------------------------------------------- #
# Engine / session helpers
# --------------------------------------------------------------------------- #
@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record) -> None:  # pragma: no cover - glue
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


_engines: dict[str, Engine] = {}


def db_url(path: str | Path | None = None) -> str:
    """Return the SQLAlchemy URL. Override with RUNWAY_DB env var or `path`."""
    p = Path(path or os.environ.get("RUNWAY_DB", DEFAULT_DB_PATH))
    p.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{p}"


def get_engine(path: str | Path | None = None) -> Engine:
    """Return a cached engine for the given database path and create tables."""
    url = db_url(path)
    if url not in _engines:
        eng = create_engine(url, connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(eng)
        _engines[url] = eng
    return _engines[url]


def reset_engines() -> None:
    """Forget cached engines (used by tests that switch RUNWAY_DB)."""
    for eng in _engines.values():
        eng.dispose()
    _engines.clear()


def session_scope(path: str | Path | None = None) -> Session:
    """Open a new session. Use as `with session_scope() as s: ...`."""
    return Session(get_engine(path))


def iter_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as s:
        yield s


# --------------------------------------------------------------------------- #
# Settings helpers: simulated clock and kill switch
# --------------------------------------------------------------------------- #
DEFAULT_TODAY = date(2026, 10, 19)  # Monday; week 1 of the forecast starts here


def get_setting(s: Session, key: str, default: str | None = None) -> str | None:
    row = s.get(Setting, key)
    return row.value if row else default


def set_setting(s: Session, key: str, value: str) -> None:
    row = s.get(Setting, key)
    if row:
        row.value = value
    else:
        s.add(Setting(key=key, value=value))
    s.commit()


def get_today(s: Session) -> date:
    """The simulated 'today' (moved forward by POST /simulate/advance)."""
    return date.fromisoformat(get_setting(s, "today", DEFAULT_TODAY.isoformat()))


def kill_switch_on(s: Session) -> bool:
    return get_setting(s, "kill_switch", "off") == "on"


def account_balance_cents(s: Session, account_id: str = "ACC-MAIN", as_of: date | None = None) -> int:
    """Opening balance plus all transactions booked up to and including `as_of`."""
    acc = s.get(Account, account_id)
    if acc is None:
        raise KeyError(account_id)
    q = select(Transaction).where(Transaction.account_id == account_id)
    if as_of is not None:
        q = q.where(Transaction.booking_date <= as_of)
    return acc.opening_balance_cents + sum(t.amount_cents for t in s.exec(q))
