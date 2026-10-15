"""Insider attacks on the audit log, for the red-team console and tests. Not used by any write path."""

from __future__ import annotations

from sqlalchemy import text
from sqlmodel import Session

from audit.log import GENESIS, _entry_hash, list_entries
from common.db import Setting


def _drop_triggers(session: Session) -> None:
    for operation in ("update", "delete"):
        session.execute(text(f"DROP TRIGGER IF EXISTS auditentry_no_{operation}"))


def edit_entry(session: Session, entry_id: int, reason: str) -> None:
    """Naive attack: change one entry with raw SQL and leave the hashes alone."""
    _drop_triggers(session)
    session.execute(text("UPDATE auditentry SET reason = :reason WHERE id = :id"),
                    {"reason": reason, "id": entry_id})
    session.commit()


def rewrite_history(session: Session, entry_id: int, reason: str) -> None:
    """Forgery: change one entry, then recompute every hash after it and the stored head."""
    _drop_triggers(session)
    previous = GENESIS
    for entry in list_entries(session):
        if entry.id == entry_id:
            entry.reason = reason
        entry.prev_hash = previous
        entry.hash = _entry_hash(entry)
        previous = entry.hash
        session.add(entry)
    head = session.get(Setting, "audit_head")
    head.value = previous
    session.add(head)
    session.commit()


def truncate_history(session: Session, keep: int) -> None:
    """Forgery: delete every entry after `keep` and set the stored head and count to match."""
    _drop_triggers(session)
    session.execute(text("DELETE FROM auditentry WHERE id > :keep"), {"keep": keep})
    entries = list_entries(session)
    head, count = session.get(Setting, "audit_head"), session.get(Setting, "audit_count")
    head.value, count.value = (entries[-1].hash if entries else GENESIS), str(len(entries))
    session.add(head)
    session.add(count)
    session.commit()
