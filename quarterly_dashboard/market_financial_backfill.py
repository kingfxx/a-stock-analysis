"""Explicit four-dataset history collection; never updates legacy financial facts."""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import hashlib

from .industry_bulk import RequestPacer
from .industry_updates import validate_request
from . import market_financial as mf
from .market_financial import atomic_write, encoded, now
from .storage import Database, DEFAULT_DATABASE, instance_lock


def periods(start, end):
    validate_request('financial_period', start)
    validate_request('financial_period', end)
    if start > end:
        raise ValueError('起始报告期不能晚于结束报告期')
    result = []
    for year in range(int(start[:4]), int(end[:4])+1):
        for suffix in ('03-31', '06-30', '09-30', '12-31'):
            period = f'{year}-{suffix}'
            if start <= period <= end:
                result.append(period)
    return result


def legacy_fingerprint(db):
    """Audit existing financial rows without changing them or loading them all."""
    value=hashlib.sha256()
    with db.connection() as conn:
        for row in conn.execute('SELECT * FROM sw_financial_facts ORDER BY import_id,stock_code,period'):
            value.update(encoded(tuple(row)).encode('utf-8'))
            value.update(b'\n')
    return value.hexdigest()


def completed_batch(conn, period):
    return conn.execute("SELECT b.id FROM market_financial_batches b WHERE b.report_date=? "
        "AND b.status='complete' AND (SELECT count(DISTINCT s.dataset) FROM market_financial_sources s "
        "WHERE s.batch_id=b.id AND s.dataset IN ('income','balance','cashflow','performance') "
        "AND json_extract(s.params_json,'$.columns')='ALL')=4 ORDER BY b.id DESC LIMIT 1",(period,)).fetchone()


def run(db, start, end, report_path, *, recheck=False):
    selected = list(reversed(periods(start, end)))
    result = {'started_at': now(), 'start': start, 'end': end, 'total_periods':len(selected),
              'periods': [], 'status': 'running', 'datasets':list(mf.FIELDS), 'legacy_updated':False}
    report_path = Path(report_path)
    with db.connection(write=True) as conn:
        if conn.execute("SELECT 1 FROM sw_update_runs WHERE status='running'").fetchone():
            raise ValueError('已有行业任务运行，未启动历史采集')
        run_id = conn.execute("INSERT INTO sw_update_runs(action,target,recheck,started_at,status) "
                              "VALUES('financial_history',?,?,?,'running')",
                              (end, int(recheck), now())).lastrowid
    def progress(message):
        print(message, flush=True)
        with db.connection(write=True) as conn:
            state=conn.execute('SELECT status FROM sw_update_runs WHERE id=?',(run_id,)).fetchone()
            if not state or state['status']!='running':
                raise ValueError('历史任务状态已被中断，停止采集；已提交的报告期保留')
            conn.execute('UPDATE sw_update_runs SET result_json=? WHERE id=?',
                         (encoded({'message': message,'start':start,'end':end,'report':str(report_path),
                                   'completed_periods':len(result['periods']),'total_periods':len(selected)}), run_id))
    pacer=RequestPacer(progress)
    source_root=db.path.parent/'market_financial_sources'
    try:
        result['legacy_before_sha256']=legacy_fingerprint(db)
        result['backup'] = str(db.backup(db.path.parent/'backups'/
            ('before-market-financial-history-'+now().replace(':', '').replace('.', '')+'.sqlite3')))
        for period in selected:
            with db.connection() as conn:
                complete = completed_batch(conn,period)
            if complete and not recheck:
                item = {'period': period, 'skipped': True, 'batch_id': complete['id']}
            else:
                progress(f'历史财务 {len(result["periods"])+1}/{len(selected)} · {period} · 四表全部字段')
                with db.connection() as conn:
                    previous=conn.execute('SELECT status FROM market_financial_batches WHERE report_date=? '
                                          'ORDER BY id DESC LIMIT 1',(period,)).fetchone()
                batch_id=mf.begin_batch(db,period)
                try:
                    data=mf.collect(source_root,period,pacer,progress,
                                    resume=bool(previous and previous['status']=='failed'))
                    progress(f'{period} 四表校验完成，正在发布新表')
                    with db.connection(write=True) as conn:
                        summary=mf.publish(conn,batch_id,period,data)
                    mf.clear_checkpoints(data)
                    item={'period':period,'batch_id':batch_id,'statements':summary}
                except Exception as exc:
                    mf.fail_batch(db,batch_id,exc)
                    raise
            result['periods'].append(item)
            result['network_requests']=pacer.requests
            atomic_write(report_path, encoded(result).encode('utf-8'))
        result['legacy_after_sha256']=legacy_fingerprint(db)
        if result['legacy_after_sha256']!=result['legacy_before_sha256']:
            raise ValueError('采集期间旧业绩表发生变化，请核对其他写入任务；本任务未写入旧表')
        result['status'] = 'complete'
    except Exception as exc:
        result.update(status='failed', error=str(exc))
        raise
    finally:
        result['finished_at'] = now()
        atomic_write(report_path, encoded(result).encode('utf-8'))
        with db.connection(write=True) as conn:
            conn.execute('UPDATE sw_update_runs SET status=?,finished_at=?,result_json=?,error=? WHERE id=?',
                         (result['status'], result['finished_at'], encoded(result), result.get('error'), run_id))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=DEFAULT_DATABASE)
    parser.add_argument('--start', required=True)
    parser.add_argument('--end', required=True)
    parser.add_argument('--recheck', action='store_true', help='重新核对已完整采集的报告期')
    parser.add_argument('--online', action='store_true', help='与已完成迁移的日常后台共用库；通过行业任务锁互斥')
    parser.add_argument('--report', type=Path, default=Path('data/verification/reports/market-financial-history')/
                        (date.today().isoformat()+'.json'))
    args = parser.parse_args()
    periods(args.start, args.end)
    def execute(db):
        result = run(db, args.start, args.end, args.report, recheck=args.recheck)
        print(json.dumps({'status': result['status'], 'periods': len(result['periods']),
                          'report': str(args.report)}, ensure_ascii=False))
    if args.online:
        db=Database(args.database)
        with db.connection():
            pass  # Validate current schema; never migrate under a running backend.
        execute(db)
    else:
        # Offline entry retains the exclusive process lock during migration.
        with instance_lock(args.database):
            db = Database(args.database)
            db.initialize()
            execute(db)
