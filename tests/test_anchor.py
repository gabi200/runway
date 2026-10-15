"""Publishing the audit head outside the database, and checking the log against it."""

import pytest

from audit.anchor import AnchorError, NotaryBackend, decode_payload, encode_payload, get_backend

HEAD = "ab" * 32


def test_payload_round_trip():
    text = encode_payload(7, HEAD)
    assert text == f"runway-audit:v1:7:{HEAD}"
    assert decode_payload(text) == (7, HEAD)


@pytest.mark.parametrize("count, head", [(0, HEAD), (-1, HEAD), (True, HEAD), (1, "AB" * 32),
                                         (1, "ab" * 31), (1, None)])
def test_payload_rejects_bad_input(count, head):
    with pytest.raises(ValueError):
        encode_payload(count, head)


@pytest.mark.parametrize("text", ["", "hello", f"runway-audit:v1:0:{HEAD}", f"runway-audit:v2:1:{HEAD}",
                                  f"runway-audit:v1:1:{HEAD}x"])
def test_decode_rejects_foreign_payloads(text):
    with pytest.raises(AnchorError):
        decode_payload(text)


def test_notary_publish_and_fetch(tmp_path):
    notary = NotaryBackend(tmp_path / "n" / "notary.jsonl")
    first = notary.publish(encode_payload(1, HEAD))
    second = notary.publish(encode_payload(2, "cd" * 32))
    assert (first, second) == ("notary:1", "notary:2")
    assert decode_payload(notary.fetch(first)) == (1, HEAD)
    assert decode_payload(notary.fetch(second))[0] == 2


@pytest.mark.parametrize("ref", ["notary:3", "notary:0", "0xabc", ""])
def test_notary_unknown_ref(tmp_path, ref):
    notary = NotaryBackend(tmp_path / "notary.jsonl")
    notary.publish(encode_payload(1, HEAD))
    with pytest.raises(AnchorError):
        notary.fetch(ref)


def test_get_backend_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNWAY_DB", str(tmp_path / "x.db"))
    backend = get_backend()
    assert backend.name == "notary" and backend.path == tmp_path / "x.notary.jsonl"
    monkeypatch.setenv("RUNWAY_NOTARY_PATH", str(tmp_path / "elsewhere.jsonl"))
    assert get_backend().path == tmp_path / "elsewhere.jsonl"
    monkeypatch.setenv("RUNWAY_ANCHOR", "paper")
    with pytest.raises(AnchorError, match="Unknown"):
        get_backend()


from sqlalchemy import text
from sqlmodel import select

from audit.anchor import anchor_head, verify_anchors
from audit.log import append, list_entries, verify_chain
from common.db import AuditAnchor, reset_engines, session_scope
from redteam.tamper import edit_entry, rewrite_history, truncate_history


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNWAY_DB", str(tmp_path / "anchor.db"))
    reset_engines()
    with session_scope() as s:
        yield s
    reset_engines()


def seed(s, n=4):
    for i in range(n):
        append(s, "agent", "get_balances", {"index": i}, "ALLOW", "Read")


def test_anchor_then_verify(session, tmp_path):
    seed(session)
    receipt = anchor_head(session, "owner")
    assert (receipt.count, receipt.backend, receipt.ref) == (4, "notary", "notary:1")
    assert receipt.head == list_entries(session)[3].hash
    last = list_entries(session)[-1]
    assert (last.id, last.actor, last.action, last.decision) == (5, "owner", "anchor_audit_head", "INFO")
    report = verify_anchors(session)
    assert report.status == "verified" and report.ok and report.covered == 4
    assert (tmp_path / "anchor.notary.jsonl").exists()


def test_no_anchor_is_unanchored_not_verified(session):
    seed(session)
    report = verify_anchors(session)
    assert report.status == "unanchored" and not report.ok


def test_forged_history_fools_chain_but_not_anchor(session):
    seed(session)
    anchor_head(session, "owner")
    rewrite_history(session, 2, "attacker")
    assert verify_chain(session).ok  # the forgery is internally consistent
    assert list_entries(session)[1].reason == "attacker"
    report = verify_anchors(session)
    assert report.status == "tampered" and "entry 4" in report.message


def test_truncation_with_fixed_head_is_detected(session):
    seed(session)
    anchor_head(session, "owner")  # covers entries 1-4; entry 5 records the anchoring
    truncate_history(session, 2)
    assert verify_chain(session).ok
    report = verify_anchors(session)
    assert report.status == "tampered" and "deleted" in report.message


def test_plain_edit_reported_as_tampered(session):
    seed(session)
    anchor_head(session, "owner")
    edit_entry(session, 2, "attacker")
    assert not verify_chain(session).ok
    assert verify_anchors(session).status == "tampered"


def test_refuses_to_anchor_broken_or_empty_chain(session):
    with pytest.raises(AnchorError, match="empty"):
        anchor_head(session, "owner")
    seed(session)
    edit_entry(session, 2, "attacker")
    with pytest.raises(AnchorError, match="broken"):
        anchor_head(session, "owner")
    assert session.exec(select(AuditAnchor)).all() == []


def test_missing_notary_record_is_unavailable(session, tmp_path):
    seed(session)
    anchor_head(session, "owner")
    (tmp_path / "anchor.notary.jsonl").unlink()
    report = verify_anchors(session)
    assert report.status == "unavailable" and "notary:1" in report.message


def test_deleted_receipts_read_as_unanchored(session):
    seed(session)
    anchor_head(session, "owner")
    session.execute(text("DELETE FROM auditanchor"))
    session.commit()
    assert verify_anchors(session).status == "unanchored"


def test_entries_after_last_anchor_are_reported(session):
    seed(session)
    anchor_head(session, "owner")
    seed(session, 2)
    report = verify_anchors(session)
    assert report.ok and "not yet published" in report.message
