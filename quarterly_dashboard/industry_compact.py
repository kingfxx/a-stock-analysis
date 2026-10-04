"""Explicit offline financial compaction with backup and value/source verification."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .industry_service import dumps, financial_signature
from .industry_sources import EXTRA_FINANCIAL_FIELDS
from .industry_storage import latest_financial_rows, store_provenance
from .storage import Database, DEFAULT_DATABASE, instance_lock

AMOUNTS=('revenue','parent_profit',*EXTRA_FINANCIAL_FIELDS)


def fingerprint(conn):
    digest=hashlib.sha256()
    rows=latest_financial_rows(conn)
    for row in sorted(rows,key=lambda r:(r['stock_code'],r['period'])):
        value=[row['import_id'],row['stock_code'],row['period'],row['notice_date'],
               *(row[m] for m in AMOUNTS),json.loads(row['provenance_json'])]
        digest.update(dumps(value).encode());digest.update(b'\n')
    return digest.hexdigest()


def prune_extension_predecessors(conn):
    # Only the pre-backfill latest row can be superseded; earlier genuine revisions stay.
    extension_ids=[r[0] for r in conn.execute("SELECT id FROM sw_imports WHERE "
        "json_extract(source_manifest_json,'$.action')='financial_extensions' AND "
        "json_extract(source_manifest_json,'$.base_metrics_preserved')=1")]
    if not extension_ids:
        return 0
    cutoff=min(extension_ids)
    slots=','.join('?' for _ in extension_ids)
    sources={r['id']:r['provenance_json'] for r in conn.execute('SELECT * FROM sw_financial_provenance')}
    query=('WITH old AS (SELECT stock_code,period,max(import_id) id FROM sw_financial_facts '
           'WHERE import_id<? GROUP BY stock_code,period), '
           f'new AS (SELECT stock_code,period,min(import_id) id FROM sw_financial_facts WHERE import_id IN ({slots}) '
           'GROUP BY stock_code,period) SELECT o.rowid old_rowid,o.provenance_json old_json,o.provenance_id old_source,'
           'n.provenance_json new_json,n.provenance_id new_source FROM old JOIN new USING(stock_code,period) '
           'JOIN sw_financial_facts o ON o.stock_code=old.stock_code AND o.period=old.period AND o.import_id=old.id '
           'JOIN sw_financial_facts n ON n.stock_code=new.stock_code AND n.period=new.period AND n.import_id=new.id '
           'WHERE '+ ' AND '.join(f'o.{m} IS n.{m}' for m in ('revenue','parent_profit','notice_date'))+
           ' AND '+ ' AND '.join(f'(o.{m} IS NULL OR o.{m} IS n.{m})' for m in EXTRA_FINANCIAL_FIELDS))
    deletes=[]
    for row in conn.execute(query,(cutoff,*extension_ids)):
        old=json.loads(sources[row['old_source']] if row['old_source'] else row['old_json'])
        new=json.loads(sources[row['new_source']] if row['new_source'] else row['new_json'])
        # The original evidence must still be present, not just the amounts.
        def base_definitions(provenance):
            return financial_signature({'revenue':None,'parent_profit':None,'notice_date':None,
                                        'provenance':provenance})[-1][:2]
        if base_definitions(old)==base_definitions(new) and all(new.get(key)==value for key,value in old.items()):
            deletes.append((row['old_rowid'],))
    conn.executemany('DELETE FROM sw_financial_facts WHERE rowid=?',deletes)
    return len(deletes)


def compact_financial_rows(conn):
    count=0;last=0;cache={}
    while True:
        batch=conn.execute('SELECT rowid,provenance_json FROM sw_financial_facts '
            'WHERE provenance_id IS NULL AND rowid>? ORDER BY rowid LIMIT 5000',(last,)).fetchall()
        if not batch:
            break
        updates=[]
        for row in batch:
            payload=row['provenance_json']
            if payload not in cache:
                cache[payload]=store_provenance(conn,json.loads(payload))
            updates.append((cache[payload],row['rowid']))
        conn.executemany("UPDATE sw_financial_facts SET provenance_json='{}',provenance_id=? WHERE rowid=?",updates)
        count+=len(batch);last=batch[-1]['rowid']
    return count


def compact(db, report_directory):
    db.initialize()
    backup=db.backup(db.path.parent/'backups'/
        ('before-sw-financial-compact-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.sqlite3'))
    result={'backup':str(backup),'before_bytes':db.path.stat().st_size}
    with db.connection(write=True) as conn:
        if conn.execute("SELECT 1 FROM sw_update_runs WHERE status='running'").fetchone():
            raise ValueError('行业任务尚未完成，未执行整理')
        before=fingerprint(conn)
        result['before_rows']=conn.execute('SELECT count(*) FROM sw_financial_facts').fetchone()[0]
        result['deleted_rows']=prune_extension_predecessors(conn)
        result['normalized_rows']=compact_financial_rows(conn)
        after=fingerprint(conn)
        if before!=after:
            raise ValueError('最新财务金额或来源校验不一致，事务已回滚')
        result.update(latest_values_and_sources_unchanged=True,latest_sha256=after,
            after_rows=conn.execute('SELECT count(*) FROM sw_financial_facts').fetchone()[0],
            provenance_profiles=conn.execute('SELECT count(*) FROM sw_financial_provenance').fetchone()[0],
            shared_provenance_bytes=conn.execute('SELECT sum(length(cast(provenance_json AS BLOB))) FROM sw_financial_provenance').fetchone()[0],
            inline_provenance_bytes=conn.execute('SELECT sum(length(cast(provenance_json AS BLOB))) FROM sw_financial_facts').fetchone()[0])
    # VACUUM runs outside a transaction, with the backend instance lock held by the caller.
    conn=db._open()
    try:
        print('整理校验通过，回收数据库文件空间',flush=True)
        conn.execute('VACUUM')
        if conn.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            raise ValueError('SQLite 完整性检查失败')
        if conn.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('来源引用完整性检查失败')
        result['after_bytes']=db.path.stat().st_size
    finally:
        conn.close()
    write_report(report_directory,result)
    print(dumps(result),flush=True)
    return result


def write_report(directory, result):
    directory.mkdir(parents=True,exist_ok=True)
    (directory/'financial_storage_compaction.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# 行业财务存储整理报告','',
        f"数据库：{result['before_bytes']/2**20:,.2f} MiB → {result['after_bytes']/2**20:,.2f} MiB。",
        f"财务明细：{result['before_rows']:,} → {result['after_rows']:,} 行；删除本次扩展完整替代的旧行 {result['deleted_rows']:,} 条。",
        f"共享来源说明 {result['provenance_profiles']:,} 份；原始响应文件保留，明细通过 provenance_id 引用。",'',
        f"最新六个金额字段、披露日和完整来源信息一致：{result['latest_values_and_sources_unchanged']}。",
        f"最新记录 SHA256：{result['latest_sha256']}。",
        '真实修订版本保留。SQLite 完整性及外键检查通过。后续导入相同指标不增行，来源说明集中保存。',
        f"原业务表与行业分类、市值一致：{result.get('other_tables_unchanged','见单独校验记录')}。",'',
        f"整理前备份：{result['backup']}。"]
    if result.get('isolated_tests_passed'):
        lines+=['',f"隔离验证 {result['isolated_tests_passed']} 项通过；8765 页面与行业读取接口正常。"]
    (directory/'financial_storage_compaction.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,default=DEFAULT_DATABASE)
    parser.add_argument('--report-directory',type=Path,required=True)
    args=parser.parse_args()
    with instance_lock(args.database):
        compact(Database(args.database),args.report_directory)
