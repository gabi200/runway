"""Agent loop: user message -> LLM (masked) -> tool calls via the executor -> reply."""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from pydantic import BaseModel, Field

from agent.prompts import REFUSAL_TEXT, SYSTEM_PROMPT, is_bypass_attempt
from llm.pseudonym import Pseudonymizer

MONDAY_PROMPT = (
    "Give me the Monday cash brief. Check the 13-week forecast and explain any shortfall. Then act on "
    "the plan: send payment reminders (send_payment_reminder) for the overdue customer invoices; "
    "request the reschedule (reschedule_payment) of a non-critical scheduled payment to its invoice due "
    "date; get the financing need with that move applied; compare financing and prepare the "
    "application for the cheapest option that arrives in time. Say what is waiting for approval."
)
MASK_FAILED = ("I could not pseudonymise this message, so nothing was sent to the language model. "
               "Please rephrase it (for example without unusual characters in names) and try again.")
FALLBACK_NOTE = ("> The language model did not answer ({error}). Runway finished this turn with its built-in "
                 "offline planner, so this reply is the deterministic version.\n\n")
_DOC_ID_RE = re.compile(r"DOC-\d+")
# First-person claims of an action, and the tool that must have run in the same turn to back each one.
_CLAIM_RE = re.compile(r"\bI(?:['’]ve| have)?\s+(?:just\s+|already\s+|now\s+)?(flagged|sent|scheduled|rescheduled|moved|prepared)\b", re.I)
_CLAIM_TOOLS = {"flagged": {"flag_document"}, "sent": {"send_payment_reminder"},
                "scheduled": {"schedule_payment", "reschedule_payment"}, "rescheduled": {"reschedule_payment"},
                "moved": {"reschedule_payment"}, "prepared": {"prepare_financing_application", "draft_email"}}


def unbacked_claims(reply: str, steps: list[dict]) -> list[str]:
    """Action verbs the reply claims in the first person with no matching tool call this turn."""
    ran = {s["tool"] for s in steps if s["status"] in {"ok", "needs_approval"}}
    return sorted({m.lower() for m in _CLAIM_RE.findall(reply) if not _CLAIM_TOOLS[m.lower()] & ran})


_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_MONEY_RE = re.compile(r"\d[\d,]*\.\d{2}\b")


def amounts_in(text: str) -> set[str]:
    """Every number in `text` as a 2-decimal amount; integers count both as cents and as whole units."""
    out = set()
    for raw in _NUMBER_RE.findall(text):
        n = raw.replace(",", "")
        out |= {f"{float(n):.2f}"} if "." in n else {f"{int(n) / 100:.2f}", f"{int(n):.2f}"}
    return out


def unbacked_figures(reply: str, evidence: set[str]) -> list[str]:
    """Money amounts in the reply that no tool result, user message or document contains."""
    return list(dict.fromkeys(m for m in _MONEY_RE.findall(reply) if f"{float(m.replace(',', '')):.2f}" not in evidence))


FULL_TURNS = 2  # ponytail: tool results kept for the last two turns only; raise it if follow-ups need more


def steps_note(steps: list[dict]) -> str:
    """Compact record of a turn's tool calls, kept in history so later turns know what really ran."""
    # ensure_ascii=False: a name with non-ASCII letters, once escaped, would not be masked when the note is resent.
    calls = "; ".join(f"{s['tool']}({json.dumps(s['args'], sort_keys=True, default=str, ensure_ascii=False)[:160]})"
                      f" -> {s['status']}"
                      + (f" [{s['approval_id']}]" if s.get("approval_id") else "")
                      + (f": {s['reason'][:120]}" if s["reason"] else "") for s in steps)
    return f"[Tool calls made in that turn: {calls or 'none'}]"


def _masked(pseudo: Pseudonymizer, m: dict) -> dict:
    """The model-facing copy of one raw message."""
    if m["role"] == "tool":
        try:
            content = json.dumps(pseudo.mask_obj(m["data"]), default=str)
        except Exception:  # fail closed: withhold a result that could not be masked
            content = json.dumps({"status": "error", "tool": m["tool"],
                                  "reason": "result withheld: it could not be pseudonymised"})
        return {"role": "tool", "tool_call_id": m["tool_call_id"], "content": content}
    if m.get("tool_calls"):
        return m  # the model's own tool-call message: already placeholders
    return {"role": m["role"], "content": pseudo.mask(m["content"])}


class AgentTurn(BaseModel):
    """Result of one agent turn: reply text plus the tool trace."""

    reply: str
    steps: list[dict[str, Any]] = Field(default_factory=list)
    refused: bool = False
    fallback: bool = False  # the model failed and the offline planner finished the turn


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
        self._turns: list[dict] = []  # earlier turns, raw (unmasked): user text, tool exchange, reply
        self._evidence: set[str] = set()  # amounts seen in tool results, user messages and documents
        self._people: list[str] = []  # person names learned in earlier turns (masked in every later turn)

    # -- helpers -----------------------------------------------------------
    @property
    def history(self) -> list[dict]:
        """Earlier turns as raw messages. The last FULL_TURNS keep their tool calls and results, so the
        model can answer a follow-up from data instead of memory; older ones keep the one-line record."""
        out: list[dict] = []
        for i, turn in enumerate(self._turns):
            out.append({"role": "user", "content": turn["user"]})
            if i >= len(self._turns) - FULL_TURNS:
                out += turn["exchange"]
            out.append({"role": "assistant", "content": turn["reply"]})
        return out

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
                self._turns.append({"user": user_message, "exchange": [], "reply": REFUSAL_TEXT})
                return AgentTurn(reply=REFUSAL_TEXT, steps=[], refused=True)
            return self._loop(session, user_message)

    def monday_brief(self) -> AgentTurn:
        """Forecast -> shortfall explanation -> plan (reminders, reschedule, financing)."""
        with self.session_factory() as session:
            return self._loop(session, MONDAY_PROMPT)

    def _loop(self, session, user_message: str) -> AgentTurn:
        pseudo = Pseudonymizer(session, people=self._people)
        full_user = self._with_documents(session, user_message)
        self._evidence |= amounts_in(full_user)
        history = self.history
        for m in history:
            if m["role"] == "tool":
                pseudo.learn_obj(m["data"])
            elif m.get("content"):
                pseudo.learn(m["content"])
        pseudo.learn(full_user)
        raw: list[dict] = [*history, {"role": "user", "content": full_user}]  # unmasked; masked per call
        turn_start = len(raw)
        steps: list[dict] = []
        reply = ""
        adapter, fallback_error = self.adapter, None
        for _ in range(self.max_steps):
            try:  # masked afresh before every call, so a name learned mid-turn is masked in earlier messages too
                messages = [{"role": "system", "content": SYSTEM_PROMPT}] + [_masked(pseudo, m) for m in raw]
            except Exception:  # fail closed: never send text that could not be masked
                return AgentTurn(reply=MASK_FAILED, steps=steps)
            try:
                resp = adapter.chat(messages, self._specs())
            except Exception as exc:  # provider down, timeout, bad key: finish this turn offline
                from llm.offline import OfflinePlanner

                if isinstance(adapter, OfflinePlanner):
                    raise  # a bug in the planner itself must surface
                from audit import log as audit_log

                fallback_error = type(exc).__name__  # the type only: messages can echo request details
                audit_log.append(session, "agent", "llm_fallback", {"error": fallback_error}, "INFO",
                                 "Language model call failed; the turn continued with the offline planner")
                adapter = OfflinePlanner()  # it reads the tool calls already made in this turn and continues
                resp = adapter.chat(messages, self._specs())
            calls = resp.get("tool_calls") or []
            if not calls:
                reply = pseudo.unmask(resp.get("content") or "")
                break
            raw.append({  # the model's own output is already in placeholders
                "role": "assistant", "content": resp.get("content"),
                "tool_calls": [{"id": c["id"], "type": "function",
                                "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])},
                                **({"extra_content": c["extra_content"]} if "extra_content" in c else {})}
                               for c in calls],
            })
            for c in calls:
                args = pseudo.unmask_obj(c["arguments"])
                try:
                    if pseudo.has_unknown_placeholder(args):
                        raise ValueError("tool arguments reference an unknown placeholder; use only placeholders from tool results")
                    result = self._run(session, c["name"], args)
                    payload = result.model_dump(mode="json")
                except Exception as exc:  # tool crashed: report, never crash the loop
                    payload = {"status": "error", "tool": c["name"], "reason": f"{type(exc).__name__}: {exc}"}
                steps.append({"tool": c["name"], "args": args, "status": payload["status"],
                              "reason": payload.get("reason", ""), "approval_id": payload.get("approval_id")})
                safe = json.loads(json.dumps(payload, default=str))
                self._evidence |= amounts_in(json.dumps(safe))
                pseudo.learn_obj(safe)
                raw.append({"role": "tool", "tool_call_id": c["id"], "tool": c["name"], "data": safe})
        else:
            reply = ("I stopped after reaching the step limit. Here is what was done so far: "
                     + (", ".join(f"{s['tool']} ({s['status']})" for s in steps) or "nothing"))
        # Deterministic checks: the model must not report actions that did not happen or figures no source shows.
        claims, figures = unbacked_claims(reply, steps), unbacked_figures(reply, self._evidence)
        if claims:
            reply += ("\n\n> Check by Runway: this reply says it " + ", ".join(claims)
                      + " something, but no matching action ran in this turn. See the tool trace for what actually happened.")
        if figures:
            reply += ("\n\n> Check by Runway: these amounts do not appear in any tool result or document: "
                      + ", ".join(figures) + ". Treat them as unverified.")
        if fallback_error:
            reply = FALLBACK_NOTE.format(error=fallback_error) + reply
        self._turns.append({"user": user_message, "exchange": raw[turn_start:],
                            "reply": reply + "\n\n" + steps_note(steps)})
        self._people = pseudo.people()  # history keeps raw text, so later turns must still know these names
        return AgentTurn(reply=reply, steps=steps, fallback=fallback_error is not None)
