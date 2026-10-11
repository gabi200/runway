"""Hash-chain verification remains the guarantee after trigger bypass."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from audit.log import GENESIS, append, canonical_json, list_entries, verify_chain
from common.db import Setting, reset_engines, session_scope


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setenv('RUNWAY_DB', str(tmp_path / 'audit.db'))
    reset_engines()
    with session_scope() as s:
        yield s
    reset_engines()


def seed(s):
    for i in range(3):
        append(s, 'agent', 'get_balances', {'index':i}, 'ALLOW', 'Read')


def test_hash_contract_and_listing(session):
    assert verify_chain(session).ok
    seed(session)
    entries = list_entries(session)
    prev = GENESIS
    for entry in entries:
        assert entry.prev_hash == prev
        assert entry.args_hash == hashlib.sha256(canonical_json(json.loads(entry.args_json)).encode()).hexdigest()
        assert entry.hash == hashlib.sha256((prev + canonical_json(entry.model_dump(exclude={'hash'}))).encode()).hexdigest()
        prev = entry.hash
    assert verify_chain(session).checked == 3
    assert len(list_entries(session, 2)) == 2
    assert list_entries(session, 0) == []
    assert session.get(Setting, 'audit_head').value == prev
    assert session.get(Setting, 'audit_count').value == '3'
    assert canonical_json({'b':2,'a':1}) == '{"a":1,"b":2}'


@pytest.mark.parametrize('sql', ["UPDATE auditentry SET reason='changed' WHERE id=1", 'DELETE FROM auditentry WHERE id=1'])
def test_triggers(session, sql):
    seed(session)
    with pytest.raises(IntegrityError, match='append-only'):
        session.execute(text(sql))
    session.rollback()
    assert verify_chain(session).ok


@pytest.mark.parametrize('sql', [
    "UPDATE auditentry SET reason='changed' WHERE id=2",
    "UPDATE auditentry SET action='changed' WHERE id=1",
    "UPDATE auditentry SET decision='DENY' WHERE id=1",
    "UPDATE auditentry SET args_json='invalid JSON' WHERE id=1",
    "UPDATE auditentry SET actor='owner' WHERE id=1",
    "UPDATE auditentry SET timestamp='fake' WHERE id=1",
    "UPDATE auditentry SET args_json='{}' WHERE id=1",
    "UPDATE auditentry SET args_hash='fake' WHERE id=1",
    "UPDATE auditentry SET prev_hash='fake' WHERE id=2",
    "UPDATE auditentry SET hash='fake' WHERE id=3",
    'DELETE FROM auditentry WHERE id=2',
    'DELETE FROM auditentry WHERE id=3',
    'DELETE FROM auditentry',
    'UPDATE auditentry SET id=id+10',
    'UPDATE auditentry SET id=CASE id WHEN 1 THEN 4 WHEN 2 THEN 5 ELSE 6 END',
])
def test_a6_raw_sql_tampering(session, sql):
    seed(session)
    # A database administrator can drop triggers; verification must still detect edits.
    session.execute(text('DROP TRIGGER auditentry_no_update'))
    session.execute(text('DROP TRIGGER auditentry_no_delete'))
    session.execute(text(sql))
    session.commit()
    report = verify_chain(session)
    assert not report.ok
    with pytest.raises(ValueError):
        append(session, 'agent', 'get_balances', {}, 'ALLOW')


def test_reordered_records(session):
    seed(session)
    session.execute(text('DROP TRIGGER auditentry_no_update'))
    for old, new in [(1,4),(2,1),(4,2)]:
        session.execute(text('UPDATE auditentry SET id=:new WHERE id=:old'), {'new':new, 'old':old})
    session.commit()
    assert not verify_chain(session).ok


def test_anchor_tamper(session):
    seed(session)
    session.get(Setting, 'audit_count').value = '2'
    session.commit()
    assert not verify_chain(session).ok


def test_transaction_rollback(session, monkeypatch):
    seed(session)
    original = session.commit
    def fail() -> None:
        session.flush()
        raise RuntimeError('simulated failure')
    monkeypatch.setattr(session, 'commit', fail)
    with pytest.raises(RuntimeError):
        append(session, 'agent', 'get_balances', {}, 'ALLOW')
    monkeypatch.setattr(session, 'commit', original)
    assert verify_chain(session).ok
    assert len(list_entries(session)) == 3


def test_concurrent_append(session):
    seed(session)
    def write(i: int) -> None:
        with session_scope() as s:
            append(s, 'agent', 'get_balances', {'i':i}, 'ALLOW')
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(8)))
    assert verify_chain(session).ok
    assert verify_chain(session).checked == 11
