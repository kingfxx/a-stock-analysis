"""Offline capitalization source compaction, with backup and semantic verification."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .industry_service import dumps
from .industry_storage import cap_rows_with_provenance, store_cap_provenance
from .storage import Database, DEFAULT_DATABASE, instance_lock


def cap_states(conn):
    return {r['id']:json.loads(r['source_manifest_json']).get('cap_status')
            for r in conn.execute('SELECT id,source_manifest_json FROM sw_imports')}


def cap_fingerprint(conn):
    """Retain every real change and its first import; ignore consecutive exact repeats."""
    states=cap_states(conn);history=hashlib.sha256();latest={};previous={};sources={}
    for row in cap_rows_with_provenance(conn):
        key=(row['stock_code'],row['trade_date']);payload=row['provenance_json']
        if payload not in sources:
            sources[payload]=dumps(json.loads(payload))
        signature=(row['total_cap'],sources[payload],states[row['import_id']])
        if previous.get(key)!=signature:
            history.update(dumps([*key,row['import_id'],*signature]).encode());history.update(b'\n')
        previous[key]=signature
        if states[row['import_id']]!='rejected':latest[key]=signature
    digest=hashlib.sha256()
    for key,signature in sorted(latest.items()):
        digest.update(dumps([*key,*signature]).encode());digest.update(b'\n')
    return {'history_sha256':history.hexdigest(),'latest_sha256':digest.hexdigest(),'latest_keys':len(latest)}


def prune_exact_cap_duplicates(conn):
    # Never remove a row from a table referenced by another table's FK.
    for table in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        name='"'+table[0].replace('"','""')+'"'
        if any(r['table']=='sw_cap_facts' for r in conn.execute('PRAGMA foreign_key_list('+name+')')):
            return 0
    states=cap_states(conn);previous={};deletes=[];sources={}
    for row in cap_rows_with_provenance(conn):
        key=(row['stock_code'],row['trade_date']);payload=row['provenance_json']
        if payload not in sources:sources[payload]=dumps(json.loads(payload))
        signature=(row['total_cap'],sources[payload],states[row['import_id']])
        if previous.get(key)==signature:
            deletes.append((row['import_id'],*key))
        previous[key]=signature
    conn.executemany('DELETE FROM sw_cap_facts WHERE import_id=? AND stock_code=? AND trade_date=?',deletes)
    return len(deletes)


def compact_cap_rows(conn):
    count=0;last=0;cache={}
    while True:
        rows=conn.execute('SELECT rowid,provenance_json FROM sw_cap_facts '
            'WHERE provenance_id IS NULL AND rowid>? ORDER BY rowid LIMIT 5000',(last,)).fetchall()
        if not rows:break
        updates=[]
        for row in rows:
            payload=row['provenance_json']
            if payload not in cache:cache[payload]=store_cap_provenance(conn,json.loads(payload))
            updates.append((cache[payload],row['rowid']))
        conn.executemany("UPDATE sw_cap_facts SET provenance_json='{}',provenance_id=? WHERE rowid=?",updates)
        last=rows[-1]['rowid'];count+=len(rows)
    return count


def other_table_hashes(conn):
    result={}
    names=[r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    for name in names:
        if name in ('sw_cap_facts','sw_cap_provenance','schema_migrations') or name.startswith('sqlite_'):continue
        digest=hashlib.sha256();quoted='"'+name.replace('"','""')+'"'
        for row in conn.execute('SELECT * FROM '+quoted+' ORDER BY rowid'):
            digest.update(dumps(list(row)).encode());digest.update(b'\n')
        result[name]=digest.hexdigest()
    return result


def compact(db, directory):
    # initialize itself takes a pre-migration backup; reuse it instead of making two copies.
    backups=db.path.parent/'backups';existing=set(backups.glob('*.sqlite3'))
    db.initialize()
    created=set(backups.glob('*.sqlite3'))-existing
    backup=max(created,key=lambda p:p.stat().st_mtime) if created else db.backup(backups/
        ('before-sw-cap-compact-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.sqlite3'))
    result={'backup':str(backup),'before_bytes':db.path.stat().st_size}
    with db.connection(write=True) as conn:
        if conn.execute("SELECT 1 FROM sw_update_runs WHERE status='running'").fetchone():
            raise ValueError('行业任务尚未完成，未整理市值记录')
        before=cap_fingerprint(conn);others=other_table_hashes(conn)
        result['before_rows']=conn.execute('SELECT count(*) FROM sw_cap_facts').fetchone()[0]
        result['deleted_exact_duplicates']=prune_exact_cap_duplicates(conn)
        result['normalized_rows']=compact_cap_rows(conn)
        if cap_fingerprint(conn)!=before:
            raise ValueError('市值有效历史或完整来源校验不一致，事务已回滚')
        if other_table_hashes(conn)!=others:
            raise ValueError('行业汇总或其他业务表变化，事务已回滚')
        result.update(before,values_and_sources_unchanged=True,other_tables_unchanged=True,other_table_hashes=others,
            after_rows=conn.execute('SELECT count(*) FROM sw_cap_facts').fetchone()[0],
            provenance_profiles=conn.execute('SELECT count(*) FROM sw_cap_provenance').fetchone()[0],
            shared_json_bytes=conn.execute('SELECT sum(length(cast(provenance_json AS BLOB))) FROM sw_cap_provenance').fetchone()[0],
            inline_json_bytes=conn.execute('SELECT sum(length(cast(provenance_json AS BLOB))) FROM sw_cap_facts').fetchone()[0])
    conn=db._open()
    try:
        print('市值与业务表校验通过，开始 VACUUM',flush=True)
        conn.execute('VACUUM')
        if conn.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or conn.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('SQLite 完整性或外键检查失败，保留备份与现场')
        result.update(after_bytes=db.path.stat().st_size,integrity_check='ok',foreign_key_check='ok')
    finally:
        conn.close()
    directory.mkdir(parents=True,exist_ok=True)
    (directory/'cap_storage_compaction.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    (directory/'cap_storage_compaction.md').write_text('\n'.join([
        '# 市值存储整理报告','',
        f"数据库：{result['before_bytes']/2**20:.2f} MiB → {result['after_bytes']/2**20:.2f} MiB。",
        f"市值明细：{result['before_rows']} → {result['after_rows']}；清理完全重复 {result['deleted_exact_duplicates']} 条。",
        f"共享来源：{result['provenance_profiles']} 份，共 {result['shared_json_bytes']} 字节。",
        '最新市值、有效历史、完整来源、行业汇总及其他业务表校验一致。真实修订、无效版本审计及原始响应文件保留。',
        'SQLite 完整性与外键检查通过。',f"整理前备份：{backup}。",'']),encoding='utf-8')
    print(dumps({k:v for k,v in result.items() if k!='other_table_hashes'}),flush=True)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,default=DEFAULT_DATABASE)
    parser.add_argument('--report-directory',type=Path,required=True)
    args=parser.parse_args()
    with instance_lock(args.database):
        compact(Database(args.database),args.report_directory)
