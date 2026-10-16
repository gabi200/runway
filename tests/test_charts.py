"""make_chart: figures computed from the ledger, policy-checked like every other tool."""

from __future__ import annotations

import pytest
from sqlmodel import select

from agent.charts import DATASETS, build_chart
from agent.executor import run_tool
from common import db
from common.db import Invoice
from forecast.engine import build_forecast


@pytest.fixture()
def session(tmp_path, monkeypatch):
    from data.generate import generate

    monkeypatch.setenv("RUNWAY_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("RUNWAY_VAR_DIR", str(tmp_path / "var"))
    db.reset_engines()
    generate(db_path=tmp_path / "t.db", out_dir=tmp_path / "sample")
    with db.session_scope() as s:
        yield s
    db.reset_engines()


@pytest.mark.parametrize("dataset", sorted(DATASETS))
def test_every_dataset_builds_through_the_executor(session, dataset):
    res = run_tool(session, "make_chart", {"dataset": dataset})
    assert res.status == "ok", res.reason
    spec = res.data
    assert spec["dataset"] == dataset and spec["title"] and spec["kind"] in {"line", "bar", "grouped_bar"}
    assert all(isinstance(r["value_cents"], int) for r in spec["rows"])
    assert set(spec["x_order"]) == {r["x"] for r in spec["rows"]}


def test_forecast_balance_matches_the_forecast_engine(session):
    fc = build_forecast(session)
    rows = build_chart(session, "forecast_balance")["rows"]
    assert [r["value_cents"] for r in rows] == [w.closing_cents for w in fc.weeks]
    assert [r["x"] for r in rows if r["below_zero"]] == [f"W{w}" for w in fc.shortfall_weeks]


def test_forecast_balance_with_scenario_adds_a_series(session):
    change = {"type": "inflow", "amount_cents": 1_000_000, "week": 2, "label": "Test inflow"}
    rows = build_chart(session, "forecast_balance", scenario=[change])["rows"]
    assert {r["series"] for r in rows} == {"Baseline", "With scenario"}


def test_receivables_total_the_open_invoices(session):
    rows = build_chart(session, "receivables")["rows"]
    open_ar = session.exec(select(Invoice).where(Invoice.direction == "AR", Invoice.status == "open")).all()
    assert sum(r["value_cents"] for r in rows) == sum(i.amount_cents for i in open_ar)
    ageing = build_chart(session, "receivables_ageing")["rows"]
    assert sum(r["value_cents"] for r in ageing) == sum(i.amount_cents for i in open_ar)


def test_unknown_dataset_is_denied_by_policy(session):
    res = run_tool(session, "make_chart", {"dataset": "everything"})
    assert res.status == "denied"
    assert run_tool(session, "make_chart", {"dataset": "payables", "sql": "drop"}).status == "denied"
