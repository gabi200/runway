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
