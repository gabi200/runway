"""Keep audit history across a demo reset or a data import, which both rebuild the database."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Session, create_engine

from agent.outbox import var_dir
from audit import log as audit
from common.db import db_path, db_url


def archive_before_wipe(session: Session) -> dict:
    """Copy the database aside (SQLite backup API, safe while open) and return what the new log must point to."""
    report = audit.verify_chain(session)
    entries = audit.list_entries(session)
    dest = var_dir() / "archive" / f"runway-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}.db"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path())) as src, closing(sqlite3.connect(dest)) as out:
        src.backup(out)
    return {"previous_head": entries[-1].hash if entries else audit.GENESIS,
            "previous_count": len(entries), "previous_chain_ok": report.ok, "archive": dest.name}


def check_link(session: Session, archive_path: Path) -> bool:
    """True when the archived chain is intact and the new log's link entry names its head and length."""
    link = next((args for e in audit.list_entries(session)
                 if "previous_head" in (args := json.loads(e.args_json))), None)
    if link is None or not Path(archive_path).is_file():
        return False
    engine = create_engine(db_url(archive_path))
    try:
        with Session(engine) as old:
            report = audit.verify_chain(old)
            entries = audit.list_entries(old)
    finally:
        engine.dispose()  # Windows keeps the file locked otherwise
    head = entries[-1].hash if entries else audit.GENESIS
    return report.ok and link["previous_head"] == head and link["previous_count"] == len(entries)
