"""Financing application PDF (reportlab). The application is prepared, never submitted."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlmodel import Session, select

from agent.financing import get_product, product_cost
from agent.outbox import COMPANY, next_id, var_dir
from common.config import load_country_pack
from common.db import (Account, FinancingApplication, Invoice, account_balance_cents, get_today)

DEFAULT_DAYS_NEEDED = 60


def prepare_application(session: Session, product_id: str, amount_cents: int, needed_by: date,
                        days_needed: int = DEFAULT_DAYS_NEEDED) -> FinancingApplication:
    """Create the PDF in var/applications/ and the FinancingApplication row."""
    from forecast.engine import build_forecast  # late import: forecast is built in parallel

    product = get_product(product_id)
    if product is None:
        raise ValueError(f"unknown financing product: {product_id}")
    pack = load_country_pack()
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
    m = pack.format_money
    shortfall = ("none" if not fc.shortfall_weeks else
                 f"lowest balance {m(fc.min_closing_cents)} in week {fc.min_week}; "
                 f"weeks below minimum: {', '.join(map(str, fc.shortfall_weeks))}")

    def table(rows: list[tuple[str, str]]) -> Table:
        t = Table(rows, colWidths=[55 * mm, 110 * mm])
        t.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 10), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                               ("LINEBELOW", (0, 0), (-1, -1), 0.25, "#cccccc")]))
        return t

    doc = SimpleDocTemplate(str(path), pagesize=A4, title=f"Financing application {app_id}")
    doc.build([
        Paragraph(f"Financing application {app_id}", st["Title"]),
        Paragraph(f"Prepared by Runway on {today.isoformat()} (DRAFT, not submitted; needs human approval).",
                  st["Normal"]),
        Spacer(1, 6 * mm), Paragraph("Company summary", st["Heading2"]),
        table([("Company", COMPANY), ("Current bank balance", m(balance)),
               ("Open receivables", m(open_ar)), ("Open payables", m(open_ap))]),
        Spacer(1, 6 * mm), Paragraph("Request", st["Heading2"]),
        table([("Product", product["name"]), ("Amount", m(amount_cents)),
               ("Needed by", needed_by.isoformat()), ("Assumed duration", f"{days_needed} days"),
               ("Terms", product["notes"])]),
        Spacer(1, 6 * mm), Paragraph("Forecast shortfall (13 weeks)", st["Heading2"]),
        table([("Shortfall", shortfall)]),
        Spacer(1, 6 * mm), Paragraph("True cost", st["Heading2"]),
        table([("Interest", m(cost["interest_cents"])), ("Fees", m(cost["fees_cents"])),
               ("Total cost", m(cost["total_cost_cents"]))]),
    ])
    row = FinancingApplication(id=app_id, product_id=product_id, amount_cents=amount_cents,
                               currency=pack.currency, needed_by=needed_by, pdf_path=str(path))
    session.add(row)
    session.commit()
    session.refresh(row)
    return row
