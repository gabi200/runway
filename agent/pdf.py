"""Financing application PDF (reportlab). The application is prepared, never submitted."""

from __future__ import annotations

import json
from datetime import date
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph, Preformatted, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlmodel import Session, select

from agent.mandate import PDF_METADATA_KEY
from agent.financing import account_currency, get_product, ledger_money, product_cost, product_terms
from agent.outbox import COMPANY, next_id, var_dir
from common.db import (Account, FinancingApplication, Invoice, account_balance_cents, get_today)

DEFAULT_DAYS_NEEDED = 60


def _mandate_section(mandate: dict, st) -> list:
    """The signed approvals as a table, a QR code and a JSON block: what a bank verifies."""
    compact = json.dumps(mandate, separators=(",", ":"))
    qr = QrCodeWidget(compact)
    x0, y0, x1, y1 = qr.getBounds()
    size = 70 * mm
    code = Drawing(size, size, transform=[size / (x1 - x0), 0, 0, size / (y1 - y0), 0, 0])
    code.add(qr)
    rows = [(s["role"], f"{s['approver_id']}, Ed25519 key {s['public_key']}") for s in mandate["signatures"]]
    return [
        Spacer(1, 6 * mm), Paragraph("Signed approvals (verifiable mandate)", st["Heading2"]),
        Paragraph("Each approver signed this request with their own Ed25519 key. Check it against the keys the "
                  "company registered: <font face='Courier'>python -m agent.mandate verify &lt;this PDF&gt; "
                  "--registered keys.json</font>, or scan the code.", st["Normal"]),
        Spacer(1, 3 * mm),
        Table(rows, colWidths=[30 * mm, 135 * mm], style=[("FONTSIZE", (0, 0), (-1, -1), 8),
                                                         ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold")]),
        code,
        Preformatted(json.dumps(mandate, indent=1), ParagraphStyle("mandate", fontName="Courier", fontSize=6,
                                                                   leading=7)),
    ]


def prepare_application(session: Session, product_id: str, amount_cents: int, needed_by: date,
                        days_needed: int = DEFAULT_DAYS_NEEDED, mandate: dict | None = None) -> FinancingApplication:
    """Create the PDF in var/applications/ and the FinancingApplication row.

    `mandate` is the signed approval of owner and accountant (agent/mandate.py), embedded for the bank.
    """
    from forecast.engine import build_forecast  # late import: forecast is built in parallel

    product = get_product(product_id)
    if product is None:
        raise ValueError(f"unknown financing product: {product_id}")
    today = get_today(session)
    fc = build_forecast(session)
    cost = product_cost(product, amount_cents, days_needed)
    app_id = next_id(session, FinancingApplication, "FIN")
    balance = sum(account_balance_cents(session, a.id) for a in session.exec(select(Account)))
    open_ar = sum(i.amount_cents for i in session.exec(
        select(Invoice).where(Invoice.direction == "AR", Invoice.status == "open")))
    open_ap = sum(i.amount_cents for i in session.exec(
        select(Invoice).where(Invoice.direction == "AP", Invoice.status == "open")))

    out = var_dir() / "applications"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{app_id}.pdf"
    st = getSampleStyleSheet()
    cur = account_currency(session)

    def m(cents: int) -> str:
        return ledger_money(cents, cur)

    shortfall = ("none" if not fc.shortfall_weeks else
                 f"lowest balance {m(fc.min_closing_cents)} in week {fc.min_week}; "
                 f"weeks below minimum: {', '.join(map(str, fc.shortfall_weeks))}")

    cell = ParagraphStyle("cell", parent=st["Normal"], fontSize=10, leading=12)

    def table(rows: list[tuple[str, str]]) -> Table:
        # Values wrap inside the 159 mm A4 frame; escape() because Paragraph parses markup.
        t = Table([(k, Paragraph(escape(v), cell)) for k, v in rows], colWidths=[50 * mm, 109 * mm])
        t.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 10), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                               ("LINEBELOW", (0, 0), (-1, -1), 0.25, "#cccccc")]))
        return t

    doc = SimpleDocTemplate(str(path), pagesize=A4, title=f"Financing application {app_id}")
    status = ("approved and signed by " + " and ".join(s["role"] for s in mandate["signatures"])
              if mandate else "DRAFT, needs human approval")
    doc.build([
        Paragraph(f"Financing application {app_id}", st["Title"]),
        Paragraph(f"Prepared by Runway on {today.isoformat()} ({status}; not submitted).", st["Normal"]),
        Spacer(1, 6 * mm), Paragraph("Company summary", st["Heading2"]),
        table([("Company", COMPANY), ("Current bank balance", m(balance)),
               ("Open receivables", m(open_ar)), ("Open payables", m(open_ap))]),
        Spacer(1, 6 * mm), Paragraph("Request", st["Heading2"]),
        table([("Product", product["name"]), ("Amount", m(amount_cents)),
               ("Needed by", needed_by.isoformat()), ("Assumed duration", f"{days_needed} days"),
               ("Terms", product_terms(product, cur))]),
        Spacer(1, 6 * mm), Paragraph("Forecast shortfall (13 weeks)", st["Heading2"]),
        table([("Shortfall", shortfall)]),
        Spacer(1, 6 * mm), Paragraph("True cost", st["Heading2"]),
        table([("Interest", m(cost["interest_cents"])), ("Fees", m(cost["fees_cents"])),
               ("Total cost", m(cost["total_cost_cents"]))]),
        *(_mandate_section(mandate, st) if mandate else []),
    ])
    if mandate:  # machine-readable copy for `python -m agent.mandate verify`
        from pypdf import PdfWriter

        writer = PdfWriter(clone_from=str(path))
        writer.add_metadata({PDF_METADATA_KEY: json.dumps(mandate, separators=(",", ":"))})
        writer.write(str(path))
    row = FinancingApplication(id=app_id, product_id=product_id, amount_cents=amount_cents,
                               currency=cur, needed_by=needed_by, pdf_path=str(path))
    session.add(row)
    session.commit()
    session.refresh(row)
    return row
