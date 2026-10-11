"""Financing comparison: true cost in integer cents, eligibility and speed filters.

Terms come from `data/financing.yaml` (fictional products, SPEC 5.7). Pure code, no AI.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from sqlmodel import Session, select

from common.config import load_country_pack
from common.db import Account, Invoice, get_today

CATALOGUE_PATH = Path(__file__).resolve().parent.parent / "data" / "financing.yaml"


@lru_cache(maxsize=1)
def load_products() -> tuple[dict[str, Any], ...]:
    """Load the product catalogue from YAML."""
    with open(CATALOGUE_PATH, encoding="utf-8") as f:
        return tuple(yaml.safe_load(f)["products"])


def get_product(product_id: str) -> dict[str, Any] | None:
    """Return one product definition by id, or None."""
    return next((p for p in load_products() if p["id"] == product_id), None)


def _round(x: Decimal) -> int:
    return int(x.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def product_cost(product: dict[str, Any], amount_cents: int, days_needed: int) -> dict[str, int]:
    """True cost (interest + all fees) of `amount_cents` held for `days_needed` days."""
    amount = Decimal(amount_cents)
    if "rate_pct_per_period" in product:
        periods = max(1, math.ceil(days_needed / product["period_days"]))
        interest = amount * Decimal(str(product["rate_pct_per_period"])) / 100 * periods
    else:
        billable = product["term_days"] if product.get("charge_full_term") else days_needed
        interest = amount * Decimal(str(product["annual_rate_pct"])) / 100 * billable / 365
    fees = amount * Decimal(str(product.get("fee_pct", 0))) / 100 + product.get("one_off_fee_cents", 0)
    i, f = _round(interest), _round(fees)
    return {"interest_cents": i, "fees_cents": f, "total_cost_cents": i + f}


def _largest_open_ar(session: Session, counterparty_id: str | None = None) -> Invoice | None:
    q = select(Invoice).where(Invoice.direction == "AR", Invoice.status == "open")
    if counterparty_id:
        q = q.where(Invoice.counterparty_id == counterparty_id)
    rows = list(session.exec(q))
    return max(rows, key=lambda i: i.amount_cents) if rows else None


def _fmt(cents: int) -> str:
    """Display string for ledger cents (country pack is display-only)."""
    return load_country_pack().display(cents)


def account_currency(session: Session) -> str:
    """Currency of the ledger / bank account (not the display currency)."""
    acc = session.get(Account, "ACC-MAIN") or session.exec(select(Account)).first()
    return acc.currency if acc else load_country_pack().currency


def _eligibility(session: Session, p: dict[str, Any], amount_cents: int) -> tuple[str | None, list[str]]:
    """Return (exclusion reason or None, warnings)."""
    warnings: list[str] = []
    req = p.get("requires")
    if req in ("open_ar_invoice", "brightfield_invoice"):
        cp = p.get("eligible_counterparty_id") if req == "brightfield_invoice" else None
        inv = _largest_open_ar(session, cp)
        if inv is None:
            what = "Brightfield invoice" if cp else "customer invoice"
            return f"No open {what} to finance against.", warnings
        cap = inv.amount_cents * p.get("advance_pct", 100) // 100
        if amount_cents > cap:
            return (f"Amount {_fmt(amount_cents)} exceeds the {p.get('advance_pct', 100)}% advance cap "
                    f"of {_fmt(cap)} on the largest eligible open invoice {inv.id}."), warnings
        warnings.append(f"Financed against invoice {inv.id}.")
    if "max_amount_cents" in p and amount_cents > p["max_amount_cents"]:
        return f"Amount {_fmt(amount_cents)} exceeds the limit of {_fmt(p['max_amount_cents'])}.", warnings
    if p.get("needs_confirmation"):
        warnings.append(p["needs_confirmation"])
    return None, warnings


def compare_financing(session: Session, amount_cents: int, needed_by: date, days_needed: int) -> dict:
    """Rank financing options by true cost; exclude slow or ineligible ones.

    Time to cash is counted in calendar days. Amounts are integer cents in the
    account currency plus `*_display` strings from the country pack. Returns
    {recommended, ranked, excluded, currency, amount_cents, needed_by, days_needed}.
    """
    if amount_cents <= 0 or days_needed <= 0:
        raise ValueError("amount_cents and days_needed must be positive")
    if isinstance(needed_by, str):
        needed_by = date.fromisoformat(needed_by)
    today = get_today(session)
    ranked: list[dict] = []
    excluded: list[dict] = []
    for p in load_products():
        arrival = today + timedelta(days=p["time_to_cash_days"])
        if arrival > needed_by:
            excluded.append({"product_id": p["id"], "name": p["name"], "arrival_date": arrival.isoformat(),
                             "time_to_cash_days": p["time_to_cash_days"], "reason": (
                f"Too slow: cash arrives {arrival.isoformat()} ({p['time_to_cash_days']} days), "
                f"after the needed date {needed_by.isoformat()}.")})
            continue
        reason, warnings = _eligibility(session, p, amount_cents)
        if reason:
            excluded.append({"product_id": p["id"], "name": p["name"], "arrival_date": arrival.isoformat(),
                             "time_to_cash_days": p["time_to_cash_days"], "reason": reason})
            continue
        cost = product_cost(p, amount_cents, days_needed)
        ranked.append({
            "product_id": p["id"], "name": p["name"], "amount_cents": amount_cents, **cost,
            "effective_cost_pct": round(cost["total_cost_cents"] / amount_cents * 100, 2),
            "interest_display": _fmt(cost["interest_cents"]), "fees_display": _fmt(cost["fees_cents"]),
            "total_cost_display": _fmt(cost["total_cost_cents"]), "amount_display": _fmt(amount_cents),
            "arrives_in_time": True,
            "arrival_date": arrival.isoformat(), "time_to_cash_days": p["time_to_cash_days"],
            "needs_confirmation": p.get("needs_confirmation"), "warnings": warnings, "terms": p["notes"],
        })
    ranked.sort(key=lambda r: (r["total_cost_cents"], r["time_to_cash_days"]))
    for n, r in enumerate(ranked, 1):
        r["rank"] = n
    return {
        "recommended": ranked[0] if ranked else None, "ranked": ranked, "excluded": excluded,
        "currency": account_currency(session), "amount_cents": amount_cents,
        "amount_display": _fmt(amount_cents), "today": today.isoformat(),
        "needed_by": needed_by.isoformat(), "days_needed": days_needed,
    }
