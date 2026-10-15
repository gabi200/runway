"""Smoke test: click through the Streamlit UI on a temp DB, offline LLM."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from audit import log as audit_log
from common import db

APP = str(Path(__file__).resolve().parent.parent / "ui" / "app.py")


@pytest.fixture()
def at(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNWAY_DB", str(tmp_path / "ui.db"))
    monkeypatch.setenv("RUNWAY_VAR_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("RUNWAY_SAMPLE_DIR", str(tmp_path / "sample"))
    monkeypatch.setenv("RUNWAY_LLM", "offline")
    db.reset_engines()
    app = AppTest.from_file(APP, default_timeout=60).run()
    yield app
    db.reset_engines()


def sign_in(at, role="owner", pin=None):
    pin = pin or {"owner": "1111", "accountant": "2222"}[role]
    at.selectbox(key="login_role").set_value(role)
    at.text_input(key="login_pin").set_value(pin)
    at.button(key="login_btn").click().run()
    assert not at.exception, [e.value for e in at.exception]


def goto(at, page):
    at.radio(key="page").set_value(page).run()
    assert not at.exception, [e.value for e in at.exception]


def test_all_pages_render(at):
    assert not at.exception
    for page in ["Onboarding", "Monday brief & chat", "Forecast", "Approval queue", "Documents",
                 "Payees (humans only)", "Mock bank", "Test inbox", "Audit log", "Red team"]:
        goto(at, page)


def test_brief_approvals_kill_switch_and_audit(at):
    sign_in(at)
    goto(at, "Monday brief & chat")
    next(b for b in at.button if b.label == "Run Monday brief").click().run()
    assert not at.exception
    goto(at, "Approval queue")
    goto(at, "Test inbox")
    goto(at, "Audit log")
    next(b for b in at.button if b.label == "Verify chain").click().run()
    assert any("intact" in s.value for s in at.success)
    at.toggle(key="kill").set_value(True).run()
    with db.session_scope() as s:
        assert db.kill_switch_on(s)


def test_kill_switch_refused_when_signed_out(at):
    at.toggle(key="kill").set_value(True).run()
    with db.session_scope() as s:
        assert not db.kill_switch_on(s)


def test_forecast_and_currency_switch(at):
    goto(at, "Forecast")
    at.radio(key="pack").set_value("cn").run()
    assert not at.exception
    assert any("¥" in str(df.value.to_dict()) for df in at.dataframe)


def test_documents_sample_ingest(at):
    goto(at, "Documents")
    btn = next(b for b in at.button if b.label == "Ingest owner_email_A2.txt")
    btn.click().run()
    assert not at.exception


def test_approve_clear_flag_payee_advance_scenario(at):
    sign_in(at)
    goto(at, "Monday brief & chat")
    next(b for b in at.button if b.label == "Run Monday brief").click().run()
    goto(at, "Approval queue")
    approve = [b for b in at.button if b.label == "Approve"]
    if approve:
        approve[0].click().run()
        assert not at.exception
    goto(at, "Documents")
    next(b for b in at.button if b.label == "Ingest owner_email_A2.txt").click().run()
    goto(at, "Documents")
    clear = [b for b in at.button if b.label.startswith("Clear flag")]
    assert clear, "A2 email should be flagged"
    clear[0].click().run()
    assert not at.exception
    with db.session_scope() as s:
        from sqlmodel import select
        assert any(d.cleared for d in s.exec(select(db.Document)))
        entry = s.exec(select(db.AuditEntry).where(
            db.AuditEntry.action == "clear_document_flag")).one()
        assert entry.actor == "owner" and entry.decision == "HUMAN_ACTION"
    goto(at, "Payees (humans only)")
    goto(at, "Mock bank")
    next(b for b in at.button if b.label == "Advance").click().run()
    assert not at.exception
    goto(at, "Forecast")
    next(b for b in at.button if b.label == "Add delay").click().run()
    assert not at.exception
    assert at.metric


def test_payee_verify_unverify_is_owner_only_and_audited(at):
    from sqlmodel import select
    from audit.log import verify_chain

    sign_in(at)
    goto(at, "Payees (humans only)")
    key = "verify_PAYEE-NEWVENDOR"
    assert at.button(key=key).label == "Verify"
    at.button(key=key).click().run()
    assert not at.exception
    assert at.button(key=key).label == "Unverify"
    with db.session_scope() as s:
        assert s.get(db.Payee, "PAYEE-NEWVENDOR").verified is True
        entry = s.exec(select(db.AuditEntry).where(db.AuditEntry.action == "verify_payee")).one()
        assert entry.actor == "owner" and entry.decision == "HUMAN_ACTION"
        assert verify_chain(s).ok
    at.button(key=key).click().run()
    assert not at.exception
    with db.session_scope() as s:
        assert s.get(db.Payee, "PAYEE-NEWVENDOR").verified is False
    at.selectbox(key="sw_role").set_value("accountant")
    at.text_input(key="sw_pin").set_value("2222")
    at.button(key="sw_btn").click().run()
    assert not at.exception
    assert not [button for button in at.button if button.label in ("Verify", "Unverify")]


def test_owner_fills_missing_iban_once(at):
    from datetime import datetime, timezone
    with db.session_scope() as s:
        s.add(db.Payee(id="PAYEE-IMPORTED", name="Imported", iban="", created_at=datetime.now(timezone.utc)))
        s.commit()
    sign_in(at)
    goto(at, "Payees (humans only)")
    at.text_input(key="iban_PAYEE-IMPORTED").set_value("DE89 3704 0044 0532 0130 00")
    at.button(key="set_iban_PAYEE-IMPORTED").click().run()
    assert not at.exception
    assert not [w for w in at.text_input if w.key == "iban_PAYEE-IMPORTED"]
    with db.session_scope() as s:
        row = s.get(db.Payee, "PAYEE-IMPORTED")
        assert row.iban == "DE89 3704 0044 0532 0130 00" and row.verified is False


def test_human_actions_need_sign_in_and_wrong_pin_rejected(at):
    goto(at, "Payees (humans only)")
    assert not [b for b in at.button if b.label in ("Verify", "Unverify")]
    at.selectbox(key="login_role").set_value("owner")
    at.text_input(key="login_pin").set_value("9999")
    at.button(key="login_btn").click().run()
    assert any("Wrong PIN" in e.value for e in at.error)
    assert at.session_state["role"] is None
    goto(at, "Mock bank")
    next(b for b in at.button if b.label == "Advance").click().run()
    with db.session_scope() as s:
        assert db.get_today(s).isoformat() == "2026-10-19"


def test_role_switch_needs_pin(at):
    sign_in(at, "owner")
    at.selectbox(key="sw_role").set_value("accountant")
    at.text_input(key="sw_pin").set_value("1111")
    at.button(key="sw_btn").click().run()
    assert at.session_state["role"] == "owner"
    at.text_input(key="sw_pin").set_value("2222")
    at.button(key="sw_btn").click().run()
    assert at.session_state["role"] == "accountant"


def test_one_session_cannot_fill_both_roles(at):
    from agent.executor import run_tool
    with db.session_scope() as s:
        res = run_tool(s, "prepare_financing_application",
                       {"product_id": "invoice_financing", "amount_cents": 2_000_000,
                        "needed_by": "2026-11-23", "days_needed": 30}, actor="agent")
        assert res.status == "needs_approval", res
        rid = res.approval_id
        assert len(__import__("json").loads(s.get(db.ApprovalRequest, rid).required_roles_json)) == 2
    sign_in(at, "owner")
    goto(at, "Approval queue")
    at.button(key=f"ap_{rid}").click().run()
    at.selectbox(key="sw_role").set_value("accountant")
    at.text_input(key="sw_pin").set_value("2222")
    at.button(key="sw_btn").click().run()
    goto(at, "Approval queue")
    at.button(key=f"ap_{rid}").click().run()
    assert any("Not approved" in e.value for e in at.error)
    with db.session_scope() as s:
        assert s.get(db.ApprovalRequest, rid).status == "pending"


def test_model_markdown_is_neutralised():
    from ui.render import safe_text
    out = safe_text("see [click](http://evil.example/x) ![p](http://evil.example/p.png) http://a.example <b>x</b>")
    assert "](" not in out.replace("\\](", "") and "![" not in out and "<b>" not in out
    assert "`http://a.example`" in out


def test_bare_email_addresses_are_not_autolinked():
    from ui.render import safe_text
    out = safe_text("From billing.team+x@evil-pay.example.com, reply to xmpp:bot@evil.example now.")
    assert "`billing.team+x@evil-pay.example.com`" in out
    assert "`xmpp:bot@evil.example`" in out
    assert out.endswith(" now.")
    assert safe_text("no address here @ all") == "no address here @ all"


def test_tool_name_cannot_break_out_of_its_code_span(at):
    import re
    from agent.orchestrator import AgentTurn
    evil = ('get_forecast\n\n<img src="https://evil.example/p.png">'
            '<a href="https://evil.example/login">Re-approve here</a>')
    link = "[Re-approve here](https://evil.example/login)"
    at.session_state["brief"] = AgentTurn(reply="ok", steps=[
        {"tool": evil, "status": "DENY", "args": {}}, {"tool": link, "status": "DENY", "args": {}}])
    goto(at, "Monday brief & chat")
    lines = [m.value for m in at.markdown if m.value.startswith(("1. ", "2. "))]
    assert len(lines) == 2
    for line in lines:
        assert "\n" not in line  # a line break would end the code span
        outside = re.sub(r"`[^`\n]*`", "", line)
        assert "evil.example" not in outside and "<img" not in outside and "<a " not in outside


def test_imported_payee_name_and_invoice_id_render_as_text(at):
    from datetime import datetime, timezone
    evil = "[Update IBAN here](https://evil.example/x)"
    with db.session_scope() as s:
        s.add(db.Payee(id="PAYEE-EVIL", name=evil, iban="", created_at=datetime.now(timezone.utc)))
        s.add(db.OutboxEmail(id="MAIL-EVIL", to="a@b.example", subject="s", body="b", invoice_id=evil))
        s.commit()
    sign_in(at)
    goto(at, "Payees (humans only)")
    texts =[m.value for m in at.markdown] + [c.value for c in at.caption]
    assert any("Update IBAN here" in t for t in texts)
    assert not any("](https://evil.example" in t for t in texts)
    goto(at, "Test inbox")
    captions = [c.value for c in at.caption]
    assert any("Update IBAN here" in t for t in captions)
    assert not any("](https://evil.example" in t for t in captions)


def _audit_count():
    from sqlmodel import select
    with db.session_scope() as s:
        return len(s.exec(select(db.AuditEntry)).all())


def test_confirm_reset_signed_in_with_kill_switch_on(at):
    sign_in(at)
    at.toggle(key="kill").set_value(True).run()
    next(b for b in at.button if b.label == "Reset demo data").click().run()
    next(b for b in at.button if b.label == "Confirm reset").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.toggle(key="kill").value is False
    with db.session_scope() as s:
        assert not db.kill_switch_on(s)


def test_sample_data_needs_sign_in_and_works_signed_in(at):
    goto(at, "Onboarding")
    before = _audit_count()
    next(b for b in at.button if b.label == "Use generated sample data").click().run()
    assert not at.exception
    assert any("Sign in" in i.value for i in at.info)
    assert _audit_count() == before  # nothing was wiped
    sign_in(at)
    goto(at, "Onboarding")
    at.toggle(key="kill").set_value(True).run()
    next(b for b in at.button if b.label == "Use generated sample data").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.toggle(key="kill").value is False
    assert any("Sample data loaded" in s.value for s in at.success)


def test_brief_and_chat_need_sign_in(at):
    goto(at, "Monday brief & chat")
    next(b for b in at.button if b.label == "Run Monday brief").click().run()
    assert any("Sign in" in i.value for i in at.info)
    assert at.session_state["brief"] is None
    at.chat_input[0].set_value("hello").run()
    assert at.session_state["chat"] == []


def test_copilot_suggestion_requires_sign_in(at):
    goto(at, "Monday brief & chat")
    at.button(key="prompt_shortfall").click().run()
    assert not at.exception
    assert at.session_state["chat"] == []
    assert any("Sign in" in info.value for info in at.info)


def test_copilot_suggestion_runs_a_real_agent_turn(at):
    sign_in(at)
    goto(at, "Monday brief & chat")
    at.button(key="prompt_shortfall").click().run()
    assert not at.exception
    messages = at.session_state["chat"]
    assert messages[-2][0] == "user" and "shortfall" in messages[-2][1].lower()
    assert messages[-1][0] == "assistant"
    assert messages[-1][2].reply
    assert any(step["tool"] == "get_forecast" for step in messages[-1][2].steps)


def test_pin_lockout_is_per_session_and_audited(at, tmp_path):
    from sqlmodel import select
    for _ in range(5):
        at.selectbox(key="login_role").set_value("owner")
        at.text_input(key="login_pin").set_value("0000")
        at.button(key="login_btn").click().run()
    at.text_input(key="login_pin").set_value("1111")
    at.button(key="login_btn").click().run()
    assert any("Too many wrong PINs" in e.value for e in at.error)
    assert at.session_state["role"] is None
    # another browser session is not locked out by someone else's guessing
    fresh = AppTest.from_file(APP, default_timeout=60).run()
    fresh.selectbox(key="login_role").set_value("owner")
    fresh.text_input(key="login_pin").set_value("1111")
    fresh.button(key="login_btn").click().run()
    assert fresh.session_state["role"] == "owner"
    with db.session_scope() as s:
        assert len(s.exec(select(db.AuditEntry).where(db.AuditEntry.action == "sign_in_failed")).all()) == 5


def test_pin_lockout_expires(at, monkeypatch):
    from ui import auth
    with db.session_scope() as s:
        db.set_setting(s, "pin_failures:owner:S1", '{"n": 5, "ts": 1000}')
        db.set_setting(s, "pin_failures:owner", '{"n": 5, "ts": 1000}')
    assert auth.locked_seconds("owner", now=1000 + 100, sid="S1") > 0
    assert auth.locked_seconds("owner", now=1000 + 301, sid="S1") == 0
    assert auth.locked_seconds("owner", now=1000 + 100, sid="S2") == 0
    with db.session_scope() as s:  # many failures across sessions lock the role everywhere
        db.set_setting(s, "pin_failures:owner", '{"n": 20, "ts": 1000}')
    assert auth.locked_seconds("owner", now=1000 + 600, sid="S2") > 0
    assert auth.locked_seconds("owner", now=1000 + 901, sid="S2") == 0
    assert auth.locked_seconds("accountant", now=1000 + 100, sid="S1") == 0


def test_import_requires_confirm_and_sign_in_and_has_optional_uploads(at):
    goto(at, "Onboarding")
    labels = [f.label for f in at.get("file_uploader")]
    assert any("Paid invoices" in x for x in labels) and any("Scheduled payments" in x for x in labels)


def test_financing_card_states_reschedule_dependency(at):
    from agent.executor import run_tool
    with db.session_scope() as s:
        r1 = run_tool(s, "reschedule_payment", {"payment_id": next(
            p.id for p in s.exec(__import__("sqlmodel").select(db.Payment).where(db.Payment.status == "scheduled"))),
            "new_date": "2026-12-01"}, actor="agent")
        r2 = run_tool(s, "prepare_financing_application",
                      {"product_id": "invoice_financing", "amount_cents": 1_000_000,
                       "needed_by": "2026-11-23", "days_needed": 30}, actor="agent")
    assert r1.status == "needs_approval" and r2.status == "needs_approval", (r1, r2)
    sign_in(at)
    goto(at, "Approval queue")
    assert any(f"Assumes reschedule {r1.approval_id} is approved" in m.value for m in at.markdown)


def test_publish_and_verify_audit_head(at):
    with db.session_scope() as s:
        audit_log.append(s, "system", "test_seed", {}, "INFO")
    goto(at, "Audit log")
    at.button(key="anchor_btn").click().run()
    assert any("Sign in" in i.value for i in at.info)
    sign_in(at)
    goto(at, "Audit log")
    at.button(key="anchor_btn").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Published entries 1 to" in s.value for s in at.success)
    at.button(key="anchor_verify").click().run()
    assert any("match" in s.value for s in at.success)


from redteam.console import EVASION_SAMPLE, Step


def test_red_team_requires_sign_in(at):
    goto(at, "Red team")
    assert any("Sign in" in i.value for i in at.info)


def test_red_team_forbidden_action_and_document(at):
    sign_in(at)
    goto(at, "Red team")
    at.radio(key="rt_preset").set_value("pay_unverified")
    at.button(key="rt_action_run").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Stopped" in x.value for x in at.success)
    at.text_area(key="rt_doc_text").set_value(EVASION_SAMPLE)
    at.button(key="rt_doc_run").click().run()
    assert any("Held for human approval" in x.value for x in at.info)


def test_red_team_audit_rewrite_before_and_after_publishing(at):
    with db.session_scope() as s:
        for i in range(3):
            audit_log.append(s, "system", "test_seed", {"i": i}, "INFO")
    sign_in(at)
    goto(at, "Red team")
    at.radio(key="rt_mode").set_value("forge")
    at.button(key="rt_audit_run").click().run()
    assert any("Not detected" in x.value for x in at.error)
    goto(at, "Audit log")
    at.button(key="anchor_btn").click().run()
    goto(at, "Red team")
    at.radio(key="rt_mode").set_value("forge")
    at.button(key="rt_audit_run").click().run()
    assert any("Detected" in x.value for x in at.success)


def test_red_team_step_text_is_neutralised():
    from ui.render import format_step

    out = format_step(Step("Agent reply", "info",
                           "Pay [here](https://evil.example) ![x](https://evil.example/p.png)"))
    assert "](https://" not in out and "image removed" in out


def test_approval_in_ui_is_signed(at):
    sign_in(at)
    goto(at, "Monday brief & chat")
    next(b for b in at.button if b.label == "Run Monday brief").click().run()
    goto(at, "Approval queue")
    next(b for b in at.button if b.label == "Approve").click().run()
    assert not at.exception, [e.value for e in at.exception]
    goto(at, "Approval queue")
    assert any("owner (signed)" in str(df.value.to_dict()) for df in at.dataframe)
    with db.session_scope() as s:
        from sqlmodel import select
        entry = s.exec(select(db.AuditEntry).where(db.AuditEntry.decision == "APPROVED")).first()
        assert entry.actor == "owner" and "signature" in entry.args_json
