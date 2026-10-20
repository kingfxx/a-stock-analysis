"""Read-only coverage and storage audit for sovereign yield collection."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
from statistics import median
from time import perf_counter

from .bond_yields import DATASET, FIELDS, _hash, parse_page
from .maintenance import MaintenanceService
from .storage import DEFAULT_DATABASE, Database


def _measure(call, repetitions=5):
    elapsed=[]
    for _ in range(repetitions):
        start=perf_counter();call();elapsed.append((perf_counter()-start)*1000)
    return round(median(elapsed),3)


def audit(database=DEFAULT_DATABASE, *, verify_files=True):
    db=Database(database)
    with db.connection() as conn:
        check=conn.execute('PRAGMA quick_check').fetchone()[0]
        foreign_keys=[tuple(row) for row in conn.execute('PRAGMA foreign_key_check')]
        count,latest=conn.execute('SELECT count(*),max(observation_date) FROM sovereign_yield_observations WHERE is_latest=1').fetchone()
        total=conn.execute('SELECT count(*) FROM sovereign_yield_observations').fetchone()[0]
        fields={}
        for column in FIELDS.values():
            row=conn.execute(f'SELECT count({column}),min(CASE WHEN {column} IS NOT NULL THEN observation_date END),'
                f'max(CASE WHEN {column} IS NOT NULL THEN observation_date END) FROM sovereign_yield_observations WHERE is_latest=1').fetchone()
            fields[column]={'valid_rows':row[0],'null_rows':count-row[0],
                            'first_valid_date':row[1],'last_valid_date':row[2]}
        first=conn.execute("SELECT count(*) FROM sovereign_yield_observations WHERE is_latest=1 "
            "AND observation_date='2002-01-04' AND china_10y IS NOT NULL").fetchone()[0]
        duplicate_latest=conn.execute('SELECT count(*) FROM (SELECT observation_date,definition_version,count(*) n '
            'FROM sovereign_yield_observations WHERE is_latest=1 GROUP BY observation_date,definition_version HAVING n<>1)').fetchone()[0]
        bad_source=conn.execute('SELECT count(*) FROM sovereign_yield_observations f JOIN collection_sources s ON s.id=f.source_id '
            'WHERE s.batch_id<>f.batch_id OR s.dataset<>?',(DATASET,)).fetchone()[0]
        source_rows=[dict(row) for row in conn.execute("SELECT s.id,s.file_path,s.raw_sha256,s.stored_sha256,s.batch_id,s.dataset,"
            "s.parse_status,s.request_json,b.result_json,b.scope_json,b.scope_start,b.scope_end,b.status,b.coverage_status "
            "FROM collection_sources s JOIN collection_batches b ON b.id=s.batch_id "
            "WHERE s.dataset=? AND b.source_system='eastmoney' ORDER BY s.id",(DATASET,))]
        batch_rows=[dict(row) for row in conn.execute("SELECT id,status,coverage_status,scope_start,scope_end,result_json,scope_json "
            "FROM collection_batches WHERE dataset=? AND source_system='eastmoney' AND scope_json NOT LIKE '%target_discovery%' ORDER BY id",(DATASET,))]
        facts=[dict(row) for row in conn.execute('SELECT * FROM sovereign_yield_observations ORDER BY observation_date,version')]
        latest_query=lambda:conn.execute('SELECT observation_date,china_10y,us_10y FROM sovereign_yield_observations '
            'WHERE is_latest=1 ORDER BY observation_date DESC LIMIT 1').fetchall()
        range_query=lambda:conn.execute('SELECT observation_date,china_10y,us_10y FROM sovereign_yield_observations '
            "WHERE is_latest=1 AND observation_date BETWEEN '2020-01-01' AND '2026-12-31' ORDER BY observation_date").fetchall()
        latest_ms=_measure(latest_query);range_ms=_measure(range_query)
        plan=[tuple(row) for row in conn.execute("EXPLAIN QUERY PLAN SELECT observation_date FROM sovereign_yield_observations "
            "WHERE is_latest=1 AND observation_date BETWEEN '2020-01-01' AND '2026-12-31' ORDER BY observation_date")]
    source_checks=[];parsed_by_source={};batch_pages={}
    root=db.path.parent.resolve()
    for item in source_rows:
        try:scope=json.loads(item['scope_json'] or '{}')
        except (TypeError,ValueError):scope={}
        is_discovery=scope.get('kind')=='target_discovery'
        entry={'source_id':item['id'],'batch_id':item['batch_id'],'raw_sha256_verified':False,
               'stored_sha256_verified':False,'parsed_rows':0}
        try:
            if not verify_files:raise RuntimeError('hash verification skipped')
            path=(root/item['file_path']).resolve()
            if not path.is_relative_to((root/'collection_sources').resolve()):raise ValueError('路径越界')
            stored=path.read_bytes()
            entry['stored_sha256_verified']=hashlib.sha256(stored).hexdigest()==item['stored_sha256']
            raw=gzip.decompress(stored)
            entry['raw_sha256_verified']=hashlib.sha256(raw).hexdigest()==item['raw_sha256']
            pages,source_total,rows=parse_page(raw)
            entry.update(declared_pages=pages,declared_total=source_total,parsed_rows=len(rows))
            if not is_discovery:
                parsed_by_source[item['id']]=rows
                state=batch_pages.setdefault(item['batch_id'],{'pages':set(),'totals':set(),'row_count':0,'sources':[]})
                state['pages'].add(pages);state['totals'].add(source_total);state['row_count']+=len(rows);state['sources'].append(item['id'])
        except RuntimeError as exc:entry['error']=str(exc)
        except (OSError,ValueError,TypeError,KeyError) as exc:entry['error']=str(exc)
        source_checks.append(entry)
    fact_by_date={}
    def fact_value_hash(fact):
        return _hash({'observation_date':fact['observation_date'],**{field:fact[field] for field in FIELDS.values()}})
    for fact in facts:fact_by_date.setdefault(fact['observation_date'],[]).append(fact)
    source_to_fact={'raw_rows':0,'exact_value_in_history':0,'current_source_reference':0,'pending_withdrawal':0,'unmatched':0}
    stored_fact_hash_mismatches=sum(fact_value_hash(fact)!=fact['content_hash'] for fact in facts)
    pending_by_pair=set()
    for batch in batch_rows:
        try:result=json.loads(batch['result_json'] or '{}')
        except (TypeError,ValueError):result={}
        for pending in result.get('pending_withdrawals',[]):
            pending_by_pair.add((pending.get('source_id'),pending.get('date')))
    for source_id,rows in parsed_by_source.items():
        for row in rows:
            source_to_fact['raw_rows']+=1;day=row['observation_date'];versions=fact_by_date.get(day,[])
            source_hash=_hash(row)
            exact=any(fact_value_hash(item)==source_hash for item in versions)
            if exact:source_to_fact['exact_value_in_history']+=1
            elif any(item['source_id']==source_id for item in versions):source_to_fact['current_source_reference']+=1
            elif (source_id,day) in pending_by_pair:source_to_fact['pending_withdrawal']+=1
            else:source_to_fact['unmatched']+=1
    fact_to_source={'published_facts':0,'source_row_exact_match':0,'unmatched':0}
    for fact in facts:
        if fact['source_id'] not in parsed_by_source:continue
        fact_to_source['published_facts']+=1
        actual_hash=fact_value_hash(fact)
        if any(row['observation_date']==fact['observation_date'] and _hash(row)==actual_hash
               for row in parsed_by_source[fact['source_id']]):fact_to_source['source_row_exact_match']+=1
        else:fact_to_source['unmatched']+=1
    batch_checks=[]
    for batch in batch_rows:
        try:result=json.loads(batch['result_json'] or '{}')
        except (TypeError,ValueError):result={}
        pages=batch_pages.get(batch['id'],{'pages':set(),'totals':set(),'row_count':0,'sources':[]})
        totals=pages['totals'];declared=result.get('total',result.get('count'));declared_pages=result.get('pages')
        batch_checks.append({'id':batch['id'],'status':batch['status'],'coverage_status':batch['coverage_status'],
            'start':batch['scope_start'],'end':batch['scope_end'],'archived_rows':pages['row_count'],
            'declared_total':declared,'declared_pages':declared_pages,'source_page_declarations':sorted(pages['pages']),
            'source_total_declarations':sorted(totals),'page_totals_agree':len(totals)<=1 and
                (declared is None or not totals or totals=={declared}) and
                (declared_pages is None or not pages['pages'] or pages['pages']=={declared_pages}),
            'archived_rows_match_total':declared is None or pages['row_count']==declared,
            'publish_elapsed_ms':result.get('publish_elapsed_ms'),'publish_row_count':result.get('publish_row_count')})
    days=sorted({row['observation_date'] for row in facts if row['is_latest']})
    gaps=[]
    for left,right in zip(days,days[1:]):
        from datetime import date
        missing=(date.fromisoformat(right)-date.fromisoformat(left)).days-1
        if missing>0:gaps.append({'after':left,'before':right,'calendar_days_between':missing})
    inventory=MaintenanceService(db).read(refresh=True)
    table=next((item for item in inventory['tables'] if item['name']=='sovereign_yield_observations'),None)
    return {'database':str(db.path),'read_only':True,'quick_check':check,'foreign_key_errors':foreign_keys,
        'facts':{'latest_rows':count,'all_revisions':total,'latest_observation_date':latest,
                 'china_10y_nonnull_on_2002_01_04':bool(first),'duplicate_latest_groups':duplicate_latest,
                 'invalid_batch_source_references':bad_source,'fields':fields},
        'batches':batch_checks,
        'sources':{'referenced_count':len(source_rows),'files':source_checks,
                   'all_hashes_verified':bool(source_checks) and all(item.get('raw_sha256_verified') and item.get('stored_sha256_verified') for item in source_checks),
                   'source_to_fact':source_to_fact,'fact_to_source':fact_to_source,
                   'stored_fact_hash_mismatches':stored_fact_hash_mismatches,
                   'calendar_gap_intervals_not_market_holidays':gaps},
        'storage':table,'performance_ms_median':{'latest_observation_query':latest_ms,'2020_to_latest_range_query':range_ms,
            'runs':5,'query_plan':plan,'total_bytes':table.get('total_bytes') if table else None,
            'bytes_per_stored_revision':round(table['total_bytes']/total,2) if table and table.get('total_bytes') and total else None,
            'write_batches_with_timing':sum(item['publish_elapsed_ms'] is not None for item in batch_checks),
            'write_batch_elapsed_ms_median':round(median([item['publish_elapsed_ms'] for item in batch_checks
                if item['publish_elapsed_ms'] is not None]),3) if any(item['publish_elapsed_ms'] is not None for item in batch_checks) else None,
            'write_time_basis':'每个国债分段校验后发布事务耗时；无该字段的旧批次显示未记录'}}


def main():
    parser=argparse.ArgumentParser(description='只读核对中美国债收益率覆盖、来源哈希和存储性能')
    parser.add_argument('--database',type=Path,default=DEFAULT_DATABASE)
    parser.add_argument('--output',type=Path,help='可选报告JSON路径；省略时输出到stdout')
    parser.add_argument('--skip-source-hashes',action='store_true',help='跳过原始来源文件双哈希读取')
    args=parser.parse_args()
    report=audit(args.database,verify_files=not args.skip_source_hashes)
    text=json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(text+'\n',encoding='utf-8')
    else:print(text)


if __name__=='__main__':main()
