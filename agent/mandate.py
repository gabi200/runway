"""Signed approvals: every human approval is an Ed25519 signature over the approval request.

Each approving role has its own key pair under var/keys/, created on first use and readable only by
the account that runs Runway. The signature and the public key go into the audit log; for a financing
application the signed mandate also goes into the PDF (a JSON block, a QR code and the PDF metadata).
A bank holding the public keys the company registered can check that two distinct human keys, and
not the AI agent, approved exactly this request, without trusting Runway's database.

`python -m agent.mandate keys` prints the public keys to register; `python -m agent.mandate verify
FIN-0001.pdf --registered keys.json` checks a mandate (a PDF or a JSON file).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from agent.outbox import COMPANY, var_dir
from audit.log import canonical_json

MANDATE_TYPE = "runway-mandate:v1"
PDF_METADATA_KEY = "/RunwayMandate"


def key_path(role: str) -> Path:
    return var_dir() / "keys" / f"{role}.pem"


def _public(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def _load(path: Path) -> Ed25519PrivateKey:
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def signing_key(role: str) -> Ed25519PrivateKey:
    """The role's private key, created on first use.

    ponytail: a file on the server, one per role; a real deployment keeps each person's key on their own
    device (passkey, smart card) so the server never holds it.
    """
    path = key_path(role)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # never overwrite an existing key
    except FileExistsError:
        return _load(path)
    key = Ed25519PrivateKey.generate()
    with os.fdopen(fd, "wb") as stream:
        stream.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    return key


def public_key(role: str) -> str:
    """Base64 raw Ed25519 public key of the role: what the company registers with its bank."""
    return _public(signing_key(role))


def registered_keys() -> dict[str, str]:
    """Public keys of the roles that have a key in this workspace (creates none)."""
    folder = var_dir() / "keys"
    return {path.stem: _public(_load(path)) for path in sorted(folder.glob("*.pem"))} if folder.is_dir() else {}


def body(request, currency: str) -> dict:
    """What every approver signs: the approval request exactly as it will be executed."""
    return {"type": MANDATE_TYPE, "company": COMPANY, "approval_id": request.id, "tool": request.tool_name,
            "args": json.loads(request.args_json), "currency": currency,
            "required_roles": json.loads(request.required_roles_json)}


def _message(mandate_body: dict, role: str, approver_id: str) -> bytes:
    return canonical_json({"mandate": mandate_body, "role": role, "approver_id": approver_id}).encode("utf-8")


def sign(mandate_body: dict, role: str, approver_id: str) -> dict:
    """One approval entry: who approved, with which public key, and the signature."""
    key = signing_key(role)
    return {"role": role, "approver_id": approver_id, "public_key": _public(key),
            "signature": base64.b64encode(key.sign(_message(mandate_body, role, approver_id))).decode("ascii")}


def document(mandate_body: dict, approvals: list[dict]) -> dict:
    """The mandate handed to the bank: the request and every signed approval."""
    return {"mandate": mandate_body, "signatures": [a for a in approvals if "signature" in a]}


def verify_mandate(mandate, registered: dict[str, str] | None = None) -> list[str]:
    """Problems with a mandate; empty means every required role signed it with its own valid key.

    `registered` maps each role to the public key the bank has on file; without it only the
    signatures themselves are checked.
    """
    try:
        mandate_body, signatures = mandate["mandate"], mandate["signatures"]
        required = list(mandate_body["required_roles"])
        if mandate_body["type"] != MANDATE_TYPE or not isinstance(signatures, list):
            raise TypeError
    except (KeyError, TypeError):
        return ["not a Runway mandate"]
    problems: list[str] = []
    valid: dict[str, dict] = {}
    for entry in signatures:
        role = entry.get("role") if isinstance(entry, dict) else None
        try:
            if not (isinstance(role, str) and isinstance(entry["approver_id"], str)):
                raise TypeError
            key = Ed25519PublicKey.from_public_bytes(base64.b64decode(entry["public_key"], validate=True))
            key.verify(base64.b64decode(entry["signature"], validate=True),
                       _message(mandate_body, role, entry["approver_id"]))
        except (KeyError, TypeError, ValueError, InvalidSignature):
            problems.append(f"signature of {role} does not verify")
            continue
        if registered is not None and registered.get(role) != entry["public_key"]:
            problems.append(f"{role} signed with a key not registered for that role")
            continue
        valid[role] = entry
    missing = [role for role in required if role not in valid]
    if missing:
        problems.append(f"no valid signature from: {', '.join(missing)}")
    if len({e["public_key"] for e in valid.values()}) < len(valid):
        problems.append("two roles signed with the same key")
    if len({e["approver_id"] for e in valid.values()}) < len(valid):
        problems.append("one approver signed for two roles")
    return problems


def from_pdf(path: str | Path) -> dict:
    """The mandate stored in a financing application's PDF metadata."""
    from pypdf import PdfReader

    text = (PdfReader(path).metadata or {}).get(PDF_METADATA_KEY)
    if not text:
        raise ValueError(f"{path} carries no Runway mandate")
    return json.loads(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the public keys to register, or verify a signed mandate.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("keys", help="public keys of this workspace's approver roles, as JSON")
    check = commands.add_parser("verify", help="verify a mandate in a financing PDF or a JSON file")
    check.add_argument("path")
    check.add_argument("--registered", help="JSON file mapping role to registered public key")
    args = parser.parse_args(argv)
    if args.command == "keys":
        print(json.dumps(registered_keys(), indent=2))
        return 0
    path = Path(args.path)
    mandate = from_pdf(path) if path.suffix.lower() == ".pdf" else json.loads(path.read_text(encoding="utf-8"))
    registered = json.loads(Path(args.registered).read_text(encoding="utf-8")) if args.registered else None
    problems = verify_mandate(mandate, registered)
    if problems:
        print("NOT valid:\n- " + "\n- ".join(problems))
        return 1
    signers = ", ".join(f"{e['role']} ({e['approver_id']}, key {e['public_key'][:12]}...)" for e in mandate["signatures"])
    checked = "registered keys" if registered is not None else "the keys in the mandate (no registry given)"
    print(f"valid: {mandate['mandate']['approval_id']} signed by {signers}; checked against {checked}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
