from datetime import date
import pytest
from sqlmodel import select
from agent.executor import run_tool
from bank.client import BankClient, BankError
from common.db import Payment
from common.config import load_country_pack

pytestmark = pytest.mark.usefixtures("unbacked_automatic")


def test_duplicate_and_bank_conflict(stack, pay_args):
    s, _, _ = stack
    first = run_tool(s, "schedule_payment", pay_args)
    second = run_tool(s, "schedule_payment", pay_args)
    assert first.status == second.status == "ok"
    assert second.data["idempotent_replay"] and second.data["id"] == first.data["id"]
    assert len(s.exec(select(Payment).where(Payment.created_by == "agent")).all()) == 1
    s.commit()
    with BankClient() as bank:
        args = dict(idempotency_key="bank-replay", payee_id="PAYEE-CARTONIA", amount_cents=100,
                    currency=load_country_pack().currency, execute_date=date(2026, 10, 22))
        original = bank.create_payment(**args)
        assert bank.create_payment(**args)["idempotent_replay"]
        with pytest.raises(BankError) as error:
            bank.create_payment(**{**args, "amount_cents": 101})
        assert error.value.status_code == 409
        assert len([p for p in bank.list_payments() if p["id"] == original["id"]]) == 1
