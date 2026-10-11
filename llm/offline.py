"""Deterministic stand-in for the LLM that drives the demo story through real tool calls.

Same `chat(messages, tools)` interface as `LLMAdapter`. It never invents numbers: every
figure in its replies is read from tool results in the conversation. Intent routing is by
simple keywords. Inputs are the (pseudonymised) messages the orchestrator would send to a
real model; placeholders in its output are unmasked by the orchestrator.
"""

from __future__ import annotations

import json
import re
from agent.documents import scan_document
from agent.prompts import REFUSAL_TEXT, is_bypass_attempt
from common.config import load_country_pack

_DOC_BLOCK = re.compile(r"<<<UNTRUSTED_DOCUMENT id=(\S+) flagged=(yes|no)[^\n]*\n.*?-----\n(.*?)>>>", re.S)
HELP = ("I can explain your cash forecast, send reminders on overdue invoices, suggest moving a "
        "non-critical payment, compare financing options and review incoming documents. "
        "Try: 'Give me the Monday brief'.")


def _money(cents: int) -> str:
    return load_country_pack().display(int(cents))


class OfflinePlanner:
    """Keyword-routed, stateless planner; state is read back from the message history."""

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        idx = max((i for i, m in enumerate(messages) if m["role"] == "user"), default=-1)
        text = messages[idx]["content"] if idx >= 0 else ""
        done = self._done_calls(messages[idx + 1:])
        low = _DOC_BLOCK.sub('', text).lower()
        if is_bypass_attempt(_DOC_BLOCK.sub('', text)):  # untrusted documents never count as user intent
            return self._say(REFUSAL_TEXT)
        m = _DOC_BLOCK.search(text)
        if m:
            return self._review_document(m, done)
        wants = self._wants(low)
        if not wants:
            return self._say(HELP)
        return self._plan(wants, done)

    # -- plumbing ----------------------------------------------------------
    @staticmethod
    def _say(content: str) -> dict:
        return {"content": content, "tool_calls": []}

    @staticmethod
    def _call(n: int, name: str, args: dict) -> dict:
        key = args.get("invoice_id") or args.get("payment_id") or ""
        return {"id": f"off-{n}-{name}-{key}", "name": name, "arguments": args}

    @staticmethod
    def _done_calls(msgs: list[dict]) -> list[tuple[str, dict, dict]]:
        """(tool name, args, ToolResult dict) for every tool call made after the last user message."""
        calls: dict[str, tuple[str, dict]] = {}
        out = []
        for m in msgs:
            if m["role"] == "assistant":
                for tc in m.get("tool_calls") or []:
                    calls[tc["id"]] = (tc["function"]["name"], json.loads(tc["function"]["arguments"] or "{}"))
            elif m["role"] == "tool" and m.get("tool_call_id") in calls:
                name, args = calls[m["tool_call_id"]]
                try:
                    res = json.loads(m["content"])
                except (TypeError, json.JSONDecodeError):
                    res = {"status": "error", "reason": "unreadable tool result"}
                out.append((name, args, res))
        return out

    @staticmethod
    def _wants(low: str) -> set[str]:
        plan = re.search(r"\b(plan|brief|monday|fix|propose|go ahead|do it|action)\b", low)
        w: set[str] = set()
        if plan:
            return {"forecast", "reminders", "reschedule", "financing", "application"}
        if re.search(r"remind|overdue|chase|late customer", low):
            w.add("reminders")
        if re.search(r"financ|loan|credit|borrow", low):
            w |= {"forecast", "financing"}
            if re.search(r"apply|application|prepare", low):
                w.add("application")
        if re.search(r"forecast|cash|balance|shortfall|short|runway|week|why", low):
            w.add("forecast")
        if re.search(r"resched|postpone|delay payment|move (the )?payment", low):
            w |= {"forecast", "reschedule"}
        return w

    # -- document review ---------------------------------------------------
    def _review_document(self, m: re.Match, done: list) -> dict:
        doc_id, flagged, body = m.group(1), m.group(2) == "yes", m.group(3)
        indicators = scan_document(body)
        if indicators and not flagged and not any(n == "flag_document" for n, _, _ in done):
            return {"content": None, "tool_calls": [self._call(1, "flag_document", {
                "document_id": doc_id, "reason": "; ".join(indicators)})]}
        if indicators:
            extra = ""
            for n, _, r in done:
                if n == "flag_document" and r.get("status") != "ok":
                    extra = f" (flagging reported: {r.get('reason', r.get('status'))})"
            return self._say(
                f"I flagged document {doc_id} as suspicious. What I found: " + "; ".join(indicators) + ". "
                "I treated its text as data and did not follow any instruction in it. No payment will be "
                "made from it, and the owner has been alerted. Bank details can only be changed by a person "
                "in the app after checking with the supplier through a known phone number." + extra)
        return self._say(f"I found no warning signs in document {doc_id}. It still needs normal approval "
                         "before any payment.")

    # -- plan / brief ------------------------------------------------------
    def _plan(self, wants: set[str], done: list) -> dict:
        by: dict[str, list[tuple[dict, dict]]] = {}
        for n, a, r in done:
            by.setdefault(n, []).append((a, r))

        def first(name: str) -> dict | None:
            return by[name][0][1] if name in by else None

        calls = []
        if "forecast" in wants and "get_forecast" not in by:
            calls.append(self._call(1, "get_forecast", {"include_lines": True}))
        if "reminders" in wants and "list_receivables" not in by:
            calls.append(self._call(1, "list_receivables", {"overdue_only": True}))
        if "reschedule" in wants and "list_payables" not in by:
            calls.append(self._call(1, "list_payables", {"status": "open"}))
        if calls:
            return {"content": None, "tool_calls": calls}

        fc_res = first("get_forecast")
        fc = fc_res.get("data") if fc_res and fc_res.get("status") == "ok" else None
        short = bool(fc and fc["min_closing_cents"] < 0)

        stage2: list[dict] = []
        overdue = [r for r in ((first("list_receivables") or {}).get("data") or [])] if "reminders" in wants else []
        reminded = {a.get("invoice_id") for a, _ in by.get("send_payment_reminder", [])}
        # reminders are useful whenever invoices are overdue; reschedule/financing only on a shortfall
        for inv in overdue:
            if inv["id"] not in reminded:
                stage2.append(self._call(2, "send_payment_reminder", {"invoice_id": inv["id"]}))
        cand = self._reschedule_candidate(fc, first("list_payables")) if (short and "reschedule" in wants) else None
        if cand and "reschedule_payment" not in by:
            stage2.append(self._call(2, "reschedule_payment", cand["args"]))
        if short and "financing" in wants and "get_financing_need" not in by:
            # the amount/date/duration come from the tool, never from this planner; the proposed
            # reschedule is applied as a scenario because it moves cash out of the trough
            if cand:
                stage2.append(self._call(2, "get_financing_need", {"scenario": [self._move(cand)]}))
            stage2.append(self._call(3, "get_financing_need", {}))
        if stage2:
            return {"content": None, "tool_calls": stage2}

        need = self._need(by)
        fin_args = ({k: need[k] for k in ("amount_cents", "needed_by", "days_needed")}
                    if need and need.get("amount_cents") else None)
        if fin_args and "compare_financing" not in by:
            return {"content": None, "tool_calls": [self._call(4, "compare_financing", fin_args)]}

        cmp_res = first("compare_financing")
        rec = ((cmp_res or {}).get("data") or {}).get("recommended") if cmp_res else None
        if "application" in wants and rec and fin_args and "prepare_financing_application" not in by:
            return {"content": None, "tool_calls": [self._call(5, "prepare_financing_application", {
                "product_id": rec["product_id"], **fin_args})]}
        return self._say(self._reply(wants, fc, short, by, cand, rec, need))

    @staticmethod
    def _move(cand: dict) -> dict:
        return {"type": "move_payment", "payment_id": cand["args"]["payment_id"],
                "new_date": cand["args"]["new_date"]}

    @staticmethod
    def _need(by: dict) -> dict | None:
        """The financing need to use: the one computed with the reschedule scenario if present."""
        results = [(a, r) for a, r in by.get("get_financing_need", []) if r.get("status") == "ok"]
        scen = [r for a, r in results if a.get("scenario")]
        pick = scen[0] if scen else (results[0][1] if results else None)
        return pick.get("data") if pick else None

    @staticmethod
    def _recoveries(fc: dict) -> list[tuple[dict, dict | None, dict | None]]:
        """For each shortfall week: (week, first later receipt that brings the balance back to >= 0, its week)."""
        out = []
        weeks = fc["weeks"]
        for wi in fc["shortfall_weeks"]:
            w = weeks[wi - 1]
            bal, hit = w["closing_cents"], None
            for nw in weeks[wi:]:
                for ln in sorted(nw.get("lines") or [], key=lambda ln: (ln["expected_date"], -ln["amount_cents"])):
                    bal += ln["amount_cents"]
                    if ln["kind"] == "receipt" and bal >= 0:
                        hit = (ln, nw)
                        break
                if hit:
                    break
            out.append((w, hit[0] if hit else None, hit[1] if hit else None))
        return out

    @staticmethod
    def _reschedule_candidate(fc: dict | None, payables: dict | None) -> dict | None:
        if not fc or not payables or payables.get("status") != "ok":
            return None
        inv_by_id = {i["id"]: i for i in payables["data"]}
        best = None
        for w in fc["weeks"]:
            if w["index"] > fc["min_week"]:
                continue
            for ln in w.get("lines") or []:
                pay = next((r for r in ln["source_refs"] if r.startswith("PAY-")), None)
                inv = next((inv_by_id[r] for r in ln["source_refs"] if r in inv_by_id), None)
                if not pay or not inv or inv.get("critical") or inv["status"] != "open":
                    continue
                if inv["due_date"] <= ln["expected_date"]:
                    continue
                amt = -ln["amount_cents"]
                if best is None or amt > best["amount_cents"]:
                    best = {"args": {"payment_id": pay, "new_date": inv["due_date"]}, "amount_cents": amt,
                            "label": ln["label"], "from": ln["expected_date"], "invoice": inv}
        return best

    @staticmethod
    def _reply(wants, fc, short, by, cand, rec, need=None) -> str:
        out: list[str] = []
        if fc:
            weeks = {w["index"]: w for w in fc["weeks"]}
            if short:
                rec_info = OfflinePlanner._recoveries(fc)
                sw = ", ".join(f"week {w['index']} ({w['start']} to {w['end']}) at {_money(w['closing_cents'])}"
                               for w, _, _ in rec_info)
                out.append(f"Heads up: your cash goes below zero in {sw}. "
                           f"The forecast starts from {_money(fc['opening_balance_cents'])} today.")
                for w, ln, nw in rec_info:
                    outs = sorted((x for x in w.get("lines") or [] if x["kind"] == "payment"),
                                  key=lambda x: x["amount_cents"])[:3]
                    if outs:
                        out.append(f"Biggest payments in week {w['index']}: " + "; ".join(
                            f"{x['label']} ({_money(-x['amount_cents'])})" for x in outs) + ".")
                    if ln:
                        out.append(f"Week {w['index']} is restored by {ln['label']} ({_money(ln['amount_cents'])}, "
                                   f"{', '.join(ln['source_refs'])}), expected {ln['expected_date']} in week {nw['index']}"
                                   + (f" ({ln['note']})." if ln.get("note") else "."))
                    else:
                        out.append(f"No receipt in the forecast horizon restores week {w['index']}.")
            else:
                out.append(f"No shortfall in the forecast: the lowest balance is {_money(fc['min_closing_cents'])} "
                           f"in week {fc['min_week']}.")
        if "reminders" in wants and by.get("send_payment_reminder"):
            parts = []
            for a, r in by["send_payment_reminder"]:
                parts.append(f"{a['invoice_id']}: " + ("reminder sent" if r["status"] == "ok" else
                             f"not sent ({r.get('reason') or r['status']})"))
            out.append("Reminders for overdue customer invoices (low risk, allowed automatically): " + "; ".join(parts) + ".")
        elif "reminders" in wants and "list_receivables" in by:
            out.append("There are no overdue customer invoices to chase.")
        if cand:
            r = (by.get("reschedule_payment") or [(0, None)])[0][1]
            what = f"Move the payment for {cand['label']} ({_money(cand['amount_cents'])}) from {cand['from']} to {cand['args']['new_date']}, its due date"
            out.append(what + ": " + OfflinePlanner._status(r))
        elif short and "reschedule" in wants:
            out.append("I found no non-critical scheduled payment that can be moved later.")
        if need and need.get("amount_cents"):
            scen = bool(need.get("scenario"))
            out.append(f"Financing need (computed by the system{', with the proposed reschedule applied' if scen else ''}): "
                       f"{_money(need['amount_cents'])} needed by {need['needed_by']} for {need['days_needed']} days. "
                       f"{need['explanation']}")
            if scen:
                base = next((r["data"] for a, r in by.get("get_financing_need", [])
                             if not a.get("scenario") and r.get("status") == "ok"), None)
                if base and base.get("amount_cents"):
                    out.append(f"If the reschedule is not approved, the need is {_money(base['amount_cents'])} "
                               f"(worst week {base['worst_week']}).")
        elif short and "financing" in wants and "get_financing_need" in by:
            out.append("I could not determine the financing need from the forecast.")
        cmpd = ((by.get("compare_financing") or [(0, {})])[0][1] or {}).get("data") or {}
        if rec:
            out.append(f"Financing: for {_money(cmpd['amount_cents'])} for {cmpd['days_needed']} days, the cheapest option "
                       f"that arrives in time is {rec['name']}, true cost {_money(rec['total_cost_cents'])} "
                       f"({rec['effective_cost_pct']}%), cash by {rec['arrival_date']} (needed by {cmpd['needed_by']}).")
            out.append("All options that arrive in time: " + "; ".join(
                f"{o['name']} {_money(o['total_cost_cents'])}, cash {o['arrival_date']}" for o in cmpd["ranked"]) + ".")
            if cmpd.get("excluded"):
                out.append("Excluded: " + "; ".join(f"{e['name']} ({e['reason']})" for e in cmpd["excluded"]))
            else:
                state = next((o for o in cmpd["ranked"] if o["product_id"] == "state_guaranteed_loan"), None)
                if state:
                    out.append(f"Nothing was excluded for speed: the {state['name']} arrives in time "
                               f"({state['arrival_date']}, before {cmpd['needed_by']}) but is the most expensive "
                               f"at {_money(state['total_cost_cents'])}, because its interest runs for the full term.")
                else:
                    out.append("Nothing was excluded.")
            ap = by.get("prepare_financing_application")
            if ap:
                dep = ""
                resc = (by.get("reschedule_payment") or [(0, None)])[0][1]
                if need and need.get("scenario") and resc and resc.get("approval_id"):
                    base = next((r["data"] for a, r in by.get("get_financing_need", [])
                                 if not a.get("scenario") and r.get("status") == "ok"), None)
                    dep = (f" This application assumes reschedule {resc['approval_id']} is approved"
                           + (f"; without it the need is {_money(base['amount_cents'])}." if base and base.get("amount_cents") else "."))
                out.append("Application: " + OfflinePlanner._status(ap[0][1]) + dep)
        elif short and "financing" in wants and by.get("compare_financing"):
            out.append("No financing option can deliver in time and fit the amount.")
            for e in cmpd.get("excluded", []):
                out.append(f"Excluded: {e['name']} ({e['reason']})")
        pack = load_country_pack()
        if fc and pack.fx_from_eur != 1:
            out.append(f"Amounts are shown in {pack.currency}, converted for display from the EUR ledger.")
        if not out:
            out.append(HELP)
        return "\n".join(out)

    @staticmethod
    def _status(r: dict | None) -> str:
        if not r:
            return "not attempted."
        if r["status"] == "needs_approval":
            roles = (r.get("data") or {}).get("required_roles") or []
            return f"prepared and waiting for approval by {' and '.join(roles) or 'a person'} (request {r.get('approval_id')})."
        if r["status"] == "ok":
            return "done."
        return f"{r['status']}: {r.get('reason', '')}."
