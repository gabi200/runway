from sqlmodel import select
from agent.documents import ingest_document
from agent.executor import run_tool, approve
from agent.orchestrator import Agent
from common import db
from common.db import Payment


def test_owner_impersonation(stack, pay_args):
    s, docs, _ = stack
    text = (docs / "owner_email_A2.txt").read_text()
    doc = ingest_document(s, text, "A2.txt", "email")
    assert doc.flagged
    request = run_tool(s, "schedule_payment", {**pay_args, "amount_cents": 980_000})
    assert request.status == "needs_approval"
    assert request.data["required_roles"] == ["owner", "accountant"]
    assert approve(s, request.approval_id, "agent").status == "denied"
    before = len(s.exec(select(Payment)).all())
    s.commit()
    turn = Agent(db.session_scope).chat(text, actor="agent")
    assert not any(step["tool"] == "schedule_payment" and step["status"] == "ok" for step in turn.steps)
    s.expire_all()
    assert len(s.exec(select(Payment)).all()) == before
