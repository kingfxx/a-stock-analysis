"""Explicit 2015–2017 composite snapshot backfill, with cache-first publication."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import nullcontext
from datetime import date, datetime
from decimal import Decimal
import json
from pathlib import Path
import re
from uuid import uuid4

from .collection import atomic_write, encoded, digest, begin_batch, finish_batch, archive_source, parsed_source, read_source
from .storage import Database, DEFAULT_DATABASE, utc_now
from .market_trade_snapshot import target_date, normalized, FIELDS, METRICS, publish
from .industry_service import value_for, company_valuation

SPEC=json.loads((Path(__file__).parent/'resources/market_snapshot_composite_definition.json').read_text(encoding='utf-8'))
DEFINITION=SPEC['definition_version']
DATASET='market.trade_snapshot'
ROOT=Path(__file__).resolve().parent.parent
RUN=ROOT/'data/verification/samples/market-trade-snapshot-composite/run-20261014'
REPORT=ROOT/'data/verification/reports/market-trade-snapshot-composite/run-20261014'
QUARTERS=[f'{year}Q{q}' for year in range(2015,2018) for q in range(1,5)]
PENDING=SPEC['pending_fields']

def save(path,value):atomic_write(path,encoded(value).encode())
def load(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def day(value):return value.isoformat()[:10] if hasattr(value,'isoformat') else str(value)[:10]
def records(table):return [{k:day(v) if isinstance(v,(datetime,date)) else v for k,v in r.items()} for r in table.to_pylist()]

def local_inputs(db,dates,overlap,*,conn=None):
    with (nullcontext(conn) if conn is not None else db.connection()) as c:
        caps=list(c.execute("SELECT c.* FROM sw_cap_facts c JOIN sw_imports i ON i.id=c.import_id WHERE i.status='complete' "
            "AND coalesce(json_extract(i.source_manifest_json,'$.cap_status'),'')!='rejected' AND c.trade_date BETWEEN '2015-01-01' AND '2018-03-31' ORDER BY c.import_id"))
        prices=list(c.execute("SELECT * FROM market_quarterly_prices WHERE quarter_end BETWEEN '2015-01-01' AND '2018-03-31' AND adjustment='raw'"))
        descriptors=list(c.execute("SELECT id,security_code,exchange,trade_date,version,source_id,batch_id,security_name_abbr,org_code,trade_market,board_code,board_name,orig_board_code FROM market_trade_snapshot WHERE is_latest=1 AND definition_version='em-valueanalysis-v1' ORDER BY trade_date,id"))
        income=list(c.execute("SELECT id,security_code,exchange,report_date,parent_netprofit,version,source_id,batch_id,notice_date,obtained_at,updated_at FROM market_financial_income WHERE is_latest=1 AND report_date BETWEEN '2014-01-01' AND '2017-12-31'"))
        performance=list(c.execute("SELECT id,security_code,exchange,report_date,bps,version,source_id,batch_id,notice_date,obtained_at,updated_at FROM market_financial_performance WHERE is_latest=1 AND report_date BETWEEN '2014-01-01' AND '2017-12-31'"))
        roster=list(c.execute("SELECT m.quarter,m.stock_code,m.industry_code,r.target_date,r.member_import_id,r.composition FROM sw_cap_quarter_members m JOIN sw_cap_quarter_rosters r ON r.quarter=m.quarter WHERE m.quarter BETWEEN '2015Q1' AND '2017Q4'"))
        overlap_rows=list(c.execute("SELECT * FROM market_trade_snapshot WHERE trade_date=? AND is_latest=1 AND definition_version='em-valueanalysis-v1'",(overlap,)))
        latest=c.execute('SELECT member_import_id FROM sw_imports ORDER BY id DESC LIMIT 1').fetchone()[0]
        members=list(c.execute('SELECT stock_code,listing_date FROM sw_memberships WHERE import_id=?',(latest,)))
    return {k:[dict(r) for r in rows] for k,rows in [('caps',caps),('prices',prices),('descriptors',descriptors),('income',income),('performance',performance),('roster',roster),('overlap',overlap_rows),('members',members)]}

def index_rows(rows,fields):return {tuple(row[k] for k in fields):row for row in rows}

def build(db,folder=RUN):
    import pyarrow as pa
    import pyarrow.parquet as pq
    scope=load(folder/'scope.json');dates=scope['dates'];overlap=scope['overlap']
    metas={label:load(folder/(label+'.json')) for label in ['bars','shares','capital','universe']}
    for label,item in metas.items():
        if digest((folder/(label+'.parquet')).read_bytes())!=item['sha256']:raise ValueError('SDK缓存哈希不符')
        for sid in item['sources']:read_source(db,sid)
    inputs=local_inputs(db,dates,overlap);input_hash=digest(encoded(inputs).encode())
    save(folder/'local-inputs.json',inputs)
    operation='snapshot-composite-build:'+uuid4().hex
    depbatch=begin_batch(db,operation,'market.trade_snapshot.composite_inputs','local',scope=dict(input_hash=input_hash),parser_version='composite-v1',definition_version=DEFINITION)
    dep=archive_source(db,depbatch,'local','market.trade_snapshot.composite_inputs',dict(operation='sqlite-readonly-export',input_hash=input_hash),1,raw=encoded(inputs).encode(),obtained_at=utc_now())
    with db.connection(write=True) as c:c.execute('UPDATE collection_sources SET hash_basis=? WHERE id=?',('sqlite_input_export_json / gzip_file',dep))
    parsed_source(db,dep);finish_batch(db,depbatch,'complete','unknown',result=dict(input_hash=input_hash,source_id=dep))
    bars=index_rows(records(pq.read_table(folder/'bars.parquet')),['instrument','date'])
    shares=index_rows(records(pq.read_table(folder/'shares.parquet')),['instrument','date'])
    events=records(pq.read_table(folder/'capital.parquet'))
    universe=records(pq.read_table(folder/'universe.parquet'))
    caps=index_rows(inputs['caps'],['stock_code','trade_date'])
    prices=index_rows(inputs['prices'],['security_code','trade_date'])
    income=index_rows(inputs['income'],['security_code','exchange','report_date'])
    perf=index_rows(inputs['performance'],['security_code','exchange','report_date'])
    descriptors={};orgs={}
    for r in inputs['descriptors']:
        key=(r['security_code'],r['exchange']);descriptors.setdefault(key,r)
        orgs.setdefault(key,set()).add(r['org_code']) if r['org_code'] else None
    listing={r['stock_code']:r['listing_date'] for r in inputs['members']}
    rejected_total=set();rejected_float=set();overlap_checks=[]
    for r in inputs['overlap']:
        symbol=r['security_code']+'.'+r['exchange'].upper();sh=shares.get((symbol,overlap));b=bars.get((symbol,overlap))
        if not sh:continue
        total_ok=sh['total_shares']==r['total_shares'];float_ok=sh['a_float_shares']==r['free_shares_a']
        if not total_ok:rejected_total.add(symbol)
        if not float_ok:rejected_float.add(symbol)
        overlap_checks.append(dict(instrument=symbol,total_ok=total_ok,float_ok=float_ok,price_ok=b and b['close']==r['close_price']))
    if not overlap_checks or sum(r['float_ok'] for r in overlap_checks)/len(overlap_checks)<.95:
        raise ValueError('流通A股映射未达到扩大样本验证门槛')
    report=dict(definition=DEFINITION,local_input_hash=input_hash,local_source_id=dep,overlap_checks=overlap_checks,
        rejected_total=sorted(rejected_total),rejected_float=sorted(rejected_float),periods=[],conflicts=[],coverage_status='unknown')
    manifest=dict(definition=DEFINITION,dates=dates,input_hash=input_hash,local_source_id=dep,sdk=metas,quarters=[])
    for q,d in dates.items():
        from .industry_updates import quarter_end
        period=quarter_end(q).isoformat();symbols={s for s,t in set(bars)|set(shares) if t==d}
        symbols.update(r['instrument'] for r in universe if r['date']==d)
        symbols.update(code+('.SH' if code.startswith('6') else '.SZ') for code,t in set(caps)|set(prices) if t==d)
        symbols.update(r['stock_code']+('.SH' if r['stock_code'].startswith('6') else '.SZ') for r in inputs['roster'] if r['quarter']==q and listing.get(r['stock_code']) and listing[r['stock_code']]<=d)
        candidates=[];reasons=Counter();old_overlap=0
        for s in sorted(symbols):
            code=s[:6];exchange=s[-2:].lower();b=bars.get((s,d),{});sh=shares.get((s,d),{});old=caps.get((code,d));price=prices.get((code,d))
            descriptor=descriptors.get((code,exchange),{});refs=dict(local_source_id=dep,bar_sources=metas['bars']['sources'],share_sources=metas['shares']['sources'],instrument=s,trade_date=d)
            raw={f:None for f in FIELDS}
            raw.update(SECURITY_CODE=code,SECUCODE=s,TRADE_DATE=d,SECURITY_NAME_ABBR=b.get('name') or descriptor.get('security_name_abbr'))
            if descriptor:
                refs['inherited_snapshot_id']=descriptor['id'];refs['descriptor_inheritance']='later_record_backfill'
                for f in ['ORG_CODE','TRADE_MARKET','BOARD_CODE','BOARD_NAME','ORIG_BOARD_CODE']:
                    raw[f]=descriptor.get(FIELDS[f]['column'])
                if len(orgs.get((code,exchange),set()))>1:raw['ORG_CODE']=None;reasons['org_code_conflict']+=1
            close=price['close'] if price else b.get('close')
            if price:refs['price_source']=dict(table='market_quarterly_prices',source_id=price['source_id'],batch_id=price['batch_id'])
            if price and b.get('close') is not None and abs(price['close']-b['close'])>1e-8:
                report['conflicts'].append(dict(instrument=s,date=d,kind='price',local=price['close'],sdk=b['close']));close=None;reasons['price_conflict']+=1
            total=sh.get('total_shares');floating=sh.get('a_float_shares')
            if total is None:
                eligible=[e for e in events if e['instrument']==s and e['change_date']<=d and e.get('total_shares') is not None]
                last=max((e['change_date'] for e in eligible),default=None);eligible=[e for e in eligible if e['change_date']==last]
                if eligible and len(set(e['total_shares'] for e in eligible))==1:
                    total=eligible[-1]['total_shares'];refs['capital_event_key']={k:eligible[-1][k] for k in ['instrument','change_date','publish_date','reason']};refs['capital_sources']=metas['capital']['sources']
                    # Event float shares are not substituted without their own
                    # historical overlap verification, even when total shares agree.
            if s in rejected_total:total=None;reasons['total_shares_overlap_conflict']+=1
            if s in rejected_float:floating=None;reasons['float_shares_overlap_conflict']+=1
            raw['CLOSE_PRICE']=close;raw['TOTAL_SHARES']=total;raw['FREE_SHARES_A']=floating
            raw['CHANGE_RATE']=b['change_ratio']*100 if b.get('change_ratio') is not None else None
            if b.get('close') is not None and b.get('pre_close') and b.get('change_ratio') is not None:
                if abs(b['close']/b['pre_close']-1-b['change_ratio'])>1e-7:raise ValueError(f'{s} {d} 日收益校验不符')
            calculated=float(Decimal(str(close))*Decimal(str(total))) if close is not None and total is not None else None
            if old and old['total_cap'] is not None and old['total_cap']>0:
                cap=old['total_cap'];refs['legacy_cap']=dict(import_id=old['import_id'],provenance_id=old['provenance_id'])
                if calculated is not None:
                    old_overlap+=1
                    if abs(cap-calculated)>max(1,abs(cap)*1e-6):
                        report['conflicts'].append(dict(instrument=s,date=d,kind='legacy_cap',legacy=cap,calculated=calculated));raw['TOTAL_SHARES']=None;reasons['legacy_cap_conflict']+=1
            else:cap=calculated
            raw['TOTAL_MARKET_CAP']=cap
            raw['NOTLIMITED_MARKETCAP_A']=float(Decimal(str(close))*Decimal(str(floating))) if close is not None and floating is not None else None
            relevant=[period] if period.endswith('12-31') else [period,str(int(period[:4])-1)+'-12-31',str(int(period[:4])-1)+period[4:]]
            facts={(code,p):{'parent_profit':income[(code,exchange,p)]['parent_netprofit']} for p in relevant if (code,exchange,p) in income}
            profit=value_for(facts,code,period,'parent_profit','ttm');performance=perf.get((code,exchange,period))
            values=company_valuation(cap,profit,close,performance.get('bps') if performance else None)
            raw['PE_TTM']=values['pe'];raw['PB_MRQ']=values['pb']
            refs['income_ids']=[income[(code,exchange,p)]['id'] for p in relevant if (code,exchange,p) in income]
            refs['performance_id']=performance['id'] if performance else None
            refs['formula_version']=SPEC['formula_version']
            if cap is None:reasons['missing_market_cap']+=1
            if close is None:reasons['missing_raw_close']+=1
            if profit is None:reasons['missing_ttm_profit_inputs']+=1
            n=normalized(raw,d)
            if any(n[k] is not None for k in PENDING):raise ValueError('待定五字段应保持NULL')
            candidates.append(dict(**n,provenance_json=encoded(refs)))
        path=folder/(q+'.parquet');pq.write_table(pa.Table.from_pylist(candidates),path)
        item=dict(quarter=q,trade_date=d,rows=len(candidates),file=path.name,sha256=digest(path.read_bytes()),
            valid_counts={v['column']:sum(r[v['column']] is not None for r in candidates) for v in FIELDS.values()},
            missing_reasons=dict(reasons),old_cap_overlap=old_overlap,
            universe_count=sum(r['date']==d for r in universe),
            source_only_outside_universe=sorted(symbols-{r['instrument'] for r in universe if r['date']==d}))
        manifest['quarters'].append(item);report['periods'].append(item)
        print(f'{q}: {len(candidates)} 行，市值 {item["valid_counts"]["total_market_cap"]}，PE {item["valid_counts"]["pe_ttm"]}，PB {item["valid_counts"]["pb_mrq"]}',flush=True)
    manifest['created_at']=utc_now();save(folder/'manifest.json',manifest);save(REPORT/'preparation.json',report)
    return manifest

def collect(db,folder=RUN):
    from .bigquant_sdk import BigQuantSDK
    REPORT.mkdir(parents=True,exist_ok=True);folder.mkdir(parents=True,exist_ok=True)
    with db.connection(write=True) as conn:
        if conn.execute("SELECT 1 FROM sw_update_runs WHERE status='running'").fetchone():raise ValueError('已有行业任务运行')
        rid=conn.execute("INSERT INTO sw_update_runs(action,target,recheck,started_at,status) VALUES('snapshot_composite_cache','2015Q1:2017Q4',0,?,'running')",(utc_now(),)).lastrowid
    operation=f'sw_update_runs:{rid}'
    result=dict(operation=operation,status='running',started_at=utc_now())
    try:
        dates={q:target_date(db,q) for q in QUARTERS};overlap=target_date(db,'2018Q1')
        save(folder/'scope.json',dict(dates=dates,overlap=overlap,definition=DEFINITION))
        days=sorted(set(dates.values())|{overlap});literal=','.join(repr(d) for d in days)
        filters={'date':[min(days),max(days)]}
        where=f"date IN ({literal}) AND (instrument LIKE '%.SH' OR instrument LIKE '%.SZ')"
        sdk=BigQuantSDK(db,operation,progress=lambda s:print(s,flush=True))
        with sdk.instrumented() as bq:
            sdk.initialize(bq)
            quota,_=sdk.call(dict(operation='get_data_quota'),lambda:bq.dai.get_data_quota())
            save(REPORT/'quota.json',sdk.sanitized(quota))
            for label,table,cols,numeric in [
                ('bars','cn_stock_real_bar1d','date,instrument,name,close,pre_close,change_ratio',['close','change_ratio']),
                ('shares','cn_stock_shares','date,instrument,total_shares,a_float_shares,free_float_shares,total_float_shares',['total_shares','a_float_shares']),
                ('universe','cn_stock_instruments','date,instrument',[])]:
                frame,item=sdk.query(bq.dai,f'SELECT {cols} FROM {table} WHERE {where} ORDER BY date,instrument',filters,label,folder)
                aggcols=','.join(f'count({c}) AS {c}_count' for c in numeric)
                count,counts=sdk.query(bq.dai,f'SELECT date,count(*) AS rows,count(DISTINCT instrument) AS codes'+(','+aggcols if aggcols else '')+f' FROM {table} WHERE {where} GROUP BY date ORDER BY date',filters,label+'-counts',folder)
                rows=records(frame);expected=records(count);actual=[]
                keys=[(r['instrument'],r['date']) for r in rows]
                if len(set(keys))!=len(keys):raise ValueError(f'{label} 重复键')
                if any(r['date'] not in days or not re.fullmatch(r'\d{6}\.(SH|SZ)',r['instrument']) for r in rows):raise ValueError(f'{label} 返回范围异常')
                for d in sorted(set(r['date'] for r in rows)):
                    group=[r for r in rows if r['date']==d]
                    actual.append(dict(date=d,rows=len(group),codes=len(set(r['instrument'] for r in group)),**{c+'_count':sum(r[c] is not None for r in group) for c in numeric}))
                if actual!=expected:raise ValueError(f'{label} 下载与来源计数不符')
                result[label]=dict(rows=len(rows),counts=actual,sources=item['sources'],count_sources=counts['sources'])
            # Download only gap/overlap-conflict events plus independent sample companies.
            import pyarrow.parquet as pq
            bars=records(pq.read_table(folder/'bars.parquet'));shares=records(pq.read_table(folder/'shares.parquet'))
            sm={(r['instrument'],r['date']):r for r in shares}
            symbols={r['instrument'] for r in bars if (r['instrument'],r['date']) not in sm or sm[(r['instrument'],r['date'])]['total_shares'] is None}
            symbols.update(['000001.SZ','000002.SZ','000725.SZ','600000.SH','600018.SH','600028.SH','600029.SH','600919.SH'])
            with db.connection() as conn:
                for r in conn.execute("SELECT security_code,exchange,total_shares,free_shares_a FROM market_trade_snapshot WHERE trade_date=? AND is_latest=1 AND definition_version='em-valueanalysis-v1'",(overlap,)):
                    s=r['security_code']+'.'+r['exchange'].upper();a=sm.get((s,overlap))
                    if a and (a['total_shares']!=r['total_shares'] or a['a_float_shares']!=r['free_shares_a']):symbols.add(s)
            # Retain all same-day baseline events using MAX, not arbitrary ROW_NUMBER.
            quoted=','.join(repr(s) for s in sorted(symbols))
            sql=f"""WITH selected AS (SELECT * FROM cn_stock_capital WHERE instrument IN ({quoted}) AND change_date <= '{max(days)}'),
baseline AS (SELECT instrument,max(change_date) AS base_date FROM selected WHERE change_date <= '{min(days)}' GROUP BY instrument)
SELECT s.* FROM selected s LEFT JOIN baseline b ON s.instrument=b.instrument
WHERE s.change_date >= '{min(days)}' OR s.change_date=b.base_date ORDER BY s.instrument,s.change_date,s.publish_date"""
            events,item=sdk.query(bq.dai,sql,{'change_date':['1990-01-01',max(days)]},'capital',folder)
            result['capital']=dict(symbols=sorted(symbols),rows=len(events),sources=item['sources'])
        result.update(status='complete',finished_at=utc_now(),request_policy=sdk.collector.report())
    except BaseException as exc:
        result.update(status='failed',error=str(exc),finished_at=utc_now())
        raise
    finally:
        save(REPORT/'collection.json',result)
        with db.connection(write=True) as conn:
            conn.execute('UPDATE sw_update_runs SET status=?,finished_at=?,result_json=?,error=? WHERE id=?',
                (result['status'],result['finished_at'],encoded(result),result.get('error'),rid))
    return result

def candidate_rows(folder,manifest):
    import pyarrow.parquet as pq
    for item in manifest['quarters']:
        path=folder/item['file']
        if digest(path.read_bytes())!=item['sha256']:raise ValueError('候选哈希不符')
        rows=pq.read_table(path).to_pylist()
        if len(rows)!=item['rows']:raise ValueError('候选行数不符')
        for row in rows:
            source={f:row[spec['column']] for f,spec in FIELDS.items()}
            n=normalized(source,item['trade_date'])
            if n['content_hash']!=row['content_hash'] or any(row[k] is not None for k in PENDING):raise ValueError('候选内容或待定字段不符')
        yield item,rows

def protected_fingerprints(db):
    import hashlib
    result={}
    with db.connection() as c:
        tables=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for table in sorted(tables):
            if table in {'collection_batches','collection_sources','collection_provider_state','sw_update_runs','schema_migrations'}:continue
            where=" WHERE definition_version='em-valueanalysis-v1'" if table=='market_trade_snapshot' else ''
            h=hashlib.sha256();count=0
            for row in c.execute(f'SELECT * FROM "{table}"'+where+' ORDER BY rowid'):
                h.update(encoded([{'bytes_hex':v.hex()} if isinstance(v,bytes) else v for v in row]).encode());h.update(b'\n');count+=1
            result[table]=dict(rows=count,sha256=h.hexdigest())
    return result

def publish_cache(db,folder=RUN,*,rehearsal=False):
    manifest=load(folder/'manifest.json')
    if digest(encoded(local_inputs(db,manifest['dates'],load(folder/'scope.json')['overlap'])).encode())!=manifest['input_hash']:
        raise ValueError('本地依赖变化，需重新准备候选')
    for sid in [manifest['local_source_id'],*[sid for m in manifest['sdk'].values() for sid in m['sources']]]:read_source(db,sid)
    prepared=list(candidate_rows(folder,manifest))
    before=protected_fingerprints(db)
    result=dict(status='running',started_at=utc_now(),manifest_hash=digest((folder/'manifest.json').read_bytes()),periods=[])
    if not rehearsal:
        rehearsal_result=load(REPORT/'rehearsal.json')
        if rehearsal_result.get('status')!='complete' or not rehearsal_result.get('idempotency_verified') or rehearsal_result['manifest_hash']!=result['manifest_hash']:raise ValueError('当前候选未通过隔离演练及幂等验证')
        result['backup']=str(db.backup(db.path.parent/'backups'/('before-composite-snapshot-'+utc_now().replace(':','').replace('+','')+'.sqlite3')))
    with db.connection(write=True) as c:
        if c.execute("SELECT 1 FROM sw_update_runs WHERE status='running'").fetchone():raise ValueError('已有行业任务运行')
        rid=c.execute("INSERT INTO sw_update_runs(action,target,recheck,started_at,status) VALUES('snapshot_composite_publish','2015Q1:2017Q4',0,?,'running')",(utc_now(),)).lastrowid
    operation=f'sw_update_runs:{rid}';sources=[]
    try:
        for item,rows in prepared:
            batch=begin_batch(db,operation,DATASET,'composite',item['trade_date'],item['trade_date'],
                scope=dict(quarter=item['quarter'],sampling='quarter_end',candidate_sha256=item['sha256'],
                           input_hash=manifest['input_hash'],market_scope='BigQuant_historical_SH_SZ_plus_explicit_local_rows'),
                frequency='daily',parser_version='historical-composite-v1',definition_version=DEFINITION)
            sid=archive_source(db,batch,'composite',DATASET,dict(operation='compose',quarter=item['quarter'],manifest_hash=result['manifest_hash']),1,
                raw=encoded(dict(definition=SPEC,local_source_id=manifest['local_source_id'],sdk=manifest['sdk'],rows=rows)).encode(),obtained_at=utc_now())
            with db.connection(write=True) as c:c.execute('UPDATE collection_sources SET hash_basis=? WHERE id=?',('computed_snapshot_json / gzip_file',sid))
            parsed_source(db,sid);sources.append((item,rows,batch,sid))
        with db.connection(write=True) as c:
            fresh=local_inputs(db,manifest['dates'],load(folder/'scope.json')['overlap'],conn=c)
            if digest(encoded(fresh).encode())!=manifest['input_hash']:raise ValueError('发布事务前本地依赖变化')
            for item,rows,batch,sid in sources:
                obtained=c.execute('SELECT obtained_at FROM collection_sources WHERE id=?',(sid,)).fetchone()[0]
                values=[({k:v for k,v in row.items() if k!='provenance_json'},sid,obtained) for row in rows]
                stats=publish(c,batch,values,definition=DEFINITION)
                summary=dict(**item,**stats,batch_id=batch,source_id=sid)
                result['periods'].append(summary)
                c.execute("UPDATE collection_batches SET status='complete',coverage_status='partial',finished_at=?,result_json=? WHERE id=?",(utc_now(),encoded(summary),batch))
            result.update(status='complete',finished_at=utc_now())
            c.execute("UPDATE sw_update_runs SET status='complete',finished_at=?,result_json=? WHERE id=?",(result['finished_at'],encoded(result),rid))
        after=protected_fingerprints(db)
        if before!=after:raise ValueError('非目标业务表保护校验失败')
        result['protected_table_count']=len(before);result['protected_fingerprints']=after
        with db.connection() as c:
            result['quick_check']=c.execute('PRAGMA quick_check').fetchone()[0]
            result['foreign_key_check']=[list(r) for r in c.execute('PRAGMA foreign_key_check')]
            if result['quick_check']!='ok' or result['foreign_key_check']:raise ValueError('SQLite完整性或外键失败')
            for item,rows,batch,sid in sources:
                raw,_=read_source(db,sid)
                expected={r['security_code']:r for r in json.loads(raw)['rows']}
                actual={r['security_code']:dict(r) for r in c.execute('SELECT * FROM market_trade_snapshot WHERE trade_date=? AND definition_version=? AND is_latest=1',(item['trade_date'],DEFINITION))}
                if set(expected)!=set(actual) or any(any(actual[k][col]!=row[col] for col in ['content_hash',*[v['column'] for v in FIELDS.values()]]) for k,row in expected.items()):raise ValueError('来源与事实双向对照失败')
        save(REPORT/('rehearsal.json' if rehearsal else 'publication.json'),result)
        print(encoded(dict(status=result['status'],rows=sum(r['rows'] for r in result['periods']),report=str(REPORT))),flush=True)
        return result
    except BaseException as exc:
        result.update(status='failed',error=str(exc),finished_at=utc_now())
        for _,_,batch,_ in sources:
            with db.connection() as c:state=c.execute('SELECT status FROM collection_batches WHERE id=?',(batch,)).fetchone()[0]
            if state=='running':finish_batch(db,batch,'failed',error=exc)
        with db.connection(write=True) as c:c.execute("UPDATE sw_update_runs SET status='failed',finished_at=?,error=? WHERE id=?",(utc_now(),str(exc),rid))
        save(REPORT/('rehearsal.json' if rehearsal else 'publication.json'),result)
        raise

def rehearse(db,folder=RUN):
    import shutil
    import tempfile
    with tempfile.TemporaryDirectory(prefix='sr-',dir=ROOT/'data/verification') as tmp:
        path=Path(tmp)/'rehearsal.sqlite3';db.backup(path)
        manifest=load(folder/'manifest.json')
        ids=[manifest['local_source_id'],*[sid for m in manifest['sdk'].values() for sid in m['sources']]]
        for sid in ids:
            _,source=read_source(db,sid)
            relative=Path(source['file_path']);destination=path.parent/relative
            destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(db.path.parent/relative,destination)
        isolated=Database(path,busy_timeout_ms=60000)
        first=publish_cache(isolated,folder,rehearsal=True)
        second=publish_cache(isolated,folder,rehearsal=True)
        if any(r['inserted'] or r['revised'] for r in second['periods']):raise ValueError('隔离重跑不幂等')
        first['idempotency_verified']=True;save(REPORT/'rehearsal.json',first)
        return first

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['collect','prepare','rehearse','publish'])
    parser.add_argument('--database',type=Path,default=DEFAULT_DATABASE)
    args=parser.parse_args()
    db=Database(args.database,busy_timeout_ms=60000)
    {'collect':collect,'prepare':build,'rehearse':rehearse,'publish':publish_cache}[args.action](db)
