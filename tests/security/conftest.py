import pytest
from common import db
from data.generate import generate


@pytest.fixture
def stack(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNWAY_DB", str(tmp_path / "security.db"))
    monkeypatch.setenv("RUNWAY_VAR_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("RUNWAY_LLM", "offline")
    monkeypatch.setenv("RUNWAY_OWNER_EMAIL", "owner@lumen-packaging.example")
    monkeypatch.delenv("RUNWAY_BANK_URL", raising=False)
    db.reset_engines()
    summary = generate(out_dir=tmp_path / "sample")
    with db.session_scope() as session:
        yield session, tmp_path / "sample" / "docs", summary
    db.reset_engines()


@pytest.fixture
def pay_args():
    return {"payee_id": "PAYEE-INKWORKS", "amount_cents": 95_000,
            "execute_date": "2026-10-22"}
