import pytest

from policy.engine import load_rules


@pytest.fixture
def unbacked_automatic(monkeypatch):
    """Let invoice-free payments run automatically, for tests of caps and idempotency mechanics."""
    monkeypatch.setitem(load_rules()["payment"], "automatic_requires_invoice", False)
