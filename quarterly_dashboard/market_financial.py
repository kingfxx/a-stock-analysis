"""Full-field Eastmoney summary statements, isolated from individual Sina reports."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from uuid import uuid4

from .industry_sources import EM

FIELDS = json.loads((Path(__file__).parent/'resources/market_financial_fields.json').read_text(encoding='utf-8'))
DESCRIPTIVE = {'SECUCODE', 'SECURITY_CODE', 'INDUSTRY_CODE', 'ORG_CODE', 'SECURITY_NAME_ABBR',
               'INDUSTRY_NAME', 'MARKET', 'SECURITY_TYPE_CODE', 'TRADE_MARKET_CODE',
               'TRADE_MARKET', 'SECURITY_TYPE', 'UPDATE_DATE', 'EITIME', 'PUBLISHNAME',
               'TRADE_MARKET_ZJG', 'ISNEW', 'BOARD_NAME', 'ORI_BOARD_CODE', 'BOARD_CODE'}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':'))


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def atomic_write(path, raw):
    """Write a report/checkpoint atomically; network collection is handled elsewhere."""
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+uuid4().hex+'.tmp')
    try:
        with temporary.open('wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)


def normalized(dataset, row, period):
    spec = FIELDS[dataset]
    unknown = set(row)-set(spec['fields'])
    if unknown:
        raise ValueError(f'{dataset} 出现未登记字段 {sorted(unknown)}；原始响应已保留，请更新字段映射')
    code, secu = row.get('SECURITY_CODE'), row.get('SECUCODE')
    if not isinstance(code, str) or not re.fullmatch(r'\d{6}', code):
        raise ValueError('证券代码无效')
    if secu not in {code+'.SH', code+'.SZ', code+'.BJ'}:
        raise ValueError('证券代码与交易所标识不一致')
    if row.get('SECURITY_TYPE_CODE') != '058001001':
        raise ValueError('来源返回非 A 股记录')
    date_field=spec.get('date_field','REPORT_DATE')
    if not isinstance(row.get(date_field), str) or row[date_field][:10] != period:
        raise ValueError('来源报告期不匹配')
    values = {}
    for field, value in row.items():
        item = spec['fields'][field]
        if item['type'] == 'REAL':
            if value is None or value == '':
                value = None
            else:
                if isinstance(value, bool):
                    raise ValueError(f'{dataset}.{field} 数值无效')
                try:
                    value = float(value)
                except (ValueError, TypeError):
                    raise ValueError(f'{dataset}.{field} 数值无效') from None
                if not math.isfinite(value):
                    raise ValueError(f'{dataset}.{field} 数值无效')
        elif value is not None:
            if isinstance(value, (dict, list, bool)):
                raise ValueError(f'{dataset}.{field} 文本无效')
            value = str(value)
        values[item['column']] = value
    return values, secu[-2:].lower()


def begin_batch(db, period):
    stamp = now()
    with db.connection(write=True) as conn:
        return conn.execute("INSERT INTO market_financial_batches(report_date,started_at,status,obtained_at) "
                            "VALUES(?,?,'running',?)", (period, stamp, stamp)).lastrowid


def fail_batch(db, batch_id, error):
    with db.connection(write=True) as conn:
        conn.execute("UPDATE market_financial_batches SET status='failed',finished_at=?,error=? "
                     "WHERE id=? AND status='running'", (now(), str(error), batch_id))


def collect_dataset(session, root, dataset, period, pacer, progress, *, checkpoint=True, resume=True):
    """Retired legacy raw requester; all new financial collection uses collect_public."""
    raise ValueError('旧 collect_dataset 原始网络入口已停用；请使用 market_financial.collect_public 经公共 RequestCollector 采集。')


def _financial_page_request(spec, period, page):
    date_field=spec.get('date_field','REPORT_DATE')
    return {'transport':'http','method':'GET','url':EM,'params':{
        'reportName':spec['report'],'columns':'ALL',
        'filter':f'({date_field}=\'{period}\')(SECURITY_TYPE_CODE="058001001")',
        'sortColumns':'SECURITY_CODE','sortTypes':'1','pageSize':500,'pageNumber':page,
        'source':'WEB','client':'WEB'}}


def _cached_financial_pages(db, dataset, period, scope, spec, same_day):
    """Return one complete, hash-verified page set from one same-day batch, or no cache."""
    from .collection import read_source, digest as source_digest
    public_dataset='market.financial.'+dataset
    first_request=_financial_page_request(spec,period,1)
    first_key=source_digest(encoded(first_request).encode())
    with db.connection() as conn:
        first_pages=[dict(row) for row in conn.execute(
            "SELECT s.* FROM collection_sources s JOIN collection_batches b ON b.id=s.batch_id "
            "WHERE s.source_system='eastmoney' AND s.dataset=? AND s.request_key=? "
            "AND s.request_status IN ('success','reused') AND s.parse_status='complete' "
            "AND substr(s.obtained_at,1,10)=? AND b.domain='market' AND b.source_system='eastmoney' "
            "AND b.scope_start=? AND b.scope_end=? AND b.scope_json=? "
            "AND b.parser_version='market-financial-v1' AND b.definition_version=? "
            "AND b.universe_version='eastmoney-a-share-v1' ORDER BY s.id DESC",
            (public_dataset,first_key,same_day,period,period,encoded(scope),spec['definition_version']))]
    date_field=spec.get('date_field','REPORT_DATE')
    # The newest page-one observation is authoritative for cache selection. If
    # its batch is incomplete/corrupt, older same-day snapshots must not mask it.
    for first in first_pages[:1]:
        try:
            with db.connection() as conn:
                records=[dict(row) for row in conn.execute(
                    "SELECT * FROM collection_sources WHERE batch_id=? AND dataset=? AND source_system='eastmoney' "
                    "AND request_status IN ('success','reused') AND parse_status='complete' "
                    "AND substr(obtained_at,1,10)=? ORDER BY id DESC",
                    (first['batch_id'],public_dataset,same_day))]
            by_page={}
            for item in records:
                request=json.loads(item['request_json'])
                page=request.get('params',{}).get('pageNumber')
                if type(page) is int and request==_financial_page_request(spec,period,page):
                    by_page.setdefault(page,item)
            if by_page.get(1,{}).get('id')!=first['id']:
                continue
            first_raw,_=read_source(db,first['id'])
            payload=json.loads(first_raw)
            if payload.get('code')==9201 and payload.get('message')=='返回数据为空':
                expected={1};count=0;pages=0
            else:
                result=payload.get('result') or {}
                count,pages=result.get('count'),result.get('pages')
                if (payload.get('success') is not True or type(count) is not int or not 0<count<=20000
                        or type(pages) is not int or pages!=math.ceil(count/500)):
                    continue
                expected=set(range(1,pages+1))
            if set(by_page)!=expected:
                continue
            seen=set()
            for page in sorted(expected):
                raw,_=read_source(db,by_page[page]['id'])
                cached=json.loads(raw)
                if page==1 and count==0:
                    if cached.get('code')!=9201 or cached.get('message')!='返回数据为空':
                        raise ValueError('空结果归档变化')
                    continue
                result=cached.get('result') or {}
                rows=result.get('data')
                if (cached.get('success') is not True or result.get('count')!=count or result.get('pages')!=pages
                        or not isinstance(rows,list) or len(rows)!=min(500,count-(page-1)*500)):
                    raise ValueError('归档分页口径或行数变化')
                for row in rows:
                    if not isinstance(row,dict) or not isinstance(row.get(date_field),str) or row[date_field][:10]!=period:
                        raise ValueError('归档报告期不匹配')
                    normalized(dataset,row,period)
                    if set(row)!=set(spec['fields']) or row['SECURITY_CODE'] in seen:
                        raise ValueError('归档字段不完整或证券重复')
                    seen.add(row['SECURITY_CODE'])
            return {page:by_page[page]['id'] for page in expected}
        except (OSError,ValueError,TypeError,KeyError,json.JSONDecodeError):
            continue
    return {}


def _collect_public_dataset(db, operation_id, period, dataset, spec, request_collector, progress,
                            *, recheck=False, progress_state=None, industry_profile=None):
    from .collection import (begin_batch as begin_public_batch, parsed_source, read_source,
                             finish_batch, reusable_source)
    public_dataset='market.financial.'+dataset
    scope={'period':period,'report_name':spec['report'],'columns':'ALL','page_size':500,
           'security_type_code':'058001001','date_field':spec.get('date_field','REPORT_DATE'),
           'definition_version':spec['definition_version']}
    batch_id=begin_public_batch(db,operation_id,public_dataset,'eastmoney',period,period,scope=scope,
        frequency='quarterly',trigger='manual',domain='market',parser_version='market-financial-v1',
        definition_version=spec['definition_version'],universe_version='eastmoney-a-share-v1')
    pages,count,page=1,None,1
    rows=[];sources=[]
    same_day=datetime.fromtimestamp(request_collector.clock(),timezone.utc).date().isoformat()
    cached_pages={} if recheck else _cached_financial_pages(db,dataset,period,scope,spec,same_day)
    def update(state, message=None):
        if progress_state:
            progress_state(dataset, state)
        if message:
            progress(message)
    update({'status':'cached' if cached_pages else 'downloading','page':0,'pages':None,'records':0,'total':None})
    try:
        while page<=pages:
            date_field=spec.get('date_field','REPORT_DATE')
            params={'reportName':spec['report'],'columns':'ALL',
                'filter':f'({date_field}=\'{period}\')(SECURITY_TYPE_CODE="058001001")',
                'sortColumns':'SECURITY_CODE','sortTypes':'1','pageSize':500,'pageNumber':page,
                'source':'WEB','client':'WEB'}
            def healthy(response):
                try:
                    payload=response.json()
                    return (payload.get('code')==9201 and payload.get('message')=='返回数据为空') or (
                        payload.get('success') is True and isinstance((payload.get('result') or {}).get('data'),list))
                except (ValueError,AttributeError):
                    return False
            source_id=reusable_source(db,batch_id,cached_pages[page]) if page in cached_pages else None
            if source_id is None:
                profile_kwargs={'industry_profile':industry_profile} if industry_profile else {}
                response,source_id=request_collector.get(batch_id,'eastmoney',public_dataset,EM,params,
                    headers={'User-Agent':'Mozilla/5.0','Referer':'https://data.eastmoney.com/'},
                    expect='json',healthy_response=healthy,**profile_kwargs)
            raw,source_record=read_source(db,source_id)
            source=dict(page_number=page,collection_source_id=source_id,obtained_at=source_record['obtained_at'],
                raw_bytes=source_record['raw_bytes'] or 0,stored_bytes=source_record['stored_bytes'] or 0,
                params=params,url=EM)
            try:
                payload=json.loads(raw)
                if payload.get('code')==9201 and payload.get('message')=='返回数据为空' and page==1:
                    parsed_source(db,source_id,fields=[])
                    update({'status':'validated_empty','page':0,'pages':0,'records':0,'total':0},
                           f'{spec["label"]} {period} · 来源明确返回空')
                    sources.append(source)
                    pages=0
                    break
                result=payload.get('result') or {}
                if not payload.get('success') or not isinstance(result.get('data'),list):
                    raise ValueError('分页响应无效')
                if count is None:
                    count,pages=result.get('count'),result.get('pages')
                    if type(count) is not int or not 0<count<=20000 or pages!=math.ceil(count/500):
                        raise ValueError('范围或分页数量异常')
                    update({'status':'cached' if cached_pages else 'downloading','page':page-1,
                            'pages':pages,'records':len(rows),'total':count})
                if result.get('count')!=count or result.get('pages')!=pages:
                    raise ValueError('分页数量变化，整段未发布')
                if len(result['data'])!=min(500,count-(page-1)*500):
                    raise ValueError('来源分页截断，整段未发布')
                for row in result['data']:
                    if not isinstance(row,dict) or not isinstance(row.get(date_field),str) or row[date_field][:10]!=period:
                        raise ValueError('来源报告期不匹配')
                    normalized(dataset,row,period)
                    if set(row)!=set(spec['fields']):
                        raise ValueError('接口字段与 ALL 映射不一致')
                parsed_source(db,source_id,fields=sorted(result['data'][0]) if result['data'] else [])
                rows.extend(result['data']);sources.append(source)
                update({'status':'cached' if page in cached_pages else 'downloading','page':page,
                        'pages':pages,'records':len(rows),'total':count},
                       f'{spec["label"]} {page}/{pages} 页 · 已获取 {len(rows)}/{count} 家')
                page+=1
            except Exception as exc:
                parsed_source(db,source_id,error=type(exc).__name__+': '+str(exc))
                raise
        if len({r['SECURITY_CODE'] for r in rows})!=len(rows):
            raise ValueError('分页证券代码重复，整段未发布')
        update({'status':'validated_empty' if pages==0 else 'validated','page':pages,'pages':pages,
                'records':len(rows),'cached_pages':len(cached_pages),
                'total':count if count is not None else 0})
        return dict(dataset=dataset,rows=rows,sources=sources,collection_batch_id=batch_id)
    except BaseException as exc:
        update({'status':'failed','page':page-1,'pages':pages if count is not None else None,
                'records':len(rows),'total':count,'error':'该接口校验失败，四表未发布'})
        finish_batch(db,batch_id,'failed',error=type(exc).__name__+': '+str(exc))
        raise


def collect_public(db, operation_id, period, request_collector, progress=lambda message:None, *, recheck=False,
                   parallel_interfaces=False, progress_state=None):
    """Collect four validated datasets; the caller publishes all four in one transaction.

    The explicitly enabled daily industry path uses two independent HTTP sessions. Historical
    backfill callers keep the legacy sequential behavior unless they opt in themselves.
    """
    if not parallel_interfaces:
        return [_collect_public_dataset(db,operation_id,period,name,spec,request_collector,progress,
                    recheck=recheck,progress_state=progress_state) for name,spec in FIELDS.items()]
    workers={}
    results={}
    errors=[]
    state_lock=Lock()
    live={name:{'status':'pending','page':0,'pages':None,'records':0,'total':None} for name in FIELDS}
    def update(dataset, details):
        with state_lock:
            live[dataset].update(details)
            snapshot={key:dict(value) for key,value in live.items()}
            if progress_state:
                progress_state({'kind':'financial','phase':'downloading','interfaces':snapshot})
    try:
        with ThreadPoolExecutor(max_workers=2,thread_name_prefix='industry-financial') as pool:
            futures={}
            for name,spec in FIELDS.items():
                worker=request_collector.spawn()
                workers[name]=worker
                futures[pool.submit(_collect_public_dataset,db,operation_id,period,name,spec,worker,progress,
                    recheck=recheck,progress_state=update,industry_profile='financial')]=name
            for future in as_completed(futures):
                name=futures[future]
                try:
                    results[name]=future.result()
                except BaseException as exc:
                    errors.append(exc)
    finally:
        request_collector.requests+=sum(worker.requests for worker in workers.values())
        request_collector.retries+=sum(worker.retries for worker in workers.values())
        request_collector.halted.update({key:value for worker in workers.values() for key,value in worker.halted.items()})
        for worker in workers.values():
            close=getattr(getattr(worker,'session',None),'close',None)
            if callable(close):
                close()
    if progress_state:
        progress_state({'kind':'financial','phase':'validated' if not errors else 'failed',
                        'interfaces':{key:dict(value) for key,value in live.items()}})
    if errors:
        from .collection import finish_batch
        reason='四表采集未完整，所有未发布接口段均关闭'
        with db.connection() as conn:
            abandoned=[row[0] for row in conn.execute(
                "SELECT id FROM collection_batches WHERE operation_id=? AND status='running'",
                (operation_id,))]
        for batch_id in abandoned:
            finish_batch(db,batch_id,'failed',error=reason)
        raise errors[0]
    return [results[name] for name in FIELDS]


def publish(conn, batch_id, period, datasets):
    """Publish old or public source references; caller owns one atomic transaction."""
    public = batch_id is None
    if public:
        if {item.get('dataset') for item in datasets}!=set(FIELDS) or len(datasets)!=len(FIELDS):
            raise ValueError('公共财务发布必须包含恰好四个独立数据集')
        operation_ids=set()
        for data in datasets:
            batch=conn.execute('SELECT * FROM collection_batches WHERE id=?',(data.get('collection_batch_id'),)).fetchone()
            if (batch is None or batch['status']!='running' or batch['dataset']!='market.financial.'+data['dataset']
                    or batch['domain']!='market' or batch['source_system']!='eastmoney'
                    or batch['scope_start']!=period or batch['scope_end']!=period
                    or batch['parser_version']!='market-financial-v1'):
                raise ValueError('公共财务采集批次范围或状态不适用')
            operation_ids.add(batch['operation_id'])
        if len(operation_ids)!=1:
            raise ValueError('四个财务段必须属于同一 operation')
    else:
        batch = conn.execute('SELECT * FROM market_financial_batches WHERE id=?', (batch_id,)).fetchone()
        if batch is None or batch['status'] != 'running' or batch['report_date'] != period:
            raise ValueError('采集批次状态不适用')
    if {d['dataset'] for d in datasets} != set(FIELDS) or len(datasets) != len(FIELDS):
        raise ValueError('必须包含三表及业绩指标，未发布')
    summaries = {}
    for data in datasets:
        dataset = data['dataset']
        source_ids = []
        if public:
            ordered_sources=sorted(data['sources'],key=lambda source:source.get('page_number',0))
            if any(not source.get('collection_source_id') for source in ordered_sources):
                raise ValueError('公共财务来源缺少 collection_source_id')
            expected_pages=max(1,math.ceil(len(data['rows'])/500))
            if [s.get('page_number') for s in ordered_sources]!=list(range(1,expected_pages+1)):
                raise ValueError('公共来源分页序号不连续或不匹配行数')
            for source in ordered_sources:
                sid=source['collection_source_id']
                record=conn.execute("SELECT * FROM collection_sources WHERE id=? AND batch_id=? AND dataset=? "
                    "AND source_system='eastmoney' AND request_status IN ('success','reused') AND parse_status='complete'",
                    (sid,data['collection_batch_id'],'market.financial.'+dataset)).fetchone()
                if not record:
                    raise ValueError('公共财务来源与本批次、接口或解析状态不匹配')
                request=json.loads(record['request_json'])
                params=request.get('params',{})
                if (params.get('pageNumber')!=source['page_number'] or params.get('columns')!='ALL'
                        or params.get('reportName')!=FIELDS[dataset]['report']):
                    raise ValueError('公共财务来源请求参数与分页口径不匹配')
            data['sources']=ordered_sources
            source_ids=[source['collection_source_id'] for source in ordered_sources]
        else:
            for source in data['sources']:
                page_offset=(source['page_number']-1)*500
                page_fields=sorted(data['rows'][page_offset]) if page_offset<len(data['rows']) else []
                source_ids.append(conn.execute('INSERT INTO market_financial_sources '
                    '(batch_id,dataset,page_number,url,params_json,file_path,content_hash,raw_bytes,stored_bytes,obtained_at,fields_json) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (batch_id, dataset, source['page_number'], source['url'], encoded(source['params']),
                     source['file_path'], source.get('raw_sha256', source['sha256']), source['raw_bytes'], source['stored_bytes'],
                     source['obtained_at'], encoded(page_fields))).lastrowid)
        summary = {'rows': len(data['rows']), 'request_count': len(data['sources']),
                   'source_count': len(data['sources']), 'verified_count': len(data['rows']),
                   'published_count': 0, 'field_value_counts': {},
                   'new': 0, 'revised': 0, 'enriched': 0, 'unchanged': 0,
                   'raw_bytes': sum(s['raw_bytes'] for s in data['sources']),
                   'stored_bytes': sum(s['stored_bytes'] for s in data['sources'])}
        summaries[dataset] = summary
        spec = FIELDS[dataset]
        summary['field_value_counts'] = {
            field: {'non_null': sum(row.get(field) is not None for row in data['rows']),
                    'null': sum(row.get(field) is None for row in data['rows'])}
            for field in spec['fields'] if field not in DESCRIPTIVE
        }
        table = 'market_financial_'+dataset
        old = {(r['security_code'], r['exchange']): dict(r) for r in conn.execute(
            f'SELECT f.*,coalesce(s.fields_json,cs.fields_json) AS fields_json FROM {table} f '
            'LEFT JOIN market_financial_sources s ON s.id=f.source_id '
            'LEFT JOIN collection_sources cs ON cs.id=f.collection_source_id AND cs.batch_id=f.collection_batch_id '
            'WHERE f.report_date=? AND f.is_latest=1', (period,))}
        if old and len(data['rows']) < len(old)*.8:
            raise ValueError(f'{spec["label"]} 较同报告期已有覆盖骤减超过20%，未发布')
        seen = set()
        for index, row in enumerate(data['rows']):
            values, exchange = normalized(dataset, row, period)
            key = (row['SECURITY_CODE'], exchange)
            if key in seen:
                raise ValueError('证券报告期重复')
            seen.add(key)
            source_id = source_ids[index//500]
            source = data['sources'][index//500]
            before = old.get(key)
            content_hash = digest(encoded(values).encode())
            revised, enriched = False, False
            if before:
                old_fields = set(json.loads(before['fields_json']))
                for field in (old_fields | set(row))-DESCRIPTIVE:
                    column = spec['fields'][field]['column']
                    previous, current = before[column], values.get(column)
                    if previous != current:
                        if previous is None and current is not None:
                            enriched = True
                        elif previous is not None and current is None:
                            raise ValueError(f'{dataset}.{field} 来源值变空，疑似撤回，待核实后再发布')
                        else:
                            revised = True
                revised |= before['definition_version'] != spec['definition_version']
            stamp = now()
            if before and not revised and not enriched:
                summary['unchanged'] += 1
            else:
                summary['enriched' if before and enriched and not revised else 'revised' if before else 'new'] += 1
                if before:
                    conn.execute(f'UPDATE {table} SET is_latest=0 WHERE id=?', (before['id'],))
                values.update(exchange=exchange, report_date=period, version=before['version']+1 if before else 1,
                    is_latest=1, batch_id=None if public else batch_id, source_id=None if public else source_id,
                    collection_batch_id=data['collection_batch_id'] if public else None,
                    collection_source_id=source_id if public else None, content_hash=content_hash,
                    definition_version=spec['definition_version'],
                    obtained_at=source['obtained_at'], updated_at=stamp)
                conn.execute(f'INSERT INTO {table} ('+','.join(values)+') VALUES ('+
                             ','.join('?' for _ in values)+')', tuple(values.values()))
        summary['missing_previous'] = len(set(old)-seen)
        summary['published_count'] = summary['new'] + summary['revised'] + summary['enriched'] + summary['unchanged']
    if public:
        for data in datasets:
            dataset=data['dataset']
            result=summaries[dataset]
            # A complete request/publish proves this requested data segment ran;
            # it does not prove full issuer or field coverage against an external universe.
            coverage='empty' if result['rows']==0 else 'unknown'
            conn.execute("UPDATE collection_batches SET status='complete',coverage_status=?,finished_at=?,result_json=? WHERE id=?",
                (coverage,now(),encoded(result),data['collection_batch_id']))
    else:
        conn.execute("UPDATE market_financial_batches SET status='complete',finished_at=?,result_json=? WHERE id=?",
                     (now(), encoded(summaries), batch_id))
    return summaries
