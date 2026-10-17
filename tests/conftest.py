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
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("RUNWAY_LLM", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")  # harmless without a key



@pytest.fixture(autouse=True)
def no_live_anchor(tmp_path, monkeypatch):
    """A .env pointing at a real chain must never make a test publish or read an anchor."""
    for name in ("RUNWAY_ANCHOR", "RUNWAY_NOTARY_PATH", "RUNWAY_EVM_RPC_URL", "RUNWAY_EVM_PRIVATE_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RUNWAY_NOTARY_PATH", str(tmp_path / "notary.jsonl"))  # automatic publishing stays out of var/
    monkeypatch.setenv("RUNWAY_AUTO_ANCHOR", "off")  # tests that need automatic publishing turn it on


@pytest.fixture(autouse=True)
def private_var_dir(tmp_path, monkeypatch):
    """Keep signing keys, e-mails and PDFs out of the repository's var/ (fixtures may point it elsewhere)."""
    monkeypatch.setenv("RUNWAY_VAR_DIR", str(tmp_path / "var"))
