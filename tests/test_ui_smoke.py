"""Smoke test: click through the Streamlit UI on a temp DB, offline LLM."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from common import db

APP = str(Path(__file__).resolve().parent.parent / "ui" / "app.py")


@pytest.fixture()
def at(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNWAY_DB", str(tmp_path / "ui.db"))
    monkeypatch.setenv("RUNWAY_VAR_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("RUNWAY_SAMPLE_DIR", str(tmp_path / "sample"))
    monkeypatch.setenv("RUNWAY_LLM", "offline")
    db.reset_engines()
    app = AppTest.from_file(APP, default_timeout=60).run()
    yield app
    db.reset_engines()


def goto(at, page):
    at.radio(key="page").set_value(page).run()
    assert not at.exception, [e.value for e in at.exception]


def test_all_pages_render(at):
    assert not at.exception
    for page in ["Onboarding", "Monday brief & chat", "Forecast", "Approval queue", "Documents",
                 "Payees (humans only)", "Mock bank", "Test inbox", "Audit log"]:
        goto(at, page)


def test_brief_approvals_kill_switch_and_audit(at):
    goto(at, "Monday brief & chat")
    next(b for b in at.button if b.label == "Run Monday brief").click().run()
    assert not at.exception
    goto(at, "Approval queue")
    goto(at, "Test inbox")
    goto(at, "Audit log")
    next(b for b in at.button if b.label == "Verify chain").click().run()
    assert any("intact" in s.value for s in at.success)
    at.toggle(key="kill").set_value(True).run()
    assert not at.exception
    with db.session_scope() as s:
        assert db.kill_switch_on(s)


def test_forecast_and_currency_switch(at):
    goto(at, "Forecast")
    at.radio(key="pack").set_value("cn").run()
    assert not at.exception
    assert any("¥" in str(df.value.to_dict()) for df in at.dataframe)


def test_documents_sample_ingest(at):
    goto(at, "Documents")
    btn = next(b for b in at.button if b.label == "Ingest owner_email_A2.txt")
    btn.click().run()
    assert not at.exception


def test_approve_clear_flag_payee_advance_scenario(at):
    goto(at, "Monday brief & chat")
    next(b for b in at.button if b.label == "Run Monday brief").click().run()
    goto(at, "Approval queue")
    approve = [b for b in at.button if b.label == "Approve"]
    if approve:
        approve[0].click().run()
        assert not at.exception
    goto(at, "Documents")
    next(b for b in at.button if b.label == "Ingest owner_email_A2.txt").click().run()
    goto(at, "Documents")
    clear = [b for b in at.button if b.label.startswith("Clear flag")]
    assert clear, "A2 email should be flagged"
    clear[0].click().run()
    assert not at.exception
    with db.session_scope() as s:
        from sqlmodel import select
        assert any(d.cleared for d in s.exec(select(db.Document)))
    goto(at, "Payees (humans only)")
    goto(at, "Mock bank")
    next(b for b in at.button if b.label == "Advance").click().run()
    assert not at.exception
    goto(at, "Forecast")
    next(b for b in at.button if b.label == "Add delay").click().run()
    assert not at.exception
    assert at.metric
