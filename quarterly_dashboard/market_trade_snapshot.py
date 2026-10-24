"""Full-field Eastmoney trading-day snapshots through the public collector."""
from __future__ import annotations

import argparse
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
import math
from pathlib import Path
import re
from uuid import uuid4
from zoneinfo import ZoneInfo

from .collection import (Cancelled, RequestCollector, archive_source, atomic_write, begin_batch,
                         digest, encoded, finish_batch, parsed_source, read_source)
from .industry_sources import EM
from .industry_updates import quarter_end
from .storage import Database, DEFAULT_DATABASE, utc_now

DATASET='market.trade_snapshot'
DEFINITION='em-valueanalysis-v1'
PARSER='market-trade-snapshot-v1'
SPEC=json.loads((Path(__file__).parent/'resources/market_trade_snapshot_fields.json').read_text(encoding='utf-8'))
FIELDS=SPEC['fields']
METRICS=[v['column'] for v in FIELDS.values() if v['type']!='TEXT']
DESCRIPTIONS=[v['column'] for v in FIELDS.values() if v['type']=='TEXT']


def normalized(row, day):
    unknown=set(row)-set(FIELDS)
    if unknown:
        raise ValueError(f'来源新增字段，原文已保留：{sorted(unknown)}')
    code,secu=row.get('SECURITY_CODE'),row.get('SECUCODE')
    if not isinstance(code,str) or not re.fullmatch(r'\d{6}',code) or secu not in {code+'.SH',code+'.SZ',code+'.BJ'}:
        raise ValueError('证券代码或交易所无效')
    if not isinstance(row.get('TRADE_DATE'),str) or row['TRADE_DATE'][:10]!=day:
        raise ValueError('来源交易日不匹配')
    out={'exchange':secu[-2:].lower(),'trade_date':day}
    for field,item in FIELDS.items():
        value=row.get(field)
        if value=='' or value is None:
            value=None
        elif item['type']=='TEXT':
            if not isinstance(value,(str,int)) or isinstance(value,bool):
                raise ValueError(f'{field} 文本无效')
            value=str(value)
        else:
            if isinstance(value,bool):
                raise ValueError(f'{field} 数值无效')
            try:
                number=Decimal(str(value))
                if not number.is_finite():
                    raise ValueError(f'{field} 数值非有限值')
                if item['type']=='INTEGER':
                    if number!=number.to_integral_value() or not -(2**63)<=number<2**63:
                        raise ValueError(f'{field} 股数非整数或溢出')
                    value=int(number)
                else:
                    value=float(number)
                    if not math.isfinite(value):
                        raise ValueError(f'{field} 溢出')
            except (InvalidOperation,TypeError):
                raise ValueError(f'{field} 数值不可解析') from None
        out[item['column']]=value
    cap,shares,close=(out[k] for k in ('total_market_cap','total_shares','close_price'))
    if cap is not None and shares is not None and close is not None:
        if abs(cap-shares*close)>max(1,abs(cap)*1e-6):
            raise ValueError(f'{code} 总市值与总股本乘未复权价不一致')
    out['content_hash']=digest(encoded({k:out[k] for k in METRICS}).encode())
    return out


def healthy(response):
    payload=response.json()
    result=payload.get('result') or {}
    return payload.get('success') is True and payload.get('code')==0 and isinstance(result.get('data'),list) and bool(result['data'])


def request_params(day,page,page_size=500):
    return dict(reportName=SPEC['report'],columns='ALL',filter=f"(TRADE_DATE='{day}')",
                sortColumns='SECURITY_CODE',sortTypes='1',pageSize=page_size,pageNumber=page,source='WEB',client='WEB')


def publish(conn,batch,rows,*,definition=DEFINITION):
    inserted=revised=unchanged=0
    stamp=utc_now()
    for values,source,obtained in rows:
        identity=(values['security_code'],values['exchange'],values['trade_date'],definition)
        old=conn.execute('SELECT * FROM market_trade_snapshot WHERE security_code=? AND exchange=? AND trade_date=? '
                         'AND definition_version=? AND is_latest=1',identity).fetchone()
        if old and old['content_hash']==values['content_hash']:
            changed=[k for k in DESCRIPTIONS if old[k]!=values[k]]
            if changed:
                conn.execute('UPDATE market_trade_snapshot SET '+','.join(k+'=?' for k in changed)+',updated_at=? WHERE id=?',
                             [*[values[k] for k in changed],stamp,old['id']])
            unchanged+=1
            continue
        if old:
            withdrawn=[k for k in METRICS if old[k] is not None and values[k] is None]
            if withdrawn:
                raise ValueError(f'{identity[0]} 有值变空待核实：{withdrawn}')
            conn.execute('UPDATE market_trade_snapshot SET is_latest=0 WHERE id=?',(old['id'],))
            revised+=1
        else:
            inserted+=1
        record={**values,'version':old['version']+1 if old else 1,'is_latest':1,'definition_version':definition,
                'batch_id':batch,'source_id':source,'obtained_at':obtained,'updated_at':stamp}
        cols=list(record)
        conn.execute('INSERT INTO market_trade_snapshot('+','.join(cols)+') VALUES('+','.join('?' for _ in cols)+')',
                     [record[k] for k in cols])
    return dict(inserted=inserted,revised=revised,unchanged=unchanged)


def publish_metric_patch(conn,batch,identity,patch,source,obtained,*,definition):
    """Merge explicit metric supplements into a full new version, never blank peers.

    The caller must archive evidence linking this record's previous source and
    the newly supplied inputs, and validate the field definitions before calling.
    """
    if not patch or set(patch)-set(METRICS):raise ValueError('补丁只能包含已登记数值字段')
    old=conn.execute('SELECT * FROM market_trade_snapshot WHERE security_code=? AND exchange=? AND trade_date=? '
        'AND definition_version=? AND is_latest=1',(*identity,definition)).fetchone()
    if not old:raise ValueError('补字段需要已有同口径记录')
    source_row=conn.execute('SELECT batch_id FROM collection_sources WHERE id=?',(source,)).fetchone()
    if not source_row or source_row[0]!=batch:raise ValueError('补充来源不属于本批次')
    raw={field:old[item['column']] for field,item in FIELDS.items()}
    for key,value in patch.items():
        field=next(field for field,item in FIELDS.items() if item['column']==key)
        raw[field]=value
    values=normalized(raw,identity[2])
    return publish(conn,batch,[(values,source,obtained)],definition=definition)


def batch_key(day,scope):
    return digest(encoded(dict(day=day,scope=scope,parser=PARSER,definition=DEFINITION,columns='ALL')).encode())


def collect_day(db,operation,day,*,quarter=None,http=None,recheck=False,progress=print,universe_version=None,
                progress_state=None):
    def report(phase, **details):
        if progress_state:
            progress_state({'kind':'snapshot','phase':phase,'trade_date':day,'quarter':quarter,**details})
    date.fromisoformat(day)
    scope=dict(trade_date=day,sampling='quarter_end' if quarter else 'all_trading_days',
               quarter=quarter,market_scope='source_returned_SH_SZ_BJ_securities',columns='ALL')
    key=batch_key(day,scope)
    with db.connection() as conn:
        completed=conn.execute("SELECT * FROM collection_batches WHERE dataset=? AND idempotency_key=? AND status='complete'",
                               (DATASET,key)).fetchone()
    if completed and not recheck:
        result=json.loads(completed['result_json'])
        # Empty is a known gap, not full coverage; subsequent manual recheck remains possible.
        report('skipped',page=result.get('pages',0),pages=result.get('pages',0),records=result.get('rows',0),
               total=result.get('count',result.get('rows',0)))
        return {**result,'batch_id':completed['id'],'skipped':True}
    batch=begin_batch(db,operation,DATASET,'eastmoney',day,day,scope=scope,frequency='daily',
                      trigger='review' if recheck else 'manual',idempotency_key=None if recheck else key,
                      parser_version=PARSER,definition_version=DEFINITION,universe_version=universe_version)
    http=http or RequestCollector(db,progress=progress)
    page=1; pages=1; count=None; rows=[]; sources=[]; seen=set(); fields=set()
    report('downloading',page=0,pages=None,records=0,total=None)
    try:
        while page<=pages:
            params=request_params(day,page)
            # Only interrupted same-day, exact-range/page evidence may be replayed.
            with db.connection() as conn:
                cached=conn.execute("SELECT s.id FROM collection_sources s JOIN collection_batches b ON b.id=s.batch_id "
                    "WHERE b.dataset=? AND b.idempotency_key=? AND b.status IN ('failed','cancelled') "
                    "AND s.parse_status='complete' AND s.request_status='success' "
                    "AND json_extract(s.request_json,'$.params.pageNumber')=? AND substr(s.obtained_at,1,10)=? "
                    "ORDER BY s.id DESC LIMIT 1",(DATASET,key,page,utc_now()[:10])).fetchone() if not recheck else None
            if cached:
                raw,previous=read_source(db,cached['id'])
                source=archive_source(db,batch,'eastmoney',DATASET,json.loads(previous['request_json']),1,
                    raw=raw,obtained_at=previous['obtained_at'],request_status='reused',reused_source_id=previous['id'])
            else:
                reply,source=http.get(batch,'eastmoney',DATASET,EM,params,
                    headers={'User-Agent':'Mozilla/5.0','Referer':'https://data.eastmoney.com/'},healthy_response=healthy,
                    industry_profile='snapshot')
                raw=reply.content
            sources.append(source)
            try:
                payload=json.loads(raw)
                result=payload.get('result') or {}
                if page==1 and payload.get('code')==9201 and payload.get('message')=='返回数据为空':
                    parsed_source(db,source,fields=[])
                    summary=dict(trade_date=day,quarter=quarter,rows=0,sources=sources,empty=True,coverage_status='empty')
                    finish_batch(db,batch,'complete','empty',result=summary)
                    report('saved',page=0,pages=0,records=0,total=0)
                    return {**summary,'batch_id':batch}
                if not payload.get('success') or not isinstance(result.get('data'),list):
                    raise ValueError('来源响应结构无效')
                if count is None:
                    count,pages=result.get('count'),result.get('pages')
                    if type(count) is not int or not 0<count<=20000 or pages!=math.ceil(count/500):
                        raise ValueError('分页总数或500行页容量异常')
                    report('downloading',page=page-1,pages=pages,records=len(rows),total=count)
                data=result['data']
                if result.get('count')!=count or result.get('pages')!=pages or len(data)!=min(500,count-(page-1)*500):
                    raise ValueError('分页总数变化、页长截断或缺页')
                with db.connection() as conn:
                    obtained=conn.execute('SELECT obtained_at FROM collection_sources WHERE id=?',(source,)).fetchone()[0]
                for row in data:
                    normalized_row=normalized(row,day)
                    identity=(normalized_row['security_code'],normalized_row['exchange'])
                    if identity in seen:
                        raise ValueError('来源跨页证券重复')
                    seen.add(identity);fields.update(row)
                    rows.append((normalized_row,source,obtained))
                parsed_source(db,source,fields=set().union(*(set(r) for r in data)))
            except Exception as exc:
                parsed_source(db,source,error=exc)
                raise
            progress(f'{day} · ALL · 分页 {page}/{pages} · {len(rows)}/{count} 家')
            report('downloading',page=page,pages=pages,records=len(rows),total=count)
            page+=1
        http.check_cancelled()
        report('publishing',page=pages,pages=pages,records=len(rows),total=count)
        valid={k:sum(v[k] is not None for v,_,_ in rows) for k in [v['column'] for v in FIELDS.values()]}
        summary=dict(trade_date=day,quarter=quarter,rows=len(rows),count=count,pages=pages,sources=sources,
                     fields=sorted(fields),valid_counts=valid,coverage_status='unknown',
                     coverage_reason='来源分页完整；历史证券名单有效期未核实，各指标缺值单独登记')
        with db.connection(write=True) as conn:
            summary.update(publish(conn,batch,rows))
            # Facts and completion share the transaction: a crash cannot publish an unfinished day.
            conn.execute("UPDATE collection_batches SET status='complete',coverage_status='unknown',finished_at=?,result_json=? WHERE id=?",
                         (utc_now(),encoded(summary),batch))
        report('saved',page=pages,pages=pages,records=len(rows),total=count)
        return {**summary,'batch_id':batch}
    except BaseException as exc:
        report('failed',page=page-1,pages=pages if count is not None else None,
               records=len(rows),total=count)
        finish_batch(db,batch,'cancelled' if isinstance(exc,(Cancelled,KeyboardInterrupt)) else 'failed',error=exc,
                     result=dict(rows_not_published=len(rows),sources=sources,trade_date=day))
        raise


def target_date(db,quarter,*,operation=None,http=None,progress=print):
    end=quarter_end(quarter).isoformat()
    with db.connection() as conn:
        roster=conn.execute('SELECT target_date FROM sw_cap_quarter_rosters WHERE quarter=?',(quarter,)).fetchone()
        prices=[r[0] for r in conn.execute('SELECT DISTINCT trade_date FROM market_quarterly_prices WHERE quarter_end=?',(end,))]
        if len(prices)>1 or roster and prices and roster[0]!=prices[0]:
            raise ValueError('既有季度市值/价格交易日冲突')
        if roster or prices:
            day=roster[0] if roster else prices[0]
            if not 0<=(date.fromisoformat(end)-date.fromisoformat(day)).days<=14:
                raise ValueError('季度交易日映射无效')
            return day
    if operation:
        from .index_collection import IndexCollector
        calendar=IndexCollector(db,operation,request_collector=http,progress=progress)
        calendar.load_calendar(cached=True)
        days=[d for d in calendar.trading_days if d<=end]
        if days and (date.fromisoformat(end)-date.fromisoformat(max(days))).days<=14:
            return max(days)
    raise ValueError(f'{quarter} 缺少经核实的本地季末交易日；需先准备交易日历')


def freeze_roster(db,quarter,day):
    """Reuse frozen lists; new historical lists explicitly use current constituents."""
    with db.connection(write=True) as conn:
        old=conn.execute('SELECT target_date FROM sw_cap_quarter_rosters WHERE quarter=?',(quarter,)).fetchone()
        if old:
            if old[0]!=day:raise ValueError('季度名单交易日冲突')
            return
        latest=conn.execute('SELECT member_import_id FROM sw_imports ORDER BY id DESC LIMIT 1').fetchone()
        if not latest:raise ValueError('缺少既有行业名单')
        conn.execute("INSERT INTO sw_cap_quarter_rosters VALUES(?,?,'current_constituents_backfill',?)",(quarter,day,latest[0]))
        conn.execute('INSERT INTO sw_cap_quarter_members(quarter,stock_code,industry_code) '
                     'SELECT ?,stock_code,industry_code FROM sw_memberships WHERE import_id=? AND listing_date IS NOT NULL AND listing_date<=?',
                     (quarter,latest[0],day))


def quarters(start,end):
    begin,finish=quarter_end(start),quarter_end(end)
    today=datetime.now(ZoneInfo('Asia/Shanghai')).date()
    if begin>finish or finish>=today or begin<date(2015,3,31):
        raise ValueError('仅支持2015Q1起至已结束季度的明确范围')
    return [f'{n//4}Q{n%4+1}' for n in range(begin.year*4+(begin.month-1)//3,finish.year*4+(finish.month-1)//3+1)]


def refresh_quarter(service,quarter,recheck,foundation,membership_check,progress,*,progress_state=None):
    # Rollback-journal maintenance reads can exceed the dashboard's 5s default.
    db=Database(service.db.path,journal_mode=service.db.journal_mode,busy_timeout_ms=60000)
    with db.connection() as conn:
        run=conn.execute("SELECT id FROM sw_update_runs WHERE status='running' ORDER BY id DESC LIMIT 1").fetchone()
    if not run:raise ValueError('季度快照必须在现有行业任务内运行')
    operation=f'sw_update_runs:{run[0]}'
    def cancelled():
        with db.connection() as conn:
            return service.collection_cancelled(run[0])
    http=RequestCollector(db,progress=progress,cancelled=cancelled)
    day=target_date(db,quarter,operation=operation,http=http,progress=progress)
    item=collect_day(db,operation,day,quarter=quarter,http=http,recheck=recheck,progress=progress,
                     universe_version=str(foundation['member_import_id']),progress_state=progress_state)
    freeze_roster(db,quarter,day)
    from .industry_cap_summary import aggregate_quarter
    if progress_state:
        progress_state({'kind':'snapshot','phase':'aggregation','trade_date':day,'quarter':quarter,
                        'page':item.get('pages',0),'pages':item.get('pages',0),'records':item.get('rows',0),
                        'total':item.get('count',item.get('rows',0)),
                        'collection_status':'skipped' if item.get('skipped') else 'saved'})
    aggregation=aggregate_quarter(db,quarter,run[0],operation)
    if progress_state:
        progress_state({'kind':'snapshot','phase':'complete','trade_date':day,'quarter':quarter,
                        'page':item.get('pages',0),'pages':item.get('pages',0),'records':item.get('rows',0),
                        'total':item.get('count',item.get('rows',0)),
                        'collection_status':'skipped' if item.get('skipped') else 'saved'})
    with db.connection() as conn:
        stocks=[r[0] for r in conn.execute('SELECT stock_code FROM sw_cap_quarter_members WHERE quarter=?',(quarter,))]
        found={r[0] for r in conn.execute('SELECT security_code FROM market_trade_snapshot WHERE trade_date=? AND is_latest=1 '
                    'AND definition_version=? AND total_market_cap IS NOT NULL AND close_price IS NOT NULL',(day,DEFINITION))}
        latest=conn.execute('SELECT max(id) FROM sw_imports').fetchone()[0]
    failures=[dict(stock_code=s,error='来源同日市值或未复权股价缺失') for s in stocks if s not in found]
    return dict(import_id=latest,action='cap_quarter',target=quarter,scope_count=len(stocks),requested_count=len(stocks),
                returned_count=len(set(stocks)&found),membership_check=membership_check,failures=failures,
                network_requests=http.requests,files=[],snapshot=item,legacy_updated=False,
                aggregation=aggregation,
                quarter_prices=dict(status='complete' if not failures else 'partial',source=DATASET,
                                    message='未复权股价随东财全字段快照同步保存'))


def audit(db,items):
    """Replay every archived page; compare the canonical business values both ways."""
    checks=[]
    for item in items:
        day=item['trade_date']; expected={}; source_count=0
        for sid in item['sources']:
            raw,_=read_source(db,sid)
            data=(json.loads(raw).get('result') or {}).get('data') or []
            source_count+=len(data)
            for row in data:
                n=normalized(row,day);key=(n['security_code'],n['exchange'])
                if key in expected:raise ValueError('验收来源重复')
                expected[key]=n
        with db.connection() as conn:
            actual={(r['security_code'],r['exchange']):dict(r) for r in conn.execute(
                'SELECT * FROM market_trade_snapshot WHERE trade_date=? AND is_latest=1 AND definition_version=?',
                (day,DEFINITION))}
            compare=['content_hash',*[v['column'] for v in FIELDS.values()]]
            mismatches=[k for k,n in expected.items() if k not in actual or any(actual[k][f]!=n[f] for f in compare)]
            source_links=conn.execute('SELECT count(*) FROM market_trade_snapshot f LEFT JOIN collection_sources s ON s.id=f.source_id AND s.batch_id=f.batch_id '
                                      'WHERE f.trade_date=? AND s.id IS NULL',(day,)).fetchone()[0]
        extra=set(actual)-set(expected)
        if mismatches or source_links or expected and extra or source_count!=item['rows']:
            raise ValueError(f'{day} 来源/事实双向验收失败')
        checks.append(dict(trade_date=day,source_rows=source_count,fact_rows=len(actual),dual_hash_verified=True,
                           coverage_status=item['coverage_status'],valid_counts=item.get('valid_counts',{})))
    return checks


def run(db,start,end,report,*,recheck=False):
    selected=list(reversed(quarters(start,end)))
    # Resolve all dates before making any source requests.
    dates={q:target_date(db,q) for q in selected}
    with db.connection(write=True) as conn:
        if conn.execute("SELECT 1 FROM sw_update_runs WHERE status='running'").fetchone():
            raise ValueError('已有行业任务运行')
        run_id=conn.execute("INSERT INTO sw_update_runs(action,target,recheck,started_at,status) VALUES('trade_snapshot_history',?,?,?,'running')",
                            (end,int(recheck),utc_now())).lastrowid
    operation=f'sw_update_runs:{run_id}'
    result=dict(start=start,end=end,total_quarters=len(selected),periods=[],status='running',operation_id=operation,started_at=utc_now())
    def cancelled():
        with db.connection() as conn:
            r=conn.execute('SELECT status FROM sw_update_runs WHERE id=?',(run_id,)).fetchone()
            return not r or r[0]!='running'
    def progress(message):
        print(message,flush=True)
        with db.connection(write=True) as conn:
            row=conn.execute('SELECT result_json FROM sw_update_runs WHERE id=? AND status=\'running\'',(run_id,)).fetchone()
            try:
                saved=json.loads(row['result_json'] or '{}') if row else {}
            except json.JSONDecodeError:
                saved={}
            payload={**saved,'message':message,'completed_quarters':len(result['periods']),
                     'total_quarters':len(selected),'report':str(report)}
            conn.execute('UPDATE sw_update_runs SET result_json=? WHERE id=? AND status=\'running\'',
                         (encoded(payload),run_id))
    def progress_state(update):
        with db.connection(write=True) as conn:
            row=conn.execute('SELECT result_json FROM sw_update_runs WHERE id=? AND status=\'running\'',(run_id,)).fetchone()
            try:
                saved=json.loads(row['result_json'] or '{}') if row else {}
            except json.JSONDecodeError:
                saved={}
            saved['collection_progress']={**(saved.get('collection_progress') or {}),**update}
            conn.execute('UPDATE sw_update_runs SET result_json=? WHERE id=? AND status=\'running\'',
                         (encoded(saved),run_id))
    http=RequestCollector(db,cancelled=cancelled,progress=progress)
    try:
        previous=json.loads(Path(report).read_text(encoding='utf-8')) if Path(report).exists() else {}
        prior_backup=Path(previous.get('backup',''))
        if (previous.get('start')==start and previous.get('end')==end and prior_backup.is_file()
                and prior_backup.resolve().parent==(db.path.parent/'backups').resolve()):
            result['backup']=str(prior_backup)
        else:
            result['backup']=str(db.backup(db.path.parent/'backups'/('before-trade-snapshot-history-'+uuid4().hex[:12]+'.sqlite3')))
        for q in selected:
            http.check_cancelled();progress(f'季度 {len(result["periods"])+1}/{len(selected)} · {q}')
            item=collect_day(db,operation,dates[q],quarter=q,http=http,recheck=recheck,progress=progress,
                             progress_state=progress_state)
            freeze_roster(db,q,dates[q])
            from .industry_cap_summary import aggregate_quarter
            aggregation=aggregate_quarter(db,q,run_id,operation)
            result.setdefault('aggregation_results',{})[q]=aggregation
            result['periods'].append(item);result['requests']=http.report()
            atomic_write(report,encoded(result).encode())
        progress('正在复核全部原文双哈希及来源/事实双向一致性')
        result['audit']=audit(db,result['periods'])
        result['empty_quarters']=[r['quarter'] for r in result['periods'] if r.get('empty')]
        result['coverage_status']='partial' if result['empty_quarters'] else 'unknown'
        result['status']='complete'
    except BaseException as exc:
        result.update(status='failed',error=str(exc))
        raise
    finally:
        result['finished_at']=utc_now();result['requests']=http.report()
        atomic_write(report,encoded(result).encode())
        with db.connection(write=True) as conn:
            prior=conn.execute('SELECT result_json FROM sw_update_runs WHERE id=?',(run_id,)).fetchone()
            try:
                saved=json.loads(prior['result_json'] or '{}') if prior else {}
            except json.JSONDecodeError:
                saved={}
            result['aggregation_results']={**(saved.get('aggregation_results') or {}),
                                           **result.get('aggregation_results',{})}
            result['collection_progress']=saved.get('collection_progress',result.get('collection_progress',{}))
            conn.execute('UPDATE sw_update_runs SET status=?,finished_at=?,result_json=?,error=? WHERE id=?',
                         (result['status'],result['finished_at'],encoded(result),result.get('error'),run_id))
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,default=DEFAULT_DATABASE)
    parser.add_argument('--start',default='2015Q1');parser.add_argument('--end',default='2026Q3')
    parser.add_argument('--recheck',action='store_true')
    parser.add_argument('--report',type=Path,default=Path('data/verification/reports/market-trade-snapshot-history-20261013.json'))
    args=parser.parse_args()
    db=Database(args.database,busy_timeout_ms=60000)
    with db.connection():pass  # Backend must migrate first; never migrate beside it.
    outcome=run(db,args.start,args.end,args.report,recheck=args.recheck)
    print(encoded(dict(status=outcome['status'],empty_quarters=outcome['empty_quarters'],report=str(args.report))),flush=True)
