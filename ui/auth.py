"""Demo-only sign-in: per-role PIN from the environment, one identity per browser session.

NOT production authentication. PINs default to 1111 / 2222 and live in env vars
(RUNWAY_OWNER_PIN, RUNWAY_ACCOUNTANT_PIN). The point is that the "Acting as" role cannot be
flipped freely and that one browser session has one approver id, so it cannot fill both
roles of a four-eyes approval. A person holding both PINs could still open two browsers;
a real deployment needs SSO/MFA.
"""

from __future__ import annotations

import hmac
import os
import uuid

import streamlit as st

ROLES = ("owner", "accountant")
MAX_FAILURES = 5


def pin_for(role: str) -> str:
    return os.environ.get(f"RUNWAY_{role.upper()}_PIN", {"owner": "1111", "accountant": "2222"}[role])


def check_pin(role: str, pin: str) -> bool:
    return hmac.compare_digest(str(pin).encode(), pin_for(role).encode())


def init() -> None:
    st.session_state.setdefault("sid", f"session-{uuid.uuid4().hex[:8]}")
    st.session_state.setdefault("role", None)
    st.session_state.setdefault("failures", 0)


def signed_in() -> bool:
    return st.session_state.get("role") in ("owner", "accountant")


def approver_id() -> str:
    """Stable per browser session, independent of the role currently held."""
    return st.session_state.sid


def identity() -> str:
    return f"{st.session_state.role}@{st.session_state.sid}"


def sign_in(role: str, pin: str) -> str | None:
    """Return an error message, or None when the PIN matched and the role is now active."""
    if st.session_state.failures >= MAX_FAILURES:
        return "Too many wrong PINs in this session. Reload the page to try again."
    if not check_pin(role, pin):
        st.session_state.failures += 1
        return "Wrong PIN."
    st.session_state.failures = 0
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
