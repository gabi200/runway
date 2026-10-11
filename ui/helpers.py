"""Small display helpers shared by the Streamlit UI (no business logic here)."""

from __future__ import annotations

import os
from pathlib import Path

from common.config import CountryPack, load_country_pack
from common.db import REPO_ROOT

SAMPLE_DIR = Path(os.environ.get("RUNWAY_SAMPLE_DIR", REPO_ROOT / "data" / "sample"))
ROLES = ("owner", "accountant")

BADGE_COLORS = {
    "ALLOW": "#1b7f3b", "ok": "#1b7f3b", "EXECUTED": "#1b7f3b", "APPROVED": "#1b7f3b",
    "executed": "#1b7f3b", "approved": "#1b7f3b",
    "NEEDS_APPROVAL": "#b7791f", "needs_approval": "#b7791f", "pending": "#b7791f",
    "DENY": "#c62828", "denied": "#c62828", "error": "#c62828", "rejected": "#c62828",
    "REJECTED": "#c62828", "FAILED": "#c62828", "failed": "#c62828",
}


def pack_for(code: str) -> CountryPack:
    """Country pack for the display switch ('eu' or 'cn')."""
    return load_country_pack(code)


def money(pack: CountryPack, eur_cents: int | None) -> str:
    """Format stored EUR cents in the selected display currency."""
    if eur_cents is None:
        return ""
    return pack.format_money(pack.convert_from_eur_cents(int(eur_cents)))


def badge(text: str) -> str:
    """Colored badge as inline HTML (render with unsafe_allow_html)."""
    color = BADGE_COLORS.get(text, "#555555")
    return (f'<span style="background:{color};color:#fff;padding:2px 8px;border-radius:10px;'
            f'font-size:0.8em;font-weight:600">{text}</span>')


def company_name(account_name: str | None) -> str:
    """Derive the company name from the main account name ('Lumen Packaging main account')."""
    if not account_name:
        return "Runway demo company"
    return account_name.removesuffix(" main account")
