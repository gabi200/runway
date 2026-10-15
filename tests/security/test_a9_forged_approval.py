"""A9: two humans sign the financing mandate; an approval faked in the database fails the bank's signature check."""

import json

from pypdf import PdfReader
from sqlalchemy import text
from sqlmodel import select

from agent.executor import approve, run_tool
from agent.mandate import from_pdf, registered_keys, verify_mandate
from audit.log import list_entries
from common.db import FinancingApplication


def _financing(s):
    request = run_tool(s, "prepare_financing_application",
                       {"product_id": "overdraft", "amount_cents": 600_000, "needed_by": "2026-11-20"})
    assert request.status == "needs_approval"
    return request.approval_id


def test_owner_and_accountant_sign_the_mandate(stack):
    s, _, _ = stack
    approval_id = _financing(s)
    assert approve(s, approval_id, "owner", "alice").status == "needs_approval"
    result = approve(s, approval_id, "accountant", "bob")
    assert result.status == "ok"
    mandate = result.data["mandate"]
    assert verify_mandate(mandate, registered_keys()) == []
    assert (mandate["mandate"]["approval_id"], mandate["mandate"]["args"]["amount_cents"]) == (approval_id, 600_000)
    approvals = [json.loads(e.args_json) for e in list_entries(s) if (e.action, e.decision) == ("approve", "APPROVED")]
    assert [(a["approver_id"], a["signature"]) for a in approvals] == [
        (x["approver_id"], x["signature"]) for x in mandate["signatures"]]
    pdf = s.exec(select(FinancingApplication)).one().pdf_path
    assert from_pdf(pdf) == mandate
    assert "runway-mandate:v1" in "".join(page.extract_text() for page in PdfReader(pdf).pages)


def test_approval_faked_in_the_database_fails_the_bank_check(stack):
    s, _, _ = stack
    approval_id = _financing(s)
    s.execute(text("UPDATE approvalrequest SET approvals_json = :fake WHERE id = :id"),
              {"fake": json.dumps([{"role": "accountant", "approver_id": "insider"}]), "id": approval_id})
    s.commit()
    result = approve(s, approval_id, "owner", "alice")
    assert result.status == "ok"  # the executor trusts its own database (known risk 7); the bank need not
    assert verify_mandate(result.data["mandate"], registered_keys()) == ["no valid signature from: accountant"]
