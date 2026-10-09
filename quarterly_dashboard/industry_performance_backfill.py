"""Resumable backfill of reported performance indicators, preserving existing amounts."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path

from .industry_bulk import RequestPacer
from .industry_service import IndustryService, dumps
from .industry_sources import EXTRA_FINANCIAL_FIELDS, PERFORMANCE_FIELDS
from .industry_performance import supplement
from .industry_storage import latest_financial_rows
from .network import create_data_session
from .storage import Database, DEFAULT_DATABASE

BASE_FIELDS = ('revenue','parent_profit','notice_date',*EXTRA_FINANCIAL_FIELDS)


def latest_facts(db, period=None):
    with db.connection() as conn:
        rows = latest_financial_rows(conn,period)
        return {(r['stock_code'],r['period']):dict(r) for r in rows}


def base_fingerprint(facts):
    rows = [(s,p,*(r[f] for f in BASE_FIELDS)) for (s,p),r in sorted(facts.items())
            if any(r[f] is not None for f in BASE_FIELDS)]
    return hashlib.sha256(dumps(rows).encode()).hexdigest()


def collect_period(service, foundation, period, directory, pacer, progress):
    folder=directory/period
    folder.mkdir(parents=True,exist_ok=True)
    universe={m['stock_code'] for m in foundation['members']}
    rows=[]
    for (stock,_),old in latest_facts(service.db,period).items():
        if stock in universe:
            row=dict(old)
            row['provenance']=json.loads(old['provenance_json'])
            rows.append(row)
    with create_data_session() as session:
        sources=supplement(session,folder,rows,period,progress,pacer)
    check={'period':period,'matched_company_count':len(rows),
        'field_counts':{m:sum(r.get(m) is not None for r in rows) for m in PERFORMANCE_FIELDS},'files':sources}
    bundle={**foundation,'asof':date.today().isoformat(),'obtained_at':datetime.now(timezone.utc).isoformat(),
        'financials':rows,'caps':[], 'manifest':{'standard':'SW2021','scope':'all_market_update',
        'pilot_industries':[],'action':'performance_fields','target':period,
        'membership_count':len(universe),'base_metrics_preserved':True,'checks':[check]}}
    return bundle,check


def report(directory,result):
    directory.mkdir(parents=True,exist_ok=True)
    (directory/'performance_fields.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')


def run(db, directory, report_directory, *, all_history=False, publish=False):
    service=IndustryService(db)
    foundation=service.foundation()
    initial=latest_facts(db)
    universe={m['stock_code'] for m in foundation['members']}
    periods=sorted({p for s,p in initial if s in universe and p>=f'{date.today().year-10}-09-30'},reverse=True)
    if not periods:
        raise ValueError('缺少可补采的行业财报期')
    if not all_history:
        periods=periods[:1]
    result={'status':'running','start':periods[-1],'end':periods[0],'periods':[],
            'base_metrics_unchanged':True,'initial_base_sha256':base_fingerprint(initial),'publish':publish}
    run_id=None
    with db.connection(write=True) as conn:
        if conn.execute("SELECT 1 FROM sw_update_runs WHERE status='running'").fetchone():
            raise ValueError('已有行业任务运行，未启动补采')
        run_id=conn.execute("INSERT INTO sw_update_runs(action,target,recheck,started_at,status) VALUES(?,?,0,?,'running')",
            ('performance_fields',result['end'],datetime.now(timezone.utc).isoformat())).lastrowid
    def progress(message):
        print(message,flush=True)
        with db.connection(write=True) as conn:
            conn.execute('UPDATE sw_update_runs SET result_json=? WHERE id=?',(dumps({'message':message}),run_id))
    pacer=RequestPacer(progress)
    try:
        if publish:
            result['backup']=str(db.backup(db.path.parent/'backups'/
                ('before-sw-performance-'+datetime.now().strftime('%Y%m%d-%H%M%S%f')+'.sqlite3')))
        for index,period in enumerate(periods,1):
            progress(f'扩展指标 {index}/{len(periods)} · {period}')
            bundle,check=collect_period(service,foundation,period,directory,pacer,progress)
            before=latest_facts(db,period)
            if publish:
                check['import_id']=service.import_bundle(bundle,capture_local=False)
                after=latest_facts(db,period)
                if base_fingerprint(before)!=base_fingerprint(after):
                    raise ValueError('原有基础指标发生变化，停止后续补采')
                check['field_counts']={m:sum(r[m] is not None for (s,p),r in after.items() if s in universe)
                                       for m in PERFORMANCE_FIELDS}
            result['periods'].append(check)
            result['network_requests']=pacer.requests
            result['base_metrics_unchanged']=base_fingerprint(latest_facts(db))==result['initial_base_sha256']
            report(report_directory,result)
        result['status']='complete'
        if not result['base_metrics_unchanged']:
            raise ValueError('原有基础指标核对不一致')
    except Exception as exc:
        result.update(status='failed',error=str(exc))
        raise
    finally:
        report(report_directory,result)
        with db.connection(write=True) as conn:
            conn.execute('UPDATE sw_update_runs SET status=?,finished_at=?,result_json=?,error=? WHERE id=?',
                ('complete' if result['status']=='complete' else 'failed',datetime.now(timezone.utc).isoformat(),
                 dumps(result),result.get('error'),run_id))
    print(dumps({'status':result['status'],'periods':len(result['periods']),
                 'base_metrics_unchanged':result['base_metrics_unchanged'],'network_requests':pacer.requests}),flush=True)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    parser.add_argument('--database',type=Path,default=DEFAULT_DATABASE)
    parser.add_argument('--report-directory',type=Path,required=True)
    parser.add_argument('--all-history',action='store_true')
    parser.add_argument('--publish',action='store_true')
    parser.add_argument('--snapshot-directory',type=Path)
    args=parser.parse_args()
    args.directory.mkdir(parents=True,exist_ok=True)
    db=Database(args.database)
    db.initialize()
    run(db,args.directory,args.report_directory,all_history=args.all_history,publish=args.publish)
    if args.snapshot_directory:
        if not args.publish:
            raise ValueError('快照要求先发布补采数据')
        from .industry_snapshot import export
        manifest=export(db.path,args.snapshot_directory)
        print(dumps({'snapshot':str(args.snapshot_directory),'files':len(manifest['files'])}),flush=True)
