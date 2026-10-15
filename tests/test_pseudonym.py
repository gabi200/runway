"""Pseudonymizer: stable, reversible masking of names, IDs and IBANs."""

import re
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


def test_plain_word_ids_not_masked_and_ids_case_sensitive(session):
    session.add(db.Counterparty(id="PAYROLL", name="Lumen staff salaries", kind="payroll"))
    session.add(db.Counterparty(id="LANDLORD", name="Hafenstrasse Properties", kind="landlord"))
    session.commit()
    p = Pseudonymizer(session)
    assert p.mask("payroll landlord") == "payroll landlord"
    assert p.mask("PAYROLL") == "PAYROLL"
    assert p.unmask(p.mask("Payroll (estimate)")) == "Payroll (estimate)"
    assert p.mask("CUST-BRIGHTFIELD") == "<CUSTOMER_1_ID>"
    assert "<CUSTOMER_1_ID>" not in p.mask("cust-brightfield")


def test_all_emails_masked_and_people_learned(session):
    p = Pseudonymizer(session)
    text = ("From: Mara Lenz <mara.lenz@lumen-packaging-mail.example>\nTo: finance@x.example\n\n"
            "Pay now.\n\nMara")
    p.learn(text)
    masked = p.mask(text)
    for secret in ("Mara", "Lenz", "lumen", "finance@", "@"):
        assert secret not in masked


def test_counterparty_email_local_part_registers_person(session):
    session.add(db.Counterparty(id="CUST-X", name="Xylo Corp", kind="customer", email="anna.berger@xylo.example"))
    session.commit()
    p = Pseudonymizer(session)
    masked = p.mask("Ask Anna Berger or berger about it")
    assert "Anna" not in masked and "Berger" not in masked.replace("berger", "")


def test_learn_does_not_treat_company_signoff_as_person(session):
    p = Pseudonymizer(session)
    p.add_company_name("Lumen Packaging")
    p.learn("Please pay.\n\nKind regards,\nLumen Packaging")
    out = p.mask("Our packaging line and Lumen Packaging, plus Cartonia Packaging GmbH.")
    assert "<PERSON_" not in out and "packaging line" in out


def test_same_name_entities_get_distinct_id_placeholders(session):
    # A customer and a supplier with one name, and two payees with one name, must never share an ID placeholder.
    session.add(db.Counterparty(id="SUP-BRIGHTFIELD", name="Brightfield Retail Ltd", kind="supplier"))
    session.add(db.Payee(id="PAYEE-0001", name="Cartonia Packaging GmbH", counterparty_id="SUP-CARTONIA",
                         iban="GB82WEST12345698765432", verified=False, created_at=datetime.now(timezone.utc)))
    session.commit()
    p = Pseudonymizer(session)
    ids = ["CUST-BRIGHTFIELD", "SUP-BRIGHTFIELD", "SUP-CARTONIA", "PAYEE-CARTONIA", "PAYEE-0001"]
    masked = [p.mask(i) for i in ids]
    assert len(set(masked)) == len(ids), masked
    assert [p.unmask(m) for m in masked] == ids


def test_turkish_i_and_long_s_never_crash_and_are_masked(session):
    # Under re.IGNORECASE i/I also match ı/İ and s matches ſ, which str.lower() does not map back.
    session.add(db.Counterparty(id="SUP-INKWORKS", name="Inkworks GmbH", kind="supplier"))
    session.add(db.Counterparty(id="SUP-YILDIZ", name="Yıldız Tekstil", kind="supplier"))
    session.add(db.Counterparty(id="SUP-IZMIR", name="İzmir Lojistik A.Ş.", kind="supplier"))
    session.add(db.Counterparty(id="SUP-SWIFTLOG", name="Swiftlog Logistics", kind="supplier"))
    session.commit()
    p = Pseudonymizer(session)
    out = p.mask("İnkworks faturası; YILDIZ TEKSTIL; pay İzmir Lojistik A.Ş. now; ſwiftlog")
    for secret in ("nkworks", "YILDIZ", "zmir", "wiftlog"):
        assert secret not in out, out
    assert p.mask("yıldız tekstil") == p.mask("Yıldız Tekstil")


@pytest.mark.parametrize("spelling", [
    "de89 3704 0044 0532 0130 00", "DE89 3704 0044 0532 0130 00",
    "DE89  3704  0044  0532  0130  00", "DE89-3704-0044-0532-0130-00", "DE89.3704.0044.0532.0130.00",
    "DE 89 3704 0044 0532 0130 00", "DE89 3704 0044\n0532 0130 00", "de89370400440532013000",
])
def test_known_iban_masked_in_any_spelling(session, spelling):
    p = Pseudonymizer(session)
    ph = p.mask("DE89370400440532013000")
    assert p.mask(f"pay {spelling} today") == f"pay {ph} today"


def test_loose_iban_match_keeps_following_words_and_ignores_lookalikes(session):
    p = Pseudonymizer(session)
    out = p.mask("send to be68 5390 0754 7034 from Monday")
    assert re.fullmatch(r"send to <IBAN_\d+> from Monday", out), out
    for text in ("on 12 March 2026 at 10am", "ab12 3456 7890 1234 5678", "room b2 12 seats"):
        assert p.mask(text) == text


def test_learn_still_learns_real_two_token_person(session):
    p = Pseudonymizer(session)
    p.learn("Hi\n\nBest regards,\nHans Muller")
    assert p.mask("call Hans Muller") .startswith("call <PERSON_")
    assert "Cartonia Packaging" not in "".join(p._rev.get(k, "") for k in p._rev if k.startswith("<PERSON"))
