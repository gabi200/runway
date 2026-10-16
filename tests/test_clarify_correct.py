"""Offline planner: ask when an action is ambiguous; withdraw a pending request when the user corrects it."""

import pytest
from sqlmodel import Session

from agent.executor import approve, pending_approvals
from agent.orchestrator import Agent
from common import db
from data.generate import generate


@pytest.fixture()
def env(tmp_path, monkeypatch):
    dbp = tmp_path / "c.db"
    monkeypatch.setenv("RUNWAY_DB", str(dbp))
    monkeypatch.setenv("RUNWAY_LLM", "offline")
    monkeypatch.delenv("RUNWAY_BANK_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    db.reset_engines()
    generate(db_path=dbp, out_dir=tmp_path / "sample")
    yield tmp_path
    db.reset_engines()


def _agent():
    return Agent(lambda: Session(db.get_engine()))


def _sent(turn):
    return [s["args"]["invoice_id"] for s in turn.steps if s["tool"] == "send_payment_reminder"]


def test_ambiguous_reminder_asks_which_invoice(env):
    turn = _agent().chat("Send a reminder to the overdue customer")
    assert _sent(turn) == []
    assert "Which one should I remind" in turn.reply and "AR-1091" in turn.reply and "AR-1092" in turn.reply


def test_bare_invoice_id_answers_the_question(env):
    agent = _agent()
    agent.chat("Send a reminder to the overdue customer")
    assert _sent(agent.chat("AR-1092")) == ["AR-1092"]


def test_a_bare_id_without_a_question_gets_the_help_text(env):
    turn = _agent().chat("AR-1092")
    assert _sent(turn) == [] and turn.steps == []


def test_named_invoice_is_reminded_alone(env):
    assert _sent(_agent().chat("Send a reminder for AR-1091")) == ["AR-1091"]


def test_all_overdue_customers_needs_no_question(env):
    assert sorted(_sent(_agent().chat("Send reminders to all overdue customers"))) == ["AR-1091", "AR-1092"]


def test_the_monday_brief_never_stops_to_ask(env):
    assert len(_sent(_agent().monday_brief())) == 2
