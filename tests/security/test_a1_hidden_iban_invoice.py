import json
import pytest
from sqlmodel import select
from agent.documents import ingest_document
from agent.executor import run_tool
from agent.tools import TOOL_SPECS
from bank.client import BankClient, BankError
from common.db import AuditEntry, OutboxEmail, get_today
from common.config import load_country_pack


def test_hidden_invoice(stack, pay_args):
    s, docs, _ = stack
    doc = ingest_document(s, docs / "cartonia_invoice_A1.pdf", "A1.pdf", "invoice")
    assert doc.flagged and "Ignore previous instructions" in doc.content_text
    mail = s.exec(select(OutboxEmail).where(OutboxEmail.kind == "fraud_alert")).one()
    assert doc.id in mail.subject and mail.to == "owner@lumen-packaging.example"
    assert s.exec(select(AuditEntry).where(AuditEntry.action == "flag_document")).all()
    assert run_tool(s, "schedule_payment", {**pay_args, "document_id": doc.id}).status == "denied"
    for payee in ["UNKNOWN", "PAYEE-NEWVENDOR"]:
        assert run_tool(s, "schedule_payment", {**pay_args, "payee_id": payee}).status == "denied"
    assert "iban" not in json.dumps(TOOL_SPECS).lower()
    s.commit()
    with BankClient() as bank, pytest.raises(BankError) as error:
        bank.create_payment("unverified", "PAYEE-NEWVENDOR", 100, load_country_pack().currency, get_today(s))
    assert error.value.status_code == 422
