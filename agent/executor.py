"""Tool executor: policy -> audit -> execute, plus the human approval workflow.

Every call is evaluated by `policy.engine.evaluate` first and every step is written
to the hash-chained audit log. The agent never sees tokens, IBANs or bank URLs.
"""

from __future__ import annotations

import json


from sqlmodel import Session, select

from agent.financing import compare_financing  # noqa: F401  (re-exported per CONTRACTS section 6)
from agent.outbox import next_id
from agent.tools import HANDLERS, TOOL_SPECS, ToolError, ToolResult, jsonable  # noqa: F401
from audit import log as audit
from common.db import ApprovalRequest, utcnow
from policy import engine as policy

VALID_ROLES = ("owner", "accountant")


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
                              reason=decision.reason)
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


def approve(session: Session, approval_id: str, role: str) -> ToolResult:
    """Record `role`'s approval; execute once all required roles approved and policy re-allows."""
    req = _get_pending(session, approval_id)
    if isinstance(req, ToolResult):
        return req
    required = json.loads(req.required_roles_json)
    args = json.loads(req.args_json)
    if role not in required:
        audit.append(session, role, "approve", {"approval_id": req.id, "tool": req.tool_name},
                     "DENY", f"role {role!r} is not an approver for this request (needs {required})")
        return ToolResult(status="denied", tool=req.tool_name, approval_id=req.id,
                          reason=f"role {role!r} cannot approve this request; required: {required}")
    given = json.loads(req.approvals_json)
    if role not in given:
        given.append(role)
        req.approvals_json = json.dumps(given)
        session.add(req)
        session.commit()
    audit.append(session, role, "approve", {"approval_id": req.id, "tool": req.tool_name},
                 "APPROVED", f"approvals so far: {given}")
    decision = policy.evaluate(session, req.tool_name, args, "agent", approvals=tuple(given))
    if decision.outcome == "DENY":
        req.status, req.decided_at, req.result_json = "rejected", utcnow(), _canon({"denied": decision.reason})
        session.add(req)
        session.commit()
        audit.append(session, role, req.tool_name, args, "DENY", f"re-check at approval: {decision.reason}")
        return ToolResult(status="denied", tool=req.tool_name, approval_id=req.id, reason=decision.reason)
    if decision.outcome == "NEEDS_APPROVAL":
        missing = [r for r in required if r not in given]
        return ToolResult(status="needs_approval", tool=req.tool_name, approval_id=req.id,
                          reason=f"waiting for: {', '.join(missing) or decision.reason}",
                          data={"required_roles": required, "approved_roles": given})
    result = _execute(session, req.tool_name, args, role, automatic=False)
    req.status = "executed" if result.status == "ok" else "failed"
    req.decided_at = utcnow()
    req.result_json = _canon(result.model_dump(mode="json"))
    session.add(req)
    session.commit()
    result.approval_id = req.id
    return result


def reject(session: Session, approval_id: str, role: str, reason: str = "") -> ToolResult:
    """Reject a pending approval request."""
    req = _get_pending(session, approval_id, "reject")
    if isinstance(req, ToolResult):
        return req
    req.status, req.decided_at = "rejected", utcnow()
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
