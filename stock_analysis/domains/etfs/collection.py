"""ETF work planning and execution inside the shared collection lifecycle."""

import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from stock_analysis.collection.collection import (
    Cancelled, RequestCollector, encoded, finish_batch, parsed_source, read_source,
)
from stock_analysis.domains.indices.index_price_codec import decode_day
from stock_analysis.infrastructure.storage import instance_lock, utc_now
from . import parsers, sources, storage


TASK_KEY = 'etfs'
DATASETS = {'prices':'历史行情','dividends':'历史分红'}
PARSER_VERSION = 'etf-v1'
ZONE = ZoneInfo('Asia/Shanghai')
LISTINGS = dict(sh510300='2012-05-28',sh512100='2016-11-04',sh512880='2016-08-08',
                sh515450='2020-02-26',sz159352='2024-10-15',sz159857='2021-02-24',sz159938='2015-01-08')
KNOWN_GAP = ('2015-01-08','2015-04-02')
SUSPENSIONS = {'sh512100':{'2022-09-02'}}


def validate_params(params, *, scheduled=False):
    if not isinstance(params,dict) or set(params)-{'mode','codes','datasets','operation_ref','announcement_url'}:
        raise ValueError('ETF 采集参数无效')
    mode = params.get('mode','incremental')
    if mode not in ('incremental','full','resume') or scheduled and mode != 'incremental':
        raise ValueError('ETF 定时只允许增量')
    if mode == 'resume':
        ref = params.get('operation_ref')
        if set(params)-{'mode','operation_ref'} or ref is not None and (
                not isinstance(ref,str) or not re.fullmatch(r'[0-9a-f]{32}',ref)):
            raise ValueError('ETF 续跑使用原任务范围')
        return {'mode':mode,'operation_ref':ref}
    raw_codes = params.get('codes')
    if not isinstance(raw_codes,list) or not 1 <= len(raw_codes) <= 100 or 'operation_ref' in params:
        raise ValueError('请指定 1 至 100 个沪深 ETF 代码')
    codes = []
    for raw in raw_codes:
        match = re.fullmatch(r'(sh|sz)?(5[0-9]{5}|15[0-9]{4})(?:\.(sh|sz))?',raw.strip().lower()) if isinstance(raw,str) else None
        if not match:
            raise ValueError(f'ETF 代码格式无效：{raw}')
        market = 'sh' if match[2][0]=='5' else 'sz'
        if any(part and part!=market for part in (match[1],match[3])):
            raise ValueError(f'ETF 交易所与代码不符：{raw}')
        codes.append(market+match[2])
    datasets = params.get('datasets')
    if not isinstance(datasets,list) or not datasets or any(not isinstance(value,str) or value not in DATASETS for value in datasets):
        raise ValueError('请选择历史行情或历史分红')
    result = dict(mode=mode,codes=sorted(set(codes)),datasets=[key for key in DATASETS if key in datasets])
    if 'announcement_url' in params:
        if scheduled or mode!='full' or len(result['codes'])!=1 or result['datasets']!=['dividends']:
            raise ValueError('正式公告核对请选择一只 ETF、全量复核和历史分红')
        result['announcement_url'],_ = sources.announcement_url(params['announcement_url'])
    return result


def shift_day(value, delta):
    return (date.fromisoformat(value)+timedelta(days=delta)).isoformat()


def latest_close_day(now):
    day = now.date() if now.hour >= 16 else now.date()-timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day.isoformat()


def market_days(conn):
    rows = conn.execute("""
        SELECT DISTINCT p.observation_date FROM index_price_observations p
        JOIN index_price_series s ON p.series_id=s.id
        WHERE s.index_code='cn:000300' AND s.provider='csindex' AND s.series_key='history.points'
        AND s.frequency='daily' AND p.close IS NOT NULL ORDER BY p.observation_date
    """)
    return {decode_day(row[0]) for row in rows}


def cached_market_days(db, conn):
    local = market_days(conn)
    evidence = conn.execute("""
        SELECT id FROM collection_sources WHERE dataset='index.calendar' AND parse_status='complete'
        AND file_path IS NOT NULL ORDER BY obtained_at DESC,id DESC LIMIT 1
    """).fetchone()
    if evidence:
        try:
            raw,_ = read_source(db,evidence[0])
            archived = sources.decode_calendar(raw.decode('utf-8-sig'))
            if len(archived)>=1000:
                return local | archived
        except (ValueError,OSError):
            pass
    return local


def has_result(conn, identity, dataset, kind=None):
    return conn.execute("""
        SELECT 1 FROM collection_batches WHERE domain='etf' AND dataset=? AND code=?
        AND json_extract(scope_json,'$.exchange')=? AND status='complete'
        AND (? IS NULL OR json_extract(scope_json,'$.kind')=?) LIMIT 1
    """, (dataset,identity[2:],identity[:2],kind,kind)).fetchone() is not None


def add_plan(conn, identity, params, target, days, scheduled):
    def work(kind, provider, start=None, end=None, **extra):
        dividend_datasets = {'identity':'identity','dividends':'dividends','official_dividend':'dividends.official',
                             'dividend_audit':'dividends.audit','initialize_dividends':'dividends'}
        dataset = dividend_datasets.get(kind,'prices')
        return (f'etf.{dataset}',provider,start,end,dict(kind=kind,exchange=identity[:2],code=identity[2:],**extra))

    if params.get('announcement_url'):
        url,provider = sources.announcement_url(params['announcement_url'])
        yield work('official_dividend',provider,announcement_url=url)
        return
    official = identity in LISTINGS or conn.execute(
        "SELECT 1 FROM portfolio_snapshot_positions WHERE exchange=? AND code=? AND asset_type='etf' LIMIT 1",
        (identity[:2],identity[2:])).fetchone() or has_result(conn,identity,'etf.identity')
    sina_first, sina_end = storage.span(conn,identity,'sina')
    east_tail = conn.execute("""
        SELECT max(p.trade_date) FROM etf_daily_prices p JOIN collection_batches b ON b.id=p.batch_id
        WHERE p.exchange=? AND p.code=? AND p.source='eastmoney'
        AND json_extract(b.scope_json,'$.kind') IN ('handoff','increment')
    """,(identity[:2],identity[2:])).fetchone()[0]
    if not official and not scheduled:
        yield work('identity','eastmoney')
    review = params['mode']=='full'
    if 'prices' in params['datasets']:
        if not sina_end and scheduled:
            yield work('initialize_prices','eastmoney',end=target)
        elif not sina_end or review:
            initial_end = sina_end or target
            yield work('initial','sina',LISTINGS.get(identity) or sina_first,initial_end)
            if identity=='sz159938':
                yield work('gap_east','eastmoney',*KNOWN_GAP)
                yield work('gap_tencent','tencent',*KNOWN_GAP)
        if review and east_tail:
            start = shift_day(sina_end,1)
            for a,b in windows(start,east_tail):
                yield work('review','eastmoney',a,b)
        if sina_end:
            latest = east_tail or sina_end
            if not east_tail and not has_result(conn,identity,'etf.prices','handoff'):
                anchors = conn.execute("""
                    SELECT DISTINCT trade_date FROM etf_daily_prices
                    WHERE exchange=? AND code=? AND source='sina'
                    ORDER BY trade_date DESC LIMIT 10
                """,(identity[:2],identity[2:])).fetchall()
                dates = sorted(row[0] for row in anchors)
                end = min(target,shift_day(latest,365))
                yield work('handoff','eastmoney',dates[0],end,anchors=dates,publish_after=latest)
                latest = max(latest,end)
            if latest < target and (not days or target>max(days) or any(latest<day<=target for day in days)):
                for a,b in windows(shift_day(latest,1),target):
                    yield work('increment','eastmoney',a,b)
    if 'dividends' in params['datasets']:
        if scheduled and not has_result(conn,identity,'etf.dividends'):
            yield work('initialize_dividends','eastmoney')
        else:
            yield work('dividends','eastmoney')
            if review:
                yield work('dividend_audit','sina')


def windows(start, end):
    while start <= end:
        boundary = min(end,shift_day(start,365))
        yield start,boundary
        start = shift_day(boundary,1)


def reserve(conn, params, *, scheduled=False, now=None):
    if conn.execute("SELECT 1 FROM collection_operations WHERE action IS NULL AND task_key<>'stocks' AND status='running'").fetchone():
        raise ValueError('已有采集任务正在运行，请先等待或停止')
    ref, stamp = uuid4().hex, utc_now()
    now = now or datetime.now(ZONE)
    target = latest_close_day(now)
    if params['mode']=='resume':
        previous = params['operation_ref']
        previous = conn.execute("""
            SELECT operation_id,scope_json FROM collection_operations o WHERE task_key=? AND (? IS NULL OR operation_id=?)
            AND EXISTS(SELECT 1 FROM collection_batches b WHERE b.operation_id=o.operation_id
                       AND (b.status IN ('pending','failed','cancelled') OR (b.status='skipped' AND b.error IS NOT NULL)))
            AND (? IS NOT NULL OR NOT EXISTS (SELECT 1 FROM collection_operations newer WHERE newer.task_key=o.task_key
                 AND json_extract(newer.scope_json,'$.resume_from')=o.operation_id))
            ORDER BY o.id DESC LIMIT 1
        """,(TASK_KEY,previous,previous,previous)).fetchone()
        if previous is None:
            raise ValueError('没有可续跑的 ETF 分段')
        original = json.loads(previous['scope_json'])
        scope = dict(original,resume_from=previous['operation_id'])
        batches = [(row['dataset'],row['source_system'],row['scope_start'],row['scope_end'],json.loads(row['scope_json']))
                   for row in conn.execute("""
                       SELECT * FROM collection_batches WHERE operation_id=?
                       AND (status IN ('pending','failed','cancelled') OR (status='skipped' AND error IS NOT NULL))
                       ORDER BY sequence,id
                   """,(previous['operation_id'],))]
    else:
        scope = dict(codes=params['codes'],datasets=params['datasets'],end=target,scope_count=len(params['codes']))
        days = market_days(conn) if 'prices' in params['datasets'] else set()
        batches = [batch for code in params['codes'] for batch in add_plan(conn,code,params,target,days,scheduled)]
    trigger = 'scheduled' if scheduled else 'manual'
    conn.execute("""
        INSERT INTO collection_operations(operation_id,task_key,mode,trigger_type,scope_json,status,started_at)
        VALUES(?,?,?,?,?,'running',?)
    """,(ref,TASK_KEY,params['mode'],trigger,encoded(scope),stamp))
    conn.executemany("""
        INSERT INTO collection_batches(operation_id,domain,dataset,source_system,scope_start,scope_end,scope_json,
            frequency,trigger_type,status,created_at,parser_version,definition_version,code,work_dataset,sequence,mode)
        VALUES(?,'etf',?,?,?,?,?,?,?,'pending',?,?,?,?,?,?,?)
    """,((ref,dataset,provider,start,end,encoded(item),'daily' if dataset=='etf.prices' else 'event',
           trigger,stamp,PARSER_VERSION,'unadjusted-v1' if dataset=='etf.prices' else 'cash-v1',
           item['code'],dataset,sequence,params['mode'])
          for sequence,(dataset,provider,start,end,item) in enumerate(batches)))
    return ref


def prepare_prices(conn, batch, scope, records, days):
    start,end = batch['scope_start'],batch['scope_end']
    symbol = scope['exchange']+scope['code']
    kind = scope['kind']
    dates = [row['trade_date'] for row in records]
    if len(set(dates))!=len(dates):
        raise ValueError('历史行情存在重复交易日')
    if kind!='initial' and any(value<start or value>end for value in dates):
        raise ValueError('来源返回请求区间外日期')
    prices = {row['trade_date']:row for row in records if (not start or row['trade_date']>=start) and row['trade_date']<=end}
    if not prices:
        raise ValueError('日行情为空，覆盖待核')
    first_known,last_known = (min(days),max(days)) if days else ('9999-12-31','0001-01-01')
    extra = {value for value in prices if first_known<=value<=last_known and value not in days}
    if extra:
        raise ValueError(f'行情出现本地日历未包含的交易日：{min(extra)}')
    expected = {value for value in days if (start or min(prices))<=value<=end} - SUSPENSIONS.get(symbol,set())
    if kind.startswith('gap_'):
        if len(prices)!=56 or min(prices)!=start or max(prices)!=end:
            raise ValueError('159938 早期行情未覆盖已核对的 56 个交易日')
        expected = set(prices)
    if kind=='initial' and symbol=='sz159938':
        expected = {value for value in expected if value>KNOWN_GAP[1]}
    if kind=='initial' and start and min(prices)!=start and not (symbol=='sz159938' and min(prices)=='2015-04-03'):
        raise ValueError('首条日线与已知上市日期不一致')
    missing = expected - prices.keys()
    if missing:
        raise ValueError(f'日行情缺少 {len(missing)} 个已知交易日，首个缺日 {min(missing)}')
    if kind=='handoff':
        previous = storage.prices(conn,symbol,'sina',scope['anchors'][0],scope['anchors'][-1])
        for value in scope['anchors']:
            old,new = previous.get(value),prices.get(value)
            if not old or not new or any(round(old[field],4)!=round(new[field],4) for field in parsers.PRICE_FIELDS[:4]):
                raise ValueError(f'{value} 行情交接价格不一致')
            if any(old[field] is None or new[field] is None for field in ('volume','amount')):
                raise ValueError(f'{value} 成交量单位或成交额未确认')
            if abs(old['volume']-new['volume'])>100 or abs(old['amount']-new['amount'])>max(1,old['amount']*.001):
                raise ValueError(f'{value} 成交量或成交额口径不一致')
        prices = {value:row for value,row in prices.items() if value>scope['publish_after']}
    coverage = 'complete' if kind.startswith('gap_') or (start and first_known<=start<=end<=last_known) else 'unknown'
    return prices,coverage


def audit_dividends(conn, scope, records):
    current = storage.latest(conn,'etf_cash_dividend_events',scope['exchange'],scope['code'],'eastmoney','event_key')
    current.update(storage.official_events(conn,scope))
    observed = {}
    previous = Decimal(0)
    for stamp,value in sorted(records.items()):
        amount = value-previous
        if amount:
            observed[stamp] = amount
        previous = value
    expected = {}
    for row in current.values():
        stamp = row['ex_date']
        expected[stamp] = expected.get(stamp,Decimal(0))+Decimal(row['cash_per_unit'])
    mismatch = [stamp for stamp in sorted(expected.keys() | observed.keys())
                if abs(expected.get(stamp,0)-observed.get(stamp,0))>Decimal('0.000001')]
    return dict(returned=len(records),published=0,mismatch_dates=mismatch,
                message='存在累计金额差异，使用正式公告核对' if mismatch else '累计金额核对一致')


def run(db, ref, *, collector_factory=RequestCollector, get_data=sources.get_data):
    with instance_lock(db.path.parent/'collection_sources'/'index-operation'):
        _run_locked(db,ref,collector_factory,get_data)


def _run_locked(db, ref, collector_factory, get_data):
    def check_cancelled(conn=None):
        if conn is None:
            with db.connection() as reader:
                return check_cancelled(reader)
        return bool(conn.execute('SELECT cancel_requested FROM collection_operations WHERE operation_id=?',(ref,)).fetchone()[0])

    with db.connection(write=True) as conn:
        if not conn.execute("""
            UPDATE collection_operations SET worker_started_at=? WHERE operation_id=? AND task_key=?
            AND status='running' AND worker_started_at IS NULL
        """,(utc_now(),ref,TASK_KEY)).rowcount:
            raise ValueError('ETF 任务不可领取')
        batches = conn.execute('SELECT * FROM collection_batches WHERE operation_id=? ORDER BY sequence,id',(ref,)).fetchall()
    with db.connection() as conn:
        days = cached_market_days(db,conn) if any(batch['dataset']=='etf.prices' for batch in batches) else set()
    collector = collector_factory(db,cancelled=check_cancelled)
    failed_identities, failed_increments = set(),set()
    errors, notices = [],[]
    status = 'complete'
    try:
        for batch in batches:
            collector.check_cancelled()
            scope = json.loads(batch['scope_json'])
            kind = scope['kind']
            symbol = scope['exchange']+scope['code']
            batch_id = batch['id']
            if kind.startswith('initialize_'):
                note = f'{symbol} {DATASETS[kind.removeprefix("initialize_")]}需先手动初始化'
                notices.append(note)
                finish_batch(db,batch_id,'skipped',result=dict(message=note))
                continue
            if symbol in failed_identities or kind=='increment' and symbol in failed_increments:
                finish_batch(db,batch_id,'skipped',error='依赖分段待续跑')
                continue
            if kind=='gap_tencent':
                with db.connection() as conn:
                    filled = storage.prices(conn,symbol,'eastmoney',*KNOWN_GAP)
                if len(filled)==56:
                    finish_batch(db,batch_id,'skipped','complete',result=dict(message='东财已覆盖早期缺口'))
                    continue
            start,end = batch['scope_start'],batch['scope_end']
            if kind=='increment' and days and min(days)<=start<=end<=max(days) and not any(start<=value<=end for value in days):
                finish_batch(db,batch_id,'skipped','complete',result=dict(message='区间无交易日'))
                continue
            with db.connection(write=True) as conn:
                conn.execute("UPDATE collection_batches SET status='running',started_at=? WHERE id=?",(utc_now(),batch_id))
            source_id = None
            try:
                records,source_id = get_data(collector,batch,scope)
                with db.connection(write=True) as conn:
                    if check_cancelled(conn):
                        raise Cancelled('ETF 采集已停止')
                    if kind=='identity':
                        coverage,result,fields = 'complete',dict(name=records),['name']
                    elif kind=='dividend_audit':
                        result = audit_dividends(conn,scope,records)
                        coverage = 'partial' if result['mismatch_dates'] else 'complete'
                        fields = ('ex_date','cumulative_cash')
                    elif kind in ('dividends','official_dividend'):
                        if kind=='official_dividend':
                            written = storage.save_official_dividend(conn,batch,scope,records,source_id)
                            records = [records]
                        else:
                            written = storage.save_dividends(conn,batch,scope,records,source_id)
                        coverage = 'complete' if records else 'empty'
                        result,fields = dict(published=written,row_count=len(records)),parsers.DIVIDEND_FIELDS
                    else:
                        observations,coverage = prepare_prices(conn,batch,scope,records,days)
                        written = storage.save_prices(conn,batch,scope,observations,source_id)
                        result = dict(published=written,returned=len(records),row_count=len(observations),
                            first_date=min(observations,default=None),last_date=max(observations,default=None),
                            field_counts={key:sum(row[key] is not None for row in observations.values()) for key in parsers.PRICE_FIELDS})
                        if kind=='handoff':
                            result['validated_overlap'] = len(scope['anchors'])
                        fields = ('trade_date',*parsers.PRICE_FIELDS)
                    conn.execute("UPDATE collection_sources SET parse_status='complete',fields_json=? WHERE id=?",
                                 (encoded(sorted(fields)),source_id))
                    conn.execute("UPDATE collection_batches SET status='complete',coverage_status=?,result_json=?,finished_at=? WHERE id=?",
                                 (coverage,encoded(result),utc_now(),batch_id))
            except Cancelled as exc:
                finish_batch(db,batch_id,'cancelled',error=exc)
                raise
            except Exception as exc:
                if source_id:
                    parsed_source(db,source_id,error=str(exc))
                errors.append(f'{symbol} {batch["dataset"]}：{exc}')
                finish_batch(db,batch_id,'failed',error=exc)
                if kind=='identity':
                    failed_identities.add(symbol)
                if kind in ('handoff','increment'):
                    failed_increments.add(symbol)
        collector.check_cancelled()
        if errors:
            status = 'failed'
    except Cancelled:
        status = 'cancelled'
    except Exception as exc:
        errors.append(str(exc))
        status = 'failed'
        raise
    finally:
        result = {**collector.report(),'errors':errors[:25],'message':'；'.join(notices) or None}
        error = errors[0] if errors else '用户已请求停止' if status=='cancelled' else None
        with db.connection(write=True) as conn:
            if status=='cancelled':
                conn.execute("UPDATE collection_batches SET status='skipped',finished_at=?,error='用户停止，本轮未请求' WHERE operation_id=? AND status='pending'",(utc_now(),ref))
            conn.execute("UPDATE collection_operations SET status=?,finished_at=?,result_json=?,error=? WHERE operation_id=?",
                         (status,utc_now(),encoded(result),error,ref))
        collector.session.close()
