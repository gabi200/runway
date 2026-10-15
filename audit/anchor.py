"""Publish the audit head outside the database and check the log against what was published.

The hash chain in audit/log.py catches edits by anyone who cannot also rewrite the stored head.
Someone with write access to the SQLite file can recompute every hash; a head published
elsewhere (a notary file, or a blockchain transaction) is what such a rewrite cannot change.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Session, select

from audit import log as audit
from common.db import AuditAnchor, db_path

PREFIX = "runway-audit:v1:"
_HEX64 = re.compile(r"[0-9a-f]{64}")
_PAYLOAD = re.compile(r"runway-audit:v1:([1-9]\d*):([0-9a-f]{64})")
_NOTARY_REF = re.compile(r"notary:([1-9]\d*)")


class AnchorError(Exception):
    """The anchor backend is unavailable, refused the request, or returned something unusable."""


def encode_payload(count: int, head: str) -> str:
    """Text written outside the database: how many entries the head covers, and the head hash."""
    if type(count) is not int or count < 1 or type(head) is not str or not _HEX64.fullmatch(head):
        raise ValueError("count must be a positive integer and head a 64-character lowercase hex hash")
    return f"{PREFIX}{count}:{head}"


def decode_payload(text: str) -> tuple[int, str]:
    """Inverse of encode_payload; anything else is not ours."""
    match = _PAYLOAD.fullmatch(text or "")
    if match is None:
        raise AnchorError("Not a Runway audit anchor")
    return int(match.group(1)), match.group(2)


class NotaryBackend:
    """Append-only JSON-lines file next to the database: a stand-in for an outside notary."""

    name = "notary"

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _records(self) -> list[str]:
        if not self.path.is_file():
            return []
        return [line for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def publish(self, payload: str) -> str:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        seq = len(self._records()) + 1
        record = {"seq": seq, "payload": payload, "at": datetime.now(timezone.utc).isoformat()}
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")
        return f"notary:{seq}"

    def fetch(self, ref: str) -> str:
        match = _NOTARY_REF.fullmatch(ref or "")
        records = self._records()
        if match is None or int(match.group(1)) > len(records):
            raise AnchorError(f"No notary record {ref!r}")
        try:
            return json.loads(records[int(match.group(1)) - 1])["payload"]
        except (ValueError, KeyError, TypeError) as exc:
            raise AnchorError(f"Notary record {ref!r} is unreadable") from exc


def get_backend():
    """Backend chosen by RUNWAY_ANCHOR: 'notary' (default)."""
    kind = (os.getenv("RUNWAY_ANCHOR") or "notary").strip().lower()
    if kind == "notary":
        return NotaryBackend(os.getenv("RUNWAY_NOTARY_PATH") or db_path().with_suffix(".notary.jsonl"))
    raise AnchorError(f"Unknown RUNWAY_ANCHOR {kind!r}; use notary or evm")


@dataclass(frozen=True)
class AnchorReport:
    status: str  # "verified" | "tampered" | "unanchored" | "unavailable"
    anchors_checked: int
    covered: int  # entries 1..covered match a published head
    message: str

    @property
    def ok(self) -> bool:
        return self.status == "verified"


def anchor_head(session: Session, actor: str, backend=None) -> AuditAnchor:
    """Publish the current head, store the receipt and audit the act. Refuses a broken or empty chain."""
    backend = backend or get_backend()
    report = audit.verify_chain(session)
    if not report.ok:
        raise AnchorError(f"Refusing to anchor a broken chain: {report.message}")
    if report.checked == 0:
        raise AnchorError("Nothing to anchor: the audit log is empty")
    head = audit.list_entries(session)[-1].hash
    ref = backend.publish(encode_payload(report.checked, head))
    receipt = AuditAnchor(count=report.checked, head=head, backend=backend.name, ref=ref,
                          created_at=datetime.now(timezone.utc).isoformat())
    session.add(receipt)
    session.commit()
    session.refresh(receipt)
    audit.append(session, actor, "anchor_audit_head",
                 {"count": receipt.count, "head": head, "backend": backend.name, "ref": ref},
                 "INFO", "Audit head published outside the database")
    return receipt


def verify_anchors(session: Session, backend=None) -> AnchorReport:
    """Recompute the chain, then compare it with every head published outside the database."""
    chain = audit.verify_chain(session)
    receipts = list(session.exec(select(AuditAnchor).order_by(AuditAnchor.id)
                                 .execution_options(populate_existing=True)))
    if not chain.ok:
        return AnchorReport("tampered", 0, 0, chain.message)
    if not receipts:
        return AnchorReport("unanchored", 0, 0, "No published head yet: nothing outside the database to compare with")
    try:
        backend = backend or get_backend()
    except AnchorError as exc:
        return AnchorReport("unavailable", 0, 0, str(exc))
    entries = audit.list_entries(session)
    problems: list[str] = []
    unavailable: list[str] = []
    covered = 0
    for receipt in receipts:
        if receipt.backend != backend.name:
            unavailable.append(f"anchor {receipt.id} was published to {receipt.backend}, not {backend.name}")
            continue
        try:
            count, head = decode_payload(backend.fetch(receipt.ref))
        except AnchorError as exc:
            unavailable.append(f"anchor {receipt.id} ({receipt.ref}): {exc}")
            continue
        if count > len(entries):
            problems.append(f"anchor {receipt.id} covers {count} entries but the log has {len(entries)}: entries were deleted")
        elif entries[count - 1].hash != head:
            problems.append(f"anchor {receipt.id}: entry {count} no longer matches the hash published at {receipt.ref}")
        else:
            covered = max(covered, count)
    if problems:
        return AnchorReport("tampered", len(receipts), covered, "; ".join(problems))
    if unavailable:
        return AnchorReport("unavailable", len(receipts), covered, "; ".join(unavailable))
    message = f"All {len(receipts)} published heads match; entries 1 to {covered} are as published"
    if len(entries) > covered:
        message += f"; entries {covered + 1} to {len(entries)} are not yet published"
    return AnchorReport("verified", len(receipts), covered, message)
