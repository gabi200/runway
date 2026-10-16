"""Safe rendering of untrusted text and human-readable approval cards."""

from __future__ import annotations

import html
import re

# URLs and bare e-mail addresses (GFM autolinks both, the latter as mailto: links)
_URL = re.compile(r"(?:https?://|ftp://|www\.|mailto:|xmpp:)[^\s<>\"')\]]+|[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.I)
_IMG = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_SPECIAL = re.compile(r"([\\`*_{}\[\]<>#|~!])")


def safe_text(text: str | None) -> str:
    """Make model/document text safe for st.markdown (HTML stays disabled).

    Images are dropped, URLs are shown as inline code (not clickable) and markdown
    syntax is escaped, so a reply cannot render links, images or tracking pixels.
    """
    t = _IMG.sub(lambda m: f"[image removed: {m.group(1)[:40]}]", str(text or ""))
    out, pos = [], 0
    for m in _URL.finditer(t):
        out.append(_SPECIAL.sub(r"\\\1", t[pos:m.start()]))
        out.append("`" + m.group(0).replace("`", "") + "`")
        pos = m.end()
    out.append(_SPECIAL.sub(r"\\\1", t[pos:]))
    return "".join(out)


_CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)
_LINK_SYNTAX = re.compile(r"([\\\[\]<])")  # a lone ">" only starts a quote block, so it stays


def reply_markdown(text: str | None) -> str:
    """Model replies for st.markdown with formatting kept (bold, lists, headings, tables, code).

    Only what could reach out or mislead is neutralised, as in safe_text: images are dropped,
    URLs and e-mail addresses become inline code, and brackets, '<' and backslashes
    are escaped so no link, autolink or HTML can form. Code spans and fences pass through,
    since nothing inside them renders.
    """
    t = _IMG.sub(lambda m: f"[image removed: {m.group(1)[:40]}]", str(text or ""))
    out, pos = [], 0
    for m in _CODE.finditer(t):
        out.append(_reply_prose(t[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_reply_prose(t[pos:]))
    return "".join(out)


def _reply_prose(t: str) -> str:
    out, pos = [], 0
    for m in _URL.finditer(t):
        out.append(_LINK_SYNTAX.sub(r"\\\1", t[pos:m.start()]))
        out.append("`" + m.group(0).replace("`", "") + "`")
        pos = m.end()
    out.append(_LINK_SYNTAX.sub(r"\\\1", t[pos:]))
    return "".join(out)


def code_span(text) -> str:
    """Untrusted text as one inline code span, shown literally even where HTML is enabled.

    Backticks are dropped and all whitespace (line breaks too) collapsed, so the text can
    neither close the span nor end the paragraph; inside the span no markdown, HTML or
    autolink applies. (A raw <code> tag would not do: markdown inside it is still parsed.)
    """
    return "`" + " ".join(str(text).replace("`", "").split()) + "`"


def esc(text) -> str:
    return html.escape(str(text), quote=True)


def describe_approval(session, tool: str, args: dict, pack) -> list[str]:
    """Plain-language lines for an approval card (all values looked up, none trusted blindly)."""
    from agent.financing import get_product
    from common.db import Invoice, Payee, Payment

    def amt(c):
        return pack.display(int(c)) if c is not None else "?"

    def inv_line(inv_id):
        inv = session.get(Invoice, inv_id) if inv_id else None
        return f"invoice {inv.number} (due {inv.due_date})" if inv else None

    lines: list[str] = []
    if tool == "reschedule_payment":
        pay = session.get(Payment, args.get("payment_id"))
        if pay:
            payee = session.get(Payee, pay.payee_id)
            lines.append(f"Move the payment of {amt(pay.amount_cents)} to {payee.name if payee else pay.payee_id}")
            lines.append(f"Current date: {pay.execute_date}  ->  new date: {args.get('new_date')}")
            if inv_line(pay.invoice_id):
                lines.append(f"For {inv_line(pay.invoice_id)}")
    elif tool == "schedule_payment":
        payee = session.get(Payee, args.get("payee_id"))
        lines.append(f"Pay {amt(args.get('amount_cents'))} to {payee.name if payee else args.get('payee_id')}"
                     + (" (UNVERIFIED payee)" if payee and not payee.verified else ""))
        lines.append(f"Execution date: {args.get('execute_date')}")
        if inv_line(args.get("invoice_id")):
            lines.append(f"For {inv_line(args.get('invoice_id'))}")
    elif tool == "send_payment_reminder":
        inv = session.get(Invoice, args.get("invoice_id"))
        if inv:
            lines.append(f"Remind the customer about invoice {inv.number}: {amt(inv.amount_cents)}, due {inv.due_date}")
    elif tool == "prepare_financing_application":
        prod = get_product(args.get("product_id", "")) or {}
        lines.append(f"Prepare a {prod.get('name', args.get('product_id'))} application for {amt(args.get('amount_cents'))}")
        lines.append(f"Money needed by: {args.get('needed_by')}")
        from sqlmodel import select
        from common.db import ApprovalRequest
        pend = [r.id for r in session.exec(select(ApprovalRequest).where(
            ApprovalRequest.tool_name == "reschedule_payment", ApprovalRequest.status == "pending"))]
        if pend:
            lines.append(f"Assumes reschedule {', '.join(pend)} is approved; without it the financing "
                         "need is larger.")
    elif tool in ("clear_document_flag", "release_supplier"):
        from common.db import Counterparty, Document

        doc = session.get(Document, args.get("document_id"))
        what = f"{doc.id} ({doc.filename}), flagged: {doc.flag_reason}" if doc else str(args.get("document_id"))
        if tool == "clear_document_flag":
            lines.append(f"Clear the fraud flag on {what}")
            lines.append("Every payment this document blocks becomes possible again.")
        else:
            cp = session.get(Counterparty, args.get("counterparty_id"))
            lines.append(f"Release payments to {cp.name if cp else args.get('counterparty_id')} from the block of {what}")
            lines.append("The document stays flagged for every other supplier it names.")
    elif tool == "verify_payee":
        payee = session.get(Payee, args.get("payee_id"))
        lines.append(f"Mark payee {payee.name if payee else args.get('payee_id')} as verified"
                     + (f" (IBAN {payee.iban})" if payee and payee.iban else ""))
        lines.append("Only approve after confirming these bank details by phone or in person.")
    if not lines:
        for k, v in args.items():
            lines.append(f"{k.replace('_cents', '').replace('_', ' ')}: "
                         + (amt(v) if k.endswith("_cents") and isinstance(v, int) else str(v)))
    return lines


OUTCOME_BADGES = {"blocked": ":green-badge[blocked]", "detected": ":green-badge[detected]",
                  "needs approval": ":orange-badge[needs approval]", "flagged": ":orange-badge[flagged]",
                  "allowed": ":red-badge[allowed]", "fooled": ":red-badge[fooled]",
                  "passed": ":gray-badge[passed]", "info": ":gray-badge[info]"}


def format_step(step) -> str:
    """One red-team trace line as safe markdown; the detail may be attacker- or model-written."""
    badge = OUTCOME_BADGES.get(step.outcome, ":gray-badge[info]")
    return f"{badge} **{safe_text(step.layer)}** {safe_text(step.detail)}"
