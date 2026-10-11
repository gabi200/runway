"""Executor flow against the real policy, audit, bank, forecast and data modules."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from sqlmodel import select

from agent import executor
from agent.executor import approve, pending_approvals, reject, run_tool
from agent.tools import TOOL_SPECS
from audit.log import verify_chain
from common import db
from common.db import (AuditEntry, Document, FinancingApplication, Invoice, OutboxEmail, Payment)


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


def _pay_args(cents: int = 50_000, **kw) -> dict:
    d = {"payee_id": "PAYEE-INKWORKS", "amount_cents": cents,
         "execute_date": (date(2026, 10, 19) + timedelta(days=3)).isoformat()}
    return {**d, **kw}


def _decisions(s) -> list[str]:
    return [e.decision for e in s.exec(select(AuditEntry).order_by(AuditEntry.id))]


def test_tool_specs_expose_no_forbidden_capability():
    names = {t["function"]["name"] for t in TOOL_SPECS}
    assert len(names) == 15 and {"schedule_payment", "compare_financing", "flag_document"} <= names
    for bad in ("payee", "limit", "export", "kill"):
        assert not [n for n in names if bad in n]
    assert "iban" not in json.dumps(TOOL_SPECS).lower()


def test_read_tool_ok_and_audited(session):
    r = run_tool(session, "get_balances", {})
    assert r.status == "ok" and r.data["accounts"][0]["account_id"] == "ACC-MAIN"
    assert _decisions(session)[-2:] == ["ALLOW", "EXECUTED"]
    assert verify_chain(session).ok


def test_unknown_tool_and_iban_denied(session):
    assert run_tool(session, "add_payee", {"name": "x"}).status == "denied"
    r = run_tool(session, "schedule_payment", _pay_args(iban="DE00 0000"))
    assert r.status == "denied"
    assert session.exec(select(Payment).where(Payment.payee_id == "PAYEE-INKWORKS",
                                              Payment.amount_cents == 50_000)).first() is None


def test_small_payment_automatic_and_idempotent(session):
    r1 = run_tool(session, "schedule_payment", _pay_args(40_000))
    assert r1.status == "ok", r1
    r2 = run_tool(session, "schedule_payment", _pay_args(40_000))
    assert r2.status == "ok" and r2.data.get("id") == r1.data["id"]
    rows = session.exec(select(Payment).where(Payment.amount_cents == 40_000)).all()
    assert len(rows) == 1 and rows[0].automatic is True


def test_unverified_payee_denied(session):
    r = run_tool(session, "schedule_payment", _pay_args(payee_id="PAYEE-NEWVENDOR"))
    assert r.status == "denied"


def test_large_payment_needs_owner_then_executes(session):
    r = run_tool(session, "schedule_payment", _pay_args(480_000))
    assert r.status == "needs_approval" and r.approval_id.startswith("APR-")
    assert run_tool(session, "schedule_payment", _pay_args(480_000)).approval_id == r.approval_id
    assert [a.id for a in pending_approvals(session)] == [r.approval_id]
    assert approve(session, r.approval_id, "intern").status == "denied"
    ok = approve(session, r.approval_id, "owner")
    assert ok.status == "ok", ok
    pay = session.exec(select(Payment).where(Payment.amount_cents == 480_000)).one()
    assert pay.automatic is False
    assert pending_approvals(session) == []
    assert approve(session, r.approval_id, "owner").status == "error"  # no double execution
    actors = [e.actor for e in session.exec(select(AuditEntry).order_by(AuditEntry.id))]
    assert "owner" in actors and verify_chain(session).ok


def test_above_5000_needs_both_roles(session):
    r = run_tool(session, "schedule_payment", _pay_args(700_000))
    assert r.status == "needs_approval"
    first = approve(session, r.approval_id, "owner")
    assert first.status == "needs_approval" and "accountant" in first.reason
    assert session.exec(select(Payment).where(Payment.amount_cents == 700_000)).first() is None
    assert approve(session, r.approval_id, "accountant").status == "ok"
    assert session.exec(select(Payment).where(Payment.amount_cents == 700_000)).one()


def test_reject(session):
    r = run_tool(session, "schedule_payment", _pay_args(480_000))
    assert reject(session, r.approval_id, "owner", "no").status == "ok"
    assert approve(session, r.approval_id, "owner").status == "error"
    assert session.exec(select(Payment).where(Payment.amount_cents == 480_000)).first() is None
    assert "REJECTED" in _decisions(session)


def test_flag_document_alerts_owner_and_blocks_payment(session):
    session.add(Document(id="DOC-T1", filename="inv.pdf", kind="invoice", sender="x@evil.example",
                         content_text="bank details changed"))
    session.commit()
    r = run_tool(session, "flag_document", {"document_id": "DOC-T1", "reason": "IBAN change"})
    assert r.status == "ok"
    session.expire_all()
    assert session.get(Document, "DOC-T1").flagged is True
    mail = session.exec(select(OutboxEmail).where(OutboxEmail.kind == "fraud_alert")).one()
    assert "DOC-T1" in mail.subject
    blocked = run_tool(session, "schedule_payment", _pay_args(document_id="DOC-T1"))
    assert blocked.status == "denied"
    assert run_tool(session, "flag_document", {"document_id": "NOPE", "reason": "x"}).status == "error"


def test_reminder_writes_outbox_and_eml(session, tmp_path):
    today = db.get_today(session)
    inv = session.exec(select(Invoice).where(Invoice.direction == "AR", Invoice.status == "open",
                                             Invoice.due_date < today)).first()
    r = run_tool(session, "send_payment_reminder", {"invoice_id": inv.id})
    assert r.status == "ok", r
    days = (today - inv.due_date).days
    assert r.data["tone"] == ("friendly" if days <= 7 else "firm" if days <= 14 else "final")
    assert (tmp_path / "var" / "outbox" / f"{r.data['email_id']}.eml").exists()
    assert session.get(OutboxEmail, r.data["email_id"]).invoice_id == inv.id
    assert run_tool(session, "send_payment_reminder", {"invoice_id": inv.id}).status == "denied"  # 1 per 7 days


def test_draft_email_does_not_send(session):
    inv = session.exec(select(Invoice).where(Invoice.direction == "AR")).first()
    n = len(session.exec(select(OutboxEmail)).all())
    r = run_tool(session, "draft_email", {"purpose": "payment_reminder", "invoice_id": inv.id})
    assert r.status == "ok" and r.data["sent"] is False and inv.number in r.data["body"]
    assert len(session.exec(select(OutboxEmail)).all()) == n


def test_forecast_tools_and_financing(session):
    f = run_tool(session, "get_forecast", {})
    assert f.status == "ok" and len(f.data["weeks"]) == 13 and "lines" not in f.data["weeks"][0]
    s = run_tool(session, "run_scenario", {"changes": [{"type": "delay_customer",
                                                        "counterparty_id": "CUST-BRIGHTFIELD", "days": 30}]})
    assert s.status == "ok" and "min_closing_delta_cents" in s.data
    e = run_tool(session, "explain_figure", {"week": 6})
    assert e.status == "ok"
    c = run_tool(session, "compare_financing", {"amount_cents": 4_000_000, "needed_by": "2026-11-23",
                                                "days_needed": 30})
    assert c.status == "ok" and c.data["recommended"]


def test_reschedule_needs_approval_and_moves_payment(session):
    pay = session.exec(select(Payment).where(Payment.status == "scheduled",
                                             Payment.invoice_id.is_not(None))).all()
    target = next(p for p in pay if (inv := session.get(Invoice, p.invoice_id)) and not inv.critical
                  and inv.due_date > p.execute_date)
    due = session.get(Invoice, target.invoice_id).due_date
    r = run_tool(session, "reschedule_payment", {"payment_id": target.id, "new_date": due.isoformat()})
    assert r.status == "needs_approval"
    assert approve(session, r.approval_id, "owner").status == "ok"
    session.expire_all()
    assert session.get(Payment, target.id).execute_date == due
    late = run_tool(session, "reschedule_payment", {"payment_id": target.id,
                                                    "new_date": (due + timedelta(days=5)).isoformat()})
    assert late.status == "denied"


def test_financing_application_pdf(session, tmp_path):
    r = run_tool(session, "prepare_financing_application",
                 {"product_id": "overdraft", "amount_cents": 400_000, "needed_by": "2026-11-20"})
    assert r.status == "needs_approval"
    ok = approve(session, r.approval_id, "owner")
    assert ok.status == "ok", ok
    row = session.exec(select(FinancingApplication)).one()
    pdf = (tmp_path / "var" / "applications" / f"{row.id}.pdf")
    assert pdf.exists() and pdf.read_bytes().startswith(b"%PDF")
    assert row.pdf_path == str(pdf)


def test_failed_execution_is_audited(session, monkeypatch):
    monkeypatch.setitem(executor.HANDLERS, "get_balances", lambda s, a, c: 1 / 0)
    r = run_tool(session, "get_balances", {})
    assert r.status == "error" and _decisions(session)[-1] == "FAILED"
