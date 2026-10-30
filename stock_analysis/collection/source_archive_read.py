"""Read source bytes from their original path or a registered immutable archive."""
import hashlib
import json
import sqlite3
from pathlib import PurePosixPath
from zipfile import BadZipFile, ZipFile


def _within_root(db, relative):
    if not isinstance(relative, str) or not relative:
        raise ValueError('来源路径无效')
    posix = PurePosixPath(relative)
    if posix.is_absolute() or '..' in posix.parts or '\\' in relative:
        raise ValueError('来源路径无效')
    root = (db.path.parent / 'collection_sources').resolve()
    unresolved = db.path.parent / relative
    current = db.path.parent
    for part in posix.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('来源路径不能包含符号链接')
    path = unresolved.resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError('来源文件不在登记的公共来源目录内')
    return path


def _member(archive_path, name, expected_size):
    member = PurePosixPath(name)
    if member.is_absolute() or '..' in member.parts or '\\' in name:
        raise ValueError('来源归档成员路径无效')
    try:
        with ZipFile(archive_path) as archive:
            matches = [i for i in archive.infolist() if i.filename == name]
            if len(matches) != 1 or matches[0].is_dir() or matches[0].file_size != expected_size:
                raise ValueError('来源归档成员缺失、重复或大小不符')
            return archive.read(matches[0])
    except BadZipFile as exc:
        raise ValueError('来源归档损坏') from exc


def _recover_from_manifests(db, item, original_path):
    """Read an old snapshot via its original path and hash, without changing it."""
    matches = []
    for path in original_path.parent.glob('????-??-*.zip'):
        with ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or names.count('archive-manifest.json') != 1:
                raise ValueError('归档清单缺失或成员重复')
            manifest = json.loads(archive.read('archive-manifest.json'))
            if manifest.get('version') != 1 or not isinstance(manifest.get('members'), list):
                raise ValueError('归档清单版本无效')
            for record in manifest['members']:
                if record.get('path') == item['file_path']:
                    if record.get('stored_sha256') != item['stored_sha256']:
                        raise ValueError('旧快照与归档哈希冲突')
                    content = _member(path, record['name'], record['stored_bytes'])
                    if hashlib.sha256(content).hexdigest() != item['stored_sha256']:
                        raise ValueError('旧快照归档成员哈希不符')
                    matches.append(content)
    if len(matches) != 1:
        raise FileNotFoundError('原始文件缺失，归档清单未找到唯一匹配')
    return matches[0]


def read_stored_source(db, item, *, archive_only=False):
    """Return stored bytes, tolerating a concurrent move after path lookup."""
    path = _within_root(db, item['file_path'])
    if not archive_only:
        try:
            return path.read_bytes()
        except FileNotFoundError:
            pass
    try:
        with db.connection() as conn:
            row = conn.execute('SELECT * FROM collection_source_archive_members WHERE file_path=?',
                               (item['file_path'],)).fetchone()
    except sqlite3.OperationalError as exc:
        if 'no such table: collection_source_archive_members' not in str(exc):
            raise
        row = None
    if not row:
        if archive_only:
            raise FileNotFoundError('来源归档映射不存在')
        try:
            return _recover_from_manifests(db, item, path)
        except BadZipFile as exc:
            raise ValueError('来源归档损坏') from exc
    entry = dict(row)
    if entry['stored_sha256'] != item['stored_sha256']:
        raise ValueError('来源归档映射哈希不符')
    archive_path = _within_root(db, entry['archive_path'])
    return _member(archive_path, entry['member_path'], entry['stored_bytes'])
