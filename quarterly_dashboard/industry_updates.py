"""Explicit single-period SSE/SZSE industry tasks, independent of stock refreshes."""
from __future__ import annotations

import hashlib
import json
import re
import time
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone

from .industry_cap_history import parse_history
from .industry_sources import EM, FINANCIAL_COLUMNS, attach_financial_source, financial_rows, parse_quotes
from .network import create_data_session
from .sources import TENCENT_URL, parse_daily_prices
from .valuation import BAIDU_URL


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
    else:
        raise ValueError('仅支持独立的行业财务或季度市值任务')
    if end >= today:
        raise ValueError('仅更新已结束的报告期或季度')
    years = 12 if action == 'financial_period' else 10
    if end.year < today.year-years:
        raise ValueError(f'仅允许最近{years}年范围内的报告期或季度')


def _response(session, directory, url, params=None, *, binary=False):
    response = session.get(url, params=params, timeout=20)
    response.raise_for_status()
    payload = response.content if binary else json.dumps(response.json(),ensure_ascii=False,sort_keys=True).encode('utf-8')
    digest = hashlib.sha256(payload).hexdigest()
    folder = directory/'responses'
    folder.mkdir(parents=True,exist_ok=True)
    path = folder/(digest+('.txt' if binary else '.json'))
    if not path.exists():
        path.write_bytes(payload)
    provenance = {'url':url,'params':params,'file':str(path.resolve()),'sha256':digest}
    return (payload if binary else json.loads(payload)),provenance


def fetch_financial_period(directory, stocks, period):
    if not stocks:
        return []
    stock_filter = ','.join('"'+s+'"' for s in stocks)
    params = {'reportName':'RPT_DMSK_FN_INCOME',
        'columns':FINANCIAL_COLUMNS,
        'filter':f'(REPORT_DATE=\'{period}\')(SECURITY_CODE in ({stock_filter}))',
        'sortColumns':'SECURITY_CODE','sortTypes':'1','pageSize':100,'pageNumber':1}
    with create_data_session() as session:
        payload,source = _response(session,directory,EM,params)
    if not payload.get('success'):
        raise ValueError('指定报告期财务接口失败')
    result = payload.get('result') or {}
    raw = result.get('data') or []
    if result.get('pages',0)>1 or result.get('count',0)!=len(raw):
        raise ValueError('指定报告期财务数据不完整')
    rows = financial_rows(raw,date.today().isoformat())
    if any(r['period']!=period or r['stock_code'] not in stocks for r in rows):
        raise ValueError('来源返回了请求范围以外的财务')
    if len({r['stock_code'] for r in rows})!=len(rows):
        raise ValueError('指定报告期财务重复')
    for row in rows:
        attach_financial_source(row,source)
    from .industry_performance import REPORT, COLUMNS, attach_performance
    params={**params,'reportName':REPORT,'columns':COLUMNS,
        'filter':f'(REPORTDATE=\'{period}\')(SECURITY_CODE in ({stock_filter}))'}
    with create_data_session() as session:
        payload,source=_response(session,directory,EM,params)
    if not payload.get('success') and payload.get('code')!=9201:
        raise ValueError('指定报告期业绩指标接口失败')
    result=payload.get('result') or {}
    raw=result.get('data') or []
    if result.get('pages',0)>1 or result.get('count',0)!=len(raw):
        raise ValueError('指定报告期业绩指标数据不完整')
    if any(r['SECURITY_CODE'] not in stocks for r in raw):
        raise ValueError('业绩指标股票超出请求范围')
    attach_performance(rows,raw,[source],period,date.today().isoformat())
    return rows


def target_trade_date(db, quarter, directory):
    end = quarter_end(quarter)
    with db.connection() as conn:
        existing = conn.execute('SELECT target_date FROM sw_cap_quarter_rosters WHERE quarter=?',(quarter,)).fetchone()
        if existing:
            return existing[0]
        # An observed calendar quarter-end is unambiguous, and reuses only stored facts.
        if conn.execute('SELECT 1 FROM raw_daily_prices WHERE trade_date=? LIMIT 1',(end.isoformat(),)).fetchone():
            return end.isoformat()
    # Independently fetch a small index calendar, without refreshing any tracked stock.
    start = (end-timedelta(days=20)).isoformat()
    with create_data_session() as session:
        payload,_ = _response(session,directory,TENCENT_URL,
                             {'param':f'sh000001,day,,{end.isoformat()},30,'})
    days = [r['date'] for r in parse_daily_prices(payload,'sh000001','') if start<=r['date']<=end.isoformat()]
    if not days or (end-date.fromisoformat(max(days))).days>14:
        raise ValueError('无法确认季末最后交易日，未采集市值')
    return max(days)


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
    with create_data_session() as session:
        symbols=','.join(('sh' if s[0]=='6' else 'sz')+s for s in missing)
        raw,quote_source=_response(session,directory,'https://qt.gtimg.cn/q='+symbols,binary=True)
        references={r['stock_code']:r for r in parse_quotes(raw.decode('gbk'))}
        for index,stock in enumerate(missing,1):
            progress(f'季度市值 {index}/{len(missing)} · {stock}')
            try:
                reference=references.get(stock)
                if reference is None:
                    raise ValueError('缺少有效总市值参考报价')
                if reference['trade_date']==target_date:
                    provenance={**reference['provenance'],**quote_source}
                    provenance.pop('raw',None)
                    facts.append({**reference,'provenance':provenance})
                    continue
                params={'openapi':'1','dspName':'iphone','tn':'tangram','client':'app','query':'总市值',
                    'code':stock,'word':'','resource_id':'51171','market':'ab','tag':'总市值',
                    'chart_select':'近一年','industry_select':'','skip_industry':'1','finClientType':'pc'}
                payload,source=_response(session,directory,BAIDU_URL,params)
                time.sleep(.5)
                values=parse_history(payload)
                common=values.get(reference['trade_date'])
                value=values.get(target_date)
                if common is None or abs(common-reference['total_cap']/1e8)>.020001:
                    raise ValueError('同日总市值来源核对不通过')
                if value is None:
                    raise ValueError('来源缺少精确目标交易日，未使用其他日期替代')
                facts.append({'stock_code':stock,'trade_date':target_date,'total_cap':value*1e8,
                    'provenance':{**source,'source':'baidu:opendata','field':'总市值','source_unit':'亿元',
                        'method':'精确季末总市值 × 1e8','source_date':target_date,
                        'validation_date':reference['trade_date'],'validation_tencent_field':'45',
                        'validation_difference_yi':abs(common-reference['total_cap']/1e8)}})
            except Exception as exc:
                failures.append({'stock_code':stock,'error':str(exc)})
    return facts,failures


def perform(service, action, target, recheck, progress):
    validate_request(action,target,recheck)
    from .market_financial import begin_batch, fail_batch
    batch_id=begin_batch(service.db,target) if action=='financial_period' else None
    try:
        return _perform(service,action,target,recheck,progress,batch_id)
    except Exception as exc:
        if batch_id is not None:
            fail_batch(service.db,batch_id,exc)
        raise


def _perform(service, action, target, recheck, progress, batch_id):
    # Import lazily: the bulk history collector also uses calendar/roster helpers here.
    from .industry_bulk import RequestPacer, cap_rows, fetch_pages
    validate_request(action,target,recheck)
    from .industry_memberships import ensure_daily_memberships
    membership_check=ensure_daily_memberships(service,progress)
    foundation=service.foundation()
    stocks=sorted(r['stock_code'] for r in foundation['members'])
    if not 4000<=len(stocks)<=10000:
        raise ValueError('全市场沪深分类名单规模异常，请先更新分类资料')
    name=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    bundle={**foundation,'asof':date.today().isoformat(),'obtained_at':datetime.now(timezone.utc).isoformat(),
            'financials':[],'caps':[],
            'manifest':{'standard':'SW2021','scope':'all_market_update','pilot_industries':[],
                        'action':action,'target':target,'recheck':recheck,'membership_count':len(stocks),
                        'historical_universe':'current_SSE_SZSE_constituents_backfill',
                        'pacing':'串行；2.5—3.5 秒随机间隔；每 20 次休息 30 秒；失败冷却 60/120 秒'}}
    directory=service.directory/'updates'/name
    directory.mkdir(parents=True,exist_ok=True)
    pacer=RequestPacer(progress)
    sources=[]
    def fetch(report, columns, filter_):
        with create_data_session() as session:
            session.headers.update({'User-Agent':'Mozilla/5.0','Referer':'https://data.eastmoney.com/'})
            return fetch_pages(session,directory,report,columns,filter_,progress,pacer)
    if action=='financial_period':
        from . import market_financial
        required=stocks
        universe=set(stocks)
        progress(f'全市场财务 {target} · 全 A 股三表及业绩指标 · 行业展示 {len(stocks)} 家沪深公司')
        with service.db.connection() as conn:
            previous=conn.execute('SELECT status FROM market_financial_batches WHERE report_date=? AND id<? '
                                  'ORDER BY id DESC LIMIT 1',(target,batch_id)).fetchone()
        datasets=market_financial.collect(service.db.path.parent/'market_financial_sources',target,pacer,progress,
                                         resume=bool(previous and previous['status']=='failed'))
        sources=[source for data in datasets for source in data['sources']]
        failures=[]
    else:
        target_date=target_trade_date(service.db,target,directory)
        roster=cap_roster(service.db,foundation,target,target_date)
        stocks=sorted(r['stock_code'] for r in roster['members'])
        known=service.cap_known(target_date)
        required=stocks if recheck else [s for s in stocks if s not in known]
        progress(f'全市场市值 {target} · 已有 {len(stocks)-len(required)} 家，待核对 {len(required)} 家')
        caps=[]
        if required:
            raw,sources=fetch('RPT_VALUEANALYSIS_DET',
                'SECURITY_CODE,TRADE_DATE,TOTAL_MARKET_CAP,TOTAL_SHARES,CLOSE_PRICE',
                f"(TRADE_DATE='{target_date}')")
            source_by_code={r['SECURITY_CODE']:sources[i//500] for i,r in enumerate(raw)}
            caps=cap_rows(raw,target_date,set(required),source_by_code)
        returned={r['stock_code'] for r in caps}
        failures=[{'stock_code':s,'error':'来源缺少指定季末有效总市值，保留原值或待补'} for s in required if s not in returned]
        bundle.update(caps=caps,cap_roster=roster)
        bundle['manifest'].update(cap_quarter=target,market_date=target_date,
                                  composition=roster['composition'])
    progress('来源已完成，正在保存新增／修订指标并汇总所选一期')
    statement_summary={}
    if action=='financial_period':
        with service.db.connection(write=True) as conn:
            statement_summary=market_financial.publish(conn,batch_id,target,datasets)
        market_financial.clear_checkpoints(datasets)
    else:
        bundle['manifest'].update(files=sources,network_requests=pacer.requests)
        import_id=service.import_bundle(bundle,capture_local=False)
    if action=='financial_period':
        data=service._load()
        import_id=data['import']['id']
        effective=service._financial_reader.window(data['token'],data['import']['member_import_id'],
                                                   data['nonfinancial'],target,'ytd')
        failures=[{'stock_code':s,'error':'指定报告期营收或归母净利润待补'} for s in stocks
                  if any(effective.get((s,target),{}).get(m) is None for m in ('revenue','parent_profit'))]
    result={'import_id':import_id,'action':action,'target':target,'scope_count':len(stocks),'membership_check':membership_check,
            'requested_count':len(required),'returned_count':sum(r['SECURITY_CODE'] in universe for r in next(d for d in datasets if d['dataset']=='income')['rows'])
                if action=='financial_period' else len(bundle['caps']),
            'network_requests':pacer.requests,'files':sources,'failures':failures}
    if batch_id is not None:
        result.update(market_financial_batch_id=batch_id,statements=statement_summary,legacy_updated=False)
    if action=='cap_quarter':
        from .industry_prices import sync_quarter
        result['quarter_prices']=sync_quarter(service,foundation,quarter_end(target).isoformat(),
                                              target_date,stocks,recheck,progress)
    (directory/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result
