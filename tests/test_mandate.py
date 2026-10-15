"""Signed approvals: Ed25519 signatures over the approval request, verifiable without Runway's database."""

import base64
import json
import os

import pytest

from agent.mandate import (MANDATE_TYPE, _message, document, key_path, main, public_key, registered_keys, sign,
                           signing_key, verify_mandate)

BODY = {"type": MANDATE_TYPE, "company": "Lumen Packaging", "approval_id": "APR-0002",
        "tool": "prepare_financing_application",
        "args": {"product_id": "supply_chain_finance", "amount_cents": 1_000_000, "needed_by": "2026-11-23"},
        "currency": "EUR", "required_roles": ["owner", "accountant"]}


def signed(body=BODY):
    return document(body, [sign(body, "owner", "alice"), sign(body, "accountant", "bob")])


def test_two_distinct_human_keys_verify():
    mandate = signed()
    assert verify_mandate(mandate) == []
    assert verify_mandate(mandate, registered_keys()) == []
    owner, accountant = mandate["signatures"]
    assert owner["public_key"] == public_key("owner") != accountant["public_key"] == public_key("accountant")


def test_changed_request_breaks_the_signatures():
    mandate = signed()
    mandate["mandate"] = {**BODY, "args": {**BODY["args"], "amount_cents": 9_000_000}}
    assert verify_mandate(mandate) == ["signature of owner does not verify", "signature of accountant does not verify",
                                       "no valid signature from: owner, accountant"]


def test_relabelled_roles_do_not_verify():
    mandate = signed()
    first, second = mandate["signatures"]
    mandate["signatures"] = [{**first, "role": "accountant"}, {**second, "role": "owner"}]
    assert "no valid signature from: owner, accountant" in verify_mandate(mandate)


def test_missing_second_signature():
    assert verify_mandate(document(BODY, [sign(BODY, "owner", "alice")])) == ["no valid signature from: accountant"]


def test_one_key_for_both_roles_is_refused():
    signing_key("owner")
    key_path("accountant").write_bytes(key_path("owner").read_bytes())
    assert "two roles signed with the same key" in verify_mandate(signed())


def test_one_approver_for_both_roles_is_refused():
    mandate = document(BODY, [sign(BODY, "owner", "alice"), sign(BODY, "accountant", "alice")])
    assert verify_mandate(mandate) == ["one approver signed for two roles"]


def test_key_not_registered_with_the_bank():
    mandate = signed()
    registered = {"owner": public_key("owner"), "accountant": public_key("owner")}
    assert verify_mandate(mandate, registered) == ["accountant signed with a key not registered for that role",
                                                   "no valid signature from: accountant"]


@pytest.mark.parametrize("mandate", [{}, "text", None, {"mandate": {**BODY, "type": "other"}, "signatures": []},
                                     {"mandate": BODY, "signatures": "x"}])
def test_foreign_documents_are_rejected(mandate):
    assert verify_mandate(mandate) == ["not a Runway mandate"]


@pytest.mark.parametrize("entry", [{"role": "owner"}, "x", {"role": "owner", "approver_id": "a",
                                                            "public_key": "!!", "signature": "!!"}])
def test_malformed_signatures_do_not_verify(entry):
    assert verify_mandate({"mandate": BODY, "signatures": [entry]})[-1] == "no valid signature from: owner, accountant"


def test_unhashable_role_signed_validly_does_not_crash():
    signature = signing_key("owner").sign(_message(BODY, ["owner"], "alice"))
    entry = {**sign(BODY, "owner", "alice"), "role": ["owner"], "signature": base64.b64encode(signature).decode()}
    assert verify_mandate({"mandate": BODY, "signatures": [entry]}) == [
        "signature of ['owner'] does not verify", "no valid signature from: owner, accountant"]


def test_keys_are_private_files_reused_across_calls():
    assert public_key("owner") == public_key("owner")
    if os.name != "nt":
        assert key_path("owner").stat().st_mode & 0o777 == 0o600
    assert set(registered_keys()) == {"owner"}


def test_cli_verifies_against_registered_keys(tmp_path, capsys):
    mandate_file, keys_file = tmp_path / "mandate.json", tmp_path / "keys.json"
    mandate_file.write_text(json.dumps(signed()))
    assert main(["keys"]) == 0
    keys_file.write_text(capsys.readouterr().out)
    assert main(["verify", str(mandate_file), "--registered", str(keys_file)]) == 0
    assert "valid" in capsys.readouterr().out
    tampered = json.loads(json.dumps(signed()))
    tampered["mandate"]["args"]["amount_cents"] += 1
    mandate_file.write_text(json.dumps(tampered))
    assert main(["verify", str(mandate_file)]) == 1
    assert "does not verify" in capsys.readouterr().out
