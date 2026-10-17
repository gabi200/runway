"""Executor flow against the real policy, audit, bank, forecast and data modules."""

from __future__ import annotations

import json
from pathlib import Path
from datetime import date, timedelta

import pytest
from sqlmodel import select

from agent import executor
from agent.executor import approve, pending_approvals, reject, run_tool
from agent.tools import TOOL_SPECS
from audit.log import verify_chain
from common import db
from common.db import (AuditEntry, Document, FinancingApplication, Invoice, OutboxEmail, Payment)

pytestmark = pytest.mark.usefixtures("unbacked_automatic")


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
    assert len(names) == 18 and {"schedule_payment", "compare_financing", "flag_document"} <= names
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
    # First reminder for this invoice: never a final notice, whatever the days overdue.
    assert r.data["tone"] == ("friendly" if days <= 7 else "firm")
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


def test_application_pdf_text_stays_on_the_page(session):
    """The Terms cell was one unwrapped line that ran about 20 mm off the A4 page."""
    from pypdf import PdfReader
    from reportlab.pdfbase.pdfmetrics import stringWidth

    from agent.pdf import prepare_application

    row = prepare_application(session, "supply_chain_finance", 1_000_000, date(2026, 11, 23), 38)
    page = PdfReader(row.pdf_path).pages[0]
    ends = []
    page.extract_text(visitor_text=lambda text, cm, tm, font, size: text.strip() and ends.append(
        tm[4] + stringWidth(text, "Helvetica", size)))
    assert ends and max(ends) <= float(page.mediabox.width) - 20


@pytest.mark.filterwarnings("ignore::sqlalchemy.exc.SAWarning")  # one session plays both requests
def test_id_collision_never_overwrites_another_application(session, monkeypatch):
    """Two concurrent applications got the same FIN id; the loser's PDF replaced the winner's."""
    import agent.pdf as pdf_mod

    first = pdf_mod.prepare_application(session, "overdraft", 400_000, date(2026, 11, 20))
    before = Path(first.pdf_path).read_bytes()
    monkeypatch.setattr(pdf_mod, "next_id", lambda *a, **k: first.id)  # the other request claimed it first
    with pytest.raises(Exception):
        pdf_mod.prepare_application(session, "overdraft", 900_000, date(2026, 11, 20))
    session.rollback()
    assert Path(first.pdf_path).read_bytes() == before
    assert list(Path(first.pdf_path).parent.iterdir()) == [Path(first.pdf_path)]


def test_failed_execution_is_audited(session, monkeypatch):
    monkeypatch.setitem(executor.HANDLERS, "get_balances", lambda s, a, c: 1 / 0)
    r = run_tool(session, "get_balances", {})
    assert r.status == "error" and _decisions(session)[-1] == "FAILED"


@pytest.mark.parametrize("explicit", [False, True])
def test_replay_before_daily_caps(session, explicit):
    args = _pay_args(95_000, **({"idempotency_key": "replay-cap"} if explicit else {}))
    first = run_tool(session, "schedule_payment", args)
    assert first.status == "ok"
    second = run_tool(session, "schedule_payment", args)
    assert second.status == "ok" and second.data["idempotent_replay"] is True
    assert second.data["id"] == first.data["id"]
    assert _decisions(session)[-1] == "IDEMPOTENT_REPLAY"
    assert pending_approvals(session) == []


@pytest.mark.parametrize("change", [{"amount_cents": 94_000}, {"currency": "USD"},
                                   {"iban": "DE00"}, {"payee_id": "PAYEE-CARTONIA"},
                                   {"execute_date": "2026-10-23"}, {"invoice_id": "AP-NOPE"},
                                   {"document_id": "DOC-NOPE"}])
def test_reused_key_different_body_denied(session, change):
    args = _pay_args(95_000, idempotency_key="conflict")
    assert run_tool(session, "schedule_payment", args).status == "ok"
    assert run_tool(session, "schedule_payment", {**args, **change}).status == "denied"
    assert len(session.exec(select(Payment).where(Payment.idempotency_key == "conflict")).all()) == 1


def test_replay_after_execution_uses_original_request(session):
    from bank.client import BankClient, bank_token

    args = _pay_args(95_000, idempotency_key="after-execution")
    first = run_tool(session, "schedule_payment", args)
    assert first.status == "ok"
    session.commit()
    with BankClient(token=bank_token("human")) as bank:
        bank.advance(4)
    replay = run_tool(session, "schedule_payment", args)
    assert replay.status == "ok" and replay.data["idempotent_replay"]
    assert replay.data["id"] == first.data["id"] and replay.data["status"] == "executed"
    assert _decisions(session)[-1] == "IDEMPOTENT_REPLAY"


def test_replay_after_human_approved_payment(session):
    args = _pay_args(980_000, idempotency_key="approved-replay")
    request = run_tool(session, "schedule_payment", args)
    assert approve(session, request.approval_id, "owner").status == "needs_approval"
    original = approve(session, request.approval_id, "accountant")
    assert original.status == "ok"
    replay = run_tool(session, "schedule_payment", args)
    assert replay.status == "ok" and replay.data["idempotent_replay"]
    assert replay.data["id"] == original.data["id"]
    assert pending_approvals(session) == []


@pytest.mark.parametrize("key", [[], {}, 123, ""])
def test_malformed_idempotency_key_denied(session, key):
    assert run_tool(session, "schedule_payment", _pay_args(idempotency_key=key)).status == "denied"


def test_replay_after_reschedule_compares_original_body(session):
    from bank.client import BankClient

    args = _pay_args(95_000, idempotency_key="rescheduled-replay")
    first = run_tool(session, "schedule_payment", args)
    assert first.status == "ok"
    session.commit()
    with BankClient() as bank:
        bank.reschedule_payment(first.data["id"], date(2026, 10, 24))
    replay = run_tool(session, "schedule_payment", args)
    assert replay.status == "ok" and replay.data["idempotent_replay"]
    assert replay.data["execute_date"] == "2026-10-24"
    conflict = run_tool(session, "schedule_payment", {**args, "execute_date": "2026-10-24"})
    assert conflict.status == "denied" and "409" in conflict.reason


@pytest.mark.parametrize("role", ["owner", "accountant"])
def test_human_clears_document_flag_and_audits(session, role):
    session.add(Document(id="DOC-CLEAR", filename="invoice.pdf", kind="invoice",
                         flagged=True, flag_reason="Bank details changed"))
    session.commit()
    result = executor.clear_document_flag(session, "DOC-CLEAR", role, "Confirmed with supplier")
    assert result.status == "ok"
    session.expire_all()
    document = session.get(Document, "DOC-CLEAR")
    assert document.cleared is True and document.flagged is True
    assert document.flag_reason == "Bank details changed"
    entry = session.exec(select(AuditEntry).order_by(AuditEntry.id.desc())).first()
    assert (entry.actor, entry.action, entry.decision) == (role, "clear_document_flag", "HUMAN_ACTION")
    assert json.loads(entry.args_json) == {"document_id": "DOC-CLEAR", "note": "Confirmed with supplier"}
    assert verify_chain(session).ok


@pytest.mark.parametrize("role", ["agent", "system", "intern", ""])
def test_clear_document_flag_rejects_nonhuman_roles(session, role):
    session.add(Document(id="DOC-CLEAR", filename="invoice.pdf", kind="invoice", flagged=True))
    session.commit()
    assert executor.clear_document_flag(session, "DOC-CLEAR", role).status == "error"
    session.expire_all()
    assert session.get(Document, "DOC-CLEAR").cleared is False


def test_clear_document_flag_missing_document(session):
    assert executor.clear_document_flag(session, "MISSING", "owner").status == "error"


def test_clear_document_flag_is_not_an_agent_tool(session):
    assert "clear_document_flag" not in {tool["function"]["name"] for tool in TOOL_SPECS}
    session.add(Document(id="DOC-CLEAR", filename="invoice.pdf", kind="invoice", flagged=True))
    session.commit()
    result = run_tool(session, "clear_document_flag", {"document_id": "DOC-CLEAR", "role": "owner"})
    assert result.status == "denied"
    session.expire_all()
    assert session.get(Document, "DOC-CLEAR").cleared is False
    assert _decisions(session)[-1] == "DENY"


def test_clear_document_flag_rolls_back_when_audit_fails(session):
    from common.db import Setting

    session.add(Document(id="DOC-CLEAR", filename="invoice.pdf", kind="invoice", flagged=True))
    session.commit()
    run_tool(session, "get_balances", {})
    head = session.get(Setting, "audit_head")
    head.value = "corrupted"
    session.add(head)
    session.commit()
    with pytest.raises(ValueError):
        executor.clear_document_flag(session, "DOC-CLEAR", "owner")
    assert session.get(Document, "DOC-CLEAR").cleared is False


def test_agent_cannot_reject(session):
    request = run_tool(session, "schedule_payment", _pay_args(980000))
    assert reject(session, request.approval_id, "agent").status == "denied"
    assert pending_approvals(session)[0].status == "pending"


def test_same_person_cannot_supply_two_approval_roles(session):
    request = run_tool(session, "schedule_payment", _pay_args(980000))
    assert approve(session, request.approval_id, "owner", approver_id="alice").status == "needs_approval"
    assert approve(session, request.approval_id, "accountant", approver_id="alice").status == "denied"
    assert approve(session, request.approval_id, "accountant", approver_id="bob").status == "ok"


def test_kill_switch_blocks_payment_replay(session):
    args = _pay_args(95000)
    assert run_tool(session, "schedule_payment", args).status == "ok"
    db.set_setting(session, "kill_switch", "on")
    assert run_tool(session, "schedule_payment", args).status == "denied"


@pytest.mark.parametrize("optional", [{}, {"amount_cents": None, "needed_by": None, "days_needed": None}])
def test_financing_defaults_evaluated_and_audited(session, optional):
    from forecast.engine import financing_need
    from common.db import ApprovalRequest

    need = financing_need(session)
    result = run_tool(session, "prepare_financing_application", {"product_id": "overdraft", **optional})
    entry = session.exec(select(AuditEntry).order_by(AuditEntry.id.desc())).first()
    effective = json.loads(entry.args_json)
    assert {key: effective[key] for key in ("amount_cents", "needed_by", "days_needed")} == {
        key: need[key] for key in ("amount_cents", "needed_by", "days_needed")}
    if result.status == "needs_approval":
        assert json.loads(session.get(ApprovalRequest, result.approval_id).args_json) == effective
    else:
        assert result.status == "denied"


def test_legacy_approval_roles_are_read_and_upgraded(session):
    from common.db import ApprovalRequest

    request = run_tool(session, "prepare_financing_application", {
        "product_id": "overdraft", "amount_cents": 600000, "needed_by": "2026-11-20"})
    row = session.get(ApprovalRequest, request.approval_id)
    row.approvals_json = json.dumps(["owner"])
    session.add(row)
    session.commit()
    result = approve(session, row.id, "accountant", approver_id="bob")
    assert result.status == "ok"
    session.refresh(row)
    assert [(e["role"], e["approver_id"]) for e in json.loads(row.approvals_json)] == [
        ("owner", "owner"), ("accountant", "bob")]
    assert "signature" in json.loads(row.approvals_json)[1]  # new approvals are signed; legacy ones are not


def test_same_approver_financing_roles(session):
    request = run_tool(session, "prepare_financing_application", {
        "product_id": "overdraft", "amount_cents": 600000, "needed_by": "2026-11-20"})
    assert approve(session, request.approval_id, "owner", approver_id="alice").status == "needs_approval"
    assert approve(session, request.approval_id, "accountant", approver_id="alice").status == "denied"
    assert approve(session, request.approval_id, "accountant", approver_id="bob").status == "ok"


def test_kill_switch_blocks_existing_replay_without_bank_call(session):
    from agent.tools import idempotency_key

    args = _pay_args(95000)
    session.add(Payment(id="REPLAY", idempotency_key=idempotency_key(args),
        payee_id=args["payee_id"], amount_cents=args["amount_cents"], currency="EUR",
        execute_date=date.fromisoformat(args["execute_date"])))
    session.commit()
    db.set_setting(session, "kill_switch", "on")
    assert run_tool(session, "schedule_payment", args).status == "denied"
    assert _decisions(session)[-1] == "DENY"


@pytest.mark.parametrize("decision", ["approve", "reject", "policy_denial"])
def test_approval_dates_follow_simulated_clock(session, monkeypatch, decision):
    request = run_tool(session, "schedule_payment", _pay_args(200000))
    req = session.get(db.ApprovalRequest, request.approval_id)
    assert req.created_at.date() == db.get_today(session)
    db.set_setting(session, "today", "2026-10-20")
    monkeypatch.setitem(executor.HANDLERS, "schedule_payment", lambda *args: {"executed": True})
    if decision == "reject":
        reject(session, req.id, "owner")
    else:
        if decision == "policy_denial":  # a lasting denial (the kill switch keeps requests pending)
            payee = session.get(db.Payee, json.loads(req.args_json)["payee_id"])
            payee.verified = False
            session.add(payee)
            session.commit()
        approve(session, req.id, "owner")
    assert req.decided_at.date() == db.get_today(session)


@pytest.mark.parametrize("identity", ["", "   ", 123])
def test_invalid_approver_identity_is_audited(session, identity):
    request = run_tool(session, "schedule_payment", _pay_args(200000))
    assert approve(session, request.approval_id, "owner", identity).status == "denied"
    assert _decisions(session)[-1] == "DENY"
    assert verify_chain(session).ok


def test_concurrent_final_approval_executes_once(session, monkeypatch):
    """Two clicks that both saw the request pending: only one may execute it."""
    req = run_tool(session, "reschedule_payment", {"payment_id": "PAY-0001", "new_date": "2026-12-01"})
    assert req.status == "needs_approval"
    with db.session_scope() as other:
        stale = other.get(executor.ApprovalRequest, req.approval_id)  # loaded before the first click lands
        assert approve(session, req.approval_id, "owner", "owner-1").status == "ok"
        monkeypatch.setattr(executor, "_get_pending", lambda *a, **k: stale)
        second = approve(other, req.approval_id, "owner", "owner-2")
    assert second.status != "ok"
    executed = [e for e in session.exec(select(AuditEntry).where(AuditEntry.action == "reschedule_payment"))
                if e.decision == "EXECUTED"]
    assert len(executed) == 1


def test_kill_switch_at_approval_keeps_request_pending(session):
    req = run_tool(session, "reschedule_payment", {"payment_id": "PAY-0001", "new_date": "2026-12-01"})
    db.set_setting(session, "kill_switch", "on")
    blocked = approve(session, req.approval_id, "owner", "owner-1")
    assert blocked.status == "denied" and "stays pending" in blocked.reason
    assert [r.id for r in pending_approvals(session)] == [req.approval_id]
    db.set_setting(session, "kill_switch", "off")
    assert approve(session, req.approval_id, "owner", "owner-1").status == "ok"


def _advance(s, days: int) -> None:
    db.set_setting(s, "today", (db.get_today(s) + timedelta(days=days)).isoformat())


def test_pending_approval_expires_after_seven_days(session):
    """A parked request stayed approvable forever (known risk 20)."""
    stale = run_tool(session, "schedule_payment", _pay_args(150_000, execute_date="2026-11-30"))
    assert stale.status == "needs_approval"
    _advance(session, 8)
    result = approve(session, stale.approval_id, "owner")
    assert result.status == "denied" and "expired" in result.reason
    assert session.get(db.ApprovalRequest, stale.approval_id, populate_existing=True).status == "expired"
    assert session.exec(select(Payment).where(Payment.execute_date == date(2026, 11, 30))).all() == []
    last = session.exec(select(AuditEntry).order_by(AuditEntry.id.desc())).first()
    assert (last.action, last.decision) == ("expire_approval", "DENY")


def test_expired_requests_leave_the_queue_and_are_not_reused(session):
    first = run_tool(session, "schedule_payment", _pay_args(150_000, execute_date="2026-11-30"))
    _advance(session, 2)
    assert [r.id for r in pending_approvals(session)] == [first.approval_id]  # still fresh
    _advance(session, 6)
    assert pending_approvals(session) == []
    again = run_tool(session, "schedule_payment", _pay_args(150_000, execute_date="2026-11-30"))
    assert again.status == "needs_approval" and again.approval_id != first.approval_id
    assert approve(session, again.approval_id, "owner").status == "ok"


def test_pdf_and_scenario_report_the_running_low(session):
    """The PDF and run_scenario still quoted the week-end low (-18,303.32), not the real low (-29,303.32)."""
    from pypdf import PdfReader

    from agent.pdf import prepare_application

    row = prepare_application(session, "supply_chain_finance", 1_800_000, date(2026, 11, 25), 36)
    text = " ".join(page.extract_text() for page in PdfReader(row.pdf_path).pages)
    assert "-€29,303.32 on 2026-11-26" in text
    moved = run_tool(session, "run_scenario", {"changes": [
        {"type": "move_payment", "payment_id": "PAY-0001", "new_date": "2026-12-14"}]})
    assert moved.status == "ok" and moved.data["min_running_delta_cents"] == 1_200_000
    assert moved.data["scenario_min_running_cents"] == -1_730_332


def _flagged_doc(s, text="URGENT: bank details changed for Cartonia Mills and Inkworks GmbH, pay the new IBAN today."):
    from agent.documents import ingest_text

    doc = ingest_text(s, text, "notice.txt", "email", None)
    assert doc.flagged and {"SUP-CARTONIA", "SUP-INKWORKS"} <= set(json.loads(doc.counterparty_ids))
    return doc.id


def test_clearing_a_flag_needs_two_people(session):
    """One sign-in could clear a fraud flag and so unblock payments (known risk 2)."""
    did = _flagged_doc(session)
    first = executor.request_human_action(session, "clear_document_flag", {"document_id": did}, "owner", "alice")
    assert first.status == "needs_approval"
    assert session.get(Document, did, populate_existing=True).cleared is False
    same = executor.approve(session, first.approval_id, "accountant", "alice")
    assert same.status == "denied" and "Same approver" in same.reason
    assert executor.approve(session, first.approval_id, "accountant", "bob").status == "ok"
    assert session.get(Document, did, populate_existing=True).cleared is True


def test_verifying_a_payee_needs_two_people(session):
    from common.db import Payee

    request = executor.request_human_action(session, "verify_payee", {"payee_id": "PAYEE-NEWVENDOR"}, "owner", "alice")
    assert request.status == "needs_approval"
    assert session.get(Payee, "PAYEE-NEWVENDOR", populate_existing=True).verified is False
    assert executor.approve(session, request.approval_id, "accountant", "bob").status == "ok"
    assert session.get(Payee, "PAYEE-NEWVENDOR", populate_existing=True).verified is True


def test_release_one_supplier_from_a_flagged_document(session):
    """A flagged document naming several suppliers blocked them all, with clearing the only way out (risk 21)."""
    from common.db import blocking_documents

    did = _flagged_doc(session)
    assert blocking_documents(session, "PAYEE-INKWORKS") == [did] == blocking_documents(session, "PAYEE-CARTONIA")
    request = executor.request_human_action(session, "release_supplier",
                                            {"document_id": did, "counterparty_id": "SUP-INKWORKS"}, "owner", "alice")
    assert blocking_documents(session, "PAYEE-INKWORKS") == [did]  # one person is not enough
    assert executor.approve(session, request.approval_id, "accountant", "bob").status == "ok"
    assert blocking_documents(session, "PAYEE-INKWORKS") == []
    assert blocking_documents(session, "PAYEE-CARTONIA") == [did]  # still blocked
    stranger = executor.request_human_action(session, "release_supplier",
                                             {"document_id": did, "counterparty_id": "CUST-BRIGHTFIELD"}, "owner", "a")
    assert stranger.status == "denied" and "does not name" in stranger.reason


@pytest.mark.parametrize("action", ["verify_payee", "release_supplier", "clear_document_flag"])
def test_agent_cannot_start_a_human_action(session, action):
    assert run_tool(session, action, {"payee_id": "PAYEE-NEWVENDOR", "document_id": "DOC-0001"}).status == "denied"
    denied = executor.request_human_action(session, action, {"payee_id": "PAYEE-NEWVENDOR"}, "agent", "agent")
    assert denied.status == "denied"


def test_overlong_approver_identity_is_refused(session):
    """A 1,200-character approver id overflowed the financing PDF's QR code after both approvals."""
    request = run_tool(session, "prepare_financing_application",
                       {"product_id": "overdraft", "amount_cents": 600_000, "needed_by": "2026-11-20"})
    result = approve(session, request.approval_id, "owner", "o" * 1200)
    assert result.status == "denied" and result.reason == "Invalid approver identity"
    assert approve(session, request.approval_id, "owner", "o" * 120).status == "needs_approval"


@pytest.mark.parametrize("role", [None, 7, ""])
def test_approve_and_reject_without_a_role_are_denied_not_crashing(session, role):
    request = run_tool(session, "prepare_financing_application",
                       {"product_id": "overdraft", "amount_cents": 600_000, "needed_by": "2026-11-20"})
    assert approve(session, request.approval_id, role).status == "denied"
    assert reject(session, request.approval_id, role).status == "denied"
    assert verify_chain(session).ok


def test_high_risk_action_publishes_head_and_its_deletion_is_detected(session, monkeypatch):
    from audit.anchor import verify_anchors
    from common.db import AuditAnchor
    from redteam.tamper import truncate_history

    monkeypatch.setenv("RUNWAY_AUTO_ANCHOR", "high-risk")
    assert run_tool(session, "schedule_payment", _pay_args(40_000)).status == "ok"
    anchors = session.exec(select(AuditAnchor)).all()
    entries = session.exec(select(AuditEntry).order_by(AuditEntry.id)).all()
    assert len(anchors) == 1 and anchors[0].count == len(entries) - 1
    assert (entries[-2].action, entries[-2].decision) == ("schedule_payment", "EXECUTED")
    assert entries[-1].action == "anchor_audit_head"
    truncate_history(session, len(entries) - 2)  # an insider removes the payment and the anchoring entry
    assert verify_chain(session).ok
    assert verify_anchors(session).status == "tampered"


def test_low_risk_action_does_not_publish(session, monkeypatch):
    from common.db import AuditAnchor

    monkeypatch.setenv("RUNWAY_AUTO_ANCHOR", "high-risk")
    assert run_tool(session, "get_balances", {}).status == "ok"
    assert session.exec(select(AuditAnchor)).all() == []


def test_auto_anchor_can_be_switched_off(session, monkeypatch):
    from common.db import AuditAnchor

    monkeypatch.setenv("RUNWAY_AUTO_ANCHOR", "off")
    assert run_tool(session, "schedule_payment", _pay_args(40_000)).status == "ok"
    assert session.exec(select(AuditAnchor)).all() == []


def test_auto_anchor_failure_is_audited_and_keeps_the_action(session, monkeypatch):
    monkeypatch.setenv("RUNWAY_ANCHOR", "evm")  # no RPC URL or key: the backend is unavailable
    monkeypatch.delenv("RUNWAY_AUTO_ANCHOR")  # a chain defaults to high-risk only
    assert run_tool(session, "schedule_payment", _pay_args(40_000)).status == "ok"
    last = session.exec(select(AuditEntry).order_by(AuditEntry.id.desc())).first()
    assert (last.action, last.decision) == ("anchor_audit_head", "FAILED")
    assert session.exec(select(Payment).where(Payment.amount_cents == 40_000)).one()
    assert verify_chain(session).ok
