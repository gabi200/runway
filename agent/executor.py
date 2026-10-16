"""Tool executor: policy -> audit -> execute, plus the human approval workflow.

New calls are evaluated by `policy.engine.evaluate`; exact payment replays return
the original record. Every decision is written to the hash-chained audit log.
The agent never sees tokens, IBANs or bank URLs.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta


from sqlalchemy import update
from sqlmodel import Session, select

from agent import mandate
from agent.financing import account_currency, compare_financing  # noqa: F401  (compare_financing re-exported per CONTRACTS section 6)
from agent.outbox import next_id
from agent.tools import HANDLERS, TOOL_SPECS, ToolError, ToolResult, _need_defaults, idempotency_key, jsonable  # noqa: F401
from audit import log as audit
from bank.client import BankClient, BankError, bank_token
from common.db import (Account, ApprovalRequest, Document, Payee, Payment, Setting, get_setting, get_today,
                       kill_switch_on, set_setting, utcnow)
from policy import engine as policy

VALID_ROLES = ("owner", "accountant")


def _sim_now(session: Session) -> datetime:
    """Approval timestamps combine simulated today with the current UTC time and timezone."""
    return datetime.combine(get_today(session), utcnow().timetz())


def clear_document_flag(session: Session, document_id: str, role: str,
                        note: str = "") -> ToolResult:
    """Record a human review and clear a document's payment block."""
    action = "clear_document_flag"
    if role not in VALID_ROLES:
        return ToolResult(status="error", tool=action, reason="Only owner or accountant can clear a flag")
    document = session.get(Document, document_id)
    if document is None:
        return ToolResult(status="error", tool=action, reason="Document not found")
    document.cleared = True
    session.add(document)
    audit.append(session, role, action, {"document_id": document_id, "note": note},
                 "HUMAN_ACTION", note)
    return ToolResult(status="ok", tool=action, data={"document_id": document_id, "cleared": True})


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _execute(session: Session, tool: str, args: dict, actor: str, automatic: bool,
             signed_mandate: dict | None = None) -> ToolResult:
    """Run the handler and audit EXECUTED / FAILED."""
    handler = HANDLERS.get(tool) or HUMAN_HANDLERS.get(tool)
    try:
        if handler is None:
            raise ToolError(f"no implementation for tool {tool!r}")
        data = jsonable(handler(session, args, {"automatic": automatic, "actor": actor, "mandate": signed_mandate}))
    except ToolError as e:
        session.rollback()
        audit.append(session, actor, tool, args, "FAILED", str(e))
        return ToolResult(status="error", tool=tool, reason=str(e))
    except Exception as e:  # noqa: BLE001 - tool failures must never crash the agent loop
        session.rollback()
        audit.append(session, actor, tool, args, "FAILED", f"{type(e).__name__}: {e}")
        return ToolResult(status="error", tool=tool, reason=f"{type(e).__name__}: {e}")
    audit.append(session, actor, tool, args, "EXECUTED", "")
    return ToolResult(status="ok", tool=tool, data=data)


def run_tool(session: Session, tool_name: str, args: dict, actor: str = "agent") -> ToolResult:
    """Evaluate policy, audit the decision, then execute, park for approval, or deny."""
    args = dict(args or {})
    if tool_name in {"compare_financing", "prepare_financing_application"}:
        try:  # policy must judge the figures the tool will actually use
            args = _need_defaults(session, args)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, ToolError) else f"Financing defaults unavailable: {type(exc).__name__}"
            audit.append(session, actor, tool_name, args, "DENY", reason)
            return ToolResult(status="denied", tool=tool_name, reason=reason)
    if tool_name == "schedule_payment":
        try:
            policy.validate_call(tool_name, args)
        except (ValueError, TypeError, KeyError) as exc:
            audit.append(session, actor, tool_name, args, "DENY", str(exc))
            return ToolResult(status="denied", tool=tool_name, reason=str(exc))
        key = args.get("idempotency_key") or idempotency_key(args)
        existing = session.exec(select(Payment).where(Payment.idempotency_key == key)
                                .execution_options(populate_existing=True)).first()
        if existing is not None:
            # Replays skip evaluate(), so runtime controls are checked here.
            session.exec(select(Setting).where(Setting.key == "kill_switch")
                         .execution_options(populate_existing=True)).all()
            if get_setting(session, "kill_switch", "off") != "off":
                reason = "Kill switch restricts tools to T0"
                audit.append(session, actor, tool_name, args, "DENY", reason)
                return ToolResult(status="denied", tool=tool_name, reason=reason)
            try:
                if actor not in {"agent", "owner", "accountant", "system"}:
                    raise ValueError("Unknown actor")
                stored = session.get(Setting, f"bank.request.{existing.id}")
                original = json.loads(stored.value) if stored else existing.model_dump(mode="json")
                account = session.get(Account, "ACC-MAIN", populate_existing=True)
                if account is None:
                    raise ValueError("Main account not found")
                body = {"idempotency_key": key, "payee_id": args["payee_id"],
                        "amount_cents": args["amount_cents"],
                        "currency": args.get("currency", account.currency),
                        "execute_date": str(args["execute_date"]),
                        "invoice_id": args.get("invoice_id"), "document_id": args.get("document_id")}
                if _canon(body) != _canon({k: original.get(k) for k in body}):
                    raise ValueError("Idempotency key already used with a different body (409)")
            except (ValueError, TypeError, KeyError) as exc:
                audit.append(session, actor, tool_name, args, "DENY", str(exc))
                return ToolResult(status="denied", tool=tool_name, reason=str(exc))
            data = dict(existing.model_dump(mode="json"), idempotent_replay=True)
            audit.append(session, actor, tool_name, args, "IDEMPOTENT_REPLAY", "Original payment returned")
            return ToolResult(status="ok", tool=tool_name, data=data)
    decision = policy.evaluate(session, tool_name, args, actor)
    audit.append(session, actor, tool_name, args, decision.outcome, decision.reason)
    if decision.outcome == "DENY":
        return ToolResult(status="denied", tool=tool_name, reason=decision.reason)
    if decision.outcome == "NEEDS_APPROVAL":
        key = _canon(args)
        for req in pending_approvals(session):  # identical pending request: reuse it
            if req.tool_name == tool_name and _canon(json.loads(req.args_json)) == key:
                return ToolResult(status="needs_approval", tool=tool_name, reason=decision.reason,
                                  approval_id=req.id, data={"required_roles": decision.roles or
                                                            json.loads(req.required_roles_json)})
        req = ApprovalRequest(id=next_id(session, ApprovalRequest, "APR"), tool_name=tool_name,
                              args_json=key, required_roles_json=json.dumps(list(decision.roles)),
                              reason=decision.reason, created_at=_sim_now(session))
        session.add(req)
        session.commit()
        return ToolResult(status="needs_approval", tool=tool_name, reason=decision.reason,
                          approval_id=req.id, data={"required_roles": list(decision.roles)})
    return _execute(session, tool_name, args, actor, automatic=decision.tier == "T2")


def _get_pending(session: Session, approval_id: str, tool: str = "approval") -> ApprovalRequest | ToolResult:
    req = session.get(ApprovalRequest, approval_id)
    if req is None:
        return ToolResult(status="error", tool=tool, reason=f"approval {approval_id!r} not found")
    if req.status != "pending":
        return ToolResult(status="error", tool=req.tool_name, approval_id=req.id,
                          reason=f"approval is already {req.status}")
    return req


def _claim(session: Session, approval_id: str, values: dict, **expected) -> bool:
    """Compare-and-set on a still-pending request, so concurrent clicks cannot both win."""
    stmt = update(ApprovalRequest).where(ApprovalRequest.id == approval_id, ApprovalRequest.status == "pending")
    for column, value in expected.items():
        stmt = stmt.where(getattr(ApprovalRequest, column) == value)
    won = session.execute(stmt.values(**values)).rowcount == 1
    session.commit()
    return won


def _expire_if_stale(session: Session, req: ApprovalRequest, actor: str) -> bool:
    """Expire a request pending longer than approvals.expiry_days (simulated days); True if it is expired."""
    days = policy.load_rules()["approvals"]["expiry_days"]
    if get_today(session) <= req.created_at.date() + timedelta(days=days):
        return False
    if _claim(session, req.id, {"status": "expired", "decided_at": _sim_now(session)}):
        audit.append(session, actor, "expire_approval", {"approval_id": req.id, "tool": req.tool_name},
                     "DENY", f"Pending for more than {days} days")
    return True


def approve(session: Session, approval_id: str, role: str, approver_id: str | None = None) -> ToolResult:
    """Record distinct human approvers and re-evaluate every denial before executing.

    Calls without an identity retain legacy role identities. Old string lists
    are migrated when recording the next approval.
    """
    role = role if isinstance(role, str) and role else "unknown"  # audited and denied below, never a crash
    req = _get_pending(session, approval_id)
    if isinstance(req, ToolResult):
        audit.append(session, role, "approve", {"approval_id": approval_id, "approver_id": approver_id},
                     "DENY", req.reason)
        return req
    if role in VALID_ROLES and _expire_if_stale(session, req, role):
        return ToolResult(status="denied", tool=req.tool_name, approval_id=req.id,
                          reason="Approval request expired; ask the agent to prepare it again")
    required = json.loads(req.required_roles_json)
    args = json.loads(req.args_json)
    if role not in VALID_ROLES or role not in required:
        audit.append(session, role, "approve", {"approval_id": req.id, "tool": req.tool_name},
                     "DENY", f"role {role!r} is not an approver for this request (needs {required})")
        return ToolResult(status="denied", tool=req.tool_name, approval_id=req.id,
                          reason=f"role {role!r} cannot approve this request; required: {required}")
    stored = json.loads(req.approvals_json)
    given = [{"role": entry, "approver_id": entry} if isinstance(entry, str) else entry
             for entry in stored]
    identity = approver_id if approver_id is not None else role
    if not isinstance(identity, str) or not identity.strip() or len(identity) > 120:  # signed into PDF and QR
        audit.append(session, role, "approve", {"approval_id": req.id, "approver_id": identity},
                     "DENY", "Invalid approver identity")
        return ToolResult(status="denied", tool=req.tool_name, reason="Invalid approver identity")
    if any(entry["approver_id"] == identity and entry["role"] != role for entry in given):
        audit.append(session, role, "approve", {"approval_id": req.id, "approver_id": identity},
                     "DENY", "Same approver cannot fill two roles")
        return ToolResult(status="denied", tool=req.tool_name, approval_id=req.id,
                          reason="Same approver cannot fill two roles")
    mandate_body = mandate.body(req, account_currency(session))
    signature: dict = {}
    if role not in [entry["role"] for entry in given]:
        signature = mandate.sign(mandate_body, role, identity)
        given.append(signature)
    if not _claim(session, req.id, {"approvals_json": json.dumps(given)}, approvals_json=req.approvals_json):
        reason = "Approval request changed at the same time; reload and try again"
        audit.append(session, role, "approve", {"approval_id": req.id, "approver_id": identity}, "DENY", reason)
        return ToolResult(status="denied", tool=req.tool_name, approval_id=req.id, reason=reason)
    approved_roles = [entry["role"] for entry in given]
    signed = {"public_key": signature["public_key"], "signature": signature["signature"]} if signature else {}
    audit.append(session, role, "approve", {"approval_id": req.id, "tool": req.tool_name, "approver_id": identity,
                                            **signed},
                 "APPROVED", f"approvals so far: {[(e['role'], e['approver_id']) for e in given]}")
    if req.tool_name in HUMAN_HANDLERS:  # two-person rule, not agent policy
        both = set(required) <= set(approved_roles)
        decision = policy.Decision("ALLOW" if both else "NEEDS_APPROVAL", "Two-person rule", "H", tuple(required))
    else:
        decision = policy.evaluate(session, req.tool_name, args, "agent", approvals=tuple(approved_roles))
    if decision.outcome == "DENY" and get_setting(session, "kill_switch", "off") != "off":
        # Temporary stop: keep the request and its approvals for when the switch is off again.
        audit.append(session, role, req.tool_name, args, "DENY", f"re-check at approval: {decision.reason}")
        return ToolResult(status="denied", tool=req.tool_name, approval_id=req.id,
                          reason=f"{decision.reason}; the request stays pending")
    if decision.outcome == "DENY":
        req.status, req.decided_at, req.result_json = "rejected", _sim_now(session), _canon({"denied": decision.reason})
        session.add(req)
        session.commit()
        audit.append(session, role, req.tool_name, args, "DENY", f"re-check at approval: {decision.reason}")
        return ToolResult(status="denied", tool=req.tool_name, approval_id=req.id, reason=decision.reason)
    if decision.outcome == "NEEDS_APPROVAL":
        missing = [r for r in required if r not in approved_roles]
        return ToolResult(status="needs_approval", tool=req.tool_name, approval_id=req.id,
                          reason=f"waiting for: {', '.join(missing) or decision.reason}",
                          data={"required_roles": required, "approved_roles": approved_roles})
    if not _claim(session, req.id, {"status": "executing"}):
        return ToolResult(status="error", tool=req.tool_name, approval_id=req.id,
                          reason="approval is already being executed or decided")
    result = _execute(session, req.tool_name, args, role, automatic=False,
                      signed_mandate=mandate.document(mandate_body, given))
    req.status = "executed" if result.status == "ok" else "failed"
    req.decided_at = _sim_now(session)
    req.result_json = _canon(result.model_dump(mode="json"))
    session.add(req)
    session.commit()
    result.approval_id = req.id
    return result


def reject(session: Session, approval_id: str, role: str, reason: str = "") -> ToolResult:
    """Reject a pending approval request."""
    role = role if isinstance(role, str) and role else "unknown"
    if role not in VALID_ROLES:
        audit.append(session, role, "reject", {"approval_id": approval_id}, "DENY",
                     "Only owner or accountant can reject")
        return ToolResult(status="denied", tool="reject", reason="Only owner or accountant can reject")
    req = _get_pending(session, approval_id, "reject")
    if isinstance(req, ToolResult):
        return req
    req.status, req.decided_at = "rejected", _sim_now(session)
    req.result_json = _canon({"rejected_by": role, "reason": reason})
    session.add(req)
    session.commit()
    audit.append(session, role, "reject", {"approval_id": req.id, "tool": req.tool_name},
                 "REJECTED", reason)
    return ToolResult(status="ok", tool=req.tool_name, approval_id=req.id, reason=f"rejected by {role}",
                      data={"status": "rejected"})


# --------------------------------------------------------------------------- #
# Human-only actions that lift a payment block: two people (owner and accountant, two sessions)
# --------------------------------------------------------------------------- #
def _human_check(session: Session, action: str, args: dict) -> str | None:
    """Why the action cannot run now, or None."""
    if action == "verify_payee":
        payee = session.get(Payee, args.get("payee_id"), populate_existing=True)
        if payee is None:
            return "Payee not found"
        return "Payee is already verified" if payee.verified else None
    if action == "kill_switch_off":
        return None if kill_switch_on(session) else "The kill switch is already off"
    if action == "advance_clock":
        days = args.get("days")
        return None if type(days) is int and 1 <= days <= 120 else "days must be a whole number from 1 to 120"
    doc = session.get(Document, args.get("document_id"), populate_existing=True)
    if doc is None or not doc.flagged or doc.cleared:
        return "Document not found or not flagged"
    if action == "release_supplier":
        cp = args.get("counterparty_id")
        if cp not in json.loads(doc.counterparty_ids):
            return f"The document does not name {cp}"
        if cp in json.loads(doc.released_counterparty_ids):
            return f"{cp} is already released from this document"
    return None


def _checked(session: Session, action: str, args: dict) -> None:
    reason = _human_check(session, action, args)
    if reason:
        raise ToolError(reason)


def _human_bank() -> BankClient:
    """Bank client with the human scope; only two-person handlers use it, never agent tools."""
    return BankClient(base_url=os.getenv("RUNWAY_BANK_URL") or None, token=bank_token("human"))


def _verify_payee(session: Session, args: dict, ctx: dict) -> dict:
    _checked(session, "verify_payee", args)
    try:  # two people approved: the human bank scope is used here, never by agent tools
        with _human_bank() as bank:
            return bank.verify_payee(args["payee_id"], True)
    except BankError as exc:
        raise ToolError(f"bank refused ({exc.status_code}): {exc.detail}") from exc


def _kill_switch_off(session: Session, args: dict, ctx: dict) -> dict:
    _checked(session, "kill_switch_off", args)
    set_setting(session, "kill_switch", "off")
    return {"kill_switch": "off"}


def _advance_clock(session: Session, args: dict, ctx: dict) -> dict:
    _checked(session, "advance_clock", args)
    try:  # moving the clock resets daily caps and expires requests, so two people must agree
        with _human_bank() as bank:
            return bank.advance(args["days"])
    except BankError as exc:
        raise ToolError(f"bank refused ({exc.status_code}): {exc.detail}") from exc


def _clear_flag(session: Session, args: dict, ctx: dict) -> dict:
    _checked(session, "clear_document_flag", args)
    result = clear_document_flag(session, args["document_id"], ctx["actor"], "Cleared by owner and accountant")
    if result.status != "ok":
        raise ToolError(result.reason)
    return result.data


def _release_supplier(session: Session, args: dict, ctx: dict) -> dict:
    _checked(session, "release_supplier", args)
    doc = session.get(Document, args["document_id"])
    doc.released_counterparty_ids = json.dumps(json.loads(doc.released_counterparty_ids) + [args["counterparty_id"]])
    session.add(doc)
    session.commit()
    return {"document_id": doc.id, "released": json.loads(doc.released_counterparty_ids)}


HUMAN_HANDLERS = {"verify_payee": _verify_payee, "clear_document_flag": _clear_flag,
                  "release_supplier": _release_supplier, "kill_switch_off": _kill_switch_off,
                  "advance_clock": _advance_clock}


def request_human_action(session: Session, action: str, args: dict, role: str,
                         approver_id: str | None = None) -> ToolResult:
    """First person asks for an action that lifts a payment block; it runs once the other role approves.

    The agent cannot reach this: it is not a tool, and run_tool denies these names.
    """
    reason = ("Only owner or accountant can request this" if role not in VALID_ROLES or action not in HUMAN_HANDLERS
              else _human_check(session, action, args))
    if reason:
        audit.append(session, role, action, args, "DENY", reason)
        return ToolResult(status="denied", tool=action, reason=reason)
    key = _canon(args)
    req = next((r for r in pending_approvals(session)
                if r.tool_name == action and _canon(json.loads(r.args_json)) == key), None)
    if req is None:
        req = ApprovalRequest(id=next_id(session, ApprovalRequest, "APR"), tool_name=action, args_json=key,
                              required_roles_json=json.dumps(list(VALID_ROLES)),
                              reason="Two-person rule: owner and accountant", created_at=_sim_now(session))
        session.add(req)
        session.commit()
        audit.append(session, role, action, args, "NEEDS_APPROVAL", "Two-person rule: owner and accountant")
    return approve(session, req.id, role, approver_id)


def pending_approvals(session: Session) -> list[ApprovalRequest]:
    """All approval requests still waiting for a decision, oldest first; stale ones are expired here."""
    rows = session.exec(select(ApprovalRequest).where(ApprovalRequest.status == "pending")
                        .order_by(ApprovalRequest.created_at, ApprovalRequest.id)).all()
    return [r for r in rows if not _expire_if_stale(session, r, "system")]
