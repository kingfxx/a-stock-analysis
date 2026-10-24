"""Explicit single-period SSE/SZSE industry tasks, independent of stock refreshes."""
from __future__ import annotations

import json
import re
from calendar import monthrange
from datetime import date, datetime, timezone

from .industry_cap_history import parse_history


def quarter_end(quarter):
    if not isinstance(quarter, str) or not re.fullmatch(r'20\d{2}Q[1-4]', quarter):
        raise ValueError('季度须为 YYYYQ1—YYYYQ4')
    month = int(quarter[-1])*3
    return date(int(quarter[:4]), month, monthrange(int(quarter[:4]), month)[1])


def validate_request(action, target, recheck=False, *, today=None):
    today = today or date.today()
    if type(recheck) is not bool:
        raise ValueError('核对修订选项必须为布尔值')
    if action == 'financial_period':
        try:
            end = date.fromisoformat(target)
        except (TypeError, ValueError):
            raise ValueError('报告期须为 YYYY-MM-DD') from None
        if end.isoformat() != target or target[5:] not in {'03-31','06-30','09-30','12-31'}:
            raise ValueError('仅支持季度报告期')
    elif action == 'cap_quarter':
        end = quarter_end(target)
        if end<date(2015,3,31):
            raise ValueError('交易快照从已授权的2015Q1开始')
    else:
        raise ValueError('仅支持独立的行业财务或季度市值任务')
    if end >= today:
        raise ValueError('仅更新已结束的报告期或季度')
    years = 12
    if end.year < today.year-years:
        raise ValueError(f'仅允许最近{years}年范围内的报告期或季度')


def fetch_financial_period(directory, stocks, period):
    raise ValueError('旧指定报告期财务联网入口已停用；请使用 market_financial.collect_public 与历史回填入口。')


def target_trade_date(db, quarter, directory):
    end = quarter_end(quarter)
    with db.connection() as conn:
        existing = conn.execute('SELECT target_date FROM sw_cap_quarter_rosters WHERE quarter=?',(quarter,)).fetchone()
        if existing:
            return existing[0]
        # An observed calendar quarter-end is unambiguous, and reuses only stored facts.
        if conn.execute('SELECT 1 FROM raw_daily_prices WHERE trade_date=? LIMIT 1',(end.isoformat(),)).fetchone():
            return end.isoformat()
    raise ValueError('旧交易日历联网入口已停用；请使用公共 market_trade_snapshot 工作流确认季末交易日。')


def cap_roster(db, foundation, quarter, target_date):
    with db.connection() as conn:
        saved = conn.execute('SELECT * FROM sw_cap_quarter_rosters WHERE quarter=?',(quarter,)).fetchone()
        if saved:
            return {**dict(saved),'members':[dict(r) for r in conn.execute(
                'SELECT stock_code,industry_code FROM sw_cap_quarter_members WHERE quarter=? ORDER BY stock_code',(quarter,))]}
    # Classification as of quarter-end; universe is still the current SSE/SZSE roster.
    if foundation['member_obtained_at'][:10]<target_date:
        raise ValueError('该季末后尚未更新分类名单，请先单独更新分类资料')
    valid = {r['code'] for r in foundation['taxonomy'] if r['level']==3}
    latest = {}
    for row in foundation['membership_history']:
        if row['effective_date']<=target_date and (row['stock_code'] not in latest or
                (row['effective_date'],row['source_update']) >
                (latest[row['stock_code']]['effective_date'],latest[row['stock_code']]['source_update'])):
            latest[row['stock_code']] = row
    members = []
    for row in foundation['members']:
        if row.get('listing_date') and row['listing_date']>target_date:
            continue
        past = latest.get(row['stock_code'])
        if past is None and row.get('effective_date') and row['effective_date']<=target_date:
            past = row
        if past is not None:
            members.append({'stock_code':row['stock_code'],
                            'industry_code':past['industry_code'] if past['industry_code'] in valid else None})
        elif row.get('listing_date'):
            members.append({'stock_code':row['stock_code'],'industry_code':None})
    if not members:
        raise ValueError('没有可确认的季末分类成员')
    return {'quarter':quarter,'target_date':target_date,'composition':'quarter_end_classification_current_universe',
            'member_import_id':foundation['member_import_id'],'members':members}


def fetch_cap_quarter(db, directory, stocks, target_date, progress, *, reuse_local=True):
    facts=[];missing=[]
    with db.connection() as conn:
        for stock in stocks:
            row=conn.execute("SELECT v.* FROM valuation_observations v JOIN instruments i ON i.id=v.instrument_id "
                "WHERE i.code=? AND v.metric='market_cap' AND v.source='baidu:opendata' AND v.observed_on=?",
                (stock,target_date)).fetchone()
            if reuse_local and row and row['value']>0:
                facts.append({'stock_code':stock,'trade_date':target_date,'total_cap':row['value']*1e8,
                    'provenance':{'source':'baidu:opendata','field':'总市值','unit':'元','source_unit':'亿元',
                        'method':'复用本地同日总市值 × 1e8','row_hash':row['content_hash'],'source_date':target_date}})
            else:
                missing.append(stock)
    failures=[]
    if not missing:
        return facts,failures
    raise ValueError('旧季度市值联网入口已停用；请使用公共 market_trade_snapshot。')
    return facts,failures


def perform(service, action, target, recheck, progress, *, run_id=None, progress_state=None):
    validate_request(action,target,recheck)
    from .market_financial import now,encoded
    batch_id=None
    owns_run=False
    if run_id is None:
        with service.db.connection(write=True) as conn:
            if conn.execute("SELECT 1 FROM sw_update_runs WHERE status='running'").fetchone():
                raise ValueError('已有行业任务运行，未启动财务采集')
            run_id=conn.execute("INSERT INTO sw_update_runs(action,target,recheck,started_at,status) "
                "VALUES(?,?,? ,?,'running')",(action,target,int(recheck),now())).lastrowid
            owns_run=True
    try:
        result=_perform(service,action,target,recheck,progress,batch_id,run_id=run_id,progress_state=progress_state)
        if owns_run:
            with service.db.connection(write=True) as conn:
                conn.execute("UPDATE sw_update_runs SET status='complete',finished_at=?,result_json=? WHERE id=?",
                    (now(),merge_run_result(conn,run_id,result),run_id))
        return result
    except Exception as exc:
        if progress_state:
            progress_state({'kind':'industry','phase':'failed','message':'任务未完成；已发布分段保留'})
        if batch_id is not None:
            from .market_financial import fail_batch
            fail_batch(service.db,batch_id,exc)
        if action=='financial_period' and run_id is not None:
            from .collection import finish_batch
            with service.db.connection() as conn:
                batch_ids=[r[0] for r in conn.execute("SELECT id FROM collection_batches WHERE operation_id=? AND status='running'",
                    ('sw_update_runs:'+str(run_id),))]
            for public_batch in batch_ids:
                finish_batch(service.db,public_batch,'failed',error=str(exc))
        if owns_run:
            with service.db.connection(write=True) as conn:
                conn.execute("UPDATE sw_update_runs SET status='failed',finished_at=?,error=? WHERE id=?",
                    (now(),str(exc),run_id))
        raise


def merge_run_result(conn, run_id, result):
    row=conn.execute('SELECT result_json FROM sw_update_runs WHERE id=?',(run_id,)).fetchone()
    try:
        saved=json.loads(row['result_json'] or '{}') if row else {}
    except json.JSONDecodeError:
        saved={}
    merged={**saved,**result}
    merged['aggregation_results']={**(saved.get('aggregation_results') or {}),
                                  **(result.get('aggregation_results') or {})}
    return json.dumps(merged,ensure_ascii=False,separators=(',',':'))


def _perform(service, action, target, recheck, progress, batch_id, *, run_id=None, progress_state=None):
    from .industry_memberships import ensure_daily_memberships
    from . import market_financial
    validate_request(action,target,recheck)
    membership_check=ensure_daily_memberships(service,progress,run_id=run_id)
    foundation=service.foundation()
    if action=='cap_quarter':
        from .market_trade_snapshot import refresh_quarter
        return refresh_quarter(service,target,recheck,foundation,membership_check,progress,
                               progress_state=progress_state)
    stocks=sorted(r['stock_code'] for r in foundation['members'])
    if not 4000<=len(stocks)<=10000:
        raise ValueError('全市场沪深分类名单规模异常，请先更新分类资料')
    name=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    directory=service.directory/'updates'/name
    directory.mkdir(parents=True,exist_ok=True)
    progress(f'全市场财务 {target} · 全 A 股三表及业绩指标 · 行业展示 {len(stocks)} 家沪深公司')
    from .collection import RequestCollector
    collector=RequestCollector(service.db,progress=progress,cancelled=lambda:service.collection_cancelled(run_id))
    operation_id='sw_update_runs:'+str(run_id) if run_id is not None else 'financial:'+str(target)
    try:
        datasets=market_financial.collect_public(service.db,operation_id,target,collector,progress,recheck=recheck,
            parallel_interfaces=True,progress_state=progress_state)
    finally:
        close=getattr(collector.session,'close',None)
        if callable(close):close()
    sources=[source for data in datasets for source in data['sources']]
    progress('来源已完成，正在保存新增／修订指标并汇总所选一期')
    if progress_state:
        progress_state({'kind':'financial','phase':'publishing','message':'四个接口均已完成全页校验，正在统一事务发布'})
    with service.db.connection(write=True) as conn:
        statement_summary=market_financial.publish(conn,None,target,datasets)
    if progress_state:
        progress_state({'kind':'financial','phase':'aggregation','message':'财务数据已保存，正在汇总行业指标'})
    data=service._load()
    import_id=data['import']['id']
    effective=service._financial_reader.window(data['token'],data['import']['member_import_id'],
                                               data['nonfinancial'],target,'ytd')
    if progress_state:
        progress_state({'kind':'financial','phase':'complete','message':'四表已发布并完成行业汇总'})
    failures=[{'stock_code':s,'error':'指定报告期营收或归母净利润待补'} for s in stocks
              if any(effective.get((s,target),{}).get(m) is None for m in ('revenue','parent_profit'))]
    universe=set(stocks)
    result={'import_id':import_id,'action':action,'target':target,'scope_count':len(stocks),'membership_check':membership_check,
            'requested_count':len(stocks),'returned_count':sum(r['SECURITY_CODE'] in universe for r in
                next(d for d in datasets if d['dataset']=='income')['rows']),
            'network_requests':collector.requests,'files':sources,'failures':failures,
            'collection_batch_ids':[d['collection_batch_id'] for d in datasets],
            'statements':statement_summary,'legacy_updated':False}
    (directory/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result
