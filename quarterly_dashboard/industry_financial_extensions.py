"""Resumable single-source backfill of four industry financial amounts only."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path

from .industry_bulk import RequestPacer, fetch_pages
from .industry_service import IndustryService, dumps
from .industry_sources import EM, EXTRA_FINANCIAL_FIELDS, financial_rows
from .industry_storage import latest_financial_rows
from .network import create_data_session
from .storage import Database, DEFAULT_DATABASE

BASE_FIELDS = ('revenue','parent_profit','notice_date')
EXTRA_COLUMNS = 'SECURITY_CODE,REPORT_DATE,NOTICE_DATE,'+','.join(EXTRA_FINANCIAL_FIELDS.values())


def latest_facts(db, period=None):
    with db.connection() as conn:
        rows = latest_financial_rows(conn,period)
        return {(r['stock_code'],r['period']):dict(r) for r in rows}


def base_fingerprint(facts):
    rows = [(s,p,*(r[f] for f in BASE_FIELDS)) for (s,p),r in sorted(facts.items())
            if any(r[f] is not None for f in BASE_FIELDS)]
    return hashlib.sha256(dumps(rows).encode()).hexdigest()


def collect_period(service, foundation, period, directory, pacer, progress):
    folder = directory/period
    folder.mkdir(parents=True,exist_ok=True)
    with create_data_session() as session:
        session.headers.update({'User-Agent':'Mozilla/5.0','Referer':'https://data.eastmoney.com/'})
        raw,sources = fetch_pages(session,folder,'RPT_DMSK_FN_INCOME',EXTRA_COLUMNS,
                                  f"(REPORT_DATE='{period}')",progress,pacer)
    if any(r['REPORT_DATE'][:10]!=period for r in raw):
        raise ValueError('扩展指标报告期不匹配，未发布')
    source_by_code = {r['SECURITY_CODE']:sources[i//500] for i,r in enumerate(raw)}
    universe = {m['stock_code'] for m in foundation['members']}
    old = latest_facts(service.db,period)
    rows = []
    for row in financial_rows(raw,date.today().isoformat()):
        if row['stock_code'] not in universe:
            continue
        before = old.get((row['stock_code'],period))
        # Preserve existing base values and their exact provenance, even when missing.
        provenance = json.loads(before['provenance_json']) if before else {}
        provenance['extension_source'] = source_by_code[row['stock_code']]
        for metric,field in EXTRA_FINANCIAL_FIELDS.items():
            if row[metric] is not None:
                provenance[metric] = {'source':'eastmoney:RPT_DMSK_FN_INCOME','field':field,
                    'unit':'元','basis':'本年累计','scope':'归母' if metric=='deduct_parent_profit' else '合并',
                    'method':'直接取数','period':period,'source_ref':'extension_source'}
        row['provenance'] = provenance
        for field in BASE_FIELDS:
            row[field] = before[field] if before else None
        if before or any(row[m] is not None for m in EXTRA_FINANCIAL_FIELDS):
            rows.append(row)
    check = {'period':period,'source_count':len(raw),'current_universe_count':len(universe),
             'matched_company_count':len(rows),'source_fields':list(EXTRA_FINANCIAL_FIELDS.values()),
             'field_counts':{m:sum(r[m] is not None for r in rows) for m in EXTRA_FINANCIAL_FIELDS},
             'zero_counts':{m:sum(r[m]==0 for r in rows) for m in EXTRA_FINANCIAL_FIELDS},
             'files':sources}
    # This is a schema/pagination check, not a completeness threshold: historical unlisted stocks have no report.
    if raw and not any(check['field_counts'][m] for m in ('operating_cost','deduct_parent_profit','operating_profit')):
        raise ValueError('来源无扩展金额，未发布；请核对字段')
    now = datetime.now(timezone.utc).isoformat()
    bundle = {**foundation,'asof':date.today().isoformat(),'obtained_at':now,'financials':rows,'caps':[],
              'manifest':{'standard':'SW2021','scope':'all_market_update','pilot_industries':[],
                  'action':'financial_extensions','target':period,'membership_count':len(universe),
                  'base_metrics_preserved':True,'new_fields_source':'eastmoney:RPT_DMSK_FN_INCOME',
                  'checks':[check]}}
    return bundle,check


def report(directory, result):
    directory.mkdir(parents=True,exist_ok=True)
    (directory/'financial_extensions.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    lines = ['# 行业财务四字段补采报告','',f"状态：{result['status']}；范围：{result['start']}—{result['end']}。",
             '', '单位元，合并／归母按指标定义，本年累计。新增四字段统一取东财；未返回值留空，有效零保留。',
             '当前沪深名单回溯，不包含已退市股票；名单总数不作为历史漏采分母。',
             'OPERATE_INCOME 缺失时保持空，不无条件替换为营业总收入。毛利率未存储，金融行业不直接套用。',
             '下表四个财务字段列为有有效值的公司家数，不是金额；没有返回适用历史财报的公司不计入覆盖，来源提供的上市前财报仍保留。',
             '',f"原有营收、归母利润和披露日保持一致：{result.get('base_metrics_unchanged',False)}。",
             f"备份：{result.get('backup','尚未发布')}。",'',
             '| 报告期 | 来源记录数 | 当前名单匹配（家） | 营业收入覆盖（家） | 营业成本覆盖（家） | 扣非归母利润覆盖（家） | 营业利润覆盖（家） |',
             '|---|---:|---:|---:|---:|---:|---:|']
    for row in result['periods']:
        counts=row['field_counts']
        lines.append('| '+ ' | '.join(map(str,[row['period'],row['source_count'],row['matched_company_count'],
                      *(counts[m] for m in EXTRA_FINANCIAL_FIELDS)]))+' |')
    if result.get('error'):
        lines+=['',f"错误：{result['error']}"]
    (directory/'financial_extensions.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


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
            ('financial_extensions',result['end'],datetime.now(timezone.utc).isoformat())).lastrowid
    def progress(message):
        print(message,flush=True)
        with db.connection(write=True) as conn:
            conn.execute('UPDATE sw_update_runs SET result_json=? WHERE id=?',(dumps({'message':message}),run_id))
    pacer=RequestPacer(progress)
    try:
        if publish:
            result['backup']=str(db.backup(db.path.parent/'backups'/
                ('before-sw-financial-extensions-'+datetime.now().strftime('%Y%m%d-%H%M%S%f')+'.sqlite3')))
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
                                       for m in EXTRA_FINANCIAL_FIELDS}
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
    args=parser.parse_args()
    args.directory.mkdir(parents=True,exist_ok=True)
    run(Database(args.database),args.directory,args.report_directory,all_history=args.all_history,publish=args.publish)
