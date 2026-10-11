"""Country packs and LLM config.

Everything country-specific (currency, VAT rate, tax calendar, default terms)
lives in config/<pack>.yaml. Nothing else in the code may hardcode a currency
or a rate.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


@dataclass(frozen=True)
class CountryPack:
    code: str  # "eu" | "cn"
    name: str
    currency: str  # ISO 4217, e.g. "EUR", "CNY"
    currency_symbol: str
    vat_rate: float  # e.g. 0.20
    vat_day_of_month: int  # day the monthly VAT payment is due
    default_customer_terms_days: int
    default_supplier_terms_days: int
    fx_from_eur: float  # display conversion factor for the demo switch

    def format_money(self, cents: int) -> str:
        """Format integer cents for display, e.g. -1800000 -> '-€18,000.00'."""
        sign = "-" if cents < 0 else ""
        return f"{sign}{self.currency_symbol}{abs(cents) / 100:,.2f}"

    def convert_from_eur_cents(self, cents: int) -> int:
        """Convert stored EUR cents into this pack's currency for display."""
        return round(cents * self.fx_from_eur)


def load_yaml(name: str) -> dict[str, Any]:
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=8)
def load_country_pack(code: str | None = None) -> CountryPack:
    """Load a country pack. Default from RUNWAY_COUNTRY env var, else 'eu'."""
    code = (code or os.environ.get("RUNWAY_COUNTRY", "eu")).lower()
    raw = load_yaml(f"{code}.yaml")
    return CountryPack(code=code, **raw)
