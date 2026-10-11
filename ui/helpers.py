"""Small display helpers shared by the Streamlit UI (no business logic here)."""

from __future__ import annotations

import html
import os
from pathlib import Path

from common.config import CountryPack, load_country_pack
from common.db import REPO_ROOT

SAMPLE_DIR = Path(os.environ.get("RUNWAY_SAMPLE_DIR", REPO_ROOT / "data" / "sample"))
ROLES = ("owner", "accountant")

BADGE_LEVELS = {
    "ALLOW": "ok", "ok": "ok", "EXECUTED": "ok", "APPROVED": "ok", "executed": "ok", "approved": "ok",
    "NEEDS_APPROVAL": "attention", "needs_approval": "attention", "pending": "attention",
    "DENY": "danger", "denied": "danger", "error": "danger", "rejected": "danger",
    "REJECTED": "danger", "FAILED": "danger", "failed": "danger",
}


def pack_for(code: str) -> CountryPack:
    """Country pack for the display switch ('eu' or 'cn')."""
    return load_country_pack(code)


def money(pack: CountryPack, ledger_cents: int | None) -> str:
    """Format ledger cents in the selected display currency (CountryPack.display)."""
    if ledger_cents is None:
        return ""
    return pack.display(int(ledger_cents))


def badge(text: str) -> str:
    """Status pill as inline HTML (render with unsafe_allow_html; styled by ui/style.css)."""
    level = BADGE_LEVELS.get(text, "")
    return f'<span class="pill {level}">{html.escape(str(text).replace("_", " "))}</span>'


def company_name(account_name: str | None) -> str:
    """Derive the company name from the main account name ('Lumen Packaging main account')."""
    if not account_name:
        return "Runway demo company"
    return account_name.removesuffix(" main account")
