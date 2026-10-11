"""Append-only audit records with transactional chain anchors."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlmodel import Session, select

from common.db import AuditEntry, Setting

GENESIS = '0' * 64


@dataclass(frozen=True)
class ChainReport:
    ok: bool
    checked: int
    first_bad_id: int | None
    message: str


def canonical_json(obj: Any) -> str:
    """Serialize using the contract's stable JSON representation."""
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), default=str)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _entry_hash(entry: AuditEntry) -> str:
    return _digest(entry.prev_hash + canonical_json(entry.model_dump(exclude={'hash'})))


def list_entries(session: Session, limit: int | None = None) -> list[AuditEntry]:
    """Return entries in chain order."""
    query = select(AuditEntry).order_by(AuditEntry.id).execution_options(populate_existing=True)
    if limit is not None:
        if type(limit) is not int or limit < 0:
            raise ValueError('limit must be a non-negative integer')
        query = query.limit(limit)
    return list(session.exec(query))


def verify_chain(session: Session) -> ChainReport:
    """Detect tampering, including tail truncation against stored anchors."""
    entries = list_entries(session)
    previous = GENESIS
    for index, entry in enumerate(entries, 1):
        try:
            valid_args = _digest(canonical_json(json.loads(entry.args_json))) == entry.args_hash
            valid = entry.id == index and entry.prev_hash == previous and valid_args and entry.hash == _entry_hash(entry)
        except (ValueError, TypeError):
            valid = False
        if not valid:
            return ChainReport(False, index - 1, entry.id, 'Audit entry or chain link was modified')
        previous = entry.hash
    anchors = {s.key: s.value for s in session.exec(select(Setting).where(
        Setting.key.in_(['audit_head', 'audit_count'])).execution_options(populate_existing=True))}
    if not entries and not anchors:
        return ChainReport(True, 0, None, 'Empty audit chain')
    if anchors.get('audit_head') != previous or anchors.get('audit_count') != str(len(entries)):
        return ChainReport(False, len(entries), None, 'Audit head/count mismatch: possible truncation')
    return ChainReport(True, len(entries), None, 'Audit chain verified')


def append(session: Session, actor: str, action: str, args: dict,
           decision: str, reason: str = '') -> AuditEntry:
    """Atomically append a record and advance both anchors; refuse broken chains."""
    args_json = canonical_json(args)
    try:
        # Acquire SQLite's writer lock before reading the head to serialize appenders.
        session.execute(text("INSERT OR IGNORE INTO setting (key, value) VALUES ('audit_head', :head)"), {'head': GENESIS})
        session.execute(text("INSERT OR IGNORE INTO setting (key, value) VALUES ('audit_count', '0')"))
        for operation in ('UPDATE', 'DELETE'):
            session.execute(text(f'''CREATE TRIGGER IF NOT EXISTS auditentry_no_{operation.lower()}
                BEFORE {operation} ON auditentry BEGIN
                SELECT RAISE(ABORT, 'auditentry is append-only'); END'''))
        report = verify_chain(session)
        if not report.ok:
            raise ValueError(report.message)
        head = session.get(Setting, 'audit_head')
        count = session.get(Setting, 'audit_count')
        assert head is not None and count is not None
        entry = AuditEntry(id=report.checked + 1, timestamp=datetime.now(timezone.utc).isoformat(),
            actor=actor, action=action, args_json=args_json, args_hash=_digest(args_json),
            decision=decision, reason=reason, prev_hash=head.value, hash='')
        entry.hash = _entry_hash(entry)
        session.add(entry)
        head.value, count.value = entry.hash, str(entry.id)
        session.add(head)
        session.add(count)
        session.commit()
        session.refresh(entry)
        return entry
    except Exception:
        session.rollback()
        raise
