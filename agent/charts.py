"""Chart data for the `make_chart` tool, computed in code from the ledger (never from model text).

A chart spec is plain data: the UI draws it; the model only chooses which dataset to show.
Money values are integer cents in the ledger currency; the UI converts them for display.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from sqlmodel import Session, select

from common.config import load_country_pack
from common.db import Counterparty, Invoice, Transaction, get_today

DATASETS = {
    "forecast_balance": "Weekly closing balance over the 13-week forecast (baseline vs scenario if one is given)",
    "forecast_cashflow": "Money in and money out per forecast week",
    "receivables": "Open customer invoices by customer",
    "receivables_ageing": "Open customer invoices by how overdue they are",
    "payables": "Open supplier invoices by supplier",
    "transactions_monthly": "Booked money in and out per month",
    "financing_options": "True cost of each financing option that arrives in time",
}
AGEING_BUCKETS = ["Not yet due", "1-30 days overdue", "31-60 days overdue", "Over 60 days overdue"]


def _spec(title: str, kind: str, x_label: str, y_label: str, rows: list[dict], note: str = "",
          x_order: list | None = None) -> dict:
    display = load_country_pack().display  # so the model quotes formatted amounts instead of converting cents
    rows = [{**r, "value_display": display(r["value_cents"])} for r in rows]
    return {"title": title, "kind": kind, "x_label": x_label, "y_label": y_label, "rows": rows,
            "note": note, "x_order": x_order or list(dict.fromkeys(r["x"] for r in rows))}


def _forecast_balance(session: Session, scenario: list[dict] | None) -> dict:
    from forecast.engine import build_forecast

    series = [("Baseline", build_forecast(session))]
    if scenario:
        series.append(("With scenario", build_forecast(session, scenario=scenario)))
    rows = [{"x": f"W{w.index}", "series": name, "value_cents": w.closing_cents, "below_zero": w.index in fc.shortfall_weeks}
            for name, fc in series for w in fc.weeks]
    base = series[0][1]
    note = (f"Lowest point {base.min_running_date.isoformat()} (week {base.min_running_week}); "
            f"weeks below zero: {', '.join(map(str, base.shortfall_weeks)) or 'none'}.")
    return _spec("Closing balance by week", "line", "Week", "Closing balance", rows, note)


def _forecast_cashflow(session: Session) -> dict:
    from forecast.engine import build_forecast

    fc = build_forecast(session)
    rows = []
    for w in fc.weeks:
        rows.append({"x": f"W{w.index}", "series": "Money in", "value_cents": w.receipts_cents})
        rows.append({"x": f"W{w.index}", "series": "Money out", "value_cents": -w.payments_cents})
    return _spec("Money in and out by week", "grouped_bar", "Week", "Amount", rows)


def _open_by_counterparty(session: Session, direction: str, overdue_only: bool) -> list[dict]:
    today = get_today(session)
    totals: dict[str, int] = defaultdict(int)
    for inv in session.exec(select(Invoice).where(Invoice.direction == direction, Invoice.status == "open")):
        if overdue_only and inv.due_date >= today:
            continue
        cp = session.get(Counterparty, inv.counterparty_id)
        totals[cp.name if cp else inv.counterparty_id] += inv.amount_cents
    return [{"x": name, "series": "Open amount", "value_cents": cents}
            for name, cents in sorted(totals.items(), key=lambda kv: -kv[1])]


def _receivables_ageing(session: Session) -> dict:
    today = get_today(session)
    totals = dict.fromkeys(AGEING_BUCKETS, 0)
    for inv in session.exec(select(Invoice).where(Invoice.direction == "AR", Invoice.status == "open")):
        days = (today - inv.due_date).days
        bucket = AGEING_BUCKETS[0 if days <= 0 else 1 if days <= 30 else 2 if days <= 60 else 3]
        totals[bucket] += inv.amount_cents
    rows = [{"x": b, "series": "Open amount", "value_cents": c} for b, c in totals.items()]
    return _spec("Open customer invoices by age", "bar", "Age", "Open amount", rows, f"As of {today.isoformat()}.",
                 AGEING_BUCKETS)


def _transactions_monthly(session: Session) -> dict:
    flows: dict[str, dict[str, int]] = defaultdict(lambda: {"Money in": 0, "Money out": 0})
    for tx in session.exec(select(Transaction)):
        month = tx.booking_date.strftime("%Y-%m")
        flows[month]["Money in" if tx.amount_cents > 0 else "Money out"] += abs(tx.amount_cents)
    rows = [{"x": m, "series": s, "value_cents": v} for m in sorted(flows) for s, v in flows[m].items()]
    return _spec("Booked money in and out by month", "grouped_bar", "Month", "Amount", rows)


def _financing_options(session: Session) -> dict:
    from agent.tools import ToolError, _need_defaults, tool_compare_financing

    try:
        cmp = tool_compare_financing(session, _need_defaults(session, {}), {})
    except ToolError as e:
        return _spec("True cost of financing options", "bar", "Option", "True cost", [], str(e))
    rows = [{"x": o["name"], "series": "True cost", "value_cents": o["total_cost_cents"]} for o in cmp["ranked"]]
    note = (f"For {cmp['amount_display']} needed by {cmp['needed_by']} for {cmp['days_needed']} days. "
            + (f"Excluded: {', '.join(e['name'] for e in cmp['excluded'])}." if cmp["excluded"] else ""))
    return _spec("True cost of financing options", "bar", "Option", "True cost", rows, note.strip())


def build_chart(session: Session, dataset: str, scenario: list[dict] | None = None,
                overdue_only: bool = False) -> dict:
    """The chart spec for one dataset; raises ValueError for an unknown dataset."""
    if dataset == "forecast_balance":
        spec = _forecast_balance(session, scenario)
    elif dataset == "forecast_cashflow":
        spec = _forecast_cashflow(session)
    elif dataset == "receivables":
        rows = _open_by_counterparty(session, "AR", overdue_only)
        spec = _spec("Overdue customer invoices by customer" if overdue_only else "Open customer invoices by customer",
                     "bar", "Customer", "Open amount", rows)
    elif dataset == "receivables_ageing":
        spec = _receivables_ageing(session)
    elif dataset == "payables":
        spec = _spec("Open supplier invoices by supplier", "bar", "Supplier", "Open amount",
                     _open_by_counterparty(session, "AP", False))
    elif dataset == "transactions_monthly":
        spec = _transactions_monthly(session)
    elif dataset == "financing_options":
        spec = _financing_options(session)
    else:
        raise ValueError(f"unknown dataset {dataset!r}")
    return {"dataset": dataset, "as_of": date.isoformat(get_today(session)), **spec}
