from agent.executor import run_tool


def test_per_payee_cap(stack, pay_args):
    s, _, _ = stack
    results = [run_tool(s, "schedule_payment", {**pay_args, "idempotency_key": f"split-{i}"}) for i in range(4)]
    assert [r.status for r in results] == ["ok", "needs_approval", "needs_approval", "needs_approval"]


def test_global_cap(stack, pay_args):
    s, _, _ = stack
    payees = ["PAYEE-INKWORKS", "PAYEE-CARTONIA", "PAYEE-VOLTA", "PAYEE-SWIFTLOG"]
    results = [run_tool(s, "schedule_payment", {**pay_args, "payee_id": p}) for p in payees]
    assert [r.status for r in results] == ["ok", "ok", "ok", "needs_approval"]
    assert results[-1].data["required_roles"] == ["owner"]
