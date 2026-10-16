"""Reset and import rebuild the database; the old audit log is archived and the new chain points at its head."""

import sqlite3
from contextlib import closing

import pytest
from sqlmodel import Session

from agent.outbox import var_dir
from audit import archive
from audit import log as audit
from common import db
from data.generate import generate


@pytest.fixture()
def env(tmp_path, monkeypatch):
    dbp = tmp_path / "e2e.db"
    monkeypatch.setenv("RUNWAY_DB", str(dbp))
    monkeypatch.chdir(tmp_path)
    db.reset_engines()
    generate(db_path=dbp, out_dir=tmp_path / "sample")
    yield dbp
    db.reset_engines()


def _wipe_and_link(dbp, tmp):
    with Session(db.get_engine()) as s:
        audit.append(s, "owner", "set_kill_switch", {"state": "on"}, "INFO")
        old_head = audit.list_entries(s)[-1].hash
        info = archive.archive_before_wipe(s)
    db.reset_engines()
    generate(db_path=dbp, out_dir=tmp / "sample2")
    with Session(db.get_engine()) as s:
        audit.append(s, "owner", "reset_demo", info, "HUMAN_ACTION")
    return old_head, info


def test_reset_archives_the_old_log_and_links_to_its_head(env, tmp_path):
    old_head, info = _wipe_and_link(env, tmp_path)
    assert info["previous_head"] == old_head and info["previous_chain_ok"]
    with Session(db.get_engine()) as s:
        assert archive.check_link(s, var_dir() / "archive" / info["archive"])


def test_a_tampered_archive_breaks_the_link(env, tmp_path):
    _, info = _wipe_and_link(env, tmp_path)
    path = var_dir() / "archive" / info["archive"]
    with closing(sqlite3.connect(path)) as con:
        con.execute("DROP TRIGGER IF EXISTS auditentry_no_update")
        con.execute("UPDATE auditentry SET reason = 'edited' WHERE id = 1")
        con.commit()
    with Session(db.get_engine()) as s:
        assert not archive.check_link(s, path)


def test_a_deleted_archive_breaks_the_link(env, tmp_path):
    _, info = _wipe_and_link(env, tmp_path)
    path = var_dir() / "archive" / info["archive"]
    path.unlink()
    with Session(db.get_engine()) as s:
        assert not archive.check_link(s, path)


def test_archiving_leaves_the_live_database_untouched(env):
    with Session(db.get_engine()) as s:
        before = len(audit.list_entries(s))
        archive.archive_before_wipe(s)
        assert len(audit.list_entries(s)) == before
        assert audit.verify_chain(s).ok
