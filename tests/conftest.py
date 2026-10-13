import pytest

from policy.engine import load_rules


@pytest.fixture
def unbacked_automatic(monkeypatch):
    """Let invoice-free payments run automatically, for tests of caps and idempotency mechanics."""
    monkeypatch.setitem(load_rules()["payment"], "automatic_requires_invoice", False)


@pytest.fixture(autouse=True)
def no_live_llm(monkeypatch):
    """A real key loaded from .env must never make a test call the API."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

