import gzip
import hashlib
import sqlite3
import shutil
from pathlib import Path
from zipfile import ZipFile

import pytest

from datetime import datetime, timezone

from stock_analysis.collection.source_archive_read import read_stored_source
from stock_analysis.collection.source_archive import build, preview


class FakeDB:
    def __init__(self, root):
        self.path = root / 'db.sqlite3'
        with self.connection() as conn:
            conn.execute('CREATE TABLE collection_source_archive_members (file_path TEXT PRIMARY KEY, archive_path TEXT, member_path TEXT, stored_sha256 TEXT, stored_bytes INTEGER, archive_sha256 TEXT, archive_version INTEGER, archived_at TEXT)')
            conn.execute('CREATE TABLE collection_sources (file_path TEXT, source_system TEXT, dataset TEXT, obtained_at TEXT, stored_sha256 TEXT, raw_sha256 TEXT, stored_bytes INTEGER, batch_id INTEGER DEFAULT 1)')
            conn.execute('CREATE TABLE collection_batches (id INTEGER, scope_json TEXT)')
            conn.execute("INSERT INTO collection_batches VALUES (1,'{}')")

    def connection(self, *, write=False):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn


def test_read_archive_after_original_removed(tmp_path):
    db = FakeDB(tmp_path)
    content = gzip.compress(b'actual response', mtime=0)
    sha = hashlib.sha256(content).hexdigest()
    source = 'collection_sources/eastmoney/rate/file.raw.gz'
    archive = 'collection_sources/eastmoney/rate/2026-10-001.zip'
    archive_file = tmp_path / archive
    archive_file.parent.mkdir(parents=True)
    with ZipFile(archive_file, 'w') as z:
        z.writestr('file.raw.gz', content)
    with db.connection() as conn:
        conn.execute('INSERT INTO collection_source_archive_members VALUES (?,?,?,?,?,?,?,?)', (source, archive, 'file.raw.gz', sha, len(content), 'hash', 1, 'time'))
    assert read_stored_source(db, {'file_path': source, 'stored_sha256': sha}) == content
    with pytest.raises(ValueError, match='哈希'):
        read_stored_source(db, {'file_path': source, 'stored_sha256': 'wrong'})


def test_read_rejects_escape_and_duplicate_member(tmp_path):
    db = FakeDB(tmp_path)
    with pytest.raises(ValueError):
        read_stored_source(db, {'file_path': 'collection_sources/../secret', 'stored_sha256': 'x'})
    archive = tmp_path / 'collection_sources/a.zip'
    archive.parent.mkdir()
    with ZipFile(archive, 'w') as z:
        z.writestr('a', b'x')
        with pytest.warns(UserWarning):
            z.writestr('a', b'x')
    with db.connection() as conn:
        conn.execute('INSERT INTO collection_source_archive_members VALUES (?,?,?,?,?,?,?,?)', ('collection_sources/a.gz', 'collection_sources/a.zip', 'a', 'x', 1, 'hash', 1, 'time'))
    with pytest.raises(ValueError, match='重复'):
        read_stored_source(db, {'file_path': 'collection_sources/a.gz', 'stored_sha256': 'x'})
    archive.write_bytes(b'broken zip')
    with pytest.raises(ValueError, match='损坏'):
        read_stored_source(db, {'file_path': 'collection_sources/a.gz', 'stored_sha256': 'x'})


def test_bundle_deduplicates_and_checks_content(tmp_path):
    db = FakeDB(tmp_path)
    source = 'collection_sources/eastmoney/rate/a.raw.gz'
    source_file = tmp_path / source
    source_file.parent.mkdir(parents=True)
    raw = b'public response'
    content = gzip.compress(raw, mtime=0)
    source_file.write_bytes(content)
    row = (source, 'eastmoney', 'rate', '2026-10-31T15:59:00+00:00', hashlib.sha256(content).hexdigest(), hashlib.sha256(raw).hexdigest(), len(content))
    with db.connection() as conn:
        conn.executemany('INSERT INTO collection_sources(file_path,source_system,dataset,obtained_at,stored_sha256,raw_sha256,stored_bytes) VALUES (?,?,?,?,?,?,?)', [row, row])
    now = datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert preview(db, '2026-10', now=now)['groups'][0]['files'] == 1
    result = build(db, '2026-10', now=now)
    assert len(result['archives']) == 1
    assert not source_file.exists()
    assert read_stored_source(db, {'file_path': source, 'stored_sha256': row[4]}) == content
    assert build(db, '2026-10', now=now)['archives'] == []
    # An older SQLite snapshot has no archive mapping, but can verify the manifest.
    with db.connection() as conn:
        conn.execute('DROP TABLE collection_source_archive_members')
    assert read_stored_source(db, {'file_path': source, 'stored_sha256': row[4]}) == content
    with pytest.raises(ValueError, match='哈希冲突'):
        read_stored_source(db, {'file_path': source, 'stored_sha256': 'different'})


def test_cleanup_preserves_original_if_archive_is_ambiguous(tmp_path):
    db = FakeDB(tmp_path)
    source = 'collection_sources/eastmoney/rate/a.raw.gz'
    source_file = tmp_path / source
    source_file.parent.mkdir(parents=True)
    raw = b'response'
    content = gzip.compress(raw, mtime=0)
    source_file.write_bytes(content)
    row = (source, 'eastmoney', 'rate', '2026-10-01T00:00:00+00:00', hashlib.sha256(content).hexdigest(), hashlib.sha256(raw).hexdigest(), len(content))
    with db.connection() as conn:
        conn.execute('INSERT INTO collection_sources(file_path,source_system,dataset,obtained_at,stored_sha256,raw_sha256,stored_bytes) VALUES (?,?,?,?,?,?,?)', row)
    now = datetime(2026, 12, 1, tzinfo=timezone.utc)
    result = build(db, '2026-10', now=now)
    archive = tmp_path / result['archives'][0]
    source_file.write_bytes(content)
    shutil.copyfile(archive, archive.with_name('2026-10-duplicate.zip'))
    result = build(db, '2026-10', now=now)
    assert result['removed'] == 0
    assert result['errors']
    assert source_file.read_bytes() == content
