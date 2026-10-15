"""13-week cash-flow forecast (pure Python, no AI).

Every line carries ``source_refs`` (Transaction / Invoice / Payment IDs) so each
figure can be traced back to ledger rows. ``payments_cents`` and every payment
line amount are signed (negative = money out), so
``closing = opening + receipts + payments``. The balance is also walked line by
line: ``min_running_cents`` is the lowest point inside a week, which can be far
below the week's closing, and shortfalls are judged on it.
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
    Account,
    Counterparty,
    Invoice,
    Payee,
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
RECURRING_SAMPLE = 3  # number of recent monthly totals whose median is used per category
KIND_CATEGORY = {  # counterparty kind -> recurring category (the mapping bank/service.py advance() books with)
    "payroll": "payroll",
    "landlord": "rent",
    "utility": "utilities",
    "tax": "vat",
    "lender": "loan_repayment",
}


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
    min_running_cents: int  # lowest running balance in the week (the opening included)
    min_running_date: date  # day it is reached (the week start when it is the opening)
    lines: list[ForecastLine]  # in walk order: by date, payments before receipts, then label


class Forecast(BaseModel):
    start_date: date
    currency: str
    opening_balance_cents: int
    weeks: list[WeekBucket]
    min_week: int  # week with the lowest CLOSING balance
    min_closing_cents: int
    min_running_week: int  # week with the lowest running balance
    min_running_cents: int
    min_running_date: date
    shortfall_weeks: list[int]  # weeks whose running balance goes below 0 at any point
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


def _recurring_lines(session: Session, start: date, end: date, vat_day: int,
                     known: dict[tuple[str, int, int], int]) -> list[ForecastLine]:
    """Estimate each recurring item from the monthly totals of seeded/booked recurring rows.

    Transactions created from a Payment (``Payment.transaction_id``) are excluded, so small
    automatic payments booked under a recurring category can never change the estimate.
    The estimate is the median of the last RECURRING_SAMPLE monthly totals.

    A month's estimate only tops up what is already known for that category and month:
    ``known`` (scheduled payments and open payables, keyed by (category, year, month)) and,
    for the current month, what is already booked (bank-executed payments included). If the
    current month's day has passed and part of it is still missing, the rest is expected
    OVERDUE_ASSUMED_DAYS after today, like an overdue receipt.
    """
    from_payments = {t for t in session.exec(select(Payment.transaction_id)).all() if t}
    lines: list[ForecastLine] = []
    for cat, label in RECURRING.items():
        txs = [
            t for t in session.exec(
                select(Transaction)
                .where(Transaction.account_id == ACCOUNT_ID, Transaction.category == cat,
                       Transaction.amount_cents < 0, Transaction.booking_date <= start)
                .order_by(Transaction.booking_date.desc())  # type: ignore[attr-defined]
            ).all() if t.id not in from_payments
        ]
        months: dict[tuple[int, int], list[Transaction]] = defaultdict(list)
        for t in txs:
            months[(t.booking_date.year, t.booking_date.month)].append(t)
        recent = sorted(months, reverse=True)[:RECURRING_SAMPLE]
        if not recent:
            continue
        amount = round(median(sum(t.amount_cents for t in months[k]) for k in recent))
        used = [t for k in recent for t in months[k]]
        day = vat_day if cat == "vat" else round(median(t.booking_date.day for t in used))
        refs = [t.id for t in used]
        booked = -sum(t.amount_cents for t in session.exec(
            select(Transaction).where(Transaction.account_id == ACCOUNT_ID, Transaction.category == cat,
                                      Transaction.amount_cents < 0, Transaction.booking_date <= start,
                                      Transaction.booking_date >= start.replace(day=1))))
        for y, m in _month_iter(start, end):
            d = date(y, m, min(day, calendar.monthrange(y, m)[1]))
            # ponytail: anything booked, scheduled or invoiced for the category counts toward its month (any
            # amount, by booking/execute/due month), and the estimate only adds the rest, so a small item never
            # removes a large estimate; a real bill below the estimate leaves the difference (conservative).
            already = known.get((cat, y, m), 0) + (booked if (y, m) == (start.year, start.month) else 0)
            rest = -amount - already
            if rest <= 0:
                continue
            note = f"median of last {len(recent)} monthly {cat} totals (bank-executed payments excluded)"
            if already:
                note += f", less {already / 100:.2f} already booked, scheduled or invoiced for {y}-{m:02d}"
            if d < start:
                d = start + timedelta(days=OVERDUE_ASSUMED_DAYS)
                note += f"; its day has passed unpaid, assumed paid {OVERDUE_ASSUMED_DAYS} days from today"
            if d <= end:
                lines.append(
                    ForecastLine(
                        label=f"{label} (estimate)",
                        kind="payment",
                        amount_cents=-rest,
                        expected_date=d,
                        source_refs=refs,
                        note=note,
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
        expected = inv.due_date + timedelta(days=late)
        note = f"due {inv.due_date}" + (f", adjusted +{late} days for avg lateness" if late else "")
        if expected < start:
            expected = start + timedelta(days=OVERDUE_ASSUMED_DAYS)
            note += f"; overdue, assumed paid {OVERDUE_ASSUMED_DAYS} days from today"
        if extra:  # after the overdue clamp, so a delay never moves a receipt earlier
            expected += timedelta(days=extra)
            note += f"; +{extra} days scenario delay"
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

    # Scheduled payments have their own lines; AP lines cover only the residual balance.
    # Both count toward the recurring estimate of their category and month (``known``).
    known: dict[tuple[str, int, int], int] = defaultdict(int)

    def category_of(counterparty_id: str | None) -> str | None:
        cp = session.get(Counterparty, counterparty_id) if counterparty_id else None
        return KIND_CATEGORY.get(cp.kind) if cp else None

    scheduled = session.exec(select(Payment).where(Payment.status == "scheduled")).all()
    committed: dict[str, list[Payment]] = defaultdict(list)
    for payment in session.exec(select(Payment).where(Payment.status.in_(["scheduled", "executed"]))):
        if payment.invoice_id:
            committed[payment.invoice_id].append(payment)
    for p in scheduled:
        payee = session.get(Payee, p.payee_id)
        if cat := category_of(payee.counterparty_id if payee else None):
            known[(cat, p.execute_date.year, p.execute_date.month)] += p.amount_cents
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
        payments = committed.get(inv.id, [])
        remaining = max(0, inv.amount_cents - sum(p.amount_cents for p in payments))
        if remaining == 0:
            continue
        if cat := category_of(inv.counterparty_id):
            known[(cat, inv.due_date.year, inv.due_date.month)] += remaining
        lines.append(
            ForecastLine(
                label=f"Payable {inv.number}",
                kind="payment",
                amount_cents=-remaining,
                expected_date=max(inv.due_date, start),
                source_refs=[inv.id] + [p.id for p in payments],
                note=f"due {inv.due_date}" + (" (critical)" if inv.critical else ""),
            )
        )

    lines += _recurring_lines(session, start, end, pack.vat_day_of_month, known)

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
    # ponytail: the time of day of a movement is unknown, so on the same day payments are counted
    # before receipts (conservative): a same-day receipt never hides a dip. No intraday model beyond that.
    lines = sorted(_collect_lines(session, start, end, scenario),
                   key=lambda ln: (ln.expected_date, ln.amount_cents >= 0, ln.label))

    buckets: list[WeekBucket] = []
    bal = opening
    for i in range(weeks):
        ws, we = start + timedelta(days=7 * i), start + timedelta(days=7 * i + 6)
        wl = [ln for ln in lines if ws <= ln.expected_date <= we]
        rec = sum(ln.amount_cents for ln in wl if ln.amount_cents > 0)
        pay = sum(ln.amount_cents for ln in wl if ln.amount_cents < 0)
        run = low = bal
        low_date = ws
        for ln in wl:
            run += ln.amount_cents
            if run < low:
                low, low_date = run, ln.expected_date
        buckets.append(
            WeekBucket(
                index=i + 1, start=ws, end=we, opening_cents=bal, receipts_cents=rec,
                payments_cents=pay, closing_cents=bal + rec + pay, min_running_cents=low,
                min_running_date=low_date, lines=wl,
            )
        )
        bal += rec + pay
    low_close = min(buckets, key=lambda b: (b.closing_cents, b.index))
    low_run = min(buckets, key=lambda b: (b.min_running_cents, b.index))
    return Forecast(
        start_date=start,
        currency=pack.currency,
        opening_balance_cents=opening,
        weeks=buckets,
        min_week=low_close.index,
        min_closing_cents=low_close.closing_cents,
        min_running_week=low_run.index,
        min_running_cents=low_run.min_running_cents,
        min_running_date=low_run.min_running_date,
        shortfall_weeks=[b.index for b in buckets if b.min_running_cents < 0],
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
        if week == 1:
            acc = session.get(Account, ACCOUNT_ID)
            txs = session.exec(
                select(Transaction).where(Transaction.account_id == ACCOUNT_ID,
                                          Transaction.booking_date <= fc.start_date)
                .order_by(Transaction.booking_date, Transaction.id)  # type: ignore[arg-type]
            ).all()
            out["account"] = {"id": ACCOUNT_ID, "opening_balance_cents": acc.opening_balance_cents if acc else 0,
                              "as_of": fc.start_date.isoformat()}
            out["transactions_summary"] = {
                "count": len(txs), "sum_cents": sum(t.amount_cents for t in txs),
                "transaction_ids": [t.id for t in txs],
                "first_booking_date": txs[0].booking_date.isoformat() if txs else None,
                "last_booking_date": txs[-1].booking_date.isoformat() if txs else None,
            }
            out["note"] = ("Week 1 opening = account opening balance + the sum of all booked transactions "
                           f"up to {fc.start_date.isoformat()} (ids listed in transactions_summary).")
        else:
            out["note"] = f"Equals closing balance of week {week - 1}."
    return out


FINANCING_ROUNDING_CENTS = 100_000  # financing amounts are rounded UP to a whole 1,000 (ledger units)
MIN_FINANCING_DAYS = 30  # shortest term quoted: products are priced per started 30 days


def financing_need(session: Session, scenario: list[dict] | None = None) -> dict:
    """How much cash is needed, by when and for how long, computed in code (no AI).

    The balance is walked line by line (same-day payments before receipts, see build_forecast):
    amount = the lowest running balance as a deficit, rounded up to a whole 1,000;
    needed_by = the first day the running balance is below zero;
    days_needed = days from needed_by until the receipt that brings the balance back to >= 0
    after its LAST dip, or the horizon end if it does not recover (at least MIN_FINANCING_DAYS).
    """
    fc = build_forecast(session, scenario=scenario)
    pack = load_country_pack(get_setting(session, "country", None))
    base = {"currency": fc.currency, "shortfall_weeks": fc.shortfall_weeks, "scenario": scenario or [],
            "worst_week": fc.min_running_week, "worst_date": fc.min_running_date.isoformat(),
            "worst_balance_cents": fc.min_running_cents,
            "worst_balance_display": pack.display(fc.min_running_cents)}
    if not fc.shortfall_weeks:
        return {**base, "amount_cents": 0, "needed_by": None, "days_needed": 0, "recovery_week": None,
                "recovery_refs": [],
                "explanation": f"No shortfall: the lowest balance is {pack.display(fc.min_running_cents)} "
                               f"on {fc.min_running_date.isoformat()} (week {fc.min_running_week}). "
                               "No financing is needed."}
    amount = -(-(-fc.min_running_cents) // FINANCING_ROUNDING_CENTS) * FINANCING_ROUNDING_CENTS
    bal = fc.opening_balance_cents
    needed_by = fc.start_date if bal < 0 else None
    recovery = None
    for w in fc.weeks:
        for ln in w.lines:  # already in walk order
            was_negative = bal < 0
            bal += ln.amount_cents
            if bal < 0:
                needed_by, recovery = needed_by or ln.expected_date, None
            elif was_negative:
                recovery = (w, ln)
    if recovery:
        rw, rl = recovery
        end, refs, rec_week = rl.expected_date, list(rl.source_refs), rw.index
        rec_text = (f"balance is back above zero with {rl.label} ({pack.display(rl.amount_cents)}) "
                    f"expected {rl.expected_date.isoformat()} in week {rw.index}")
    else:
        end, refs, rec_week = fc.weeks[-1].end, [], None
        rec_text = "no receipt within the forecast horizon brings the balance back above zero"
    days = max(MIN_FINANCING_DAYS, (end - needed_by).days)
    return {
        **base, "amount_cents": amount, "amount_display": pack.display(amount), "needed_by": needed_by.isoformat(),
        "days_needed": days, "recovery_week": rec_week, "recovery_refs": refs,
        "explanation": (
            f"Shortfall in week(s) {', '.join(map(str, fc.shortfall_weeks))}: the balance first goes below zero "
            f"on {needed_by.isoformat()} and is lowest at {pack.display(fc.min_running_cents)} on "
            f"{fc.min_running_date.isoformat()} (week {fc.min_running_week}; same-day payments counted before "
            f"receipts). Amount = that deficit rounded up to a whole 1,000: {pack.display(amount)}. "
            f"Needed from {needed_by.isoformat()} for {days} days: {rec_text}."),
    }
