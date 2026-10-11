"""Demo-only sign-in: per-role PIN from the environment, one identity per browser session.

NOT production authentication. PINs default to 1111 / 2222 and live in env vars
(RUNWAY_OWNER_PIN, RUNWAY_ACCOUNTANT_PIN). The point is that the "Acting as" role cannot be
flipped freely and that one browser session has one approver id, so it cannot fill both
roles of a four-eyes approval. A person holding both PINs could still open two browsers;
a real deployment needs SSO/MFA.
"""

from __future__ import annotations

import hmac
import json
import os
import time
import uuid

import streamlit as st

from audit import log as audit
from common import db
from common.db import session_scope

ROLES = ("owner", "accountant")
MAX_FAILURES = 5
LOCKOUT_SECONDS = 300


def pin_for(role: str) -> str:
    return os.environ.get(f"RUNWAY_{role.upper()}_PIN", {"owner": "1111", "accountant": "2222"}[role])


def check_pin(role: str, pin: str) -> bool:
    return hmac.compare_digest(str(pin).encode(), pin_for(role).encode())


def init() -> None:
    st.session_state.setdefault("sid", f"session-{uuid.uuid4().hex[:8]}")
    st.session_state.setdefault("role", None)


def signed_in() -> bool:
    return st.session_state.get("role") in ("owner", "accountant")


def approver_id() -> str:
    """Stable per browser session, independent of the role currently held."""
    return st.session_state.sid


def identity() -> str:
    return f"{st.session_state.role}@{st.session_state.sid}"


def _failures(role: str) -> tuple[int, float]:
    """(failure count, last failure epoch) for a role, persisted server-side."""
    with session_scope() as s:
        raw = db.get_setting(s, f"pin_failures:{role}")
    try:
        d = json.loads(raw) if raw else {}
        return int(d.get("n", 0)), float(d.get("ts", 0))
    except (ValueError, TypeError, AttributeError):
        return 0, 0.0


def locked_seconds(role: str, now: float | None = None) -> int:
    """Seconds of lockout left for this role (0 = not locked)."""
    n, ts = _failures(role)
    if n < MAX_FAILURES:
        return 0
    return max(0, int(LOCKOUT_SECONDS - ((now if now is not None else time.time()) - ts)))


def _record(role: str, ok: bool) -> None:
    with session_scope() as s:
        if ok:
            db.set_setting(s, f"pin_failures:{role}", json.dumps({"n": 0, "ts": 0}))
            return
        n, ts = _failures(role)
        if n >= MAX_FAILURES and time.time() - ts >= LOCKOUT_SECONDS:
            n = 0  # lockout expired: start counting again
        db.set_setting(s, f"pin_failures:{role}", json.dumps({"n": n + 1, "ts": time.time()}))
        audit.append(s, "anonymous", "sign_in_failed",
                     {"role": role, "session": st.session_state.get("sid"), "failures": n + 1},
                     "DENY", "Wrong PIN")


def sign_in(role: str, pin: str) -> str | None:
    """Return an error message, or None when the PIN matched and the role is now active."""
    left = locked_seconds(role)
    if left:
        return f"Too many wrong PINs for {role}. Try again in {left // 60 + 1} minute(s)."
    if not check_pin(role, pin):
        _record(role, False)
        return "Wrong PIN."
    _record(role, True)
    st.session_state.role = role
    return None


def sign_out() -> None:
    st.session_state.role = None


def require_login(what: str) -> bool:
    """For human-only actions: True if signed in, else show a hint and return False."""
    if signed_in():
        return True
    st.info(f"Sign in (top of the page) to {what}.")
    return False
