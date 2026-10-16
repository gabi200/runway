"""The agent can withdraw its own pending request (it only lowers risk), never a human two-person request."""

import pytest
from sqlmodel import Session

from agent.executor import approve, pending_approvals, request_human_action, run_tool
from agent.orchestrator import Agent
from common import db
from data.generate import generate


@pytest.fixture()
def env(tmp_path, monkeypatch):
    dbp = tmp_path / "w.db"
    monkeypatch.setenv("RUNWAY_DB", str(dbp))
    monkeypatch.setenv("RUNWAY_LLM", "offline")
    monkeypatch.delenv("RUNWAY_BANK_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    db.reset_engines()
    generate(db_path=dbp, out_dir=tmp_path / "sample")
    Agent(lambda: Session(db.get_engine())).monday_brief()
    yield tmp_path
    db.reset_engines()


def _reschedule_id(s):
    return next(r.id for r in pending_approvals(s) if r.tool_name == "reschedule_payment")


def test_withdrawn_request_leaves_the_queue_and_cannot_be_approved(env):
    with Session(db.get_engine()) as s:
        rid = _reschedule_id(s)
        res = run_tool(s, "withdraw_request", {"approval_id": rid, "reason": "user corrected"}, actor="agent")
        assert res.status == "ok" and res.data["status"] == "withdrawn"
        assert rid not in {r.id for r in pending_approvals(s)}
        assert approve(s, rid, "owner", "owner@demo").status == "error"


def test_withdraw_works_while_the_kill_switch_is_on(env):
    with Session(db.get_engine()) as s:
        db.set_setting(s, "kill_switch", "on")
        res = run_tool(s, "withdraw_request", {"approval_id": _reschedule_id(s), "reason": "stop"}, actor="agent")
    assert res.status == "ok"


def test_human_requests_cannot_be_withdrawn_by_the_agent(env):
    with Session(db.get_engine()) as s:
        db.set_setting(s, "kill_switch", "on")
        req = request_human_action(s, "kill_switch_off", {}, "owner", "owner@demo")
        res = run_tool(s, "withdraw_request", {"approval_id": req.approval_id, "reason": "x"}, actor="agent")
    assert res.status == "error" and "agent prepared" in res.reason


@pytest.mark.parametrize("args", [{"approval_id": "APR-9999", "reason": "x"}, {"approval_id": "APR-0001"}, {}])
def test_unknown_or_malformed_withdrawals_fail(env, args):
    with Session(db.get_engine()) as s:
        assert run_tool(s, "withdraw_request", args, actor="agent").status in {"error", "denied"}
