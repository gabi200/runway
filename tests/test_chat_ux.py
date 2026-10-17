"""The copilot knows the app and its ids: no private notes in replies, payee ids on supplier invoices, clear
reasons when a payee is wrong, a scenario for paying a supplier invoice on another day, and page guidance."""

from datetime import date

import pytest
from sqlmodel import Session, select

from agent.executor import run_tool
from agent.orchestrator import Agent
from agent.tools import list_payables
from common import db
from data.generate import generate
from forecast.engine import build_forecast
from llm.offline import OfflinePlanner
from policy import engine as policy


@pytest.fixture()
def s(tmp_path, monkeypatch):
    dbp = tmp_path / "c.db"
    monkeypatch.setenv("RUNWAY_DB", str(dbp))
    monkeypatch.delenv("RUNWAY_BANK_URL", raising=False)
    db.reset_engines()
    generate(db_path=dbp, out_dir=tmp_path / "sample")
    with Session(db.get_engine()) as session:
        yield session
    db.reset_engines()


class Scripted:
    def __init__(self, *replies):
        self.replies = list(replies)

    def chat(self, messages, tools):
        return {"content": self.replies.pop(0), "tool_calls": []}


def test_a_copied_tool_call_note_never_reaches_the_user(s):
    agent = Agent(lambda: Session(db.get_engine()), Scripted(
        'Done.\n\n[Tool calls made in that turn: schedule_payment({"payee_id": "X"}) -> denied]\n\nAnything else?'))
    assert agent.chat("pay it").reply == "Done.\n\nAnything else?"


def test_supplier_invoices_name_their_payee(s):
    rows = {r["number"]: r for r in list_payables(s, {}, {})}
    assert rows["CM-2026-0919"]["payee_id"] == "PAYEE-CARTONIA" and rows["CM-2026-0919"]["payee_verified"] is True


def test_a_wrong_payee_id_and_an_unverified_payee_get_different_reasons(s):
    args = {"amount_cents": 100, "execute_date": "2026-11-18"}
    assert policy.evaluate(s, "schedule_payment", {**args, "payee_id": "SUP-CARTONIA"}).reason.startswith("Unknown payee id")
    unverified = policy.evaluate(s, "schedule_payment", {**args, "payee_id": "PAYEE-NEWVENDOR"}).reason
    assert unverified.startswith("Payee is not verified yet") and "Payees page" in unverified


def test_paying_a_supplier_invoice_earlier_is_a_scenario(s):
    inv = s.exec(select(db.Invoice).where(db.Invoice.number == "CM-2026-0919")).one()
    today = db.get_today(s).isoformat()
    change = {"type": "move_invoice", "invoice_id": inv.id, "new_date": today, "label": "pay now"}
    assert policy.evaluate(s, "run_scenario", {"changes": [change]}).outcome == "ALLOW"  # a label is fine
    base, scen = build_forecast(s), build_forecast(s, scenario=[change])
    line = next(ln for w in scen.weeks for ln in w.lines if inv.id in ln.source_refs)
    assert line.expected_date == date.fromisoformat(today) and "scenario: paid" in line.note
    assert (base.min_running_cents, scen.min_running_cents) == (-2_930_332, -5_545_166)  # paid before receipts arrive
    res = run_tool(s, "run_scenario", {"changes": [{"type": "move_payment", "payment_id": inv.id,
                                                    "new_date": today}]}, actor="agent")
    assert "use move_invoice" in res.reason  # an invoice id given as a payment id says what to do instead


def test_offline_planner_points_to_the_page():
    plan = OfflinePlanner()
    reply = plan.chat([{"role": "user", "content": "Where do I verify the payee's bank details?"}])["content"]
    assert reply.startswith("Go to **Payees**")
    assert "Go to" not in plan.chat([{"role": "user", "content": "Hello"}])["content"]


def test_a_payment_request_waiting_for_approval_counts_as_prepared():
    from agent.orchestrator import unbacked_claims
    waiting = [{"tool": "schedule_payment", "status": "needs_approval"}]
    assert unbacked_claims("I've prepared the payment; it waits for approval.", waiting) == []
    assert unbacked_claims("I've prepared the payment.", []) == ["prepared"]  # still caught when nothing ran
