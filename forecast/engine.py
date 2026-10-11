"""13-week cash-flow forecast (pure Python, no AI).

Every line carries ``source_refs`` (Transaction / Invoice / Payment IDs) so each
figure can be traced back to ledger rows. ``payments_cents`` and every payment
line amount are signed (negative = money out), so
``closing = opening + receipts + payments``.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date, timedelta
from statistics import median
from typing import Any

from pydantic import BaseModel, Field
from sqlmodel import Session, select

from common.config import load_country_pack
from common.db import (
    Invoice,
    Payment,
    Transaction,
    account_balance_cents,
    get_setting,
    get_today,
)

ACCOUNT_ID = "ACC-MAIN"
OVERDUE_ASSUMED_DAYS = 3  # overdue AR is assumed to arrive this many days after today
RECURRING = {  # ledger category -> label
    "payroll": "Payroll",
    "rent": "Rent",
    "utilities": "Utilities",
    "loan_repayment": "Loan repayment",
    "vat": "VAT payment",
}
RECURRING_SAMPLE = 3  # number of recent transactions averaged per category


class ForecastLine(BaseModel):
    label: str
    kind: str  # "receipt" | "payment"
    amount_cents: int  # signed: + in, - out
    expected_date: date
    source_refs: list[str]
    note: str = ""


class WeekBucket(BaseModel):
    index: int  # 1-based
    start: date
    end: date
    opening_cents: int
    receipts_cents: int
    payments_cents: int  # negative
    closing_cents: int
    lines: list[ForecastLine]


class Forecast(BaseModel):
    start_date: date
    currency: str
    opening_balance_cents: int
    weeks: list[WeekBucket]
    min_week: int
    min_closing_cents: int
    shortfall_weeks: list[int]  # weeks with closing < 0
    scenario: list[dict] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def customer_lateness_days(session: Session, counterparty_id: str) -> int:
    """Average days paid after due date, from the customer's paid AR invoices (>= 0)."""
    rows = session.exec(
        select(Invoice).where(
            Invoice.direction == "AR",
            Invoice.counterparty_id == counterparty_id,
            Invoice.status == "paid",
        )
    ).all()
    delays = [(r.paid_date - r.due_date).days for r in rows if r.paid_date]
    if not delays:
        return 0
    return max(0, round(sum(delays) / len(delays)))


def _month_iter(start: date, end: date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m > 12:
            y, m = y + 1, 1


def _recurring_lines(session: Session, start: date, end: date, vat_day: int) -> list[ForecastLine]:
    lines: list[ForecastLine] = []
    for cat, label in RECURRING.items():
        txs = session.exec(
            select(Transaction)
            .where(
                Transaction.account_id == ACCOUNT_ID,
                Transaction.category == cat,
                Transaction.amount_cents < 0,
                Transaction.booking_date <= start,
            )
            .order_by(Transaction.booking_date.desc())  # type: ignore[attr-defined]
            .limit(RECURRING_SAMPLE)
        ).all()
        if not txs:
            continue
        amount = round(sum(t.amount_cents for t in txs) / len(txs))
        day = vat_day if cat == "vat" else round(median(t.booking_date.day for t in txs))
        refs = [t.id for t in txs]
        for y, m in _month_iter(start, end):
            d = date(y, m, min(day, calendar.monthrange(y, m)[1]))
            if start <= d <= end:
                lines.append(
                    ForecastLine(
                        label=f"{label} (estimate)",
                        kind="payment",
                        amount_cents=amount,
                        expected_date=d,
                        source_refs=refs,
                        note=f"average of last {len(txs)} {cat} transactions",
                    )
                )
    return lines


def _collect_lines(session: Session, start: date, end: date, scenario: list[dict]) -> list[ForecastLine]:
    pack = load_country_pack(get_setting(session, "country", None))
    delays: dict[str, int] = defaultdict(int)
    moves: dict[str, date] = {}
    inflows: list[dict] = []
    for ch in scenario:
        t = ch.get("type")
        if t == "delay_customer":
            delays[ch["counterparty_id"]] += int(ch["days"])
        elif t == "move_payment":
            moves[ch["payment_id"]] = date.fromisoformat(str(ch["new_date"]))
        elif t == "inflow":
            inflows.append(ch)

    lines: list[ForecastLine] = []

    # Receipts: open AR shifted by the customer's average lateness.
    ar = session.exec(select(Invoice).where(Invoice.direction == "AR", Invoice.status == "open")).all()
    for inv in ar:
        late = customer_lateness_days(session, inv.counterparty_id)
        extra = delays.get(inv.counterparty_id, 0)
        expected = inv.due_date + timedelta(days=late + extra)
        note = f"due {inv.due_date}"
        if late or extra:
            note += f", adjusted +{late + extra} days"
            if extra:
                note += f" (incl. +{extra} scenario delay)"
            else:
                note += " for avg lateness"
        if expected < start:
            expected = start + timedelta(days=OVERDUE_ASSUMED_DAYS)
            note += f"; overdue, assumed paid {OVERDUE_ASSUMED_DAYS} days from today"
        lines.append(
            ForecastLine(
                label=f"Receipt {inv.number}",
                kind="receipt",
                amount_cents=inv.amount_cents,
                expected_date=expected,
                source_refs=[inv.id],
                note=note,
            )
        )

    # Payments: scheduled first, then open AP without a scheduled payment.
    scheduled = session.exec(select(Payment).where(Payment.status == "scheduled")).all()
    covered = {p.invoice_id for p in scheduled if p.invoice_id}
    for p in scheduled:
        d = moves.get(p.id, p.execute_date)
        refs = [p.id] + ([p.invoice_id] if p.invoice_id else [])
        note = "scheduled payment" + (f", moved from {p.execute_date}" if p.id in moves else "")
        lines.append(
            ForecastLine(
                label=f"Scheduled payment {p.id}",
                kind="payment",
                amount_cents=-p.amount_cents,
                expected_date=max(d, start),
                source_refs=refs,
                note=note,
            )
        )
    ap = session.exec(select(Invoice).where(Invoice.direction == "AP", Invoice.status == "open")).all()
    for inv in ap:
        if inv.id in covered:
            continue
        lines.append(
            ForecastLine(
                label=f"Payable {inv.number}",
                kind="payment",
                amount_cents=-inv.amount_cents,
                expected_date=max(inv.due_date, start),
                source_refs=[inv.id],
                note=f"due {inv.due_date}" + (" (critical)" if inv.critical else ""),
            )
        )

    lines += _recurring_lines(session, start, end, pack.vat_day_of_month)

    for ch in inflows:
        wk = max(1, int(ch["week"]))
        lines.append(
            ForecastLine(
                label=str(ch.get("label", "scenario inflow")),
                kind="receipt",
                amount_cents=int(ch["amount_cents"]),
                expected_date=start + timedelta(days=7 * (wk - 1)),
                source_refs=[f"SCENARIO:inflow:w{wk}"],
                note="scenario",
            )
        )
    return [ln for ln in lines if start <= ln.expected_date <= end]


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def build_forecast(session: Session, weeks: int = 13, scenario: list[dict] | None = None) -> Forecast:
    """Build the weekly cash forecast starting at the simulated today."""
    scenario = scenario or []
    start = get_today(session)
    end = start + timedelta(days=7 * weeks - 1)
    pack = load_country_pack(get_setting(session, "country", None))
    opening = account_balance_cents(session, ACCOUNT_ID, as_of=start)
    lines = sorted(_collect_lines(session, start, end, scenario), key=lambda ln: (ln.expected_date, ln.label))

    buckets: list[WeekBucket] = []
    bal = opening
    for i in range(weeks):
        ws, we = start + timedelta(days=7 * i), start + timedelta(days=7 * i + 6)
        wl = [ln for ln in lines if ws <= ln.expected_date <= we]
        rec = sum(ln.amount_cents for ln in wl if ln.amount_cents > 0)
        pay = sum(ln.amount_cents for ln in wl if ln.amount_cents < 0)
        buckets.append(
            WeekBucket(
                index=i + 1, start=ws, end=we, opening_cents=bal, receipts_cents=rec,
                payments_cents=pay, closing_cents=bal + rec + pay, lines=wl,
            )
        )
        bal += rec + pay
    low = min(buckets, key=lambda b: (b.closing_cents, b.index))
    return Forecast(
        start_date=start,
        currency=pack.currency,
        opening_balance_cents=opening,
        weeks=buckets,
        min_week=low.index,
        min_closing_cents=low.closing_cents,
        shortfall_weeks=[b.index for b in buckets if b.closing_cents < 0],
        scenario=scenario,
    )


def _row_dict(row: Any) -> dict:
    return row.model_dump(mode="json")


def explain_figure(session: Session, week: int, field: str = "closing_cents",
                   scenario: list[dict] | None = None) -> dict:
    """Explain one weekly figure: its lines plus the actual source rows as dicts."""
    fc = build_forecast(session, scenario=scenario)
    if not 1 <= week <= len(fc.weeks):
        raise ValueError(f"week must be 1..{len(fc.weeks)}")
    if field not in {"opening_cents", "receipts_cents", "payments_cents", "closing_cents"}:
        raise ValueError(f"unknown field {field!r}")
    b = fc.weeks[week - 1]
    if field == "receipts_cents":
        lines = [ln for ln in b.lines if ln.amount_cents > 0]
    elif field == "payments_cents":
        lines = [ln for ln in b.lines if ln.amount_cents < 0]
    elif field == "opening_cents":
        lines = []
    else:
        lines = b.lines
    refs = sorted({r for ln in lines for r in ln.source_refs})
    sources: dict[str, list[dict]] = {"transactions": [], "invoices": [], "payments": []}
    for ref in refs:
        if ref.startswith("TX-") and (row := session.get(Transaction, ref)):
            sources["transactions"].append(_row_dict(row))
        elif ref.startswith("PAY-") and (row := session.get(Payment, ref)):
            sources["payments"].append(_row_dict(row))
        elif row := session.get(Invoice, ref):
            sources["invoices"].append(_row_dict(row))
    out: dict = {
        "week": week, "field": field, "value_cents": getattr(b, field),
        "start": b.start.isoformat(), "end": b.end.isoformat(),
        "opening_cents": b.opening_cents, "currency": fc.currency,
        "lines": [ln.model_dump(mode="json") for ln in lines], "sources": sources,
    }
    if field == "opening_cents":
        out["note"] = (
            "Week 1 opening = account balance (opening balance + all booked transactions)."
            if week == 1 else f"Equals closing balance of week {week - 1}."
        )
    return out
