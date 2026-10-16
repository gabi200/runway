"""A10: one signed-in person cannot switch the agent back on or move the clock (which resets caps)."""

from datetime import timedelta

import pytest
from sqlmodel import Session

from agent.executor import approve, request_human_action, run_tool
from common import db
from data.generate import generate


@pytest.fixture()
def env(tmp_path, monkeypatch):
    dbp = tmp_path / "a10.db"
    monkeypatch.setenv("RUNWAY_DB", str(dbp))
    monkeypatch.delenv("RUNWAY_BANK_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    db.reset_engines()
    generate(db_path=dbp, out_dir=tmp_path / "sample")
    yield tmp_path
    db.reset_engines()


def _fresh():
    return Session(db.get_engine())


def test_turning_the_kill_switch_off_needs_both_roles(env):
    with _fresh() as s:
        db.set_setting(s, "kill_switch", "on")
        first = request_human_action(s, "kill_switch_off", {}, "owner", "owner@demo")
    assert first.status == "needs_approval"
    with _fresh() as s:
        assert db.kill_switch_on(s)
        second = approve(s, first.approval_id, "accountant", "accountant@demo")
    assert second.status == "ok"
    with _fresh() as s:
        assert not db.kill_switch_on(s)


def test_one_person_cannot_fill_both_roles_for_kill_switch_off(env):
    with _fresh() as s:
        db.set_setting(s, "kill_switch", "on")
        first = request_human_action(s, "kill_switch_off", {}, "owner", "same-person")
        second = approve(s, first.approval_id, "accountant", "same-person")
    assert second.status == "denied"
    with _fresh() as s:
        assert db.kill_switch_on(s)


def test_kill_switch_off_is_refused_when_already_off(env):
    with _fresh() as s:
        res = request_human_action(s, "kill_switch_off", {}, "owner", "owner@demo")
    assert res.status == "denied"


def test_advancing_the_clock_needs_both_roles(env):
    with _fresh() as s:
        today = db.get_today(s)
        first = request_human_action(s, "advance_clock", {"days": 7}, "owner", "owner@demo")
    assert first.status == "needs_approval"
    with _fresh() as s:
        assert db.get_today(s) == today
        assert approve(s, first.approval_id, "accountant", "accountant@demo").status == "ok"
    with _fresh() as s:
        assert db.get_today(s) == today + timedelta(days=7)


@pytest.mark.parametrize("days", [0, 121, "7", None, True])
def test_clock_days_are_validated(env, days):
    with _fresh() as s:
        res = request_human_action(s, "advance_clock", {"days": days}, "owner", "owner@demo")
    assert res.status == "denied"


@pytest.mark.parametrize("action,args", [("kill_switch_off", {}), ("advance_clock", {"days": 7})])
def test_the_agent_cannot_reach_these_actions(env, action, args):
    with _fresh() as s:
        db.set_setting(s, "kill_switch", "on")
        assert run_tool(s, action, args, actor="agent").status == "denied"
        assert request_human_action(s, action, args, "agent").status == "denied"
