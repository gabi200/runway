"""Tool executor: policy -> audit -> execute, plus the human approval workflow.

New calls are evaluated by `policy.engine.evaluate`; exact payment replays return
the original record. Every decision is written to the hash-chained audit log.
The agent never sees tokens, IBANs or bank URLs.
"""

from __future__ import annotations

import json
from datetime import datetime


from sqlalchemy import update
from sqlmodel import Session, select

from agent.financing import compare_financing  # noqa: F401  (re-exported per CONTRACTS section 6)
from agent.outbox import next_id
from agent.tools import HANDLERS, TOOL_SPECS, ToolError, ToolResult, idempotency_key, jsonable  # noqa: F401
from audit import log as audit
from common.db import Account, ApprovalRequest, Document, Payment, Setting, get_setting, get_today, utcnow
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


def _execute(session: Session, tool: str, args: dict, actor: str, automatic: bool) -> ToolResult:
    """Run the handler and audit EXECUTED / FAILED."""
    handler = HANDLERS.get(tool)
    try:
        if handler is None:
            raise ToolError(f"no implementation for tool {tool!r}")
        data = jsonable(handler(session, args, {"automatic": automatic, "actor": actor}))
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
        from forecast import engine as forecast

        financing_need = getattr(forecast, "financing_need", None)
        missing = [key for key in ("amount_cents", "needed_by", "days_needed") if args.get(key) is None]
        if missing and financing_need is not None:
            try:
                need = financing_need(session)
                args.update({key: need[key] for key in missing})
            except Exception as exc:
                reason = f"Financing defaults unavailable: {type(exc).__name__}"
                audit.append(session, actor, tool_name, args, "DENY", reason)
                return ToolResult(status="denied", tool=tool_name, reason=reason)
    # Runtime controls must apply even to exact idempotent replays.
    session.exec(select(Setting).where(Setting.key == "kill_switch")
                 .execution_options(populate_existing=True)).all()
    if get_setting(session, "kill_switch", "off") != "off":
        decision = policy.evaluate(session, tool_name, args, actor)
        if decision.outcome == "DENY":
            audit.append(session, actor, tool_name, args, "DENY", decision.reason)
            return ToolResult(status="denied", tool=tool_name, reason=decision.reason)
    if tool_name == "schedule_payment":
        try:
            policy._validate(args, *policy._SCHEMAS[tool_name])
            if args["amount_cents"] > policy.load_rules()["payment"]["max_payment_cents"]:
                raise ValueError("Payment exceeds maximum amount")
        except (ValueError, TypeError, KeyError) as exc:
            audit.append(session, actor, tool_name, args, "DENY", str(exc))
            return ToolResult(status="denied", tool=tool_name, reason=str(exc))
        key = args.get("idempotency_key") or idempotency_key(args)
        existing = session.exec(select(Payment).where(Payment.idempotency_key == key)
                                .execution_options(populate_existing=True)).first()
        if existing is not None:
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


def approve(session: Session, approval_id: str, role: str, approver_id: str | None = None) -> ToolResult:
    """Record distinct human approvers and re-evaluate every denial before executing.

    Calls without an identity retain legacy role identities. Old string lists
    are migrated when recording the next approval.
    """
    req = _get_pending(session, approval_id)
    if isinstance(req, ToolResult):
        audit.append(session, role, "approve", {"approval_id": approval_id, "approver_id": approver_id},
                     "DENY", req.reason)
        return req
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
    if not isinstance(identity, str) or not identity.strip():
        audit.append(session, role, "approve", {"approval_id": req.id, "approver_id": identity},
                     "DENY", "Invalid approver identity")
        return ToolResult(status="denied", tool=req.tool_name, reason="Invalid approver identity")
    if any(entry["approver_id"] == identity and entry["role"] != role for entry in given):
        audit.append(session, role, "approve", {"approval_id": req.id, "approver_id": identity},
                     "DENY", "Same approver cannot fill two roles")
        return ToolResult(status="denied", tool=req.tool_name, approval_id=req.id,
                          reason="Same approver cannot fill two roles")
    if role not in [entry["role"] for entry in given]:
        given.append({"role": role, "approver_id": identity})
    if not _claim(session, req.id, {"approvals_json": json.dumps(given)}, approvals_json=req.approvals_json):
        reason = "Approval request changed at the same time; reload and try again"
        audit.append(session, role, "approve", {"approval_id": req.id, "approver_id": identity}, "DENY", reason)
        return ToolResult(status="denied", tool=req.tool_name, approval_id=req.id, reason=reason)
    approved_roles = [entry["role"] for entry in given]
    audit.append(session, role, "approve", {"approval_id": req.id, "tool": req.tool_name, "approver_id": identity},
                 "APPROVED", f"approvals so far: {given}")
    decision = policy.evaluate(session, req.tool_name, args, "agent", approvals=tuple(approved_roles))
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
    result = _execute(session, req.tool_name, args, role, automatic=False)
    req.status = "executed" if result.status == "ok" else "failed"
    req.decided_at = _sim_now(session)
    req.result_json = _canon(result.model_dump(mode="json"))
    session.add(req)
    session.commit()
    result.approval_id = req.id
    return result


def reject(session: Session, approval_id: str, role: str, reason: str = "") -> ToolResult:
    """Reject a pending approval request."""
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


def pending_approvals(session: Session) -> list[ApprovalRequest]:
    """All approval requests still waiting for a decision, oldest first."""
    return list(session.exec(select(ApprovalRequest).where(ApprovalRequest.status == "pending")
                             .order_by(ApprovalRequest.created_at, ApprovalRequest.id)))
