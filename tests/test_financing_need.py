"""financing_need: amount, needed_by, duration and recovery receipt computed in code."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlmodel import Session

from common.db import get_engine, reset_engines
from data.generate import generate
from forecast.engine import build_forecast, financing_need


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    path = tmp_path_factory.mktemp("fn") / "f.db"
    generate(path, seed=42, out_dir=path.parent / "out")
    yield path
    reset_engines()


@pytest.fixture()
def s(db):
    with Session(get_engine(db)) as session:
        yield session


def test_need_covers_worst_week_rounded_up(s):
    fc = build_forecast(s)
    need = financing_need(s)
    assert need["shortfall_weeks"] == fc.shortfall_weeks == [6, 10]
    assert need["worst_week"] == 6 and need["worst_closing_cents"] == fc.min_closing_cents
    assert need["amount_cents"] % 100_000 == 0
    assert 0 <= need["amount_cents"] + fc.min_closing_cents < 100_000
    assert need["needed_by"] == fc.weeks[5].start.isoformat()
    assert need["explanation"]


def test_recovery_is_first_receipt_after_last_shortfall_week(s):
    fc = build_forecast(s)
    need = financing_need(s)
    assert need["recovery_week"] == 11 and need["recovery_refs"]
    rec_date = next(ln.expected_date for ln in fc.weeks[10].lines if ln.source_refs == need["recovery_refs"])
    assert need["days_needed"] == max(30, (rec_date - date.fromisoformat(need["needed_by"])).days)


def test_scenario_changes_need(s):
    base = financing_need(s)
    moved = financing_need(s, [{"type": "move_payment", "payment_id": "PAY-0001", "new_date": "2026-12-14"}])
    assert moved["amount_cents"] < base["amount_cents"]
    assert moved["worst_week"] == 10


def test_no_shortfall(s):
    need = financing_need(s, [{"type": "inflow", "amount_cents": 5_000_000, "week": 1, "label": "x"}])
    assert need["amount_cents"] == 0 and need["needed_by"] is None and need["shortfall_weeks"] == []


def test_tool_and_outbox_tone(s):
    from agent.outbox import tone_for
    from agent.tools import HANDLERS, TOOL_SPECS
    assert any(t["function"]["name"] == "get_financing_need" for t in TOOL_SPECS)
    assert HANDLERS["get_financing_need"](s, {}, {})["amount_cents"] == financing_need(s)["amount_cents"]
    assert tone_for(30, 0) == "firm"  # never final on a first email
    assert tone_for(3, 0) == "friendly" and tone_for(3, 1) == "firm" and tone_for(3, 2) == "final"
    assert tone_for(20, 1) == "final"
