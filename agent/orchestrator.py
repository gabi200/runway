"""Agent loop: user message -> LLM (masked) -> tool calls via the executor -> reply."""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from pydantic import BaseModel, Field

from agent.prompts import REFUSAL_TEXT, SYSTEM_PROMPT, is_bypass_attempt
from llm.pseudonym import Pseudonymizer

MONDAY_PROMPT = (
    "Give me the Monday cash brief. Check the 13-week forecast, explain any shortfall, then "
    "propose and prepare a plan: reminders for overdue customer invoices, rescheduling a "
    "non-critical payment, and the cheapest financing that arrives in time."
)
_DOC_ID_RE = re.compile(r"DOC-\d+")


class AgentTurn(BaseModel):
    """Result of one agent turn: reply text plus the tool trace."""

    reply: str
    steps: list[dict[str, Any]] = Field(default_factory=list)
    refused: bool = False


class Agent:
    """Tool-calling agent. All privileged work goes through `run_tool` (rules engine inside)."""

    def __init__(self, session_factory: Callable[[], Any], adapter=None, max_steps: int = 8,
                 tool_specs: list[dict] | None = None, tool_runner: Callable | None = None) -> None:
        self.session_factory = session_factory
        if adapter is None:
            from llm.adapter import get_adapter

            adapter = get_adapter()
        self.adapter = adapter
        self.max_steps = max_steps
        self._tool_specs = tool_specs
        self._tool_runner = tool_runner
        self.history: list[dict] = []  # raw (unmasked) user/assistant text of earlier turns

    # -- helpers -----------------------------------------------------------
    def _specs(self) -> list[dict]:
        if self._tool_specs is None:
            from agent.tools import TOOL_SPECS

            self._tool_specs = TOOL_SPECS
        return self._tool_specs

    def _run(self, session, name: str, args: dict):
        if self._tool_runner is not None:
            return self._tool_runner(session, name, args, actor="agent")
        from agent.executor import run_tool

        return run_tool(session, name, args, actor="agent")

    def _with_documents(self, session, text: str) -> str:
        """Append delimited untrusted blocks for any DOC-xxxx the user mentions."""
        from agent.documents import wrap_untrusted
        from common.db import Document

        blocks = []
        for did in dict.fromkeys(_DOC_ID_RE.findall(text)):
            doc = session.get(Document, did)
            if doc is not None:
                blocks.append(wrap_untrusted(doc))
        return text + ("\n\n" + "\n\n".join(blocks) if blocks else "")

    def _log_refusal(self, session, actor: str, text: str) -> None:
        from audit import log as audit_log

        audit_log.append(session, "agent", "chat_refusal", {"from": actor, "message": text[:500]},
                         "DENY", "Request to bypass rules/limits/payee controls; no such tool exists")

    # -- public API --------------------------------------------------------
    def chat(self, user_message: str, actor: str = "owner") -> AgentTurn:
        """Run one conversational turn."""
        with self.session_factory() as session:
            if is_bypass_attempt(user_message):
                self._log_refusal(session, actor, user_message)
                self.history += [{"role": "user", "content": user_message},
                                 {"role": "assistant", "content": REFUSAL_TEXT}]
                return AgentTurn(reply=REFUSAL_TEXT, steps=[], refused=True)
            return self._loop(session, user_message)

    def monday_brief(self) -> AgentTurn:
        """Forecast -> shortfall explanation -> plan (reminders, reschedule, financing)."""
        with self.session_factory() as session:
            return self._loop(session, MONDAY_PROMPT)

    def _loop(self, session, user_message: str) -> AgentTurn:
        pseudo = Pseudonymizer(session)
        full_user = self._with_documents(session, user_message)
        messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages += [{"role": m["role"], "content": pseudo.mask(m["content"])} for m in self.history]
        messages.append({"role": "user", "content": pseudo.mask(full_user)})
        steps: list[dict] = []
        reply = ""
        for _ in range(self.max_steps):
            resp = self.adapter.chat(messages, self._specs())
            calls = resp.get("tool_calls") or []
            if not calls:
                reply = pseudo.unmask(resp.get("content") or "")
                break
            messages.append({
                "role": "assistant", "content": resp.get("content"),
                "tool_calls": [{"id": c["id"], "type": "function",
                                "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                               for c in calls],
            })
            for c in calls:
                args = pseudo.unmask_obj(c["arguments"])
                try:
                    result = self._run(session, c["name"], args)
                    payload = result.model_dump(mode="json")
                except Exception as exc:  # tool crashed: report, never crash the loop
                    payload = {"status": "error", "tool": c["name"], "reason": f"{type(exc).__name__}: {exc}"}
                steps.append({"tool": c["name"], "args": args, "status": payload["status"],
                              "reason": payload.get("reason", ""), "approval_id": payload.get("approval_id")})
                messages.append({"role": "tool", "tool_call_id": c["id"],
                                 "content": json.dumps(pseudo.mask_obj(payload), default=str)})
        else:
            reply = ("I stopped after reaching the step limit. Here is what was done so far: "
                     + (", ".join(f"{s['tool']} ({s['status']})" for s in steps) or "nothing"))
        self.history += [{"role": "user", "content": user_message}, {"role": "assistant", "content": reply}]
        return AgentTurn(reply=reply, steps=steps)
