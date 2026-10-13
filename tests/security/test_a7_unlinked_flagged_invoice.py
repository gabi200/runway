import json
from datetime import timedelta
from agent.documents import ingest_file, ingest_text
from agent.executor import approve, clear_document_flag, run_tool
from bank.client import BankClient, BankError
from common.db import get_today
from common.config import load_country_pack

import pytest

CART_AMOUNT = 6_000_000  # open Cartonia invoice CM-2026-0919 (60 000 EUR)


def _cart(s, summary, **extra):
    return {"payee_id": "PAYEE-CARTONIA", "amount_cents": CART_AMOUNT, "execute_date": "2026-10-22",
            "invoice_id": summary["cartonia_invoice_id"], **extra}


def _links(doc):
    return json.loads(doc.invoice_ids), json.loads(doc.counterparty_ids)


def test_flagged_pdf_blocks_unlinked_cartonia_payment(stack, pay_args):
    s, docs, summary = stack
    doc = ingest_file(s, docs / "cartonia_invoice_A1.pdf", "invoice", None, docs)
    assert doc.flagged and "SUP-CARTONIA" in _links(doc)[1]
    res = run_tool(s, "schedule_payment", _cart(s, summary))  # no document_id
    assert res.status == "denied" and "Linked document" in res.reason
    other = run_tool(s, "schedule_payment", pay_args)  # unrelated verified payee
    assert other.status != "denied" or "Linked document" not in other.reason


def test_clearing_flag_unblocks_cartonia(stack):
    s, docs, summary = stack
    doc = ingest_file(s, docs / "cartonia_invoice_A1.pdf", "invoice", None, docs)
    assert run_tool(s, "schedule_payment", _cart(s, summary)).status == "denied"
    assert clear_document_flag(s, doc.id, "owner").status == "ok"
    res = run_tool(s, "schedule_payment", _cart(s, summary))
    assert res.status != "denied", res.reason


def test_clean_pdf_links_invoice_and_does_not_block(stack):
    s, docs, summary = stack
    doc = ingest_file(s, docs / "cartonia_invoice_clean.pdf", "invoice", None, docs)
    assert not doc.flagged
    assert summary["cartonia_invoice_id"] in _links(doc)[0]
    res = run_tool(s, "schedule_payment", _cart(s, summary))
    assert res.status != "denied", res.reason


def test_approval_rechecked_when_flagged_doc_arrives_later(stack):
    s, docs, summary = stack
    pending = run_tool(s, "schedule_payment", _cart(s, summary))
    assert pending.status == "needs_approval", pending.reason
    ingest_file(s, docs / "cartonia_invoice_A1.pdf", "invoice", None, docs)
    res = approve(s, pending.approval_id, "owner")
    assert res.status == "denied" and "Linked document" in res.reason


def test_scheduled_payment_fails_at_bank_when_flagged_doc_arrives_later(stack):
    s, docs, _ = stack
    when = get_today(s) + timedelta(days=1)
    s.commit()
    with BankClient() as bank:
        pay = bank.create_payment("a7-late-flag", "PAYEE-CARTONIA", 10_000,
                                  load_country_pack().currency, when)
    ingest_file(s, docs / "cartonia_invoice_A1.pdf", "invoice", None, docs)
    s.commit()
    with BankClient(token="human-dev-token") as human:
        human.advance(3)
        status = {p["id"]: p["status"] for p in human.list_payments()}[pay["id"]]
    assert status == "failed"


def test_bank_create_payment_422_when_blocked(stack):
    s, docs, _ = stack
    ingest_file(s, docs / "cartonia_invoice_A1.pdf", "invoice", None, docs)
    s.commit()
    with BankClient() as bank, pytest.raises(BankError) as error:
        bank.create_payment("a7-blocked", "PAYEE-CARTONIA", 10_000, load_country_pack().currency,
                            get_today(s) + timedelta(days=1))
    assert error.value.status_code == 422 and "Linked document" in str(error.value)


def test_link_matching_uses_whole_tokens(stack):
    s, _, summary = stack
    near = ingest_text(s, "Invoice CM-2026-09190 from Cartonia Millsworth", "near.txt", "invoice")
    assert _links(near) == ([], [])
    hit = ingest_text(s, "Invoice CM-2026-0919 from Cartonia Mills", "hit.txt", "invoice")
    inv, cps = _links(hit)
    assert summary["cartonia_invoice_id"] in inv and "SUP-CARTONIA" in cps


def test_list_documents_exposes_links(stack):
    s, docs, summary = stack
    doc = ingest_file(s, docs / "cartonia_invoice_clean.pdf", "invoice", None, docs)
    row = next(r for r in run_tool(s, "list_documents", {}).data if r["id"] == doc.id)
    assert summary["cartonia_invoice_id"] in row["invoice_ids"]
    assert "SUP-CARTONIA" in row["counterparty_ids"]
    assert isinstance(row["invoice_ids"], list) and isinstance(row["counterparty_ids"], list)
