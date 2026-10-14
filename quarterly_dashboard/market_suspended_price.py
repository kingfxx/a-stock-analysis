"""Validate pre_close for suspended quarter-end valuations before publication."""
from __future__ import annotations

import argparse
from pathlib import Path
import json
from collections import Counter, defaultdict
from decimal import Decimal
from uuid import uuid4

from .storage import Database, DEFAULT_DATABASE, utc_now
from .market_snapshot_backfill import RUN as BASE, save, load, records, local_inputs, index_rows
from .bigquant_sdk import BigQuantSDK
from .collection import read_source, digest, encoded, begin_batch, finish_batch, archive_source, parsed_source
from .market_trade_snapshot import FIELDS, normalized, publish
from .industry_service import value_for, company_valuation

def event_total_at(events, when):
    selected=[e for e in events if e['change_date']<=when]
    latest=max((e['change_date'] for e in selected),default=None)
    values=[e.get('total_shares') for e in selected if e['change_date']==latest]
    if not values or any(v is None for v in values) or len(set(values))!=1:return None
    return values[0]

def validate_reference(anchor,target_shares,events,dividends,suspend):
    """A carried raw price is accepted only after all interval checks pass."""
    start=anchor.get('price_date');end=anchor['target_date'];price=anchor.get('pre_close')
    if not start or start>=end or price is None or not Decimal(str(price)).is_finite() or price<=0:return ['invalid_anchor_or_preclose']
    reasons=[]
    if not suspend:reasons.append('no_same_day_suspension_evidence')
    if anchor.get('anchor_close') is None or abs(price-anchor['anchor_close'])>1e-7:reasons.append('preclose_differs_from_last_raw_close')
    total=target_shares.get('total_shares');prior=anchor.get('anchor_total_shares')
    if total is None or prior is None or total!=prior or total<=0 or not Decimal(str(total)).is_finite():reasons.append('daily_total_shares_changed_or_missing')
    if event_total_at(events,start)!=prior or event_total_at(events,end)!=total:reasons.append('capital_event_daily_mismatch_or_missing')
    interval=[e for e in events if start<e['change_date']<=end]
    if any(e.get('total_shares') is None or e['total_shares']!=prior for e in interval):reasons.append('total_shares_event_during_suspension')
    if any(start<e['ex_date']<=end for e in dividends):reasons.append('ex_dividend_event_during_suspension')
    return reasons

ROOT=Path(__file__).resolve().parent.parent
RUN=ROOT/'data/verification/samples/market-suspended-price/run-20261014'
REPORT=ROOT/'data/verification/reports/market-suspended-price/run-20261014'
DEFINITION='historical-suspended-preclose-v1'

def collect(db):
    RUN.mkdir(parents=True,exist_ok=True);REPORT.mkdir(parents=True,exist_ok=True)
    with db.connection(write=True) as c:
        if c.execute("SELECT 1 FROM sw_update_runs WHERE status='running'").fetchone():raise ValueError('已有行业任务运行')
        rid=c.execute("INSERT INTO sw_update_runs(action,target,recheck,started_at,status) VALUES('suspended_price_cache','2015Q1:2017Q4',0,?,'running')",(utc_now(),)).lastrowid
    sdk=BigQuantSDK(db,f'sw_update_runs:{rid}',progress=lambda s:print(s,flush=True))
    result=dict(status='running',started_at=utc_now(),operation_id=f'sw_update_runs:{rid}')
    try:
        with sdk.instrumented() as bq:
            sdk.initialize(bq)
            for table in ['cn_stock_suspend','cn_stock_capital','cn_stock_dividend']:
                schema_path=RUN/(table+'-schema.json')
                if schema_path.exists():
                    prior=load(schema_path);schema=prior['schema']
                    for sid in prior['sources']:read_source(db,sid)
                else:
                    schema,ids=sdk.call(dict(operation='get_datasource_schema',table=table),lambda t=table:bq.dai.get_datasource_schema(t))
                    save(schema_path,dict(schema=schema,sources=ids))
                print(table+' fields: '+','.join(f['name'] for f in schema['fields']),flush=True)
            import pyarrow.parquet as pq
            bars=records(pq.read_table(BASE/'bars.parquet'))
            missing=[r for r in bars if r['date']<'2018-01-01' and r['close'] is None]
            symbols=sorted(set(r['instrument'] for r in missing));days=sorted(set(r['date'] for r in missing))
            quoted=','.join(repr(s) for s in symbols);dates=','.join(repr(d) for d in days)
            save(RUN/'targets.json',dict(rows=missing,base_bar_hash=digest((BASE/'bars.parquet').read_bytes())))
            sql=f"""WITH targets AS (
SELECT instrument,date AS target_date,pre_close FROM cn_stock_real_bar1d
WHERE date IN ({dates}) AND close IS NULL AND instrument IN ({quoted})),
anchors AS (SELECT t.instrument,t.target_date,t.pre_close,max(b.date) AS price_date
FROM targets t LEFT JOIN cn_stock_real_bar1d b ON b.instrument=t.instrument AND b.date<t.target_date AND b.close IS NOT NULL
GROUP BY t.instrument,t.target_date,t.pre_close)
SELECT a.*,b.close AS anchor_close,s.total_shares AS anchor_total_shares,s.a_float_shares AS anchor_float_a
FROM anchors a LEFT JOIN cn_stock_real_bar1d b ON b.instrument=a.instrument AND b.date=a.price_date
LEFT JOIN cn_stock_shares s ON s.instrument=a.instrument AND s.date=a.price_date ORDER BY a.target_date,a.instrument"""
            anchor_table,meta=sdk.query(bq.dai,sql,{'date':['1990-01-01',max(days)]},'anchors',RUN)
            anchors=records(anchor_table)
            if {(r['instrument'],r['target_date']) for r in anchors}!={(r['instrument'],r['date']) for r in missing} or len(anchors)!=len(missing):raise ValueError('停牌价格锚点返回范围或数量不符')
            for r in anchors:
                original=next(x for x in missing if x['instrument']==r['instrument'] and x['date']==r['target_date'])
                if r['pre_close']!=original['pre_close']:raise ValueError('原始pre_close已修订，需重新核验')
            result['anchors']=dict(rows=len(anchors),sources=meta['sources'])
            first=min(r['price_date'] for r in anchors if r['price_date'])
            selected=f"SELECT * FROM cn_stock_capital WHERE instrument IN ({quoted}) AND change_date <= '{max(days)}'"
            capsql=f"""WITH selected AS ({selected}), baseline AS (
SELECT instrument,max(change_date) AS base_date FROM selected WHERE change_date <= '{first}' GROUP BY instrument)
SELECT s.* FROM selected s LEFT JOIN baseline b ON s.instrument=b.instrument
WHERE s.change_date >= '{first}' OR s.change_date=b.base_date ORDER BY s.instrument,s.change_date,s.publish_date"""
            cap,capmeta=sdk.query(bq.dai,capsql,{'change_date':['1990-01-01',max(days)]},'capital',RUN)
            # Independent aggregate of the same selected event range detects truncation.
            capcount,ccmeta=sdk.query(bq.dai,'SELECT instrument,count(*) AS rows FROM ('+capsql+') events GROUP BY instrument ORDER BY instrument',{'change_date':['1990-01-01',max(days)]},'capital-counts',RUN)
            from collections import Counter
            actual=Counter(r['instrument'] for r in records(cap))
            if dict(actual)!={r['instrument']:r['rows'] for r in records(capcount)}:raise ValueError('股本事件下载与独立计数不符')
            result['capital']=dict(rows=len(cap),symbols=len(actual),sources=capmeta['sources'])
            suspend,smeta=sdk.query(bq.dai,f'SELECT * FROM cn_stock_suspend WHERE date IN ({dates}) AND instrument IN ({quoted}) ORDER BY date,instrument',{'date':[min(days),max(days)]},'suspend',RUN)
            result['suspend']=dict(rows=len(suspend),sources=smeta['sources'])
            suspend_counts,_=sdk.query(bq.dai,f'SELECT date,count(*) AS rows FROM cn_stock_suspend WHERE date IN ({dates}) AND instrument IN ({quoted}) GROUP BY date ORDER BY date',{'date':[min(days),max(days)]},'suspend-counts',RUN)
            if dict(Counter(r['date'] for r in records(suspend)))!={r['date']:r['rows'] for r in records(suspend_counts)}:raise ValueError('停牌记录与独立计数不符')
            divwhere=f"instrument IN ({quoted}) AND ex_date BETWEEN '{first}' AND '{max(days)}'"
            div,dmeta=sdk.query(bq.dai,f'SELECT * FROM cn_stock_dividend WHERE {divwhere} ORDER BY instrument,ex_date,publish_date',{'ex_date':[first,max(days)]},'dividend',RUN)
            div_counts,_=sdk.query(bq.dai,f'SELECT instrument,count(*) AS rows FROM cn_stock_dividend WHERE {divwhere} GROUP BY instrument ORDER BY instrument',{'ex_date':[first,max(days)]},'dividend-counts',RUN)
            if dict(Counter(r['instrument'] for r in records(div)))!={r['instrument']:r['rows'] for r in records(div_counts)}:raise ValueError('除权事件与独立计数不符')
            result['dividend']=dict(rows=len(div),sources=dmeta['sources'])
        result.update(status='complete',finished_at=utc_now())
    except BaseException as exc:
        result.update(status='failed',error=str(exc),finished_at=utc_now());raise
    finally:
        save(REPORT/'collection.json',result)
        with db.connection(write=True) as c:c.execute('UPDATE sw_update_runs SET status=?,finished_at=?,result_json=?,error=? WHERE id=?',
            (result['status'],result['finished_at'],json.dumps(result,ensure_ascii=False),result.get('error'),rid))

def base_rows(db,*,conn=None):
    from contextlib import nullcontext
    with (nullcontext(conn) if conn is not None else db.connection()) as c:
        return [dict(r) for r in c.execute("SELECT * FROM market_trade_snapshot WHERE definition_version='historical-composite-v1' AND is_latest=1 AND close_price IS NULL AND trade_date BETWEEN '2015-01-01' AND '2017-12-31' ORDER BY trade_date,security_code")]

def prepare(db):
    import pyarrow as pa
    import pyarrow.parquet as pq
    labels=['anchors','capital','capital-counts','suspend','suspend-counts','dividend','dividend-counts']
    meta={label:load(RUN/(label+'.json')) for label in labels}
    for label,item in meta.items():
        if digest((RUN/(label+'.parquet')).read_bytes())!=item['sha256']:raise ValueError('停牌验证缓存哈希不符')
        for sid in item['sources']:read_source(db,sid)
    base_sdk={label:load(BASE/(label+'.json')) for label in ['bars','shares']}
    for label,item in base_sdk.items():
        if digest((BASE/(label+'.parquet')).read_bytes())!=item['sha256']:raise ValueError('基础行情/股本缓存哈希不符')
        for sid in item['sources']:read_source(db,sid)
    anchors=records(pq.read_table(RUN/'anchors.parquet'));shares=index_rows(records(pq.read_table(BASE/'shares.parquet')),['instrument','date'])
    events=defaultdict(list);dividends=defaultdict(list);suspension=defaultdict(list)
    for e in records(pq.read_table(RUN/'capital.parquet')):events[e['instrument']].append(e)
    for e in records(pq.read_table(RUN/'dividend.parquet')):
        if e.get('ex_date'):dividends[e['instrument']].append(e)
    for e in records(pq.read_table(RUN/'suspend.parquet')):suspension[(e['instrument'],e['date'])].append(e)
    originals=base_rows(db);lookup={(r['secucode'],r['trade_date']):r for r in originals}
    scope=load(BASE/'scope.json');inputs=local_inputs(db,scope['dates'],scope['overlap'])
    income=index_rows(inputs['income'],['security_code','exchange','report_date']);perf=index_rows(inputs['performance'],['security_code','exchange','report_date'])
    report=dict(definition=DEFINITION,targets=len(anchors),eligible=0,blocked=0,reason_counts={},checks=[],periods=[])
    candidates=defaultdict(list);reason_counts=Counter()
    for a in anchors:
        s=a['instrument'];d=a['target_date'];old=lookup.get((s,d))
        if not old:raise ValueError('待补快照状态已变化')
        sh=shares.get((s,d),{});reasons=validate_reference(a,sh,events[s],dividends[s],suspension[(s,d)])
        report['checks'].append(dict(instrument=s,date=d,anchor_date=a['price_date'],pre_close=a['pre_close'],anchor_close=a['anchor_close'],reasons=reasons,
            events_in_interval=sum(a['price_date']<e['change_date']<=d for e in events[s]) if a['price_date'] else 0))
        if reasons:reason_counts.update(reasons);report['blocked']+=1;continue
        raw={f:old[spec['column']] for f,spec in FIELDS.items()};raw['CLOSE_PRICE']=a['pre_close']
        # Date-specific daily/event agreement supersedes the old blanket 2018
        # overlap quarantine for these independently verified suspended records.
        raw['TOTAL_SHARES']=sh['total_shares']
        raw['TOTAL_MARKET_CAP']=float(Decimal(str(a['pre_close']))*Decimal(str(sh['total_shares'])))
        if old['free_shares_a'] is not None:raw['NOTLIMITED_MARKETCAP_A']=float(Decimal(str(a['pre_close']))*Decimal(str(old['free_shares_a'])))
        code=old['security_code'];exchange=old['exchange']
        from .industry_updates import quarter_end
        q=f'{d[:4]}Q{(int(d[5:7])-1)//3+1}';period=quarter_end(q).isoformat()
        relevant=[period] if period.endswith('12-31') else [period,str(int(period[:4])-1)+'-12-31',str(int(period[:4])-1)+period[4:]]
        facts={(code,p):{'parent_profit':income[(code,exchange,p)]['parent_netprofit']} for p in relevant if (code,exchange,p) in income}
        performance=perf.get((code,exchange,period));profit=value_for(facts,code,period,'parent_profit','ttm')
        valuation=company_valuation(raw['TOTAL_MARKET_CAP'],profit,a['pre_close'],performance.get('bps') if performance else None)
        raw['PE_TTM']=valuation['pe'];raw['PB_MRQ']=valuation['pb']
        n=normalized(raw,d)
        provenance=dict(base_snapshot_id=old['id'],base_source_id=old['source_id'],price_method='suspended_pre_close',price_date=a['price_date'],
            raw_close=None,reference_pre_close=a['pre_close'],anchor_close=a['anchor_close'],original_quarter_trade_date=d,
            capital_verification='daily_anchor_target_equal; all_interval_events_equal; event_daily_baseline_equal',
            total_shares_restored=old['total_shares'] is None,
            source_refs={label:item['sources'] for label,item in meta.items()},income_ids=[income[(code,exchange,p)]['id'] for p in relevant if (code,exchange,p) in income],performance_id=performance['id'] if performance else None)
        candidates[q].append(dict(**n,provenance_json=encoded(provenance)));report['eligible']+=1
    manifest=dict(definition=DEFINITION,base_hash=digest(encoded(originals).encode()),local_hash=digest(encoded(inputs).encode()),scope=scope,
        sdk=meta,base_sdk=base_sdk,quarters=[],created_at=utc_now())
    base_manifest=load(BASE/'manifest.json')
    if base_manifest['input_hash']!=manifest['local_hash']:raise ValueError('财务或旧输入已修订，需先重新归档该输入')
    manifest['financial_input_source_id']=base_manifest['local_source_id']
    for q,rows in sorted(candidates.items()):
        path=RUN/(q+'.parquet');pq.write_table(pa.Table.from_pylist(rows),path)
        item=dict(quarter=q,trade_date=rows[0]['trade_date'],file=path.name,rows=len(rows),sha256=digest(path.read_bytes()),valid_counts={spec['column']:sum(r[spec['column']] is not None for r in rows) for spec in FIELDS.values()})
        manifest['quarters'].append(item);report['periods'].append(item)
    report['reason_counts']=dict(reason_counts)
    save(RUN/'manifest.json',manifest);save(REPORT/'preparation.json',report)
    print(encoded(dict(eligible=report['eligible'],blocked=report['blocked'],reasons=report['reason_counts'])),flush=True)
    return manifest

def dependency_check(db,manifest,*,conn=None):
    if digest(encoded(base_rows(db,conn=conn)).encode())!=manifest['base_hash']:raise ValueError('原空价快照已变化，重新准备')
    scope=manifest['scope']
    if digest(encoded(local_inputs(db,scope['dates'],scope['overlap'],conn=conn)).encode())!=manifest['local_hash']:raise ValueError('财务或本地依赖已变化')

def original_series_hash(db):
    with db.connection() as c:
        return digest(encoded([dict(r) for r in c.execute("SELECT * FROM market_trade_snapshot WHERE definition_version='historical-composite-v1' ORDER BY id")]).encode())

def publish_reference(db,*,rehearsal=False):
    import pyarrow.parquet as pq
    from .market_snapshot_backfill import protected_fingerprints
    manifest=load(RUN/'manifest.json');dependency_check(db,manifest)
    for label,item in manifest['base_sdk'].items():
        if digest((BASE/(label+'.parquet')).read_bytes())!=item['sha256']:raise ValueError('基础行情/股本缓存已变化')
    deps=[manifest['financial_input_source_id'],*[sid for meta in [*manifest['sdk'].values(),*manifest['base_sdk'].values()] for sid in meta['sources']]]
    for sid in deps:read_source(db,sid)
    prepared=[]
    for item in manifest['quarters']:
        path=RUN/item['file']
        if digest(path.read_bytes())!=item['sha256']:raise ValueError('停牌候选哈希不符')
        rows=pq.read_table(path).to_pylist()
        if len(rows)!=item['rows']:raise ValueError('停牌候选行数不符')
        for row in rows:
            n=normalized({f:row[spec['column']] for f,spec in FIELDS.items()},item['trade_date'])
            if n['content_hash']!=row['content_hash']:raise ValueError('停牌候选内容不符')
        prepared.append((item,rows))
    before=protected_fingerprints(db);original_hash=original_series_hash(db)
    result=dict(status='running',started_at=utc_now(),manifest_hash=digest((RUN/'manifest.json').read_bytes()),periods=[])
    if not rehearsal:
        proof=load(REPORT/'rehearsal.json')
        if proof.get('status')!='complete' or not proof.get('idempotency_verified') or proof['manifest_hash']!=result['manifest_hash']:raise ValueError('当前停牌候选未通过演练')
        result['backup']=str(db.backup(db.path.parent/'backups'/('before-suspended-price-'+utc_now().replace(':','').replace('+','')+'.sqlite3')))
    with db.connection(write=True) as c:
        if c.execute("SELECT 1 FROM sw_update_runs WHERE status='running'").fetchone():raise ValueError('已有行业任务运行')
        rid=c.execute("INSERT INTO sw_update_runs(action,target,recheck,started_at,status) VALUES('suspended_price_publish','2015Q1:2017Q4',0,?,'running')",(utc_now(),)).lastrowid
    sources=[]
    try:
        for item,rows in prepared:
            batch=begin_batch(db,f'sw_update_runs:{rid}','market.trade_snapshot','composite',item['trade_date'],item['trade_date'],
                scope=dict(quarter=item['quarter'],sampling='quarter_end',definition=DEFINITION,candidate_hash=item['sha256']),frequency='daily',
                parser_version='suspended-reference-v1',definition_version=DEFINITION)
            evidence=dict(definition=DEFINITION,rule='same-day suspension; preclose=last raw close; daily/event total shares unchanged; no interval ex-dividend',
                manifest_hash=result['manifest_hash'],dependencies=deps,rows=rows)
            sid=archive_source(db,batch,'composite','market.trade_snapshot',dict(operation='suspended_preclose',quarter=item['quarter']),1,
                raw=encoded(evidence).encode(),obtained_at=utc_now())
            with db.connection(write=True) as c:c.execute('UPDATE collection_sources SET hash_basis=? WHERE id=?',('computed_suspended_reference_json / gzip_file',sid))
            parsed_source(db,sid);sources.append((item,rows,batch,sid))
        with db.connection(write=True) as c:
            dependency_check(db,manifest,conn=c)
            for item,rows,batch,sid in sources:
                obtained=c.execute('SELECT obtained_at FROM collection_sources WHERE id=?',(sid,)).fetchone()[0]
                stats=publish(c,batch,[({k:v for k,v in row.items() if k!='provenance_json'},sid,obtained) for row in rows],definition=DEFINITION)
                price_dates={row['security_code']:json.loads(row['provenance_json'])['price_date'] for row in rows}
                summary=dict(**item,**stats,batch_id=batch,source_id=sid,price_method='suspended_pre_close',price_dates=price_dates)
                result['periods'].append(summary)
                c.execute("UPDATE collection_batches SET status='complete',coverage_status='partial',finished_at=?,result_json=? WHERE id=?",(utc_now(),encoded(summary),batch))
            result.update(status='aggregating')
            c.execute("UPDATE sw_update_runs SET result_json=? WHERE id=?",(encoded(result),rid))
        from .industry_cap_summary import aggregate_quarter
        for item,_,_,_ in sources:
            aggregation=aggregate_quarter(db,item['quarter'],rid,f'sw_update_runs:{rid}')
            result.setdefault('aggregation_results',{})[item['quarter']]=aggregation
        result.update(status='complete',finished_at=utc_now())
        with db.connection(write=True) as c:
            c.execute("UPDATE sw_update_runs SET status='complete',finished_at=?,result_json=? WHERE id=?",
                      (result['finished_at'],encoded(result),rid))
        if protected_fingerprints(db)!=before or original_series_hash(db)!=original_hash:raise ValueError('原有业务或原合成系列保护核对失败')
        result['protected_tables']=len(before);result['original_series_unchanged']=True
        with db.connection() as c:
            result['quick_check']=c.execute('PRAGMA quick_check').fetchone()[0]
            result['foreign_keys']=[list(r) for r in c.execute('PRAGMA foreign_key_check')]
            if result['quick_check']!='ok' or result['foreign_keys']:raise ValueError('完整性或外键失败')
            for item,rows,batch,sid in sources:
                raw,_=read_source(db,sid);expected={r['security_code']:r for r in json.loads(raw)['rows']}
                actual={r['security_code']:dict(r) for r in c.execute('SELECT * FROM market_trade_snapshot WHERE definition_version=? AND trade_date=? AND is_latest=1',(DEFINITION,item['trade_date']))}
                if set(expected)!=set(actual) or any(any(actual[k][col]!=r[col] for col in ['content_hash',*[spec['column'] for spec in FIELDS.values()]]) for k,r in expected.items()):raise ValueError('停牌来源事实回放不一致')
        save(REPORT/('rehearsal.json' if rehearsal else 'publication.json'),result)
        print(encoded(dict(status='complete',rows=sum(r['rows'] for r in result['periods']))),flush=True)
        return result
    except BaseException as exc:
        result.update(status='failed',error=str(exc),finished_at=utc_now())
        for _,_,batch,_ in sources:
            with db.connection() as c:status=c.execute('SELECT status FROM collection_batches WHERE id=?',(batch,)).fetchone()[0]
            if status=='running':finish_batch(db,batch,'failed',error=exc)
        with db.connection(write=True) as c:c.execute("UPDATE sw_update_runs SET status='failed',finished_at=?,error=? WHERE id=?",(utc_now(),str(exc),rid))
        save(REPORT/('rehearsal.json' if rehearsal else 'publication.json'),result);raise

def rehearse(db):
    import shutil,tempfile
    manifest=load(RUN/'manifest.json')
    ids=[manifest['financial_input_source_id'],*[sid for meta in [*manifest['sdk'].values(),*manifest['base_sdk'].values()] for sid in meta['sources']]]
    with tempfile.TemporaryDirectory(prefix='sp-',dir=ROOT/'data/verification') as tmp:
        path=Path(tmp)/'isolated.sqlite3';db.backup(path)
        for sid in ids:
            _,meta=read_source(db,sid);relative=Path(meta['file_path']);target=path.parent/relative;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(db.path.parent/relative,target)
        isolated=Database(path,busy_timeout_ms=60000);isolated.initialize()
        first=publish_reference(isolated,rehearsal=True);second=publish_reference(isolated,rehearsal=True)
        if any(r['inserted'] or r['revised'] for r in second['periods']):raise ValueError('停牌发布重跑不幂等')
        first['idempotency_verified']=True;save(REPORT/'rehearsal.json',first)
        return first

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['collect','prepare','rehearse','publish'])
    parser.add_argument('--database',type=Path,default=DEFAULT_DATABASE)
    args=parser.parse_args();{'collect':collect,'prepare':prepare,'rehearse':rehearse,'publish':publish_reference}[args.action](Database(args.database,busy_timeout_ms=60000))
