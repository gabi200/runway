"""A failing language model never crashes a turn: Runway finishes it with the offline planner."""

import pytest
from sqlmodel import Session

from agent.orchestrator import Agent
from audit import log as audit
from common import db
from data.generate import generate


@pytest.fixture()
def env(tmp_path, monkeypatch):
    dbp = tmp_path / "e2e.db"
    monkeypatch.setenv("RUNWAY_DB", str(dbp))
    monkeypatch.setenv("RUNWAY_LLM", "offline")
    monkeypatch.delenv("RUNWAY_BANK_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    db.reset_engines()
    generate(db_path=dbp, out_dir=tmp_path / "sample")
    yield tmp_path
    db.reset_engines()


class Down:
    def chat(self, messages, tools=None):
        raise TimeoutError("provider timed out")


class DiesAfterOneCall:
    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            return {"content": None, "tool_calls": [
                {"id": "m-1", "name": "get_forecast", "arguments": {"include_lines": True}}]}
        raise ConnectionError("connection reset")


def _agent(adapter):
    return Agent(lambda: Session(db.get_engine()), adapter=adapter)


def test_model_down_finishes_the_brief_offline(env):
    turn = _agent(Down()).monday_brief()
    assert turn.fallback
    assert turn.reply.startswith("> The language model did not answer (TimeoutError)")
    assert any(s["tool"] == "reschedule_payment" and s["status"] == "needs_approval" for s in turn.steps)


def test_fallback_mid_turn_does_not_repeat_tool_calls(env):
    turn = _agent(DiesAfterOneCall()).chat("How does my cash look?")
    assert turn.fallback
    assert [s["tool"] for s in turn.steps].count("get_forecast") == 1


def test_fallback_is_audited_with_the_error_type_only(env):
    _agent(Down()).chat("How does my cash look?")
    with Session(db.get_engine()) as s:
        rows = [e for e in audit.list_entries(s) if e.action == "llm_fallback"]
    assert len(rows) == 1
    assert "TimeoutError" in rows[0].args_json and "timed out" not in rows[0].args_json


def test_a_working_model_is_not_marked_as_fallback(env):
    from llm.offline import OfflinePlanner
    turn = _agent(OfflinePlanner()).chat("How does my cash look?")
    assert not turn.fallback and not turn.reply.startswith("> The language model")


def test_offline_planner_bugs_are_not_swallowed(env, monkeypatch):
    from llm.offline import OfflinePlanner

    def boom(self, messages, tools=None):
        raise RuntimeError("planner bug")

    monkeypatch.setattr(OfflinePlanner, "chat", boom)
    with pytest.raises(RuntimeError):
        _agent(OfflinePlanner()).chat("How does my cash look?")
