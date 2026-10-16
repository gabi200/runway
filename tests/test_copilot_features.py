"""Copilot features end to end with the OfflinePlanner: charts on request, invoice links, targeted reminders."""

from __future__ import annotations

import pytest
from sqlmodel import Session, select

from agent.invoices import find_invoice_refs, invoice_details, invoice_pdf
from agent.orchestrator import Agent
from common import db
from common.db import Invoice, OutboxEmail


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from data.generate import generate

    dbp = tmp_path / "e2e.db"
    monkeypatch.setenv("RUNWAY_DB", str(dbp))
    monkeypatch.setenv("RUNWAY_VAR_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("RUNWAY_LLM", "offline")
    db.reset_engines()
    summary = generate(db_path=dbp, out_dir=tmp_path / "sample")
    yield summary
    db.reset_engines()


def _agent() -> Agent:
    return Agent(lambda: Session(db.get_engine()))


def test_forecast_reply_offers_a_chart_and_yes_draws_it(env):
    agent = _agent()
    first = agent.chat("Explain the cash forecast and the shortfall weeks.")
    assert "Would you like a chart of the weekly balance?" in first.reply
    assert not any(s.get("chart") for s in first.steps)
    turn = agent.chat("Yes please")
    chart = next(s["chart"] for s in turn.steps if s["tool"] == "make_chart")
    assert chart["dataset"] == "forecast_balance" and len(chart["rows"]) == 13
    assert "Here is the chart" in turn.reply


def test_explicit_chart_request_picks_the_dataset(env):
    turn = _agent().chat("Show me a graph of the overdue customer invoices")
    step = next(s for s in turn.steps if s["tool"] == "make_chart")
    assert step["args"] == {"dataset": "receivables", "overdue_only": True} and step["status"] == "ok"
    assert {r["series"] for r in step["chart"]["rows"]} == {"Open amount"}


def test_declining_or_no_offer_draws_nothing(env):
    agent = _agent()
    agent.chat("Explain the cash forecast")
    assert not any(s["tool"] == "make_chart" for s in agent.chat("No thanks").steps)
    assert not any(s["tool"] == "make_chart" for s in _agent().chat("Yes").steps)
    assert not any(s["tool"] == "make_chart" for s in _agent().chat("Explain the forecast, don't show a chart").steps)


def test_reminder_for_a_named_invoice_reminds_only_that_one(env):
    target, other = env["overdue_invoice_ids"][:2]
    with Session(db.get_engine()) as s:
        number = s.get(Invoice, target).number
    turn = _agent().chat(f"Send a payment reminder for invoice {number} ({target}).")
    reminders = [s for s in turn.steps if s["tool"] == "send_payment_reminder"]
    assert [(s["args"]["invoice_id"], s["status"]) for s in reminders] == [(target, "ok")]
    with Session(db.get_engine()) as s:
        sent = s.exec(select(OutboxEmail).where(OutboxEmail.kind == "reminder")).all()
    assert [m.invoice_id for m in sent] == [target] and other != target


def test_invoice_refs_details_and_pdf(env):
    target = env["overdue_invoice_ids"][0]
    with Session(db.get_engine()) as s:
        inv = s.get(Invoice, target)
        text = f"Invoice {inv.number} is overdue; see also AP-9999 and {target}. Not an invoice: APR-0001."
        assert find_invoice_refs(s, text) == [target]
        d = invoice_details(s, target)
        assert d["can_remind"] and d["overdue"] and d["days_overdue"] > 0 and d["number"] == inv.number
        assert d["net_cents"] + d["vat_cents"] == inv.amount_cents
        pdf = invoice_pdf(s, target)
        assert pdf.startswith(b"%PDF") and invoice_details(s, "AR-0000") is None
        paid = s.exec(select(Invoice).where(Invoice.status == "paid")).first()
        assert not invoice_details(s, paid.id)["can_remind"]
