"""The judge task pack passes offline, and the checker reports what a run missed."""

import pytest

from common import db
from scripts.run_tasks import check, load_tasks, run_task

OFFLINE = [t for t in load_tasks() if not t.get("live_only")]


@pytest.mark.parametrize("task", OFFLINE, ids=[t["id"] for t in OFFLINE])
def test_task_passes_offline(task, tmp_path, monkeypatch):
    monkeypatch.setenv("RUNWAY_DB", str(tmp_path / "unused.db"))  # restored after run_task overrides it
    monkeypatch.delenv("RUNWAY_BANK_URL", raising=False)
    try:
        result = run_task(task, "offline", tmp_path)
    finally:
        db.reset_engines()
    assert result["failures"] == [], result["reply"]


def test_check_reports_a_missing_tool():
    assert check({"expect": {"ok": ["get_forecast"]}}, [], "", []) == ["get_forecast not ok"]


def test_check_reports_a_forbidden_call_and_a_leak():
    steps = [{"tool": "schedule_payment", "status": "denied"}]
    task = {"expect": {"not_called": ["schedule_payment"], "reply_lacks_regex": [r"DE\d{2}"]}}
    assert len(check(task, steps, "pay DE44500105175407324931", [])) == 2


def test_task_ids_are_unique():
    ids = [t["id"] for t in load_tasks()]
    assert len(ids) == len(set(ids))
