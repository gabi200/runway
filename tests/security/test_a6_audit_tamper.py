import pytest
from sqlalchemy import text
from agent.executor import run_tool
from audit.log import verify_chain


@pytest.mark.parametrize("attack", ["untouched", "edit", "middle", "tail"])
def test_audit_tamper(stack, attack):
    s, _, _ = stack
    for _ in range(2):
        assert run_tool(s, "get_balances", {}).status == "ok"
    assert verify_chain(s).ok
    if attack == "untouched":
        return
    operation = "update" if attack == "edit" else "delete"
    s.execute(text(f"DROP TRIGGER auditentry_no_{operation}"))
    sql = {"edit": "UPDATE auditentry SET reason='attacker' WHERE id=2",
           "middle": "DELETE FROM auditentry WHERE id=2", "tail": "DELETE FROM auditentry WHERE id=4"}[attack]
    s.execute(text(sql))
    s.commit()
    report = verify_chain(s)
    assert not report.ok
    assert report.first_bad_id == {"edit": 2, "middle": 3, "tail": None}[attack]
    if attack == "tail":
        assert "truncation" in report.message
