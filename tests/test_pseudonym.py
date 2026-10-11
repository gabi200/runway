"""Pseudonymizer: stable, reversible masking of names, IDs and IBANs."""

from datetime import datetime, timezone

import pytest
from sqlmodel import Session

from common import db
from llm.pseudonym import Pseudonymizer


@pytest.fixture()
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNWAY_DB", str(tmp_path / "t.db"))
    db.reset_engines()
    with Session(db.get_engine()) as s:
        s.add(db.Counterparty(id="CUST-BRIGHTFIELD", name="Brightfield Retail Ltd", kind="customer"))
        s.add(db.Counterparty(id="SUP-CARTONIA", name="Cartonia Packaging GmbH", kind="supplier"))
        s.add(db.Payee(id="PAYEE-CARTONIA", name="Cartonia Packaging GmbH", counterparty_id="SUP-CARTONIA",
                       iban="DE89 3704 0044 0532 0130 00", verified=True,
                       created_at=datetime.now(timezone.utc)))
        s.commit()
        yield s
    db.reset_engines()


def test_mask_and_unmask_roundtrip(session):
    p = Pseudonymizer(session, people=["Maria Keller"])
    text = ("Brightfield Retail Ltd (CUST-BRIGHTFIELD) owes money; pay Cartonia Packaging GmbH "
            "DE89370400440532013000 via PAYEE-CARTONIA. Contact Maria Keller.")
    masked = p.mask(text)
    for secret in ("Brightfield", "Cartonia", "DE89", "Maria", "BRIGHTFIELD"):
        assert secret not in masked
    assert "<CUSTOMER_1>" in masked and "<SUPPLIER_1>" in masked and "<PERSON_1>" in masked
    assert p.unmask(masked) == text


def test_stable_placeholders_and_unknown_iban(session):
    p = Pseudonymizer(session)
    a = p.mask("pay GB82WEST12345698765432")
    b = p.mask("again GB82 WEST 1234 5698 7654 32")
    assert a.split()[-1] == b.split()[-1]
    assert "GB82" not in a
    assert p.mask("Cartonia") == p.mask("cartonia")


def test_ids_masked_and_unmasked_in_tool_args(session):
    p = Pseudonymizer(session)
    masked = p.mask_obj({"counterparty_id": "CUST-BRIGHTFIELD", "payee_id": "PAYEE-CARTONIA", "n": 5})
    assert "BRIGHTFIELD" not in str(masked) and "CARTONIA" not in str(masked)
    assert masked["n"] == 5
    assert p.unmask_obj(masked) == {"counterparty_id": "CUST-BRIGHTFIELD", "payee_id": "PAYEE-CARTONIA", "n": 5}


def test_unmask_unknown_placeholder_untouched(session):
    assert Pseudonymizer(session).unmask("<SUPPLIER_99>") == "<SUPPLIER_99>"


def test_does_not_mask_substrings_of_other_words(session):
    p = Pseudonymizer(session)
    assert p.mask("Cartoniaville") == "Cartoniaville"
