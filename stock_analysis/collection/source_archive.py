"""Build verified immutable monthly bundles of public response files."""
from collections import defaultdict
from datetime import datetime
import gzip
import hashlib
import json
import os
from pathlib import PurePosixPath
from uuid import uuid4
from zipfile import ZIP_STORED, ZipFile
from zoneinfo import ZoneInfo

from stock_analysis.collection.source_archive_read import _recover_from_manifests, _within_root, read_stored_source
from stock_analysis.collection.collection import read_source
from stock_analysis.infrastructure.storage import instance_lock, utc_now

ZONE = ZoneInfo('Asia/Shanghai')
MAX_MEMBERS = 1000


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda:handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def candidates(db, cutoff, *, now=None):
    now = now or datetime.now(ZONE)
    try:
        parsed = datetime.strptime(cutoff, '%Y-%m')
        if parsed.strftime('%Y-%m') != cutoff or cutoff >= now.astimezone(ZONE).strftime('%Y-%m'):
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError('截止月份必须是已结束的月份，格式 YYYY-MM')
    with db.connection() as conn:
        rows = [dict(row) for row in conn.execute(
            'SELECT file_path,source_system,dataset,obtained_at,stored_sha256,raw_sha256,stored_bytes '
            'FROM collection_sources WHERE file_path IS NOT NULL ORDER BY file_path')]
        mapped = {row[0] for row in conn.execute('SELECT file_path FROM collection_source_archive_members')}
    grouped = defaultdict(list)
    for row in rows:
        grouped[row['file_path']].append(row)
    eligible, errors = defaultdict(list), []
    for name, refs in grouped.items():
        if name in mapped:
            continue
        try:
            path = _within_root(db, name)
            months = set()
            for ref in refs:
                dt = datetime.fromisoformat(ref['obtained_at'])
                if dt.utcoffset() is None:
                    raise ValueError('取得时间缺少时区')
                months.add(dt.astimezone(ZONE).strftime('%Y-%m'))
            signatures = {(r['source_system'], r['dataset'], r['stored_sha256'], r['raw_sha256'], r['stored_bytes']) for r in refs}
            if len(months) != 1 or len(signatures) != 1:
                raise ValueError('来源引用月份或元数据不一致')
            month = next(iter(months))
            if month > cutoff:
                continue
            provider, dataset, stored_hash, raw_hash, length = next(iter(signatures))
            parts = PurePosixPath(name).parts
            if len(parts) != 4 or parts[:3] != ('collection_sources', provider, dataset):
                raise ValueError('路径与来源不匹配')
            if not path.is_file():
                raise ValueError('原始文件不存在')
            eligible[(provider, dataset, month)].append(dict(path=name, name=parts[-1],
                stored_sha256=stored_hash, raw_sha256=raw_hash, stored_bytes=length))
        except (TypeError, ValueError, OSError) as exc:
            errors.append(dict(path=name, error=str(exc)))
    return eligible, errors


def preview(db, cutoff, *, now=None):
    groups, errors = candidates(db, cutoff, now=now)
    with db.connection() as conn:
        empty = conn.execute("SELECT count(*),sum(CASE WHEN json_extract(b.scope_json,'$.evidence_mode')='metadata_only' THEN 1 ELSE 0 END) "
            "FROM collection_sources s JOIN collection_batches b ON b.id=s.batch_id WHERE s.file_path IS NULL").fetchone()
    return dict(groups=[dict(provider=k[0], dataset=k[1], month=k[2], files=len(v),
                             bytes=sum(i['stored_bytes'] or 0 for i in v),
                             packages=(len(v)+MAX_MEMBERS-1)//MAX_MEMBERS)
                        for k, v in sorted(groups.items())], errors=errors,
                without_file=empty[0], metadata_only=empty[1] or 0)


def _verified(db, item):
    stored = _within_root(db, item['path']).read_bytes()
    if len(stored) != item['stored_bytes'] or sha(stored) != item['stored_sha256']:
        raise ValueError('来源存储哈希或大小不符: ' + item['path'])
    if sha(gzip.decompress(stored)) != item['raw_sha256']:
        raise ValueError('来源原文哈希不符: ' + item['path'])
    return stored


def create_bundle(db, group, items, *, on_item=None):
    provider, dataset, month = group
    directory = _within_root(db, f'collection_sources/{provider}/{dataset}/placeholder').parent
    directory.mkdir(parents=True, exist_ok=True)
    # UUID names avoid overwriting an orphan from an interrupted run.
    archive_path = directory / f'{month}-{uuid4().hex}.zip'
    temporary = archive_path.with_suffix('.tmp')
    manifest = {'version': 1, 'members': items}
    try:
        with temporary.open('xb') as handle:
            with ZipFile(handle, 'w', compression=ZIP_STORED, allowZip64=True) as archive:
                for item in items:
                    archive.writestr(item['name'], _verified(db, item))
                    if on_item:
                        on_item()
                archive.writestr('archive-manifest.json', json.dumps(manifest, ensure_ascii=False, sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        with ZipFile(temporary) as archive:
            if len(archive.namelist()) != len(set(archive.namelist())):
                raise ValueError('归档包含重复成员')
            if json.loads(archive.read('archive-manifest.json')) != manifest:
                raise ValueError('归档清单不符')
            for item in items:
                stored = archive.read(item['name'])
                if len(stored) != item['stored_bytes'] or sha(stored) != item['stored_sha256'] or sha(gzip.decompress(stored)) != item['raw_sha256']:
                    raise ValueError('归档成员校验失败')
        package_hash = file_sha(temporary)
        if os.name == 'nt':
            os.rename(temporary, archive_path)
        else:
            os.link(temporary, archive_path)
            temporary.unlink()
        relative = archive_path.relative_to(db.path.parent).as_posix()
        stamp = utc_now()
        with db.connection(write=True) as conn:
            for item in items:
                conn.execute('INSERT INTO collection_source_archive_members VALUES (?,?,?,?,?,?,?,?)',
                    (item['path'], relative, item['name'], item['stored_sha256'], item['stored_bytes'], package_hash, 1, stamp))
        return relative
    finally:
        temporary.unlink(missing_ok=True)


def cleanup(db, cutoff, *, on_progress=None):
    """Continue interrupted cleanup only after revalidating both copies and references."""
    with db.connection() as conn:
        rows = [dict(r) for r in conn.execute(
            'SELECT m.*,s.raw_sha256,s.stored_sha256 AS source_stored_sha256,s.stored_bytes AS source_stored_bytes,s.obtained_at FROM collection_source_archive_members m '
            'JOIN collection_sources s ON s.file_path=m.file_path ORDER BY m.file_path')]
    refs = defaultdict(list)
    for row in rows:
        refs[row['file_path']].append(row)
    removed, errors, checked_archives = 0, [], {}
    for index, (name, entries) in enumerate(refs.items(), 1):
        if on_progress:
            on_progress(index, len(refs))
        try:
            path = _within_root(db, name)
            if not path.exists():
                continue
            first = entries[0]
            times = [datetime.fromisoformat(e['obtained_at']) for e in entries]
            if any(t.utcoffset() is None or t.astimezone(ZONE).strftime('%Y-%m') > cutoff for t in times):
                continue
            months = {t.astimezone(ZONE).strftime('%Y-%m') for t in times}
            if len(months) != 1 or not PurePosixPath(first['archive_path']).name.startswith(next(iter(months))+'-'):
                raise ValueError('来源引用月份冲突')
            if len({(e['raw_sha256'],e['source_stored_sha256'],e['source_stored_bytes']) for e in entries}) != 1 or any(
                e['source_stored_sha256'] != first['stored_sha256'] or e['source_stored_bytes'] != first['stored_bytes'] for e in entries):
                raise ValueError('来源引用哈希冲突')
            bundle = _within_root(db, first['archive_path'])
            if bundle not in checked_archives:
                checked_archives[bundle] = file_sha(bundle)
            if checked_archives[bundle] != first['archive_sha256']:
                raise ValueError('归档包哈希不符')
            item = dict(file_path=name, stored_sha256=first['stored_sha256'])
            stored = read_stored_source(db, item, archive_only=True)
            if sha(stored) != first['stored_sha256'] or sha(gzip.decompress(stored)) != first['raw_sha256']:
                raise ValueError('归档成员双哈希不符')
            if _recover_from_manifests(db, item, path) != stored:
                raise ValueError('旧数据库快照恢复验证失败')
            if path.read_bytes() != stored:
                raise ValueError('散文件与归档成员不一致')
            # Serialize against new SQLite source references for the final removal.
            with db.connection(write=True) as conn:
                current = conn.execute('SELECT stored_sha256,raw_sha256,stored_bytes,obtained_at FROM collection_sources WHERE file_path=?', (name,)).fetchall()
                for ref in current:
                    stamp = datetime.fromisoformat(ref['obtained_at'])
                    if (stamp.utcoffset() is None or stamp.astimezone(ZONE).strftime('%Y-%m') not in months
                        or (ref['stored_sha256'],ref['raw_sha256'],ref['stored_bytes']) !=
                           (first['stored_sha256'],first['raw_sha256'],first['stored_bytes'])):
                        raise ValueError('清理前来源引用发生变化')
                if not current:
                    raise ValueError('清理前来源引用消失')
                path.unlink()
            removed += 1
        except (OSError, ValueError, TypeError) as exc:
            errors.append(dict(path=name, error=str(exc)))
    return removed, errors


def adopt_orphans(db, groups):
    """Recover a published, verified bundle interrupted before its database commit."""
    pending = {item['path']: item for items in groups.values() for item in items}
    directories = {_within_root(db, path).parent for path in pending}
    with db.connection() as conn:
        known = {row[0] for row in conn.execute('SELECT DISTINCT archive_path FROM collection_source_archive_members')}
    adopted = set()
    for directory in sorted(directories):
        for path in sorted(directory.glob('????-??-*.zip')):
            relative = path.relative_to(db.path.parent).as_posix()
            if relative in known:
                continue
            with ZipFile(path) as archive:
                names = archive.namelist()
                if len(names) != len(set(names)) or names.count('archive-manifest.json') != 1:
                    raise ValueError('未登记归档包清单异常: '+relative)
                manifest = json.loads(archive.read('archive-manifest.json'))
                items = manifest.get('members')
                if manifest.get('version') != 1 or not isinstance(items,list) or not items:
                    raise ValueError('未登记归档包格式异常: '+relative)
                if not any(item.get('path') in pending for item in items):
                    continue
                if any(item.get('path') not in pending or item['path'] in adopted or pending[item['path']] != item for item in items):
                    raise ValueError('未登记归档包来源冲突: '+relative)
                if set(names) != {'archive-manifest.json', *(item['name'] for item in items)}:
                    raise ValueError('未登记归档包成员不符: '+relative)
                for item in items:
                    stored = archive.read(item['name'])
                    if stored != _verified(db, item):
                        raise ValueError('未登记归档包内容不符: '+relative)
            package_hash = file_sha(path)
            stamp = utc_now()
            with db.connection(write=True) as conn:
                for item in items:
                    conn.execute('INSERT INTO collection_source_archive_members VALUES (?,?,?,?,?,?,?,?)',
                        (item['path'],relative,item['name'],item['stored_sha256'],item['stored_bytes'],package_hash,1,stamp))
            adopted.update(item['path'] for item in items)
    return {key:[item for item in items if item['path'] not in adopted] for key,items in groups.items()}


def build(db, cutoff, *, now=None, on_progress=None):
    """Create verified bundles then clean verified originals, with restartable cleanup."""
    with instance_lock(db.path.parent / 'collection_sources' / 'archive-operation'):
        groups, errors = candidates(db, cutoff, now=now)
        groups = adopt_orphans(db, groups)
        archives = []
        total = sum(len(items) for items in groups.values())
        count = 0
        def packed():
            nonlocal count
            count += 1
            if on_progress:
                on_progress(dict(phase='打包',done=count,total=total))
        for group, items in sorted(groups.items()):
            for start in range(0, len(items), MAX_MEMBERS):
                archives.append(create_bundle(db, group, items[start:start+MAX_MEMBERS], on_item=packed))
        removed, cleanup_errors = cleanup(db, cutoff, on_progress=(lambda done,total:on_progress(dict(phase='核验与清理',done=done,total=total))) if on_progress else None)
        return dict(archives=archives, removed=removed, errors=errors + cleanup_errors)


def restore_source(db, source_id):
    """Restore one source at its original path, never overwriting another file."""
    if type(source_id) is not int or source_id <= 0:
        raise ValueError('来源 ID 无效')
    with instance_lock(db.path.parent / 'collection_sources' / 'archive-operation'):
        _, item = read_source(db, source_id)
        stored = read_stored_source(db, item)
        path = _within_root(db, item['file_path'])
        if path.exists():
            if path.read_bytes() != stored:
                raise ValueError('原路径已有不同文件')
            return dict(path=item['file_path'], restored=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.' + uuid4().hex + '.restore.tmp')
        try:
            with temporary.open('xb') as handle:
                handle.write(stored)
                handle.flush()
                os.fsync(handle.fileno())
            if os.name == 'nt':
                os.rename(temporary, path)  # Windows rename fails if the target exists.
            else:
                os.link(temporary, path)  # Atomic exclusive publish without replacement.
        finally:
            temporary.unlink(missing_ok=True)
        return dict(path=item['file_path'], restored=True)
