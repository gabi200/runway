"""Smoke test: click through the Streamlit UI on a temp DB, offline LLM."""

from pathlib import Path
import json
import os

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


def test_llm_mode_switch_is_session_local(at, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    at.session_state["chat"] = [("user", "Old conversation", None)]
    at.session_state["agent"] = object()
    at.selectbox(key="llm_mode").set_value("gemini").run()
    assert not at.exception
    assert at.session_state["chat"] == [] and "agent" not in at.session_state
    assert at.session_state["llm_ready"]
    assert os.environ["RUNWAY_LLM"] == "offline"
    other = AppTest.from_file(APP, default_timeout=60).run()
    assert other.selectbox(key="llm_mode").value == "offline"


def test_missing_gemini_key_disables_agent(at):
    at.selectbox(key="llm_mode").set_value("gemini").run()
    assert not at.exception
    assert not at.session_state["llm_ready"]
    assert any("GEMINI_API_KEY" in w.value for w in at.warning)
    sign_in(at)
    goto(at, "Red team")
    assert at.button(key="rt_chat_run").disabled


def test_gemini_startup_without_key_shows_setup(at, monkeypatch):
    monkeypatch.setenv("RUNWAY_LLM", "gemini")
    other = AppTest.from_file(APP, default_timeout=60).run()
    assert not other.exception
    assert other.selectbox(key="llm_mode").value == "gemini"
    assert any("GEMINI_API_KEY" in w.value for w in other.warning)


@pytest.mark.parametrize("mode,key", [("deepseek", "DEEPSEEK_API_KEY"), ("gemini", "GEMINI_API_KEY")])
def test_chat_uses_selected_provider(at, monkeypatch, mode, key):
    from llm.adapter import LLMAdapter

    monkeypatch.setenv(key, "test-key")
    monkeypatch.setattr(LLMAdapter, "chat", lambda self, messages, tools:
                        {"content": f"Reply from {self.config['provider']}.", "tool_calls": []})
    sign_in(at)
    at.selectbox(key="llm_mode").set_value(mode).run()
    at.chat_input(key="copilot_input").set_value("Hello").run()
    assert not at.exception
    assert at.session_state["chat"][-1][1] == f"Reply from {mode}."


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


def test_kill_switch_shown_from_db_in_every_session(at):
    other = AppTest.from_file(APP, default_timeout=60).run()
    sign_in(other)
    other.toggle(key="kill").set_value(True).run()
    at.run()
    assert at.toggle(key="kill").value is True
    assert any("Agent read-only" in m.value for m in at.sidebar.markdown)
    other.toggle(key="kill").set_value(False).run()  # one person cannot switch the agent back on
    assert other.toggle(key="kill").value is True
    assert any("other role must approve" in w.value for w in other.sidebar.warning)
    from sqlmodel import select
    from agent.executor import approve as approve_request
    with db.session_scope() as s:
        assert db.kill_switch_on(s)
        request = s.exec(select(db.ApprovalRequest).where(db.ApprovalRequest.tool_name == "kill_switch_off")).one()
        assert approve_request(s, request.id, "accountant", "accountant@other-session").status == "ok"
    at.run()
    assert at.toggle(key="kill").value is False
    assert any("Agent active within rules" in m.value for m in at.sidebar.markdown)


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
    assert any("other role must approve" in s.value for s in at.success)
    with db.session_scope() as s:
        from sqlmodel import select
        from agent.executor import approve as approve_request
        assert not any(d.cleared for d in s.exec(select(db.Document)))  # one person is not enough
        request = s.exec(select(db.ApprovalRequest).where(
            db.ApprovalRequest.tool_name == "clear_document_flag")).one()
        assert approve_request(s, request.id, "accountant", "accountant@other-session").status == "ok"
        assert any(d.cleared for d in s.exec(select(db.Document).execution_options(populate_existing=True)))
        entry = s.exec(select(db.AuditEntry).where(
            db.AuditEntry.action == "clear_document_flag", db.AuditEntry.decision == "HUMAN_ACTION")).one()
        assert entry.actor == "accountant"
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

    from agent.executor import approve as approve_request

    sign_in(at)
    goto(at, "Payees (humans only)")
    assert _verify_button(at).label == "Request verification"
    _verify_button(at).click().run()
    assert not at.exception
    with db.session_scope() as s:
        assert s.get(db.Payee, "PAYEE-NEWVENDOR").verified is False  # one person is not enough
        request = s.exec(select(db.ApprovalRequest).where(db.ApprovalRequest.tool_name == "verify_payee")).one()
        assert approve_request(s, request.id, "accountant", "accountant@other-session").status == "ok"
        assert s.get(db.Payee, "PAYEE-NEWVENDOR", populate_existing=True).verified is True
        entry = s.exec(select(db.AuditEntry).where(db.AuditEntry.action == "verify_payee",
                                                   db.AuditEntry.decision == "EXECUTED")).one()
        assert entry.actor == "accountant"
        assert verify_chain(s).ok
    goto(at, "Payees (humans only)")
    assert _verify_button(at).label == "Unverify"
    _verify_button(at).click().run()
    assert not at.exception
    with db.session_scope() as s:
        assert s.get(db.Payee, "PAYEE-NEWVENDOR").verified is False
        assert s.exec(select(db.AuditEntry).where(db.AuditEntry.action == "unverify_payee")).one().actor == "owner"
    at.selectbox(key="sw_role").set_value("accountant")
    at.text_input(key="sw_pin").set_value("2222")
    at.button(key="sw_btn").click().run()
    assert not at.exception
    assert not [button for button in at.button if button.label in ("Request verification", "Unverify")]


def _verify_button(app, payee_id="PAYEE-NEWVENDOR"):
    return next(b for b in app.button if (b.key or "").startswith(f"verify_{payee_id}"))


def _pick(app, key, item_id):
    app.session_state[f"{key}_pick"] = item_id  # what a click on the item's table row stores
    app.run()
    assert not app.exception, [e.value for e in app.exception]


def test_stale_unverify_click_does_not_reverify(at):
    with db.session_scope() as s:  # verified earlier by two people
        payee = s.get(db.Payee, "PAYEE-NEWVENDOR")
        payee.verified = True
        s.add(payee)
        s.commit()
    sign_in(at)
    goto(at, "Payees (humans only)")
    _pick(at, "payee", "PAYEE-NEWVENDOR")  # all verified: the card opens on the first payee
    other = AppTest.from_file(APP, default_timeout=60).run()
    sign_in(other)
    goto(other, "Payees (humans only)")
    _pick(other, "payee", "PAYEE-NEWVENDOR")
    assert _verify_button(other).label == "Unverify"
    _verify_button(at).click().run()  # unverified in the first tab
    with db.session_scope() as s:
        assert s.get(db.Payee, "PAYEE-NEWVENDOR").verified is False
    stale = _verify_button(other)
    assert stale.label == "Unverify"  # the second tab still shows the old state
    stale.click().run()
    assert not other.exception
    with db.session_scope() as s:
        assert s.get(db.Payee, "PAYEE-NEWVENDOR").verified is False
    assert _verify_button(other).label == "Request verification"


def test_payee_search_and_filter_narrow_the_list(at):
    goto(at, "Payees (humans only)")
    assert len(at.dataframe[0].value) == 9
    at.text_input(key="payee_q").set_value("newvendor").run()
    assert list(at.dataframe[0].value["ID"]) == ["PAYEE-NEWVENDOR"]
    assert at.session_state["payee_pick"] == "PAYEE-NEWVENDOR"
    at.text_input(key="payee_q").set_value("no such payee").run()
    assert not at.dataframe and not at.exception


def test_documents_open_on_the_first_flagged_one(at):
    from agent.documents import ingest_text

    with db.session_scope() as s:
        ingest_text(s, "Invoice for March, thanks.", "clean.txt", "email", None)
        flagged = ingest_text(s, "URGENT: bank details changed, pay the new IBAN.", "notice.txt", "email", None).id
    goto(at, "Documents")
    assert at.session_state["doc_pick"] == flagged
    at.text_input(key="doc_q").set_value("clean.txt").run()
    assert list(at.dataframe[0].value["File"]) == ["clean.txt"]
    assert at.session_state["doc_pick"] != flagged


def test_owner_fills_missing_iban_once(at):
    from datetime import datetime, timezone
    with db.session_scope() as s:
        s.add(db.Payee(id="PAYEE-IMPORTED", name="Imported", iban="", created_at=datetime.now(timezone.utc)))
        s.commit()
    sign_in(at)
    goto(at, "Payees (humans only)")
    _pick(at, "payee", "PAYEE-IMPORTED")
    at.text_input(key="iban_PAYEE-IMPORTED").set_value("DE89 3704 0044 0532 0130 00")
    at.button(key="set_iban_PAYEE-IMPORTED").click().run()
    assert not at.exception
    assert not [w for w in at.text_input if w.key == "iban_PAYEE-IMPORTED"]
    with db.session_scope() as s:
        row = s.get(db.Payee, "PAYEE-IMPORTED")
        assert row.iban == "DE89 3704 0044 0532 0130 00" and row.verified is False


def test_human_actions_need_sign_in_and_wrong_pin_rejected(at):
    goto(at, "Payees (humans only)")
    assert not [b for b in at.button if b.label in ("Request verification", "Unverify")]
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


def test_pins_do_not_stay_in_the_fields(at):
    sign_in(at, "owner")
    at.selectbox(key="sw_role").set_value("accountant")
    at.text_input(key="sw_pin").set_value("2222")
    at.button(key="sw_btn").click().run()
    assert at.session_state["role"] == "accountant"
    assert at.text_input(key="sw_pin").value == ""
    goto(at, "Forecast")
    assert at.text_input(key="sw_pin").value == ""
    at.button(key="signout_btn").click().run()
    assert at.session_state["role"] is None
    assert at.text_input(key="login_pin").value == ""
    at.text_input(key="login_pin").set_value("0000")
    at.button(key="login_btn").click().run()
    assert any("Wrong PIN" in e.value for e in at.error)
    assert at.text_input(key="login_pin").value == ""


def _chat_and_arm_reset(at):
    goto(at, "Monday brief & chat")
    at.button(key="prompt_shortfall").click().run()
    next(b for b in at.button if b.label == "Reset demo data").click().run()
    assert at.session_state["chat"] and at.session_state["confirm_reset"] is True
    assert "agent" in at.session_state


def test_sign_out_and_role_switch_drop_the_conversation(at):
    sign_in(at, "owner")
    _chat_and_arm_reset(at)
    at.button(key="signout_btn").click().run()
    assert at.session_state["chat"] == [] and at.session_state["confirm_reset"] is False
    assert "agent" not in at.session_state
    sign_in(at, "owner")
    _chat_and_arm_reset(at)
    at.selectbox(key="sw_role").set_value("accountant")
    at.text_input(key="sw_pin").set_value("2222")
    at.button(key="sw_btn").click().run()
    assert at.session_state["role"] == "accountant"
    assert at.session_state["chat"] == [] and at.session_state["confirm_reset"] is False
    assert "agent" not in at.session_state
    assert not [b for b in at.button if b.label == "Confirm reset"]


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
    assert any("Not approved" in e.value and "four-eyes" in e.value for e in at.error)
    with db.session_scope() as s:
        assert s.get(db.ApprovalRequest, rid).status == "pending"


def test_approve_under_kill_switch_says_the_approval_is_recorded(at):
    import json
    from sqlmodel import select
    from agent.executor import run_tool
    with db.session_scope() as s:
        pay = s.exec(select(db.Payment).where(db.Payment.status == "scheduled")).first()
        res = run_tool(s, "reschedule_payment", {"payment_id": pay.id, "new_date": "2026-12-01"}, actor="agent")
    assert res.status == "needs_approval", res
    sign_in(at, "owner")
    at.toggle(key="kill").set_value(True).run()
    goto(at, "Approval queue")
    at.button(key=f"ap_{res.approval_id}").click().run()
    assert not at.exception
    shown = [m.value for m in at.error] + [m.value for m in at.warning]
    assert not any("four-eyes" in t for t in shown), shown
    assert any("approval is recorded" in t and "Kill switch" in t for t in shown), shown
    with db.session_scope() as s:
        req = s.get(db.ApprovalRequest, res.approval_id)
        assert req.status == "pending"
        assert [g["role"] for g in json.loads(req.approvals_json)] == ["owner"]


def test_model_markdown_is_neutralised():
    from ui.render import safe_text
    out = safe_text("see [click](http://evil.example/x) ![p](http://evil.example/p.png) http://a.example <b>x</b>")
    assert "](" not in out.replace("\\](", "") and "![" not in out and "<b>" not in out
    assert "`http://a.example`" in out


def test_reply_markdown_keeps_formatting():
    from ui.render import reply_markdown
    text = "## Plan\n\n- **Pay** the *supplier*\n- Call `INV-7`\n\n| Week | Balance |\n|---|---|\n| 3 | -1,200 |"
    assert reply_markdown(text) == text


def test_reply_markdown_neutralises_links_images_and_html():
    from ui.render import reply_markdown
    out = reply_markdown(r"**Act now:** [click](http://evil.example/x) ![p](http://evil.example/p.png) "
                         r"<img src=x> <https://evil.example> [ref]: http://evil.example \[a](b) ops@evil.example")
    assert out.startswith("**Act now:** ")
    assert "![" not in out and "](http" not in out
    assert r"\[click\](`http://evil.example/x`)" in out
    assert r"\<img src=x>" in out and r"\<`https://evil.example`>" in out
    assert reply_markdown("Done.\n\n> Check by Runway: x") == "Done.\n\n> Check by Runway: x"  # quote blocks stay
    assert r"\[ref\]: `http://evil.example`" in out
    assert r"\\[a\](b)" in out  # a model's own backslash cannot cancel the escape
    assert out.endswith("`ops@evil.example`")


def test_reply_markdown_leaves_code_untouched():
    from ui.render import reply_markdown
    text = "Run `rate[0] < 5` then:\n```\nif a[1] > b: print('<x>')\n```"
    assert reply_markdown(text) == text


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
    _pick(at, "payee", "PAYEE-EVIL")
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


def test_reset_demo_is_audited_with_the_acting_role(at):
    from sqlmodel import select
    from audit.log import verify_chain
    sign_in(at, "accountant")
    next(b for b in at.button if b.label == "Reset demo data").click().run()
    next(b for b in at.button if b.label == "Confirm reset").click().run()
    assert not at.exception
    with db.session_scope() as s:
        entry = s.exec(select(db.AuditEntry).where(db.AuditEntry.action == "reset_demo")).one()
        assert entry.actor == "accountant" and entry.decision == "HUMAN_ACTION"
        assert at.session_state["sid"] in entry.args_json
        assert verify_chain(s).ok
        from agent.outbox import var_dir
        from audit import archive
        name = json.loads(entry.args_json)["archive"]
        assert archive.check_link(s, var_dir() / "archive" / name)  # the old log survives the reset


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


def test_non_utf8_csv_preview_shows_an_error(at):
    goto(at, "Onboarding")
    latin1 = "account,name,debit,credit\n1000,Caisse société,10.00,0\n".encode("cp1252")
    at.file_uploader(key="up_tb").set_value(("tb.csv", latin1, "text/csv")).run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("UTF-8" in e.value for e in at.error)
    utf8 = "date,description,amount\n2026-10-01,Café,-5.00\n".encode("utf-8")
    at.file_uploader(key="up_bs").set_value(("bs.csv", utf8, "text/csv")).run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("1 transactions" in i.value for i in at.info)  # the valid file is still previewed


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
    assert at.session_state["apr_pick"] == r1.approval_id  # the queue opens on the oldest request
    _pick(at, "apr", r2.approval_id)
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
    _pick(at, "apr", "APR-0001")  # after an approval the card moves on; open the approved one
    assert any("Owner approved (signed)" in m.value for m in at.markdown)  # the request's story on the card
    with db.session_scope() as s:
        from sqlmodel import select
        entry = s.exec(select(db.AuditEntry).where(db.AuditEntry.decision == "APPROVED")).first()
        assert entry.actor == "owner" and "signature" in entry.args_json


def test_forecast_page_shows_the_running_low_and_a_valid_default_move(at):
    goto(at, "Forecast")
    assert any("29,303.32" in str(m.value) for m in at.metric), [m.value for m in at.metric]
    next(b for b in at.button if b.label == "Add move").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert not at.error, [e.value for e in at.error]


def test_release_one_supplier_from_a_flagged_document(at):
    from sqlmodel import select
    from agent.documents import ingest_text

    with db.session_scope() as s:
        did = ingest_text(s, "URGENT: bank details changed for Cartonia Mills and Inkworks GmbH, pay the new IBAN.",
                          "notice.txt", "email", None).id
    sign_in(at)
    goto(at, "Documents")
    _pick(at, "doc", did)
    at.button(key=f"rel_{did}_SUP-INKWORKS").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("other role must approve" in s.value for s in at.success)
    with db.session_scope() as s:
        request = s.exec(select(db.ApprovalRequest).where(db.ApprovalRequest.tool_name == "release_supplier")).one()
        assert '"SUP-INKWORKS"' in request.args_json and db.blocking_documents(s, "PAYEE-INKWORKS") == [did]


def test_forecast_table_shows_the_lowest_point_of_each_week(at):
    goto(at, "Forecast")
    table = next(df for df in at.dataframe if "Lowest in week" in df.value.columns)
    assert "Lowest in week" in (table.proto.column_order or table.value.columns)


def test_reply_shows_chart_and_invoice_buttons_and_reminder_goes_through_agent(at):
    from agent.orchestrator import AgentTurn
    from common.db import Invoice
    from sqlmodel import select

    with db.session_scope() as s:
        today = db.get_today(s)
        inv = next(i for i in s.exec(select(Invoice).where(Invoice.direction == "AR", Invoice.status == "open"))
                   if i.due_date < today)
        inv_id, number = inv.id, inv.number
    chart = {"dataset": "receivables", "title": "Open customer invoices by customer", "kind": "bar",
             "x_label": "Customer", "y_label": "Open amount", "note": "", "x_order": ["A"],
             "rows": [{"x": "A", "series": "Open amount", "value_cents": 12345}]}
    sign_in(at)
    at.session_state["brief"] = AgentTurn(reply=f"Invoice {number} is overdue.", steps=[
        {"tool": "make_chart", "args": {"dataset": "receivables"}, "status": "ok", "reason": "", "chart": chart}])
    goto(at, "Monday brief & chat")
    assert any("Figures computed from the ledger, not by AI." in c.value for c in at.caption)  # under the agent's chart
    labels = [b.label for b in at.button]
    assert number in labels and f"Remind: {number}" in labels
    at.session_state["brief"] = None
    next(b for b in at.button if b.label == f"Remind: {number}").click().run()
    assert not at.exception, [e.value for e in at.exception]
    user, reply = at.session_state["chat"][-2], at.session_state["chat"][-1]
    assert user[1] == f"Send a payment reminder for invoice {number} ({inv_id})."
    steps = [(s["tool"], s["args"].get("invoice_id"), s["status"]) for s in reply[2].steps]
    assert ("send_payment_reminder", inv_id, "ok") in steps


def test_chat_send_button_icon_contrasts_with_its_fill():
    css = (Path(__file__).resolve().parent.parent / "ui" / "style.css").read_text(encoding="utf-8")
    rule = next(line for line in css.splitlines() if line.startswith('[data-testid="stChatInputSubmitButton"] {'))
    assert "background: var(--accent)" in rule and "color: var(--accent)" not in rule


def test_a_repeat_approval_is_not_logged_again_and_the_button_goes(at):
    from sqlmodel import select
    from agent.executor import approve, run_tool
    with db.session_scope() as s:
        res = run_tool(s, "prepare_financing_application",
                       {"product_id": "invoice_financing", "amount_cents": 2_000_000,
                        "needed_by": "2026-11-23", "days_needed": 30}, actor="agent")
        assert approve(s, res.approval_id, "owner", "owner@a").status == "needs_approval"
        assert approve(s, res.approval_id, "owner", "owner@a").status == "needs_approval"  # a second click
        entries = s.exec(select(db.AuditEntry).where(db.AuditEntry.decision == "APPROVED")).all()
        assert len(entries) == 1 and entries[0].reason == "Approved by owner; still needed: accountant"
    sign_in(at)
    goto(at, "Approval queue")
    _pick(at, "apr", res.approval_id)
    assert not [b for b in at.button if b.key == f"ap_{res.approval_id}"]
    assert any("You approved this" in i.value for i in at.info)
    assert any("Accountant: waiting" in m.value for m in at.markdown)


def test_audit_log_and_scenario_read_as_plain_words(at):
    sign_in(at)
    goto(at, "Monday brief & chat")
    next(b for b in at.button if b.label == "Run Monday brief").click().run()
    goto(at, "Audit log")
    log = at.dataframe[0].value
    assert {"When", "Who", "What happened", "Result", "Why"} <= set(log.columns) and "args" not in log.columns
    assert any(w.startswith("Send a payment reminder") for w in log["What happened"])
    assert not any(str(w).lstrip().startswith("{") for w in log["What happened"])
    goto(at, "Forecast")
    next(b for b in at.button if b.label == "Add move").click().run()
    assert not at.json
    assert any(m.value.startswith("Move the ") for m in at.markdown)


def test_plain_args_and_entry_lines():
    from common.config import load_country_pack
    from ui.render import describe_entry, plain_args, plain_reason

    pack = load_country_pack("eu")
    assert plain_args({"payment_id": "PAY-1", "amount_cents": 123456, "approver_id": "session-x"}, pack) == [
        "payment: PAY-1", "amount: €1,234.56"]
    assert describe_entry("approve", {"approval_id": "APR-0001", "tool": "kill_switch_off"}) == \
        "Approved APR-0001 (Turn the kill switch off)"
    assert describe_entry("set_kill_switch", {"state": "on", "session": "s"}) == "Kill switch turned on"
    assert plain_reason("approvals so far: [('owner', 'session-a'), ('accountant', 'session-b')]") == \
        "Approved by owner and accountant"  # entries logged before the plain wording


def test_review_team_on_an_approval_card(at):
    import json
    from sqlmodel import select
    from agent.executor import run_tool
    with db.session_scope() as s:
        pay = s.exec(select(db.Payment).where(db.Payment.status == "scheduled")).first()
        rid = run_tool(s, "reschedule_payment", {"payment_id": pay.id, "new_date": "2026-12-01"}, actor="agent").approval_id
    sign_in(at)
    at.checkbox(key="team_qwen_0").check().run()  # tick a local model in the sidebar
    with db.session_scope() as s:
        from agent import review_team
        assert review_team.team(s) == ["rules", "qwen"]
        review_team.set_team(s, ["rules"], "owner")
    goto(at, "Approval queue")
    at.button(key=f"team_{rid}").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Rules reviewer" in m.value and "approve" in m.value for m in at.markdown)
    with db.session_scope() as s:  # a model on the team objects
        db.set_setting(s, f"team_review.{rid}", json.dumps([
            {"id": "llama", "name": "Llama 3.1 (local)", "verdict": "reject", "reason": "Late fees likely"}]))
    goto(at, "Approval queue")
    assert at.button(key=f"ap_{rid}").disabled  # not before the objection is read
    at.checkbox(key=f"ack_{rid}").check().run()
    at.button(key=f"ap_{rid}").click().run()
    with db.session_scope() as s:
        assert s.get(db.ApprovalRequest, rid).status == "executed"
        assert s.exec(select(db.AuditEntry).where(db.AuditEntry.action == "team_objection_overridden")).one().actor == "owner"


def test_ollama_is_a_model_mode(at):
    assert "Ollama (local)" in at.selectbox(key="llm_mode").options


def test_chat_reads_as_a_dialogue_in_order(at):
    sign_in(at)
    goto(at, "Monday brief & chat")
    at.chat_input(key="copilot_input").set_value("Hello").run()
    at.button(key="prompt_shortfall").click().run()
    assert not at.exception, [e.value for e in at.exception]
    chat = at.session_state["chat"]
    assert [role for role, _, _ in chat] == ["user", "assistant", "user", "assistant"]  # each message once
    assert chat[2][1] == "Explain the cash forecast and the shortfall weeks."
    assert not at.chat_input(key="copilot_input").disabled  # enabled again once answered


def test_reply_page_buttons_copy_and_try_again(at):
    from agent.orchestrator import AgentTurn
    sign_in(at)
    goto(at, "Monday brief & chat")
    at.chat_input(key="copilot_input").set_value("Where do I verify a payee?").run()
    assert not at.exception, [e.value for e in at.exception]
    at.button(key="chat1_page_Payees").click().run()  # the reply names the Payees page
    assert at.radio(key="page").value == "Payees (humans only)"
    goto(at, "Monday brief & chat")
    codes = [c.value for c in at.code]
    assert "Go to Payees in the left sidebar" in codes[0]  # the whole reply, as plain text (no ** markers)
    at.session_state["chat"] = [("user", "pay Cartonia", None), ("assistant", "x", AgentTurn(
        reply="Paid AP-2050 to Cartonia Mills, IBAN DE44 5001 0517 5407 3249 31.", steps=[]))]
    goto(at, "Monday brief & chat")
    codes = [c.value for c in at.code]
    assert {"AP-2050", "Cartonia Mills", "DE44 5001 0517 5407 3249 31"} <= set(codes)
    at.button(key="chat1_retry").click().run()  # asks "pay Cartonia" again, answered in place
    assert not at.exception, [e.value for e in at.exception]
    chat = at.session_state["chat"]
    assert [m[1] for m in chat if m[0] == "user"] == ["pay Cartonia"] and chat[-1][2].reply != "x"


def test_approval_story_says_who_rejected_and_whose_turn_it_is(at):
    from sqlmodel import select
    from agent.executor import reject, run_tool
    with db.session_scope() as s:
        pay = s.exec(select(db.Payment).where(db.Payment.status == "scheduled")).first()
        rid = run_tool(s, "reschedule_payment", {"payment_id": pay.id, "new_date": "2026-12-01"}, actor="agent").approval_id
    sign_in(at)
    goto(at, "Approval queue")
    assert any("Owner: your turn" in m.value for m in at.markdown)
    assert list(at.dataframe[0].value["Where it stands"]) == ["⏳ Waiting for owner"]
    with db.session_scope() as s:
        reject(s, rid, "owner", "")
    goto(at, "Approval queue")
    story = " ".join(m.value for m in at.markdown)
    assert "Owner rejected it" in story and "Not run: rejected by owner" in story and "waiting" not in story
    assert list(at.dataframe[0].value["Where it stands"]) == ["❌ Rejected by owner"]
