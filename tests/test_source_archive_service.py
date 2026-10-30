from datetime import datetime
import sqlite3
from threading import Event

import pytest

from stock_analysis.collection.source_archive_service import KEY, SourceArchiveService, ZONE
from stock_analysis.infrastructure.storage import Database, StorageError, instance_lock
from stock_analysis.maintenance.database_restore import RequestGate
from stock_analysis.maintenance.maintenance import DESCRIPTIONS, table_time
from stock_analysis.collection.collection import archive_source, begin_batch, read_source
from stock_analysis.collection.source_archive import build, restore_source
from stock_analysis.collection import source_archive
from stock_analysis.infrastructure import storage


def test_schedule_disabled_by_default_and_runs_once(tmp_path, monkeypatch):
    db = Database(tmp_path / 'test.sqlite3')
    db.initialize()
    service = SourceArchiveService(db, RequestGate())
    assert service.state()['schedule']['enabled'] is False
    calls = []
    monkeypatch.setattr('stock_analysis.collection.source_archive_service.build', lambda db, cutoff, **kwargs: calls.append(cutoff) or {'archives': [], 'removed': 0, 'errors': []})
    service.configure({'enabled': True, 'day': 31, 'local_time':'02:00'})
    with db.connection(write=True) as conn:
        conn.execute('UPDATE collection_schedules SET updated_at=? WHERE schedule_id=?', ('2026-01-01T00:00:00+00:00',KEY))
    now = datetime(2026, 3, 1, 3, tzinfo=ZONE)
    service.tick(now)
    service.close()
    service.tick(now)
    assert calls == ['2026-02']
    assert service.state()['runs'][0]['status'] == 'complete'


def test_manual_run_and_saved_configuration_do_not_trigger_automatically(tmp_path, monkeypatch):
    db = Database(tmp_path / 'test.sqlite3')
    db.initialize()
    service = SourceArchiveService(db, RequestGate())
    calls = []
    monkeypatch.setattr('stock_analysis.collection.source_archive_service.build', lambda db, cutoff, **kwargs: calls.append(cutoff) or {'archives': [], 'removed': 0, 'errors': []})
    service.configure({'enabled': False, 'day': 1, 'local_time': '02:00'})
    service.tick()
    assert calls == []
    token = service.preview('2020-01')['token']
    service.start('2020-01', token)
    service.close()
    assert calls == ['2020-01']


def test_actual_database_archive_survives_old_snapshot(tmp_path):
    db = Database(tmp_path / 'test.sqlite3')
    db.initialize()
    batch = begin_batch(db, 'legacy-test', 'rate', 'eastmoney')
    sid = archive_source(db, batch, 'eastmoney', 'rate', {'page': 1}, 1,
                         raw=b'original', obtained_at='2020-01-02T00:00:00+00:00')
    backup = tmp_path / 'before.sqlite3'
    db.backup(backup)
    with sqlite3.connect(backup) as conn:
        conn.execute('DROP TABLE collection_source_archive_members')
    result = build(db, '2020-01')
    assert result['removed'] == 1 and not result['errors']
    assert read_source(db, sid)[0] == b'original'
    assert read_source(Database(backup), sid)[0] == b'original'
    assert restore_source(db, sid)['restored'] is True
    assert restore_source(db, sid)['restored'] is False
    assert read_source(db, sid)[0] == b'original'


def test_published_orphan_can_be_recovered(tmp_path, monkeypatch):
    db = Database(tmp_path / 'test.sqlite3')
    db.initialize()
    batch = begin_batch(db, 'legacy-test', 'rate', 'eastmoney')
    sid = archive_source(db, batch, 'eastmoney', 'rate', {'page': 1}, 1,
                         raw=b'original', obtained_at='2020-01-02T00:00:00+00:00')
    groups, _ = source_archive.candidates(db, '2020-01')
    group, items = next(iter(groups.items()))
    source_archive.create_bundle(db, group, items)
    # Simulate a crash after publishing the bundle but before committing the mapping.
    with db.connection(write=True) as conn:
        conn.execute('DELETE FROM collection_source_archive_members')
    result = build(db, '2020-01')
    assert result['archives'] == [] and result['removed'] == 1 and not result['errors']
    assert read_source(db, sid)[0] == b'original'


def test_corrupt_bundle_never_deletes_original(tmp_path):
    db = Database(tmp_path / 'test.sqlite3')
    db.initialize()
    batch = begin_batch(db, 'legacy-test', 'rate', 'eastmoney')
    sid = archive_source(db, batch, 'eastmoney', 'rate', {'page': 1}, 1,
                         raw=b'original', obtained_at='2020-01-02T00:00:00+00:00')
    groups, _ = source_archive.candidates(db, '2020-01')
    group, items = next(iter(groups.items()))
    relative = source_archive.create_bundle(db, group, items)
    with (tmp_path / relative).open('ab') as stream:
        stream.write(b'corruption')
    result = build(db, '2020-01')
    assert result['removed'] == 0 and result['errors']
    assert read_source(db, sid)[0] == b'original'


def test_manual_worker_holds_gate_and_rejects_overlap(tmp_path, monkeypatch):
    db = Database(tmp_path / 'test.sqlite3')
    db.initialize()
    gate = RequestGate()
    service = SourceArchiveService(db, gate)
    started, finish = Event(), Event()
    def slow_build(db, cutoff, **kwargs):
        started.set()
        assert finish.wait(10)
        return {'archives': [], 'removed': 0, 'errors': []}
    monkeypatch.setattr('stock_analysis.collection.source_archive_service.build', slow_build)
    token = service.preview('2020-01')['token']
    service.start('2020-01', token)
    try:
        assert started.wait(3) and gate.active == 1
        second = service.preview('2020-01')['token']
        with pytest.raises(ValueError, match='正在运行'):
            service.start('2020-01', second)
    finally:
        finish.set()
        service.close()
    assert gate.active == 0


def test_archive_lock_prevents_parallel_builds(tmp_path):
    db = Database(tmp_path / 'test.sqlite3')
    db.initialize()
    lock_path = db.path.parent / 'collection_sources' / 'archive-operation'
    with instance_lock(lock_path):
        with pytest.raises(StorageError):
            build(db, '2020-01')


def test_archive_table_is_registered_and_uses_archive_time(tmp_path):
    db = Database(tmp_path / 'test.sqlite3')
    db.initialize()
    assert DESCRIPTIONS['collection_source_archive_members'][0] == '运行与配置'
    with db.connection(write=True) as conn:
        cols = [row['name'] for row in conn.execute('PRAGMA table_info(collection_source_archive_members)')]
        assert table_time(conn,'collection_source_archive_members',cols)[0] is None
        conn.execute('INSERT INTO collection_source_archive_members VALUES (?,?,?,?,?,?,?,?)',
                     ('a','b','c','hash',1,'hash',1,'2020-02-03T00:00:00+00:00'))
        assert table_time(conn,'collection_source_archive_members',cols) == ('2020-02-03T00:00:00+00:00','archived_at')


def test_interrupted_scheduled_run_resumes_once(tmp_path, monkeypatch):
    db = Database(tmp_path / 'test.sqlite3')
    db.initialize()
    service = SourceArchiveService(db, RequestGate())
    calls = []
    monkeypatch.setattr('stock_analysis.collection.source_archive_service.build', lambda db, cutoff, **kwargs: calls.append(cutoff) or {'archives': [], 'removed': 0, 'errors': []})
    service.configure({'enabled': True, 'day': 1, 'local_time':'02:00'})
    with db.connection(write=True) as conn:
        conn.execute('UPDATE collection_schedules SET updated_at=? WHERE schedule_id=?', ('2026-01-01T00:00:00+00:00',KEY))
        conn.execute("INSERT INTO collection_schedule_slots VALUES(?,?,'running',?,1,?,NULL,?,NULL)",
                     (KEY,'2026-03-01','old','2026-03-01T00:00:00+00:00','2026-03-01T00:00:00+00:00'))
    service.recover()
    service.tick(datetime(2026,3,1,3,tzinfo=ZONE))
    service.close()
    service.tick(datetime(2026,3,1,3,tzinfo=ZONE))
    assert calls == ['2026-02']
    with db.connection() as conn:
        row = conn.execute('SELECT status,attempts FROM collection_schedule_slots WHERE schedule_id=?',(KEY,)).fetchone()
        assert (row['status'],row['attempts']) == ('complete',2)


def test_old_v46_snapshot_migrates_and_reads_archived_response(tmp_path, monkeypatch):
    original_migrations = storage.MIGRATIONS
    monkeypatch.setattr(storage, 'MIGRATIONS', original_migrations[:-1])
    db = Database(tmp_path / 'test.sqlite3')
    db.initialize()
    batch = begin_batch(db, 'test', 'rate', 'eastmoney')
    sid = archive_source(db, batch, 'eastmoney', 'rate', {'page': 1}, 1,
                         raw=b'old-snapshot', obtained_at='2020-01-02T00:00:00+00:00')
    backup = tmp_path / 'before.sqlite3'
    db.backup(backup)
    monkeypatch.setattr(storage, 'MIGRATIONS', original_migrations)
    db.initialize()
    assert build(db, '2020-01')['removed'] == 1
    restored = Database(backup)
    restored.initialize()
    assert read_source(restored, sid)[0] == b'old-snapshot'
