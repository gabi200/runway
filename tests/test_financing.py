"""Financing comparison: true cost, eligibility, speed filter, ranking."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlmodel import Session

from agent.financing import compare_financing, get_product, load_products, product_cost
from common import db
from common.db import Account, Counterparty, Invoice, set_setting

TODAY = date(2026, 10, 19)


@pytest.fixture()
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNWAY_DB", str(tmp_path / "t.db"))
    db.reset_engines()
    with db.session_scope() as s:
        s.add(Account(id="ACC-MAIN", name="Main", currency="EUR"))
        s.add(Counterparty(id="CUST-BRIGHTFIELD", name="Brightfield", kind="customer"))
        s.add(Counterparty(id="CUST-SMALL-1", name="Small", kind="customer"))
        s.commit()
        set_setting(s, "today", TODAY.isoformat())
        yield s
    db.reset_engines()


def _ar(s: Session, id_: str, cp: str, cents: int) -> None:
    s.add(Invoice(id=id_, number=id_, direction="AR", counterparty_id=cp, issue_date=TODAY,
                  due_date=TODAY + timedelta(days=30), amount_cents=cents, currency="EUR"))
    s.commit()


def test_catalogue_has_five_products():
    assert {p["id"] for p in load_products()} == {
        "invoice_financing", "overdraft", "short_term_loan", "supply_chain_finance",
        "state_guaranteed_loan"}


def test_true_cost_integer_cents():
    c = product_cost(get_product("overdraft"), 4_000_000, 30)
    # 12% * 30/365 on 40,000.00 = 394.52 interest + 200 fee
    assert c == {"interest_cents": 39452, "fees_cents": 20000, "total_cost_cents": 59452}
    inv = product_cost(get_product("invoice_financing"), 4_000_000, 45)  # 2 started periods
    assert inv["total_cost_cents"] == 4_000_000 * 15 // 1000 * 2
    loan = product_cost(get_product("short_term_loan"), 1_000_000, 10)  # full 183-day term + 1% fee
    assert loan["interest_cents"] == round(1_000_000 * 0.09 * 183 / 365) and loan["fees_cents"] == 10000


def test_ranking_excludes_slow_and_ineligible(session):
    _ar(session, "AR-BF", "CUST-BRIGHTFIELD", 9_500_000)
    r = compare_financing(session, 4_000_000, date(2026, 11, 2), 30)
    ids = [o["product_id"] for o in r["ranked"]]
    excl = {e["product_id"]: e["reason"] for e in r["excluded"]}
    assert "state_guaranteed_loan" in excl and "slow" in excl["state_guaranteed_loan"].lower()
    costs = [o["total_cost_cents"] for o in r["ranked"]]
    assert costs == sorted(costs) and r["recommended"] == r["ranked"][0]
    assert set(ids) == {"invoice_financing", "overdraft", "short_term_loan", "supply_chain_finance"}
    scf = next(o for o in r["ranked"] if o["product_id"] == "supply_chain_finance")
    assert scf["needs_confirmation"] == "Needs Brightfield confirmation"
    assert r["recommended"]["product_id"] == "supply_chain_finance"  # 0.6%/30d is the cheapest
    assert r["currency"] == "EUR"


def test_overdraft_limit_and_invoice_cap(session):
    _ar(session, "AR-S", "CUST-SMALL-1", 5_000_000)  # cap 40,000
    r = compare_financing(session, 6_000_000, date(2026, 12, 1), 30)
    excl = {e["product_id"]: e["reason"] for e in r["excluded"]}
    assert "invoice_financing" in excl and "cap" in excl["invoice_financing"]
    assert "overdraft" in excl and "limit" in excl["overdraft"]
    assert "supply_chain_finance" in excl  # no Brightfield invoice


def test_needed_by_too_early_leaves_only_fast_options(session):
    _ar(session, "AR-BF", "CUST-BRIGHTFIELD", 9_500_000)
    r = compare_financing(session, 1_000_000, TODAY + timedelta(days=1), 10)
    assert [o["product_id"] for o in r["ranked"]] == ["overdraft"]
    assert len(r["excluded"]) == 4


def test_nothing_deliverable(session):
    r = compare_financing(session, 1_000_000, TODAY, 10)
    assert r["recommended"] is None and r["ranked"] == []
