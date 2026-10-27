"""Eastmoney China/US sovereign yield history using shared collection provenance."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from time import perf_counter
from zoneinfo import ZoneInfo

from stock_analysis.collection.collection import Cancelled, begin_batch, encoded, finish_batch, parsed_source

DATASET='bond_zh_us_rate'
DEFINITION_VERSION='v1'
PARSER_VERSION='bond-yields-v1'
URL='https://datacenter.eastmoney.com/api/data/get'
FIELDS={
    'EMM00588704':'china_2y','EMM00166462':'china_5y','EMM00166466':'china_10y',
    'EMM00166469':'china_30y','EMM01276014':'china_10y_2y_spread','EMM00000024':'china_gdp_yoy',
    'EMG00001306':'us_2y','EMG00001308':'us_5y','EMG00001310':'us_10y',
    'EMG00001312':'us_30y','EMG01339436':'us_10y_2y_spread','EMG00159635':'us_gdp_yoy',
}
PARAMS={'type':'RPTA_WEB_TREASURYYIELD','sty':'ALL','st':'SOLAR_DATE','sr':'-1',
        'token':'894050c76af8597a853f5b408b759f5d','ps':'500'}


def _decimal(value):
    if value is None or value=='':return None
    try:
        number=value if isinstance(value,Decimal) else Decimal(str(value))
    except (InvalidOperation,ValueError) as exc:
        raise ValueError('国债收益率字段不是有效十进制数') from exc
    if not number.is_finite():raise ValueError('国债收益率字段不是有限数')
    text=format(number,'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _date(value):
    if not isinstance(value,str):raise ValueError('国债观察日不是日期文本')
    return date.fromisoformat(value[:10]).isoformat()


def parse_page(raw):
    try:payload=json.loads(raw.decode('utf-8-sig'),parse_float=Decimal,parse_int=Decimal)
    except (UnicodeDecodeError,ValueError) as exc:raise ValueError('东方财富国债响应不是有效JSON') from exc
    result=payload.get('result') if isinstance(payload,dict) else None
    if not isinstance(result,dict) or not isinstance(result.get('data'),list):
        raise ValueError('东方财富国债响应缺少result.data')
    try:pages=int(result['pages']);total=int(result['count'])
    except (KeyError,TypeError,ValueError) as exc:raise ValueError('东方财富国债响应缺少总页数或总记录数') from exc
    if pages<1 or total<0:raise ValueError('东方财富国债响应分页总数无效')
    rows=[]
    for item in result['data']:
        if not isinstance(item,dict) or 'SOLAR_DATE' not in item or not set(FIELDS)<=set(item):
            raise ValueError('东方财富国债记录字段集合不完整')
        day=_date(item['SOLAR_DATE'])
        row={'observation_date':day}
        row.update({target:_decimal(item.get(source)) for source,target in FIELDS.items()})
        rows.append(row)
    return pages,total,rows


def _hash(row):
    return hashlib.sha256(encoded(row).encode('utf-8')).hexdigest()


def _manifest(db):
    with db.connection() as conn:
        rows=conn.execute("SELECT * FROM collection_batches WHERE dataset=? AND source_system='eastmoney' "
            "AND status IN ('running','failed','cancelled') ORDER BY id DESC",(DATASET+'.manifest',)).fetchall()
    for row in rows:
        scope=json.loads(row['scope_json'])
        if (scope.get('kind')=='initialization_manifest' and scope.get('definition_version')==DEFINITION_VERSION
                and scope.get('parser_version')==PARSER_VERSION):
            return dict(row)
    return None


def _new_batch(db,operation,year,start,end,trigger,manifest_id=None):
    scope={'kind':'annual_segment','year':year,'start':start,'end':end,'definition_version':DEFINITION_VERSION,
           'parser_version':PARSER_VERSION,'manifest_id':manifest_id}
    key=f'{operation}:{DATASET}:{DEFINITION_VERSION}:{PARSER_VERSION}:{start}:{end}:{trigger}'
    return begin_batch(db,operation,DATASET,'eastmoney',start,end,scope=scope,frequency='daily',trigger=trigger,
        idempotency_key=key,domain='macro',parser_version=PARSER_VERSION,definition_version=DEFINITION_VERSION)


def _manifest_batch(db,operation,trigger):
    scope={'kind':'initialization_manifest','start':'2002-01-04','target_end':None,'definition_version':DEFINITION_VERSION,
           'parser_version':PARSER_VERSION,'annual_segments':[],'freshness':{}}
    return begin_batch(db,operation,DATASET+'.manifest','eastmoney','2002-01-04',None,scope=scope,
        frequency='daily',trigger=trigger,idempotency_key=None,domain='macro',parser_version=PARSER_VERSION,
        definition_version=DEFINITION_VERSION)


def _discover_target_end(db,collector,operation,manifest_id,cancelled):
    scope={'kind':'target_discovery','manifest_id':manifest_id,'definition_version':DEFINITION_VERSION,
           'parser_version':PARSER_VERSION}
    batch=begin_batch(db,operation,DATASET,'eastmoney',scope=scope,frequency='daily',trigger='manual',
        domain='macro',parser_version=PARSER_VERSION,definition_version=DEFINITION_VERSION)
    params={**PARAMS,'p':1,'pageNo':1,'pageNum':1}
    source_id=None
    try:
        response,source_id=collector.get(batch,'eastmoney',DATASET,URL,params,expect='json',
            healthy_response=lambda item:_is_healthy(item))
        pages,total,rows=parse_page(response.content)
        if pages<1 or not rows:raise ValueError('东方财富首页未返回最新国债记录')
        if pages!=max(1,(total+int(PARAMS['ps'])-1)//int(PARAMS['ps'])):
            raise ValueError('东方财富首页总页数与总记录数、页容量不符')
        if len(rows)!=min(int(PARAMS['ps']),total):raise ValueError('东方财富首页页长与声明总记录数不符')
        dates=[item['observation_date'] for item in rows]
        if dates!=sorted(dates,reverse=True) or len(set(dates))!=len(dates):
            raise ValueError('东方财富首页日期未严格倒序或存在重复')
        if len(rows)>int(PARAMS['ps']):raise ValueError('东方财富首页超过固定页容量')
        if cancelled():raise Cancelled('用户取消采集；初始化清单保持待恢复')
        parsed_source(db,source_id,fields=['SOLAR_DATE',*FIELDS])
        result={'kind':'target_discovery','pages':pages,'count':total,'rows':len(rows),
                'latest_date':dates[0],'oldest_date':dates[-1],'source_id':source_id}
        finish_batch(db,batch,'complete','partial' if result.get('pending_withdrawals') else 'unknown',result=result)
        return dates[0]
    except BaseException as exc:
        if source_id is not None:
            try:parsed_source(db,source_id,error=exc)
            except Exception:pass
        finish_batch(db,batch,'cancelled' if isinstance(exc,Cancelled) else 'failed',error=exc)
        raise


def _is_healthy(response):
    try:
        payload=json.loads(response.content.decode('utf-8-sig'))
        result=payload.get('result')
        return (isinstance(result,dict) and isinstance(result.get('data'),list)
                and str(payload.get('code','200')) in ('0','200') and payload.get('success') is not False
                and 'pages' in result and 'count' in result)
    except (UnicodeDecodeError,ValueError,AttributeError):
        return False


def _save_scope(db,batch_id,scope):
    with db.connection(write=True) as conn:
        conn.execute('UPDATE collection_batches SET scope_json=? WHERE id=?',(encoded(scope),batch_id))


def _get_scope(db,batch_id):
    with db.connection() as conn:
        return json.loads(conn.execute('SELECT scope_json FROM collection_batches WHERE id=?',(batch_id,)).fetchone()[0])


def _save_freshness(db,result,freshness=None):
    with db.connection(write=True) as conn:
        row=conn.execute("SELECT id,result_json FROM collection_batches WHERE operation_id=? AND dataset=? "
            "AND scope_start=? AND scope_end=? ORDER BY id DESC LIMIT 1",
            (result['operation_id'],DATASET,result['start'],result['end'])).fetchone()
        if row:
            saved=json.loads(row['result_json']);saved['freshness']=freshness or result.get('freshness',{})
            conn.execute('UPDATE collection_batches SET result_json=? WHERE id=?',(encoded(saved),row['id']))


def _completed_year(db,start,end):
    with db.connection() as conn:
        rows=conn.execute("SELECT id,result_json FROM collection_batches WHERE domain='macro' AND dataset=? AND source_system='eastmoney' "
            "AND scope_start=? AND scope_end=? AND definition_version=? AND parser_version=? AND status='complete' "
            "ORDER BY id DESC",(DATASET,start,end,DEFINITION_VERSION,PARSER_VERSION)).fetchall()
    for row in rows:
        try:
            if not json.loads(row['result_json']).get('pending_withdrawals'):return row['id']
        except (TypeError,ValueError):continue
    return None


def _batch_result(db,batch_id):
    with db.connection() as conn:
        row=conn.execute('SELECT result_json FROM collection_batches WHERE id=?',(batch_id,)).fetchone()
    return json.loads(row[0]) if row else {}


def _unresolved_withdrawals(db):
    with db.connection() as conn:
        rows=conn.execute("SELECT id,result_json FROM collection_batches WHERE dataset=? AND source_system='eastmoney' "
            "AND status IN ('complete','failed','cancelled') ORDER BY id",(DATASET,)).fetchall()
    pending=[]
    for row in rows:
        try:items=json.loads(row['result_json'] or '{}').get('pending_withdrawals',[])
        except (TypeError,ValueError):continue
        for item in items:
            if not item.get('resolved') and item.get('date'):
                pending.append({'batch_id':row['id'],**item})
    unique={item['date']:item for item in pending}
    return sorted(unique.values(),key=lambda item:item['date'])


def _resolve_prior_withdrawals(db,rows,source_ids,batch_id,pending):
    blocked={item.get('date') for item in pending}
    current={row['observation_date']:(row,source_id) for row,source_id in zip(rows,source_ids)}
    resolved=[]
    with db.connection(write=True) as conn:
        for day,(candidate,source_id) in current.items():
            if day in blocked:continue
            latest=conn.execute('SELECT content_hash FROM sovereign_yield_observations WHERE observation_date=? '
                'AND definition_version=? AND is_latest=1',(day,DEFINITION_VERSION)).fetchone()
            if not latest or latest['content_hash']!=_hash(candidate):continue
            batches=conn.execute("SELECT id,result_json FROM collection_batches WHERE dataset=? AND source_system='eastmoney' "
                "AND id<>? AND result_json IS NOT NULL ORDER BY id",(DATASET,batch_id)).fetchall()
            for previous in batches:
                try:result=json.loads(previous['result_json'] or '{}')
                except (TypeError,ValueError):continue
                changed=False
                for item in result.get('pending_withdrawals',[]):
                    if item.get('date')==day and not item.get('resolved'):
                        item.update(resolved=True,resolved_by_batch_id=batch_id,resolved_by_source_id=source_id)
                        changed=True;resolved.append({'date':day,'batch_id':previous['id'],'source_id':source_id})
                if changed:
                    conn.execute('UPDATE collection_batches SET result_json=? WHERE id=?',(encoded(result),previous['id']))
    return resolved


def _publish(db,batch_id,rows,sources,cancelled):
    by_date={}
    for row,source_id in zip(rows,sources):by_date[row['observation_date']]=(row,source_id)
    published=0;revised=0;unchanged=0;pending=[]
    operation_id=None
    with db.connection(write=True) as conn:
        operation_id=conn.execute('SELECT operation_id FROM collection_batches WHERE id=?',(batch_id,)).fetchone()[0]
        active=conn.execute('SELECT cancel_requested FROM collection_operations WHERE operation_id=?',(operation_id,)).fetchone()
        if cancelled() or not active or active[0]:raise Cancelled('用户取消采集；已发布年度分段保留')
        for day,(row,source_id) in sorted(by_date.items()):
            old=conn.execute('SELECT * FROM sovereign_yield_observations WHERE observation_date=? AND definition_version=? AND is_latest=1',
                (day,DEFINITION_VERSION)).fetchone()
            values={key:row[key] for key in FIELDS.values()}
            candidate_hash=_hash(row)
            if old and old['content_hash']==candidate_hash:
                unchanged+=1;continue
            if old and any(old[key] is not None and values[key] is None for key in values):
                pending.append({'date':day,'fields':[key for key in values if old[key] is not None and values[key] is None],
                                'source_id':source_id})
                continue
            version=(old['version']+1) if old else 1
            if old:conn.execute('UPDATE sovereign_yield_observations SET is_latest=0 WHERE id=?',(old['id'],))
            columns=['observation_date','definition_version',*values,'content_hash','version','is_latest','batch_id','source_id','updated_at']
            bound=[day,DEFINITION_VERSION,*values.values(),candidate_hash,version,1,batch_id,source_id,
                   datetime.now(timezone.utc).isoformat()]
            conn.execute('INSERT INTO sovereign_yield_observations('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',bound)
            if old:revised+=1
            else:published+=1
    return {'inserted':published,'revised':revised,'unchanged':unchanged,'pending_withdrawals':pending}


def _fetch_year(db,collector,operation,year,start,end,trigger,manifest_id,cancelled,progress):
    batch=_new_batch(db,operation,year,start,end,trigger,manifest_id)
    params={**PARAMS,'p':1,'pageNo':1,'pageNum':1,
            'filter':f"(SOLAR_DATE>='{start}')(SOLAR_DATE<='{end}')"}
    rows=[];source_ids=[];page_sources=[];reported_pages=None;reported_total=None;seen=set();previous_date=None;parsed_complete=False
    try:
        page=1
        while True:
            if cancelled():raise Cancelled('用户取消采集；已完成年度分段保留')
            params.update(p=page,pageNo=page,pageNum=page)
            response,source_id=collector.get(batch,'eastmoney',DATASET,URL,params,expect='json',
                healthy_response=lambda item:_is_healthy(item))
            page_sources.append(source_id)
            try:pages,total,page_rows=parse_page(response.content)
            except Exception as exc:
                parsed_source(db,source_id,error=exc)
                raise
            if reported_pages is None:reported_pages,reported_total=pages,total
            elif (pages,total)!=(reported_pages,reported_total):raise ValueError('分页总数在年度请求期间发生变化')
            expected_pages=max(1,(total+int(PARAMS['ps'])-1)//int(PARAMS['ps']))
            if pages!=expected_pages:raise ValueError('分页总页数与记录总数、页容量不符')
            if len(page_rows)>int(PARAMS['ps']):raise ValueError('年度分页返回行数超过页容量')
            dates=[item['observation_date'] for item in page_rows]
            if any(not start<=day<=end for day in dates):raise ValueError('服务端日期筛选返回范围外记录')
            if dates!=sorted(dates,reverse=True):raise ValueError('年度分页日期未按倒序排列')
            if seen.intersection(dates):raise ValueError('年度分页存在重复观察日')
            if any(left<=right for left,right in zip(dates,dates[1:])):raise ValueError('年度分页内日期不严格递减')
            expected=max(0,min(int(PARAMS['ps']),total-len(rows)))
            if len(page_rows)!=expected:raise ValueError(f'第{page}页行数不符：收到 {len(page_rows)}，预期 {expected}')
            if dates and previous_date and previous_date<=dates[0]:raise ValueError('跨页日期未严格倒序或重复')
            if dates:previous_date=dates[-1]
            seen.update(dates);rows.extend(page_rows);source_ids.extend([source_id]*len(page_rows))
            progress(f'{year}年：已校验第 {page}/{pages} 页，取得 {len(rows)}/{total} 行')
            if page>=pages:break
            if not page_rows:raise ValueError('分页在声明总页数前返回空页')
            page+=1
        if len(rows)!=reported_total:raise ValueError(f'年度分页行数不符：收到 {len(rows)}，声明 {reported_total}')
        if len(seen)!=len(rows):raise ValueError('年度分页日期重复')
        if any(int(day[:4])!=year for day in seen):raise ValueError('年度分页越过年份边界')
        if rows and (max(seen)>end or min(seen)<start):raise ValueError('年度分页边界不符')
        if year==2002 and start=='2002-01-04' and not any(
                row['observation_date']=='2002-01-04' and row['china_10y'] is not None for row in rows):
            raise ValueError('初始化未验证2002-01-04中国10年期首个有效观察值')
        if not rows:
            for source_id in set(page_sources):parsed_source(db,source_id,fields=['SOLAR_DATE',*FIELDS])
            parsed_complete=True
            field_coverage={field:{'valid':0,'first_valid':None,'last_valid':None,'missing':0} for field in FIELDS.values()}
            result={'operation_id':operation,'year':year,'start':start,'end':end,'pages':reported_pages,
                    'total':reported_total,'first_date':None,'last_date':None,'sources':sorted(set(page_sources)),
                    'field_coverage':field_coverage,'pending_withdrawals':[],'empty':True,
                    'freshness':_freshness_state([],datetime.now(timezone.utc).isoformat())}
            finish_batch(db,batch,'failed','empty',result=result,error='空响应不作为年度覆盖证明')
            return result
        if cancelled():raise Cancelled('用户取消采集；已完成年度分段保留')
        for source_id in set(page_sources):parsed_source(db,source_id,fields=['SOLAR_DATE',*FIELDS])
        parsed_complete=True
        publish_started=perf_counter()
        result=_publish(db,batch,rows,source_ids,cancelled)
        publish_elapsed_ms=round((perf_counter()-publish_started)*1000,3)
        resolved_withdrawals=_resolve_prior_withdrawals(db,rows,source_ids,batch,result.get('pending_withdrawals',[]))
        freshness=_freshness_state(rows,datetime.now(timezone.utc).isoformat())
        if result.get('pending_withdrawals'):
            for state in freshness.values():state.update(status='pending',ttl_seconds=3600)
        field_coverage={field:{'valid':sum(1 for row in rows if row[field] is not None),
            'first_valid':min((row['observation_date'] for row in rows if row[field] is not None),default=None),
            'last_valid':max((row['observation_date'] for row in rows if row[field] is not None),default=None),
            'missing':sum(1 for row in rows if row[field] is None)} for field in FIELDS.values()}
        result.update(operation_id=operation,year=year,start=start,end=end,pages=reported_pages,total=reported_total,
                      first_date=max(seen) if seen else None,last_date=min(seen) if seen else None,
                      coverage='unknown',sources=sorted(set(page_sources)),field_coverage=field_coverage,
                      freshness=freshness,resolved_withdrawals=resolved_withdrawals,
                      publish_elapsed_ms=publish_elapsed_ms,publish_row_count=len(rows))
        finish_batch(db,batch,'complete','partial' if result.get('pending_withdrawals') else 'unknown',result=result)
        return result
    except BaseException as exc:
        if not parsed_complete:
            for source_id in set(page_sources):
                try:parsed_source(db,source_id,error=exc)
                except Exception:pass
        finish_batch(db,batch,'cancelled' if isinstance(exc,Cancelled) else 'failed',error=exc)
        raise


def _freshness(db):
    with db.connection() as conn:
        rows=conn.execute("SELECT result_json FROM collection_batches WHERE dataset=? AND source_system='eastmoney' "
            "AND status IN ('complete','failed') ORDER BY id DESC",(DATASET,)).fetchall()
    merged={}
    for row in rows:
        try:fresh=json.loads(row[0]).get('freshness',{})
        except (TypeError,ValueError):continue
        for country,item in fresh.items():
            if country not in merged or item.get('last_checked_at','')>merged[country].get('last_checked_at',''):
                merged[country]=item
    return merged


def _freshness_state(rows,checked_at):
    result={}
    for country,zone,fields in (
        ('china','Asia/Shanghai',[key for key in FIELDS.values() if key.startswith('china_') and key!='china_gdp_yoy']),
        ('us','America/New_York',[key for key in FIELDS.values() if key.startswith('us_') and key!='us_gdp_yoy'])):
        local_yesterday=(datetime.now(ZoneInfo(zone)).date()-timedelta(days=2)).isoformat()
        valid_dates=[row['observation_date'] for row in rows if any(row[key] is not None for key in fields)]
        last=max(valid_dates,default=None)
        last_checked=checked_at
        missing=[key for key in fields if not any(row[key] is not None for row in rows)]
        per_field={key:max((row['observation_date'] for row in rows if row[key] is not None),default=None) for key in fields}
        lagging=[key for key,value in per_field.items() if value is None or value<local_yesterday]
        behind=last is None or last<local_yesterday
        result[country]={'last_checked_at':last_checked,'last_valid_date':last,'reference_date':local_yesterday,
                         'fields':per_field,'fields_without_values':missing,'fields_lagging':lagging,
                         'status':'pending' if behind or lagging else 'current',
                         'ttl_seconds':3600 if behind or lagging else 21600}
    return result


def _fresh_enough(state):
    now=datetime.now(timezone.utc)
    for item in state.values():
        try:age=(now-datetime.fromisoformat(item['last_checked_at']).astimezone(timezone.utc)).total_seconds()
        except (KeyError,ValueError,TypeError):return False
        if age>=item.get('ttl_seconds',0):return False
    return bool(state)


def collect(db,operation,*,collector,cancelled,progress):
    """Resume initialization first; after it completes, backfill and refresh 14 days."""
    now=datetime.now(timezone.utc).isoformat()
    manifest=_manifest(db)
    with db.connection() as conn:
        has_facts=conn.execute('SELECT 1 FROM sovereign_yield_observations LIMIT 1').fetchone() is not None
    trigger='manual'
    manifest_id=None
    if manifest:
        manifest_id=manifest['id'];scope=json.loads(manifest['scope_json'])
        target_end=scope.get('target_end')
        progress('恢复未完成的国债收益率历史初始化')
    elif not has_facts:
        manifest_id=_manifest_batch(db,operation,trigger);scope=_get_scope(db,manifest_id);target_end=None
        progress('初始化国债收益率历史：确认最新日期')
    else:
        scope=None;target_end=None
    initialized_now=bool(manifest_id)
    if manifest_id:
        if not target_end:
            progress('请求东方财富首页，固定本次初始化最新观察日')
            target_end=_discover_target_end(db,collector,operation,manifest_id,cancelled)
            scope['target_end']=target_end;scope['target_year']=int(target_end[:4]);scope['annual_segments']=[]
            _save_scope(db,manifest_id,scope)
        target_year=int(target_end[:4]);segments=[]
        for year in range(2002,target_year+1):
            start='2002-01-04' if year==2002 else f'{year:04d}-01-01'
            end=target_end if year==target_year else f'{year:04d}-12-31'
            segments.append((year,start,end))
        init_pending=False
        completed_years={item['year'] for item in scope.get('annual_segments',[]) if item.get('status')=='complete'}
        for year,start,end in reversed(segments):
            if cancelled():raise Cancelled('用户取消采集；初始化年度状态已保留')
            progress(f'历史初始化年度进度 {len(completed_years)}/{len(segments)}；准备{year}年')
            completed_batch=_completed_year(db,start,end)
            if completed_batch:
                if year not in completed_years:
                    old_result=_batch_result(db,completed_batch)
                    scope['annual_segments'].append({'year':year,'start':start,'end':end,'status':'complete',
                        'rows':old_result.get('total',0),'pages':old_result.get('pages'),'sources':old_result.get('sources',[])})
                    completed_years.add(year);_save_scope(db,manifest_id,scope)
                continue
            result=_fetch_year(db,collector,operation,year,start,end,'manual',manifest_id,cancelled,progress)
            if result.get('empty'):
                init_pending=True
                continue
            if year==target_year:
                with db.connection(write=True) as conn:
                    actual=conn.execute('SELECT id,scope_json FROM collection_batches WHERE id IN '
                        '(SELECT max(id) FROM collection_batches WHERE operation_id=? AND dataset=?)',
                        (operation,DATASET)).fetchone()
                    if actual:
                        batch_scope=json.loads(actual['scope_json']);batch_scope['end']=end
                        conn.execute('UPDATE collection_batches SET scope_end=?,scope_json=? WHERE id=?',
                                     (end,encoded(batch_scope),actual['id']))
                _save_freshness(db,result)
            if result.get('pending_withdrawals'):
                init_pending=True
                continue
            scope['annual_segments'].append({'year':year,'start':start,'end':end,'status':'complete',
                'rows':result['total'],'pages':result['pages'],'sources':result['sources']})
            completed_years.add(year)
            _save_scope(db,manifest_id,scope)
            progress(f'历史初始化年度进度 {len(completed_years)}/{len(segments)}；已完成{year}年')
        if init_pending:
            return {'status':'partial','message':'年度数据存在非空值撤回候选，待核实后继续初始化','freshness':_freshness(db)}
        scope['initialized_at']=datetime.now(timezone.utc).isoformat()
        with db.connection(write=True) as conn:
            conn.execute("UPDATE collection_batches SET status='complete',coverage_status='unknown',finished_at=?,scope_json=? WHERE id=?",
                         (datetime.now(timezone.utc).isoformat(),encoded(scope),manifest_id))
    freshness=_freshness(db)
    unresolved=_unresolved_withdrawals(db)
    for candidate in unresolved:
        if cancelled():raise Cancelled('用户取消采集；待核实字段候选保留')
        day=candidate['date'];year=int(day[:4])
        progress(f'重检待核实撤回候选：{day}')
        result=_fetch_year(db,collector,operation,year,day,day,'manual',None,cancelled,progress)
        if result.get('empty') or any(item.get('date')==day for item in result.get('pending_withdrawals',[])):
            return {'status':'partial','start':day,'end':day,'annual_batches':[result],
                'freshness':freshness,'requests':result.get('pages') or 0,
                'message':f'{day}字段撤回候选仍未核实，暂停普通增量'}
    if _fresh_enough(freshness) and not initialized_now:
        progress('中美国债收益率检查均在有效期内，跳过重复请求')
        return {'status':'current','freshness':freshness,'requests':0}
    today=datetime.now(ZoneInfo('Asia/Shanghai')).date()
    with db.connection() as conn:
        watermark=conn.execute('SELECT max(observation_date) FROM sovereign_yield_observations WHERE is_latest=1').fetchone()[0]
    reference_start=(today-timedelta(days=14)).isoformat()
    tail_start=(date.fromisoformat(watermark)+timedelta(days=1)).isoformat() if watermark else '2002-01-04'
    start=min(reference_start,tail_start)
    end=today.isoformat()
    output=[]
    for year in range(int(start[:4]),int(end[:4])+1):
        low=max(start,f'{year:04d}-01-01');high=min(end,f'{year:04d}-12-31')
        result=_fetch_year(db,collector,operation,year,low,high,'manual',None,cancelled,progress)
        output.append(result)
        if result.get('empty') or result.get('pending_withdrawals'):
            fresh=result.get('freshness',freshness)
            return {'status':'empty' if result.get('empty') else 'partial','start':start,'end':end,
                    'annual_batches':output,'freshness':fresh,'requests':sum(x['pages'] or 0 for x in output),
                    'message':f'{year}年分段尚未通过校验，停止后续年度；下次点击可从水位恢复'}
    rows=[]
    # Freshness describes the just-validated response window, including explicit empty results.
    if output:
        fresh=next((item['freshness'] for item in reversed(output) if item.get('year')==int(end[:4])),output[-1]['freshness'])
        _save_freshness(db,output[-1],fresh)
    else:fresh=freshness
    return {'status':'partial' if any(item.get('pending_withdrawals') for item in output) else
            'empty' if output and all(item.get('empty') for item in output) else 'complete',
            'start':start,'end':end,'annual_batches':output,'freshness':fresh,'requests':sum(x['pages'] or 0 for x in output)}
