import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

from quarterly_dashboard import storage
from quarterly_dashboard.financial_storage import (
    MAGIC, HEADER_BYTES, MAX_RAW_BYTES, compress_json, decompress_json, financial_row,
)
from quarterly_dashboard.storage import Database, StorageError, SyncKey, SyncResult, instance_lock


RAW = '{"未知字段":[null,0,-3,"0.000000","--",""],"data":[],"rCurrency":"CNY"}'


def test_payload_is_byte_exact_and_research_reader_compatible():
    payload = compress_json(RAW)
    assert isinstance(payload, bytes) and payload.startswith(MAGIC)
    assert decompress_json(payload) == RAW
    assert decompress_json(RAW) == RAW
    path = Path(__file__).resolve().parents[1] / '.agents/skills/a-share-value-research/scripts/research.py'
    spec = importlib.util.spec_from_file_location('standalone_research_codec', path)
    research = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(research)
    assert research.financial_json_text(payload) == RAW
    for invalid in (payload[:-1], payload + b'extra', payload[:HEADER_BYTES] + b'broken'):
        with pytest.raises(ValueError):
            decompress_json(invalid)
        with pytest.raises(ValueError):
            research.financial_json_text(invalid)


@pytest.mark.parametrize('kind', ['version', 'length', 'digest', 'oversize', 'stream'])
def test_corrupt_or_oversized_payload_rejected(kind):
    payload = bytearray(compress_json(RAW))
    index = {'version':len(MAGIC)-1, 'length':len(MAGIC)+7, 'digest':HEADER_BYTES-1, 'stream':len(payload)-1}
    if kind == 'oversize':
        payload[len(MAGIC):len(MAGIC)+8] = (MAX_RAW_BYTES+1).to_bytes(8,'big')
    else:
        payload[index[kind]] ^= 1
    with pytest.raises(ValueError):
        decompress_json(bytes(payload))


def old_database(tmp_path, monkeypatch):
    database = Database(tmp_path / 'old.sqlite3')
    migrations = list(storage._migration_files())
    with monkeypatch.context() as patch:
        patch.setattr(storage, '_migration_files', lambda: iter(item for item in migrations if item[0]<=34))
        database.initialize()
        identity = database.ensure_instrument('600519', '测试')
        key = SyncKey(identity, 'financial:lrb', 'sina')
        run = database.start_sync(key, parser_version='p1', methodology_version='m1')
        def save(conn):
            for period in ('2025-12-31', '2026-06-30'):
                conn.execute('INSERT INTO financial_reports VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (identity,'sina','lrb',period,'2026-08-01','source-original',RAW,
                     hashlib.sha256(RAW.encode()).hexdigest(),'2026-09-01T01:02:03Z',run))
        database.complete_sync(run, SyncResult(2,'2025-12-31','2026-06-30','2026-06-30'), save)
    return database


def test_v34_history_migration_preserves_every_column_and_backup(tmp_path, monkeypatch):
    database = old_database(tmp_path, monkeypatch)
    with sqlite3.connect(database.path) as conn:
        conn.row_factory = sqlite3.Row
        before = [dict(r) for r in conn.execute('SELECT * FROM financial_reports ORDER BY period')]
    with instance_lock(database.path):
        assert database.initialize()['schema_version'] == storage.MIGRATIONS[-1][0]
    with database.connection() as conn:
        stored = conn.execute('SELECT *,typeof(raw_json) AS encoding FROM financial_reports ORDER BY period').fetchall()
        assert all(r['encoding']=='blob' for r in stored)
        after = [financial_row({k:r[k] for k in r.keys() if k!='encoding'}) for r in stored]
    assert before == after
    assert database.check()['integrity'] == 'ok'
    with sqlite3.connect(next((tmp_path/'backups').glob('pre-migration-*.sqlite3'))) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 34
        assert conn.execute("SELECT count(*) FROM financial_reports WHERE typeof(raw_json)='text'").fetchone()[0] == 2
    # Reinitialization doesn't recompress or modify acquisition metadata.
    database.initialize()
    assert database.financial_reports(before[0]['instrument_id'],'sina','lrb') == before


def test_migration_hash_failure_rolls_back_all_rows(tmp_path, monkeypatch):
    database = old_database(tmp_path, monkeypatch)
    with sqlite3.connect(database.path) as conn:
        conn.execute("UPDATE financial_reports SET content_hash='wrong' WHERE period='2026-06-30'")
    with pytest.raises(ValueError, match='hash mismatch'):
        database.initialize()
    with sqlite3.connect(database.path) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 34
        assert conn.execute("SELECT count(*) FROM financial_reports WHERE typeof(raw_json)='text'").fetchone()[0] == 2


def test_new_writes_compressed_backup_restore_and_damage_detection(tmp_path):
    database = Database(tmp_path/'new.sqlite3');database.initialize()
    identity = database.ensure_instrument('600519')
    key = SyncKey(identity,'financial:lrb','sina')
    run = database.start_sync(key,parser_version='p1',methodology_version='m1')
    database.complete_sync(run,SyncResult(1,'2025-12-31','2025-12-31','2025-12-31'),
        lambda conn: database.upsert_financial_reports(conn,key,run,[{'period':'2025-12-31','raw_json':json.loads(RAW)}]))
    expected = database.financial_reports(identity,'sina','lrb')
    with database.connection() as conn:
        assert conn.execute('SELECT typeof(raw_json) FROM financial_reports').fetchone()[0] == 'blob'
    restored = Database(database.backup(tmp_path/'copy.sqlite3'))
    assert restored.financial_reports(identity,'sina','lrb') == expected
    assert restored.check()['integrity']=='ok'
    with database.connection(write=True) as conn:
        conn.execute('UPDATE financial_reports SET raw_json=?',(compress_json(RAW)[:-1],))
    with pytest.raises(StorageError):
        database.check()
    with pytest.raises(ValueError):
        database.financial_reports(identity,'sina','lrb')
