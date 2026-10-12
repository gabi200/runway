"""Runway - Streamlit UI. Run with: .venv/bin/streamlit run ui/app.py

The UI is the *human* side of the system: it never gives the agent extra powers.
Agent actions go through agent.executor (rules engine + audit); human actions
(approve, clear flag, payees, kill switch) are audited with the acting role.
"""

from __future__ import annotations

import csv
from html import escape as esc
import io
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import altair as alt  # noqa: E402
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
from sqlmodel import select  # noqa: E402

from agent import documents as docs_mod  # noqa: E402
from agent import executor  # noqa: E402
from agent.orchestrator import Agent  # noqa: E402
from audit import log as audit  # noqa: E402
from bank.client import BankClient, BankError  # noqa: E402
from common import db  # noqa: E402
from common.db import (  # noqa: E402
    Account, ApprovalRequest, Counterparty, Document, FinancingApplication, Invoice,
    OutboxEmail, Payee, Payment, session_scope,
)
from data.generate import generate  # noqa: E402
from forecast.engine import build_forecast, explain_figure  # noqa: E402
from data.importer import ImportErrorReport, import_files  # noqa: E402
from ui import auth  # noqa: E402
from ui.helpers import BADGE_LEVELS, SAMPLE_DIR, badge, company_name, money, pack_for  # noqa: E402
from ui.render import describe_approval, safe_text  # noqa: E402

SECTIONS = ["Onboarding", "Monday brief & chat", "Forecast", "Approval queue", "Documents",
            "Payees (humans only)", "Mock bank", "Test inbox", "Audit log"]

st.set_page_config(page_title="Runway - AI finance copilot", page_icon=None, layout="wide")


# --------------------------------------------------------------------------- #
# Bootstrap and shared state
# --------------------------------------------------------------------------- #
def ensure_data() -> None:
    """Auto-generate sample data when the database is empty."""
    with session_scope() as s:
        empty = s.exec(select(Account)).first() is None
    if empty:
        generate(out_dir=SAMPLE_DIR)


def human_bank() -> BankClient:
    """Bank client with the HUMAN token. The agent never receives this token."""
    return BankClient(base_url=os.getenv("RUNWAY_BANK_URL") or None,
                      token=os.getenv("RUNWAY_BANK_HUMAN_TOKEN", "human-dev-token"))


def log_human(session, action: str, args: dict, decision: str, reason: str = "") -> None:
    """Audit a human action performed in the UI, attributed to the current role."""
    audit.append(session, st.session_state.role, action,
                 {**args, "session": st.session_state.sid}, decision, reason)


def get_agent() -> Agent:
    if "agent" not in st.session_state:
        st.session_state.agent = Agent(session_factory=session_scope)
    return st.session_state.agent


def init_state() -> None:
    auth.init()
    st.session_state.setdefault("pack", "eu")
    st.session_state.setdefault("chat", [])
    st.session_state.setdefault("brief", None)
    st.session_state.setdefault("confirm_reset", False)
    # Widget keys cannot be written after their widget exists; actions queue the value here.
    if "kill_next" in st.session_state:
        st.session_state.kill = st.session_state.pop("kill_next")
    elif "kill" not in st.session_state:
        with session_scope() as s:
            st.session_state.kill = db.kill_switch_on(s)


def on_kill_toggle() -> None:
    """Persist the kill switch and audit it with the acting human role."""
    on = bool(st.session_state.kill)
    if not auth.signed_in():  # human actions need a signed-in role
        with session_scope() as s:
            st.session_state.kill = db.kill_switch_on(s)
        st.session_state.flash = "Sign in to change the kill switch."
        return
    with session_scope() as s:
        db.set_setting(s, "kill_switch", "on" if on else "off")
        log_human(s, "set_kill_switch", {"state": "on" if on else "off"}, "INFO",
                  "Human toggled the kill switch from the UI")


def reset_demo() -> None:
    generate(out_dir=SAMPLE_DIR)
    st.session_state.update(agent=Agent(session_factory=session_scope), chat=[], brief=None,
                            kill_next=False, confirm_reset=False)
    st.session_state.pop("scenario", None)


# --------------------------------------------------------------------------- #
# Header
# --------------------------------------------------------------------------- #
def sign_in_box(col) -> None:
    """Demo-only PIN sign-in; switching role needs the other role's PIN."""
    if not auth.signed_in():
        col.selectbox("Sign in as", auth.ROLES, key="login_role")
        col.text_input("PIN (demo only)", type="password", key="login_pin",
                       help="Demo PINs from RUNWAY_OWNER_PIN / RUNWAY_ACCOUNTANT_PIN (defaults 1111 / 2222).")
        if col.button("Sign in", key="login_btn"):
            err = auth.sign_in(st.session_state.login_role, st.session_state.login_pin)
            if err:
                col.error(err)
            else:
                st.rerun()
        return
    col.markdown(f'<span class="pill ok">{esc(auth.identity())}</span>', unsafe_allow_html=True)
    other = [r for r in auth.ROLES if r != st.session_state.role]
    with col.expander("Switch role / sign out"):
        st.selectbox("Switch to", other, key="sw_role")
        st.text_input("PIN of that role", type="password", key="sw_pin")
        if st.button("Switch role", key="sw_btn"):
            err = auth.sign_in(st.session_state.sw_role, st.session_state.sw_pin)
            if err:
                st.error(err)
            else:
                st.rerun()
        if st.button("Sign out", key="signout_btn"):
            auth.sign_out()
            st.rerun()


def header() -> None:
    """Page title row in the main area; session controls live in the sidebar."""
    with session_scope() as s:
        acc = s.get(Account, "ACC-MAIN")
        today = db.get_today(s)
    st.markdown(f'<div class="page-top"><span class="company">{esc(company_name(acc.name if acc else None))}</span>'
                f'<span class="tag">Simulated today · {today:%a %d %b %Y}</span></div>', unsafe_allow_html=True)


def controls() -> None:
    """Sidebar: sign-in, display currency, kill switch, reset."""
    sb = st.sidebar
    sb.markdown('<div class="panel-label">Session</div>', unsafe_allow_html=True)
    sign_in_box(sb)
    sb.markdown('<div class="panel-label">Agent</div>', unsafe_allow_html=True)
    sb.toggle("Kill switch", key="kill", on_change=on_kill_toggle,
              help="ON = the agent may only run read-only (T0) tools. Every toggle is audited.")
    agent_on = not st.session_state.kill
    sb.markdown(f'<span class="live {"" if agent_on else "off"}"><span class="led"></span>'
                f'{"Agent active within rules" if agent_on else "Agent read-only"}</span>',
                unsafe_allow_html=True)
    if st.session_state.pop("flash", None):
        sb.warning("Sign in to change the kill switch.")
    sb.markdown('<div class="panel-label">Display</div>', unsafe_allow_html=True)
    sb.radio("Display currency", ["eu", "cn"], key="pack", horizontal=True,
             format_func=lambda c: pack_for(c).currency, label_visibility="collapsed",
             help="Display conversion only. Stored data stays in EUR.")
    sb.divider()
    if sb.button("Reset demo data", width="stretch") and auth.require_login("reset the demo"):
        st.session_state.confirm_reset = True
    if st.session_state.confirm_reset and auth.signed_in():
        sb.warning("This wipes the database (incl. audit log) and regenerates sample data.")
        a, b = sb.columns(2)
        if a.button("Confirm reset", type="primary"):
            reset_demo()
            st.rerun()
        if b.button("Cancel"):
            st.session_state.confirm_reset = False
            st.rerun()
    sb.caption("The agent proposes; rules and humans decide.")

# --------------------------------------------------------------------------- #
# 1. Onboarding
# --------------------------------------------------------------------------- #
def read_csv(upload) -> list[dict]:
    return list(csv.DictReader(io.StringIO(upload.getvalue().decode("utf-8-sig"))))


def num(x: str | None) -> float:
    try:
        return float(x) if x not in (None, "") else 0.0
    except ValueError:
        return 0.0


def page_onboarding() -> None:
    st.header("Onboarding")
    st.caption("A **trial balance** lists every account in the books with its total; debits must equal "
               "credits. The **bank statement** is the list of real money movements.")
    tb = st.file_uploader("Trial balance CSV", type="csv", key="up_tb")
    bs = st.file_uploader("Bank statement CSV", type="csv", key="up_bs")
    ar = st.file_uploader("Open receivables CSV (optional)", type="csv", key="up_ar")
    ap = st.file_uploader("Open payables CSV (optional)", type="csv", key="up_ap")
    st.caption("Optional history files. Without them the forecast cannot know each customer's usual "
               "lateness or which payments are already scheduled, so it differs from the demo company's.")
    pi = st.file_uploader("Paid invoices history CSV (optional)", type="csv", key="up_pi")
    sp = st.file_uploader("Scheduled payments CSV (optional)", type="csv", key="up_sp")
    notice = st.session_state.pop("notice", None)
    if notice:
        st.success(notice)
    st.caption("Generated sample data REPLACES the database, including the audit log.")
    if st.button("Use generated sample data") and auth.require_login("load the sample data"):
        with st.spinner("Generating 12 months of sample data..."):
            summary = generate(out_dir=SAMPLE_DIR)
        st.session_state.update(agent=Agent(session_factory=session_scope), chat=[], brief=None,
                                kill_next=False, notice="Sample data loaded.")
        st.session_state.pop("scenario", None)
        with session_scope() as s:
            log_human(s, "load_sample_data", {k: v for k, v in summary.items() if k != "warnings"},
                      "HUMAN_ACTION", "Sample data regenerated from the UI")
        st.rerun()
    if tb is not None:
        rows = read_csv(tb)
        debit, credit = sum(num(r.get("debit")) for r in rows), sum(num(r.get("credit")) for r in rows)
        st.subheader("Trial balance summary")
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
        ok = abs(debit - credit) < 0.005
        (st.success if ok else st.error)(
            f"{len(rows)} accounts. Debits {debit:,.2f}, credits {credit:,.2f}: "
            + ("balanced." if ok else "NOT balanced, check the file."))
    if bs is not None:
        rows = read_csv(bs)
        total_in = sum(num(r["amount"]) for r in rows if num(r.get("amount")) > 0)
        total_out = sum(num(r["amount"]) for r in rows if num(r.get("amount")) < 0)
        st.subheader("Bank statement summary")
        st.dataframe(pd.DataFrame(rows).tail(20), width="stretch", hide_index=True)
        st.info(f"{len(rows)} transactions. Money in {total_in:,.2f}, money out {abs(total_out):,.2f}.")
    if tb is not None and bs is not None:
        st.warning("Importing REPLACES the whole database with the uploaded company. The current audit log "
                   "will be replaced too. Payees created by an import are unverified until a human "
                   "verifies them.")
        sure = st.checkbox("I understand the current data and audit log will be replaced", key="imp_confirm")
        if st.button("Import uploaded files", type="primary", disabled=not sure) \
                and auth.require_login("import data"):
            try:
                summary = import_files(
                    None, tb.getvalue(), bs.getvalue(),
                    ar.getvalue() if ar is not None else None, ap.getvalue() if ap is not None else None,
                    paid_invoices_csv=pi.getvalue() if pi is not None else None,
                    scheduled_payments_csv=sp.getvalue() if sp is not None else None)
            except ImportErrorReport as exc:
                st.error(f"Import rejected, nothing was changed: {exc}")
            else:
                st.session_state.update(agent=Agent(session_factory=session_scope), chat=[], brief=None,
                                        kill_next=False)
                st.session_state.pop("scenario", None)
                with session_scope() as s:
                    log_human(s, "import_files", {k: v for k, v in summary.items() if k != "warnings"},
                              "HUMAN_ACTION", "Company data imported from uploaded CSV files")
                st.success("Imported. The forecast now uses the uploaded data.")
                for w in summary["warnings"]:
                    st.warning(w)
                st.json(summary, expanded=False)
    elif tb is not None or bs is not None:
        st.caption("Upload both the trial balance and the bank statement to enable the import.")
    st.subheader("Current database")
    with session_scope() as s:
        counts = {"invoices": len(s.exec(select(Invoice)).all()),
                  "open AR invoices": len(s.exec(select(Invoice).where(
                      Invoice.direction == "AR", Invoice.status == "open")).all()),
                  "open AP invoices": len(s.exec(select(Invoice).where(
                      Invoice.direction == "AP", Invoice.status == "open")).all()),
                  "payees": len(s.exec(select(Payee)).all()),
                  "documents": len(s.exec(select(Document)).all()),
                  "balance": money(pack_for(st.session_state.pack), db.account_balance_cents(s, as_of=db.get_today(s)))}
    cols = st.columns(len(counts))
    for col, (k, v) in zip(cols, counts.items()):
        col.metric(k, v, border=True)


# --------------------------------------------------------------------------- #
# 2. Monday brief + chat
# --------------------------------------------------------------------------- #
def render_steps(steps: list[dict]) -> None:
    if not steps:
        st.caption("No tools were called.")
        return
    for i, step in enumerate(steps, 1):
        st.markdown(
            f"{i}. `{str(step['tool']).replace(chr(96), '')}` {badge(step['status'])}"
            + (f" approval **{safe_text(step['approval_id'])}**" if step.get("approval_id") else ""),
            unsafe_allow_html=True)
        if step.get("reason"):
            st.caption(f"Reason: {safe_text(step['reason'])}")
        with st.expander("arguments", expanded=False):
            st.json(step.get("args") or {})


def render_turn(turn) -> None:
    if turn.refused:
        st.warning("The agent refused this request (logged in the audit trail).")
    st.markdown(safe_text(turn.reply))
    with st.expander(f"Tool trace ({len(turn.steps)} steps)", expanded=True):
        st.caption("Every tool call is checked by the rules engine first: ALLOW runs, NEEDS_APPROVAL "
                   "waits for a human, DENY is blocked.")
        render_steps(turn.steps)


def page_chat() -> None:
    st.header("Monday brief & chat")
    st.caption("The agent proposes and prepares; it cannot move money outside the rules. "
               "Anything above its limits lands in the approval queue.")
    if st.button("Run Monday brief", type="primary") and auth.require_login("run the Monday brief"):
        with st.spinner("Agent is working..."):
            st.session_state.brief = get_agent().monday_brief()
    if st.session_state.brief is not None:
        st.subheader("Monday brief")
        render_turn(st.session_state.brief)
    st.subheader("Chat")
    for role, content, turn in st.session_state.chat:
        with st.chat_message(role):
            if turn is None:
                st.markdown(safe_text(content))
            else:
                render_turn(turn)
    msg = st.chat_input("Ask about cash, invoices, financing...")
    if msg and not auth.require_login("chat with the agent"):
        msg = None
    if msg:
        st.session_state.chat.append(("user", msg, None))
        with st.spinner("Thinking..."):
            turn = get_agent().chat(msg, actor=st.session_state.role)
        st.session_state.chat.append(("assistant", turn.reply, turn))
        st.rerun()


# --------------------------------------------------------------------------- #
# 3. Forecast
# --------------------------------------------------------------------------- #
def weeks_frame(fc, pack) -> pd.DataFrame:
    return pd.DataFrame([{
        "Week": w.index, "Start": w.start, "End": w.end,
        "Opening": money(pack, w.opening_cents), "Receipts (in)": money(pack, w.receipts_cents),
        "Payments (out)": money(pack, w.payments_cents), "Closing": money(pack, w.closing_cents),
        "Shortfall": "SHORTFALL" if w.index in fc.shortfall_weeks else "",
    } for w in fc.weeks])


def closing_chart(fc, pack, fc2=None):
    rows = []
    for w in fc.weeks:
        rows.append({"week": w.index, "closing": pack.convert_from_eur_cents(w.closing_cents) / 100,
                     "series": "Baseline", "short": w.index in fc.shortfall_weeks})
    if fc2 is not None:
        for w in fc2.weeks:
            rows.append({"week": w.index, "closing": pack.convert_from_eur_cents(w.closing_cents) / 100,
                         "series": "With scenario", "short": w.index in fc2.shortfall_weeks})
    df = pd.DataFrame(rows)
    if fc2 is None:
        base = alt.Chart(df).mark_bar().encode(
            x=alt.X("week:O", title="Week"), y=alt.Y("closing:Q", title=f"Closing balance ({pack.currency})"),
            color=alt.condition("datum.short", alt.value("#c62828"), alt.value("#2b6cb0")),
            tooltip=["week", "closing"])
    else:
        base = alt.Chart(df).mark_line(point=True).encode(
            x=alt.X("week:O", title="Week"), y=alt.Y("closing:Q", title=f"Closing balance ({pack.currency})"),
            color=alt.Color("series:N", scale=alt.Scale(range=["#888888", "#1b7f3b"])),
            tooltip=["series", "week", "closing"])
    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color="black", strokeDash=[4, 4]).encode(y="y:Q")
    return (base + zero).properties(height=320)


def page_forecast() -> None:
    pack = pack_for(st.session_state.pack)
    st.header("13-week cash forecast")
    st.caption("A **cash-flow forecast** predicts the bank balance week by week. A **shortfall** is a week "
               "where the balance drops below the safety minimum (0). It is computed by plain code "
               "from ledger rows, no AI.")
    with session_scope() as s:
        fc = build_forecast(s)
    if fc.shortfall_weeks:
        st.error(f"Shortfall in week(s) {', '.join(map(str, fc.shortfall_weeks))}. "
                 f"Lowest point: week {fc.min_week} at {money(pack, fc.min_closing_cents)}.")
    else:
        st.success(f"No shortfall. Lowest point: week {fc.min_week} at {money(pack, fc.min_closing_cents)}.")
    st.altair_chart(closing_chart(fc, pack), width="stretch")
    st.caption("Click a row (checkbox on the left) to choose the week to trace. "
               f"Default: the lowest week ({fc.min_week}).")
    sel = st.dataframe(weeks_frame(fc, pack), width="stretch", hide_index=True, key="fc_table",
                       on_select="rerun", selection_mode="single-row")
    rows = list(sel.selection.rows) if sel is not None and sel.selection else []
    selected_week = rows[0] + 1 if rows else (fc.min_week or 1)

    st.subheader("Where does a number come from?")
    field = st.radio("Figure", ["closing_cents", "receipts_cents", "payments_cents", "opening_cents"],
                     horizontal=True, key="fc_field")
    week = selected_week
    with session_scope() as s:
        ex = explain_figure(s, week, field)
    st.markdown(f"**Selected: week {week}** ({ex['start']} to {ex['end']}) - `{field}` = "
                f"**{money(pack, ex['value_cents'])}**")
    if ex.get("note"):
        st.caption(ex["note"])
    if ex["lines"]:
        st.dataframe(pd.DataFrame([{
            "Label": ln["label"], "Kind": ln["kind"], "Amount": money(pack, ln["amount_cents"]),
            "Expected": ln["expected_date"], "Sources": ", ".join(ln["source_refs"]), "Note": ln["note"],
        } for ln in ex["lines"]]), width="stretch", hide_index=True)
    for name, rows in ex["sources"].items():
        if rows:
            with st.expander(f"Source ledger rows: {name} ({len(rows)})"):
                st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    st.subheader("What-if scenario")
    st.caption("Try a change without touching any data, then compare before and after.")
    scenario_panel(fc, pack)


def scenario_panel(fc, pack) -> None:
    st.session_state.setdefault("scenario", [])
    with session_scope() as s:
        customers = [(c.id, c.name) for c in s.exec(select(Counterparty).where(Counterparty.kind == "customer"))]
        ledger_cur = (s.get(Account, "ACC-MAIN").currency if s.get(Account, "ACC-MAIN") else "")
        pays = [(p.id, f"{p.id} - {p.payee_id} - {pack.display(p.amount_cents)} on {p.execute_date}")
                for p in s.exec(select(Payment).where(Payment.status == "scheduled"))]
        today = db.get_today(s)
    t1, t2, t3 = st.tabs(["Delay a customer", "Add an inflow", "Move a payment"])
    with t1:
        cid = st.selectbox("Customer", customers, format_func=lambda c: c[1], key="sc_cust")
        days = st.number_input("Delay (days)", 1, 120, 30, key="sc_days")
        if st.button("Add delay") and cid:
            st.session_state.scenario.append({"type": "delay_customer", "counterparty_id": cid[0], "days": int(days)})
    with t2:
        amt = st.number_input(f"Amount ({ledger_cur}, ledger currency)", 0.0, 10_000_000.0, 20_000.0, step=1000.0, key="sc_amt")
        wk = st.number_input("Week", 1, 13, 5, key="sc_wk")
        lbl = st.text_input("Label", "financing", key="sc_lbl")
        if st.button("Add inflow"):
            st.session_state.scenario.append({"type": "inflow", "amount_cents": int(round(amt * 100)),
                                              "week": int(wk), "label": lbl or "inflow"})
    with t3:
        if pays:
            pay = st.selectbox("Scheduled payment", pays, format_func=lambda p: p[1], key="sc_pay")
            nd = st.date_input("New date", today + timedelta(days=35), key="sc_date")
            if st.button("Add move"):
                st.session_state.scenario.append({"type": "move_payment", "payment_id": pay[0],
                                                  "new_date": nd.isoformat()})
        else:
            st.info("No scheduled payments.")
    scen = st.session_state.scenario
    if not scen:
        return
    st.write("Scenario changes:")
    st.json(scen, expanded=False)
    if st.button("Clear scenario"):
        st.session_state.scenario = []
        st.rerun()
    try:
        with session_scope() as s:
            fc2 = build_forecast(s, scenario=scen)
    except Exception as exc:  # invalid scenario input: show, do not crash
        st.error(f"Scenario could not be applied: {exc}")
        return
    m1, m2 = st.columns(2)
    m1.metric("Lowest balance before", money(pack, fc.min_closing_cents), f"week {fc.min_week}", delta_color="off", border=True)
    m2.metric("Lowest balance after", money(pack, fc2.min_closing_cents),
              money(pack, fc2.min_closing_cents - fc.min_closing_cents), border=True)
    st.altair_chart(closing_chart(fc, pack, fc2), width="stretch")
    st.dataframe(pd.DataFrame({
        "Week": [w.index for w in fc.weeks],
        "Closing before": [money(pack, w.closing_cents) for w in fc.weeks],
        "Closing after": [money(pack, w.closing_cents) for w in fc2.weeks]}),
        width="stretch", hide_index=True)


# --------------------------------------------------------------------------- #
# 4. Approval queue
# --------------------------------------------------------------------------- #
def page_approvals() -> None:
    st.header("Approval queue")
    role = st.session_state.role
    pack = pack_for(st.session_state.pack)
    can_act = auth.signed_in()
    st.caption((f"Signed in as **{auth.identity()}**. " if can_act else "Not signed in: sign in to approve or reject. ")
               + "Risky agent actions wait here. Large amounts need two roles "
               "(four-eyes principle: two different people must agree; one browser session cannot fill both roles).")
    with session_scope() as s:
        pending = executor.pending_approvals(s)
        history = list(s.exec(select(ApprovalRequest).where(ApprovalRequest.status != "pending")
                              .order_by(ApprovalRequest.created_at.desc())))
        pending_view = [(r.id, r.tool_name, json.loads(r.args_json), json.loads(r.required_roles_json),
                         [g['role'] if isinstance(g, dict) else g for g in json.loads(r.approvals_json)], r.reason,
                         describe_approval(s, r.tool_name, json.loads(r.args_json), pack)) for r in pending]
    if not pending_view:
        st.info("Nothing waiting for approval.")
    for rid, tool, args, required, given, reason, summary in pending_view:
        with st.container(border=True):
            st.markdown(f"**{rid}** `{tool}` {badge('NEEDS_APPROVAL')}", unsafe_allow_html=True)
            for line in summary:
                st.markdown(safe_text(line))
            st.caption(f"Why: {safe_text(reason)}")
            st.write(f"Required roles: {', '.join(required)}  |  Approved so far: {', '.join(given) or 'nobody'}")
            with st.expander("Raw arguments"):
                st.json(args, expanded=True)
            a, b, _ = st.columns([1, 1, 4])
            if a.button("Approve", key=f"ap_{rid}", disabled=not can_act):
                with session_scope() as s:
                    res = executor.approve(s, rid, role, approver_id=auth.approver_id())
                if res.status == "ok":
                    st.rerun()
                elif res.status == "needs_approval":
                    st.success("Your approval is recorded. Waiting for the other role "
                               "(it must be signed in from a different browser session).")
                else:
                    st.error(f"Not approved: {res.reason}. The same person cannot give both approvals "
                             "of a four-eyes request.")
            if b.button("Reject", key=f"rj_{rid}", disabled=not can_act):
                with session_scope() as s:
                    executor.reject(s, rid, role, "rejected in UI")
                st.rerun()
    st.subheader("History")
    if history:
        st.dataframe(pd.DataFrame([{
            "ID": r.id, "Tool": r.tool_name, "Status": r.status, "Required": r.required_roles_json,
            "Approved by": r.approvals_json, "Reason": r.reason, "Decided": r.decided_at,
            "Args": r.args_json} for r in history]), width="stretch", hide_index=True)
    else:
        st.caption("No decided requests yet.")


# --------------------------------------------------------------------------- #
# 5. Documents
# --------------------------------------------------------------------------- #
def page_documents() -> None:
    st.header("Documents")
    st.caption("Incoming PDFs and emails are **untrusted data**. They are scanned for hidden instructions "
               "and bank-detail-change tricks (a classic fraud). Flagged documents block any linked payment "
               "until the owner or accountant clears them.")
    up = st.file_uploader("Upload an invoice PDF or email text", type=["pdf", "txt", "eml"], key="up_doc")
    kind = st.selectbox("Kind", ["invoice", "email", "other"])
    sender = st.text_input("Sender (optional)")
    if up is not None and st.button("Ingest uploaded document"):
        with session_scope() as s:
            data = up.getvalue()
            if data[:5] == b"%PDF-":
                doc = docs_mod.ingest_document(s, data, up.name, kind, sender or None)
            else:
                doc = docs_mod.ingest_text(s, data.decode("utf-8", errors="replace"), up.name, kind,
                                           sender or None)
            st.session_state.doc_result = (doc.id, doc.flagged, doc.flag_reason)
        st.rerun()
    result = st.session_state.pop("doc_result", None)
    if result:  # shown full width after the rerun, so the banner count is already updated
        did, flagged, reason = result
        if flagged:
            st.error(f"**{did} flagged.** {safe_text(reason)}. Linked payments are blocked until a human clears it.")
        else:
            st.success(f"**{did}** ingested, no indicators found.")
    st.subheader("Demo documents")
    sample_dir = SAMPLE_DIR / "docs"
    if sample_dir.is_dir():
        cols = st.columns(3)
        for col, f in zip(cols, sorted(sample_dir.iterdir())):
            if col.button(f"Ingest {f.name}", key=f"samp_{f.name}"):
                with session_scope() as s:
                    k = "email" if f.suffix in (".txt", ".eml") else "invoice"
                    doc = docs_mod.ingest_file(s, f, k, None, sample_dir)
                    st.session_state.doc_result = (doc.id, doc.flagged, doc.flag_reason)
                st.rerun()
    else:
        st.caption("No sample documents found. Use 'Reset demo data'.")
    st.subheader("Stored documents")
    with session_scope() as s:
        docs = list(s.exec(select(Document).order_by(Document.id)))
        view = [(d.id, d.filename, d.kind, d.sender, d.flagged, d.cleared, d.flag_reason, d.content_text)
                for d in docs]
    if not view:
        st.caption("No documents yet.")
    for did, fn, k, snd, flagged, cleared, reason, text in view:
        status = ":blue-badge[Cleared by human]" if (flagged and cleared) else (
            ":red-badge[Flagged]" if flagged else ":green-badge[Clean]")
        with st.expander(f"{status} {safe_text(f'{did} - {fn}')}"):
            if flagged:
                st.warning(f"Reasons: {safe_text(reason)}")
            st.caption(safe_text(f"Kind: {k}  |  Sender: {snd or 'unknown'}"))
            st.text(text[:3000])
            if flagged and not cleared:
                if auth.signed_in():
                    if st.button("Clear flag (I reviewed this document)", key=f"clr_{did}"):
                        with session_scope() as s:
                            result = executor.clear_document_flag(
                                s, did, st.session_state.role, "Reviewed and cleared in the UI")
                        if result.status == "ok":
                            st.rerun()
                        st.error(result.reason)
                else:
                    st.caption("Sign in as owner or accountant to clear a flag.")


# --------------------------------------------------------------------------- #
# 6. Payees
# --------------------------------------------------------------------------- #
def page_payees() -> None:
    st.header("Payees (human only)")
    st.info("A **payee** is someone we pay; it must be **verified** before any payment. The agent has "
            "**no tool** to add or change payees or IBANs, and the bank rejects its token on this endpoint. "
            "Only a human with the human bank token can do it here.")
    try:
        with human_bank() as bank:
            payees = bank.list_payees()
    except BankError as exc:
        st.error(f"Bank error: {exc.detail}")
        return
    st.dataframe(pd.DataFrame(payees), width="stretch", hide_index=True,
                 column_order=["name", "verified", "iban", "id", "created_by"],
                 column_config={"name": "Name", "verified": "Verified", "iban": "IBAN", "id": "ID",
                                "created_by": "Created by"})
    if st.session_state.role != "owner":
        st.caption("Sign in as the owner to add or verify payees.")
        return
    for payee in payees:
        details, action = st.columns([4, 1])
        details.write(f"{payee['name']} ({payee['id']})")
        if not payee["iban"].strip():  # imported payees arrive without bank details
            new_iban = details.text_input("IBAN (can be set once)", key=f"iban_{payee['id']}")
            if details.button("Set IBAN", key=f"set_iban_{payee['id']}") and new_iban:
                try:
                    with human_bank() as bank:
                        bank.set_payee_iban(payee["id"], new_iban)
                    with session_scope() as s:
                        log_human(s, "set_payee_iban", {"payee_id": payee["id"]},
                                  "HUMAN_ACTION", "IBAN filled in via the bank's human token; payee unverified")
                    st.rerun()
                except BankError as exc:
                    st.error(f"Bank refused: {exc.detail}")
        verified = not payee["verified"]
        if action.button("Verify" if verified else "Unverify", key=f"verify_{payee['id']}"):
            try:
                with human_bank() as bank:
                    bank.verify_payee(payee["id"], verified)
                with session_scope() as s:
                    log_human(s, "verify_payee", {"payee_id": payee["id"], "verified": verified},
                              "HUMAN_ACTION", "Verification changed via the bank's human token")
                st.rerun()
            except BankError as exc:
                st.error(f"Bank refused: {exc.detail}")
    st.subheader("Add a payee")
    with session_scope() as s:
        cps = [(None, "(none)")] + [(c.id, c.name) for c in s.exec(select(Counterparty))]
    with st.form("add_payee"):
        name = st.text_input("Name")
        iban = st.text_input("IBAN")
        cp = st.selectbox("Counterparty", cps, format_func=lambda c: c[1])
        verified = st.checkbox("Verified (I confirmed these bank details by phone / in person)")
        if st.form_submit_button("Add payee") and name and iban and auth.require_login("add a payee"):
            try:
                with human_bank() as bank:
                    row = bank.create_payee(name, iban, cp[0], verified)
                with session_scope() as s:
                    log_human(s, "add_payee", {"payee_id": row["id"], "name": name, "verified": verified},
                              "EXECUTED", "Created by a human via the bank's human token")
                st.success(f"Created {row['id']} (verified={row['verified']}).")
            except BankError as exc:
                st.error(f"Bank refused: {exc.detail}")
    st.caption("A missing IBAN can be filled in once. To use a different IBAN, create a new payee: "
               "existing bank details cannot be edited.")


# --------------------------------------------------------------------------- #
# 7. Mock bank
# --------------------------------------------------------------------------- #
def page_bank() -> None:
    pack = pack_for(st.session_state.pack)
    st.header("Mock bank")
    st.caption("A simulated bank with its own checks: it independently refuses payments to unverified payees.")
    try:
        with human_bank() as bank:
            accounts, payments = bank.get_accounts(), bank.list_payments()
    except BankError as exc:
        st.error(f"Bank error: {exc.detail}")
        return
    for a in accounts:
        st.metric(f"{a['name']} ({a['id']})", money(pack, a["balance_cents"]), border=True)
    for status in ("scheduled", "executed", "cancelled"):
        rows = [p for p in payments if p["status"] == status]
        st.subheader(f"{status.capitalize()} payments ({len(rows)})")
        if rows:
            df = pd.DataFrame(rows)
            df["amount"] = [money(pack, v) for v in df["amount_cents"]]
            # Key columns first with readable names; raw fields stay below the fold of the table
            st.dataframe(df, width="stretch", hide_index=True,
                         column_order=["id", "payee_id", "amount", "execute_date", "invoice_id",
                                       "automatic", "created_by", "idempotency_key"],
                         column_config={"id": "Payment", "payee_id": "Payee", "amount": "Amount",
                                        "execute_date": "Executes on", "invoice_id": "Invoice",
                                        "automatic": "Auto (T2)", "created_by": "Created by",
                                        "idempotency_key": "Idempotency key"})
    st.subheader("Advance the simulated clock")
    st.caption("Moves 'today' forward and executes scheduled payments that fall due.")
    days = st.number_input("Days", 1, 120, 7)
    if st.button("Advance") and auth.require_login("advance the clock"):
        try:
            with human_bank() as bank:
                res = bank.advance(int(days))
            with session_scope() as s:
                log_human(s, "simulate_advance", {"days": int(days)}, "HUMAN_ACTION", "Simulated clock advanced")
            st.success("Clock advanced.")
            st.json(res, expanded=False)
        except BankError as exc:
            st.error(exc.detail)


# --------------------------------------------------------------------------- #
# 8. Test inbox
# --------------------------------------------------------------------------- #
def page_inbox() -> None:
    pack = pack_for(st.session_state.pack)
    st.header("Test inbox")
    st.caption("Nothing is really sent. Emails the system would send (reminders, fraud alerts) land here.")
    with session_scope() as s:
        mails = list(s.exec(select(OutboxEmail).order_by(OutboxEmail.sent_at.desc())))
        apps = list(s.exec(select(FinancingApplication).order_by(FinancingApplication.id)))
        mview = [(m.id, m.kind, m.to, m.subject, m.body, m.invoice_id, m.sent_at) for m in mails]
        aview = [(a.id, a.product_id, a.amount_cents, a.needed_by, a.status, a.pdf_path) for a in apps]
    st.subheader(f"Emails ({len(mview)})")
    if not mview:
        st.caption("No emails yet. Run the Monday brief to create reminders.")
    for mid, kind, to, subj, body, inv, sent in mview:
        with st.expander(safe_text(f"[{kind}] {subj}  ->  {to}")):
            st.caption(f"{mid} | invoice: {inv or '-'} | {sent}")
            st.text(body)
    st.subheader(f"Financing applications ({len(aview)})")
    st.caption("Prepared, never submitted. A human reviews and signs the PDF.")
    if not aview:
        st.caption("None prepared yet.")
    for aid, prod, amt, by, status, path in aview:
        st.write(f"**{aid}** - {prod} - {money(pack, amt)} needed by {by} ({status})")
        p = Path(path)
        p = p if p.is_absolute() else REPO_ROOT / p
        if p.is_file():
            st.download_button(f"Download {p.name}", p.read_bytes(), file_name=p.name,
                               mime="application/pdf", key=f"dl_{aid}")
        else:
            st.caption(f"PDF not found at {path}")


# --------------------------------------------------------------------------- #
# 9. Audit log
# --------------------------------------------------------------------------- #
def page_audit() -> None:
    st.header("Audit log")
    st.caption("Every decision is recorded in a **hash chain**: each entry contains the hash of the previous "
               "one, so any edit or deletion breaks the chain and is detected.")
    if st.button("Verify chain", type="primary"):
        with session_scope() as s:
            rep = audit.verify_chain(s)
        if rep.ok:
            st.success(f"✅ Chain intact - {rep.checked} entries verified.")
        else:
            st.error(f"🚨 TAMPERING DETECTED - first bad entry id: {rep.first_bad_id} ({rep.message})")
    with session_scope() as s:
        entries = audit.list_entries(s)
        rows = [{"id": e.id, "time": e.timestamp, "actor": e.actor, "action": e.action,
                 "decision": e.decision, "reason": e.reason, "hash": e.hash[:12], "args": e.args_json}
                for e in entries]
    st.write(f"{len(rows)} entries (newest first)")
    df = pd.DataFrame(rows[::-1])
    if not df.empty:
        df["time"] = pd.to_datetime(df["time"], utc=True, format="mixed").dt.strftime("%Y-%m-%d %H:%M:%S")
    colors = {"ok": "#2ea043", "attention": "#d29922", "danger": "#f85149"}
    st.dataframe(df.style.map(lambda d: f"color: {colors[BADGE_LEVELS[d]]}; font-weight: 700"
                              if d in BADGE_LEVELS else "", subset=["decision"]) if not df.empty else df,
                 width="stretch", hide_index=True)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
PAGES = {"Onboarding": page_onboarding, "Monday brief & chat": page_chat, "Forecast": page_forecast,
         "Approval queue": page_approvals, "Documents": page_documents,
         "Payees (humans only)": page_payees, "Mock bank": page_bank, "Test inbox": page_inbox,
         "Audit log": page_audit}


def status_banner() -> None:
    """Canary-style headline: is cash safe over the next 13 weeks, and what waits on a human."""
    pack = pack_for(st.session_state.pack)
    with session_scope() as s:
        fc = build_forecast(s)
        balance = db.account_balance_cents(s, as_of=db.get_today(s))
        pending = len(executor.pending_approvals(s))
        flagged = len(s.exec(select(Document).where(Document.flagged == True, Document.cleared == False)).all())  # noqa: E712
    if fc.shortfall_weeks:
        level, status = "danger", "Cash shortfall ahead"
        headline = f"Cash goes below zero in week {', '.join(map(str, fc.shortfall_weeks))}"
    elif pending or flagged:
        level, status, headline = "attention", "Action needed", "Cash stays positive for 13 weeks"
    else:
        level, status, headline = "ok", "All clear", "Cash stays positive for 13 weeks"
    sub = f"Lowest point: week {fc.min_week} at {money(pack, fc.min_closing_cents)}"
    low = "danger" if fc.min_closing_cents < 0 else ""

    def metric(n, label, cls=""):
        return f'<div><div class="n {cls}">{esc(str(n))}</div><div class="l">{label}</div></div>'

    st.markdown(
        f'<div class="banner {level}"><div class="grow"><div class="status">{status}</div>'
        f'<div class="headline">{esc(headline)}</div><div class="sub">{esc(sub)}</div></div>'
        + metric(money(pack, balance), "Bank balance")
        + metric(money(pack, fc.min_closing_cents), "13-week low", low)
        + metric(pending, "Awaiting approval", "attention" if pending else "")
        + metric(flagged, "Flagged documents", "danger" if flagged else "")
        + "</div>", unsafe_allow_html=True)


def sidebar() -> str:
    st.sidebar.markdown('<div class="brand">Runway<span class="dot">.</span></div>', unsafe_allow_html=True)
    st.sidebar.caption("AI finance copilot for SMEs")
    page = st.sidebar.radio("Navigation", SECTIONS, key="page", label_visibility="collapsed")
    controls()
    return page


def main() -> None:
    st.markdown(f"<style>{(Path(__file__).parent / 'style.css').read_text()}</style>", unsafe_allow_html=True)
    ensure_data()
    init_state()
    page = sidebar()
    header()
    status_banner()
    PAGES[page]()


main()
