"""Index source adapters and resumable segmented collection orchestration.

Run with ``python -m quarterly_dashboard.index_collection --mode full`` after
the daily server has initialized the schema. No theme requests are constructed.
"""
from __future__ import annotations

import argparse
from calendar import monthrange
from concurrent.futures import ThreadPoolExecutor,as_completed
from copy import deepcopy
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
import io
import hashlib
import json
import math
from pathlib import Path
from threading import Thread
from uuid import uuid4
from zoneinfo import ZoneInfo

from .collection import (Cancelled, ProviderBlocked, RequestCollector, archive_source, atomic_write,
                         begin_batch, digest, encoded, finish_batch, parsed_source, read_source)
from .index_definitions import BOARD_CODES, CS_CODES, CS_CANDIDATES, DEFINITIONS, LG_CODES, METRICS, STRATEGY_CODES, canonical
from .index_repository import catalog, publish
from .index_prices import parse_prices, publish_prices, price_coverage
from .storage import DEFAULT_DATABASE, Database, instance_lock, utc_now

CS_HISTORY='https://www.csindex.com.cn/csindex-home/perf/index-perf'
CS_BOARD='https://www.csindex.com.cn/csindex-home/data-service/indexValuation'
SW_ANALYSIS='https://www.swsresearch.com/institute-sw/api/index_analysis/index_analysis_report/'
CALENDAR='https://finance.sina.com.cn/realstock/company/klc_td_sh.txt'


def number(value):
    if value is None or str(value).strip() in ('','-','--','null','None','N/A'):
        return None
    if isinstance(value,bool):
        raise ValueError('布尔值不能作为估值')
    try:
        result=float(value)
    except (ValueError,TypeError) as exc:
        raise ValueError('来源估值字段不是数值或已登记空值') from exc
    if not math.isfinite(result):
        raise ValueError('来源估值不是有限数值')
    return result


def day(value):
    if isinstance(value,(int,float)) and abs(value)>10**10:
        return datetime.fromtimestamp(value/1000,ZoneInfo('Asia/Shanghai')).date().isoformat()
    text=str(value).strip()[:10]
    compact=text.replace('-','')
    if len(compact)==8 and compact.isdigit():
        text=f'{compact[:4]}-{compact[4:6]}-{compact[6:]}'
    return date.fromisoformat(text).isoformat()


def rolling_start(today):
    return today.replace(year=today.year-10,day=min(today.day,monthrange(today.year-10,today.month)[1]))


def segments(start,end):
    current=date.fromisoformat(start)
    end=date.fromisoformat(end)
    while current<=end:
        last=min(end,current.replace(day=monthrange(current.year,current.month)[1]))
        yield current.isoformat(),last.isoformat()
        current=last+timedelta(days=1)


def json_data(response):
    try:
        payload=response.json()
    except ValueError as exc:
        raise ValueError('来源不是可解析 JSON') from exc
    if not isinstance(payload,dict) or 'data' not in payload:
        raise ValueError('来源缺少 data')
    if payload.get('code') is not None and str(payload['code']) not in ('200','0'):
        raise ValueError('来源业务状态失败')
    if payload.get('success') is False:
        raise ValueError('来源业务状态失败')
    return payload['data']


def parse_rows(rows, *, provider, series_keys, code_key, name_key, date_key, start, end,
               trading_days, batch_id, source_id, expected_code=None, level=None):
    if not isinstance(rows,list):
        raise ValueError('来源数据不是记录列表')
    observations=[]
    identities={}
    anomalies=[]
    seen=set()
    boundary_placeholders={}
    if provider=='csindex' and series_keys==['history.rolling']:
        grouped={}
        for raw in rows:
            if isinstance(raw,dict) and code_key in raw and date_key in raw:
                grouped.setdefault(str(raw[code_key]).strip(),[]).append(raw)
        for code,items in grouped.items():
            items=sorted(items,key=lambda item:day(item[date_key]))
            if len(items)<2:
                continue
            first,next_row=items[:2]
            first_day,next_day=day(first[date_key]),day(next_row[date_key])
            # The endpoint can copy its first base-price row onto the requested
            # boundary, even when that boundary is a real trading day.
            if (first_day==start and (date.fromisoformat(next_day)-date.fromisoformat(first_day)).days>7
                    and first.get('close') is not None and first.get('tradingVol') is not None
                    and 'peg' in first and number(first['peg']) is None
                    and {k:v for k,v in first.items() if k!=date_key}=={k:v for k,v in next_row.items() if k!=date_key}):
                boundary_placeholders[code,first_day]=next_day
    for raw in rows:
        if not isinstance(raw,dict):
            raise ValueError('来源记录不是对象')
        required={code_key,name_key,date_key}|set().union(*(DEFINITIONS[provider,key]['fields'] for key in series_keys))
        if not required<=set(raw):
            raise ValueError('来源字段缺失：'+','.join(sorted(required-set(raw))))
        code=str(raw[code_key]).strip()
        if expected_code and code!=expected_code:
            raise ValueError('来源指数代码与请求不一致')
        if not code or not str(raw[name_key]).strip():
            raise ValueError('来源指数代码／名称为空')
        observed=day(raw[date_key])
        if (code,observed) in boundary_placeholders:
            anomalies.append(dict(code=code,date=observed,reason='boundary_placeholder_duplicate',copied_from=boundary_placeholders[code,observed]))
            continue
        if not start<=observed<=end:
            anomalies.append(dict(code=code,date=observed,reason='out_of_window'))
            continue
        if observed not in trading_days:
            anomalies.append(dict(code=code,date=observed,reason='non_trading_day'))
            continue
        identity=canonical(code,provider)
        kind='industry' if provider=='sws' else 'strategy' if code.split('.')[0] in STRATEGY_CODES else 'broad'
        # Do not rewrite current navigation from older response names.
        if identity not in identities or observed>identities[identity]['observed_on']:
            identities[identity]=dict(code=identity,name=str(raw[name_key]).strip(),kind=kind,provider=provider,
                provider_code=code,source_id=source_id,level=level,system='sw' if provider=='sws' else None,observed_on=observed)
        if (code,observed) in seen:
            raise ValueError('来源重复指数／观察日')
        seen.add((code,observed))
        for series in series_keys:
            definition=DEFINITIONS[provider,series]
            values={target:number(raw[field]) for field,target in definition['fields'].items()}
            observations.append(dict(index_code=identity,provider=provider,series_key=series,observation_date=observed,
                frequency=definition['frequency'],batch_id=batch_id,source_id=source_id,explicit_nulls=True,**values))
    return observations,list(identities.values()),anomalies


def compact_coverage(items):
    metrics={}
    missing_by_index={}
    index_codes=set()
    for item in items:
        code=item.get('index_code')
        if code is not None:
            index_codes.add(code)
        fields=item.get('metrics',item.get('fields',{}))
        for name,counts in fields.items():
            missing=counts.get('missing_in_returned_rows',counts.get('missing',0)) or 0
            totals=metrics.setdefault(name,dict(valid=0,missing=0))
            totals['valid']+=counts.get('valid',0) or 0
            totals['missing']+=missing
            if missing and code is not None:
                fields_by_name=missing_by_index.setdefault(code,{})
                fields_by_name[name]=fields_by_name.get(name,0)+missing
    return dict(unique_indices=len(index_codes),metrics=metrics,indices_with_missing=[
        dict(index_code=code,fields=fields) for code,fields in sorted(missing_by_index.items())])


def publish_segment(db, batch_id, observations, identities, result, *, complete=True, coverage='unknown', check_cancelled=lambda: None, prices=(), compact_index_coverage=False):
    # Entire page set has already validated; bounded fact transactions follow.
    result=dict(result)
    for key,summary_key in (('anomalies','anomaly_summary'),('price_anomalies','price_anomaly_summary')):
        details=result.pop(key,None)
        if details is not None:
            by_reason={}
            for item in details:
                reason=item.get('reason','unknown') if isinstance(item,dict) else 'unknown'
                by_reason[reason]=by_reason.get(reason,0)+1
            result[summary_key]=dict(total=len(details),by_reason=by_reason,samples=details[:3])
    if compact_index_coverage and isinstance(result.get('coverage'),list):
        result['coverage_summary']=compact_coverage(result.pop('coverage'))
    counts=dict(inserted=0,revised=0,unchanged=0)
    if prices:
        price_result=price_coverage(prices)
        result={**result,('price_coverage_summary' if compact_index_coverage else 'price_coverage'):
                compact_coverage(price_result) if compact_index_coverage else price_result}
    price_counts=dict(inserted=0,revised=0,unchanged=0)
    groups={}
    for row in observations:
        groups.setdefault(row['observation_date'][:7],dict(valuations=[],prices=[]))['valuations'].append(row)
    for row in prices:
        groups.setdefault(row['observation_date'][:7],dict(valuations=[],prices=[]))['prices'].append(row)
    with db.connection(write=True) as conn:
        for item in identities:
            catalog(conn,**{k:v for k,v in item.items() if k!='observed_on'})
    for month,rows in sorted(groups.items()):
        check_cancelled()
        with db.connection(write=True) as conn:
            published=publish(conn,rows['valuations']) if rows['valuations'] else dict.fromkeys(counts,0)
            points=publish_prices(conn,rows['prices']) if rows['prices'] else dict.fromkeys(price_counts,0)
            for key in counts:
                counts[key]+=published[key]
                price_counts[key]+=points[key]
            conn.execute('UPDATE collection_batches SET result_json=? WHERE id=?',
                (encoded({**result,**counts,'price_counts':price_counts,'last_published_month':month}),batch_id))
    result={**result,**counts,'observations':len(observations),'price_counts':price_counts,'price_observations':len(prices)}
    if complete:
        finish_batch(db,batch_id,'complete',coverage,result=result)
    return result


def coverage_summary(observations, trading_days, start, end, *, expected_codes=None, monthly=False):
    groups={}
    for row in observations:
        key=(row['index_code'],row['series_key'])
        group=groups.setdefault(key,dict(index_code=key[0],series_key=key[1],dates=set(),metrics={}))
        group['dates'].add(row['observation_date'])
        for metric in DEFINITIONS[row['provider'],row['series_key']]['fields'].values():
            group['metrics'].setdefault(metric,set())
            if row.get(metric) is not None:
                group['metrics'][metric].add(row['observation_date'])
    expected={d for d in trading_days if start<=d<=end}
    summaries=[]
    for group in groups.values():
        dates=group.pop('dates')
        denominator=expected if expected_codes and group['index_code'] in expected_codes and not monthly else None
        summaries.append({**group,'first':min(dates),'last':max(dates),'observations':len(dates),
            'expected':len(denominator) if denominator is not None else None,
            'missing_dates':sorted(denominator-dates) if denominator is not None else None,
            'metrics':{metric:dict(valid=len(valid),missing_in_returned_rows=len(dates-valid),
                first=min(valid) if valid else None,last=max(valid) if valid else None,
                coverage=len(valid&denominator)/len(denominator) if denominator else None) for metric,valid in group['metrics'].items()}})
    # All selected known-supported CS codes must exist. SW historical universe unknown stays unknown.
    complete=bool(summaries) and bool(expected_codes) and not monthly and {s['index_code'] for s in summaries}==set(expected_codes) and all(
        s['missing_dates']==[] and all(m['missing_in_returned_rows']==0 for m in s['metrics'].values()) for s in summaries)
    return ('empty' if not summaries else 'complete' if complete else 'unknown'),summaries


class CsrfParser(HTMLParser):
    token=None
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='meta' and attrs.get('name')=='_csrf':
            self.token=attrs.get('content')


class OperationProgress:
    """Persist task-unit counts in the existing operation result JSON."""
    states=('success','skipped','failed','cancelled','pending','running')

    def __init__(self,db,operation_id):
        self.db=db;self.operation_id=operation_id
        self.update(lambda result: result.setdefault('work_progress',dict(version=1,phase='planning',unit='独立采集任务',tasks={})))

    @staticmethod
    def _counts(tasks):
        result={'total':len(tasks)}
        result.update({state:sum(1 for item in tasks.values() if item.get('status')==state) for state in OperationProgress.states})
        return result

    def update(self,mutate):
        with self.db.connection(write=True) as conn:
            row=conn.execute('SELECT result_json FROM index_collection_operations WHERE operation_id=?',(self.operation_id,)).fetchone()
            result=json.loads(row[0]) if row and row[0] else {}
            progress=result.setdefault('work_progress',dict(version=1,phase='planning',unit='独立采集任务',tasks={}))
            mutate(result)
            tasks=progress.setdefault('tasks',{})
            by_provider={}
            for item in tasks.values():by_provider.setdefault(item['provider'],{})[item.get('key','')]=item
            provider_counts={provider:self._counts(items) for provider,items in by_provider.items()}
            for provider,items in by_provider.items():
                active=next((item for item in reversed(list(items.values())) if item.get('status')=='running'),None)
                if active:provider_counts[provider]['current']=active.get('message') or active['label']
            progress.update(self._counts(tasks),providers=provider_counts)
            result['work_progress']=progress
            conn.execute('UPDATE index_collection_operations SET result_json=? WHERE operation_id=?',(encoded(result),self.operation_id))

    def register(self,provider,key,label):
        task_id=provider+':'+key
        def mutate(result):
            progress=result['work_progress'];tasks=progress.setdefault('tasks',{})
            tasks.setdefault(task_id,dict(provider=provider,key=key,label=label,status='pending'))
        self.update(mutate)
        return task_id

    def transition(self,task_id,status,message=None):
        if status not in self.states:raise ValueError('无效的采集任务状态')
        def mutate(result):
            progress=result['work_progress'];task=progress.setdefault('tasks',{}).get(task_id)
            if task is None:return
            task['status']=status
            if message:task['message']=str(message)[:300]
            if status=='running':
                progress['current']=dict(provider=task['provider'],label=task['label'],message=message or task['label'])
            elif progress.get('current',{}).get('provider')==task['provider'] and progress.get('current',{}).get('label')==task['label']:
                progress['current']=dict(provider=task['provider'],label=task['label'],message=message or task['label'])
        self.update(mutate)

    def set_phase(self,phase,status=None):
        def mutate(result):
            progress=result['work_progress'];progress['phase']=phase
            if status:progress['operation_status']=status
        self.update(mutate)

    def finish(self,status):
        def mutate(result):
            progress=result['work_progress'];progress['phase']='finished';progress['operation_status']=status
            if status=='cancelled':
                for task in progress.get('tasks',{}).values():
                    if task.get('status')=='pending':task.update(status='cancelled',message='任务停止前尚未开始')
        self.update(mutate)

    def update_message(self,provider,message):
        def mutate(result):
            progress=result['work_progress'];current=progress.get('current')
            if current and current.get('provider')==provider:
                current['message']=str(message)[:300]
                task=next((item for item in progress.get('tasks',{}).values()
                    if item.get('provider')==provider and item.get('status')=='running'),None)
                if task:task['message']=str(message)[:300]
        self.update(mutate)


SKIPPED_WORK=object()


def close_worker_sessions(workers):
    """Close only the independently-owned HTTP sessions created for workers."""
    for worker in workers:
        session=getattr(getattr(getattr(worker,'http',None),'session',None),'close',None)
        if callable(session):
            session()


class IndexCollector:
    def __init__(self, db, operation_id, *, request_collector=None, progress=lambda message: None, sw_page_size=500):
        self.db=db
        self.operation_id=operation_id
        self.progress=progress
        self.owns_http=request_collector is None
        if type(sw_page_size) is not int or not 1<=sw_page_size<=500:
            raise ValueError('申万分页容量须为1至500')
        self.sw_page_size=sw_page_size
        def cancelled():
            with db.connection() as conn:
                row=conn.execute('SELECT cancel_requested FROM index_collection_operations WHERE operation_id=?',(operation_id,)).fetchone()
            return bool(row and row[0])
        self.http=request_collector or RequestCollector(db,cancelled=cancelled,progress=progress)
        from .industry_sources import SWAdapter
        self.http.session.mount('https://www.swsresearch.com/',SWAdapter())
        self.trading_days=set()
        self.lg_token=None
        self.lg_headers=None
        self.task_progress=None

    def request(self, batch, provider, dataset, url, params=None, **kwargs):
        response,source_id=self.http.get(batch,provider,dataset,url,params,index_parallel=True,**kwargs)
        try:
            return json_data(response),source_id
        except Exception as exc:
            parsed_source(self.db,source_id,error=exc)
            raise

    def new_worker(self):
        worker=IndexCollector(self.db,self.operation_id,progress=self.progress,sw_page_size=self.sw_page_size)
        worker.trading_days=self.trading_days
        worker.auto_incremental=getattr(self,'auto_incremental',False)
        worker.task_progress=self.task_progress
        worker.http.session.cookies=deepcopy(self.http.session.cookies)
        return worker

    def register_work_unit(self,provider,key,label):
        tracker=getattr(self,'task_progress',None)
        return tracker.register(provider,key,label) if tracker else None

    def execute_work_unit(self,provider,key,label,work):
        tracker=getattr(self,'task_progress',None)
        task_id=tracker.register(provider,key,label) if tracker else None
        if tracker:tracker.transition(task_id,'running',label)
        try:result=work()
        except BaseException as exc:
            if tracker:tracker.transition(task_id,'cancelled' if isinstance(exc,Cancelled) else 'failed',
                                          '已停止' if isinstance(exc,Cancelled) else '本任务失败')
            raise
        if tracker:tracker.transition(task_id,'skipped' if result is SKIPPED_WORK else 'success',label)
        return result

    def plan_operation_work_units(self,start,end,mode,providers,legu_start):
        """Register the full denominator using local state before estimate requests start."""
        if not self.task_progress:return
        if 'csindex' in providers:
            candidates=self.accepted_cs_candidates() if mode not in ('full','backfill') else CS_CANDIDATES
            self.planned_cs_candidates=candidates
            codes=[*CS_CODES,*candidates]
            for code in dict.fromkeys(codes):self.register_work_unit('csindex','history:'+code,'中证指数历史 '+code)
            self.register_work_unit('csindex','dashboard','中证板块看板')
            self.register_work_unit('csindex','recent-xls','中证近期估值文件')
        if 'legu' in providers:
            for code,name in LG_CODES.items():
                for dataset in ('pe','pb'):
                    self.register_work_unit('legu',code+':'+dataset,f'乐咕 {name} {dataset.upper()}')
        if 'sws' in providers:
            starts=self.sw_incremental_starts(start,end) if getattr(self,'auto_incremental',False) else {1:start,2:start}
            if min(starts.values())<=end:
                for month_begin,finish in reversed(list(segments(min(starts.values()),end))):
                    if not any(month_begin<=d<=finish for d in self.trading_days):continue
                    for level,label in ((1,'一级行业'),(2,'二级行业')):
                        level_begin=max(month_begin,starts[level])
                        if level_begin<=finish:
                            key=f'{level}:{level_begin}:{finish}'
                            self.register_work_unit('sws',key,f'申万{label} {level_begin}—{finish}')

    @staticmethod
    def close_worker_sessions(workers):
        close_worker_sessions(workers)

    def close_open_batches(self,batch_ids,exc):
        status='cancelled' if isinstance(exc,Cancelled) else 'failed'
        with self.db.connection() as conn:
            opened={r['id'] for r in conn.execute('SELECT id FROM collection_batches WHERE status=\'running\' AND id IN ('+
                ','.join('?' for _ in batch_ids)+')',tuple(batch_ids))} if batch_ids else set()
        for batch_id in opened:finish_batch(self.db,batch_id,status,error=exc)

    def decode_calendar(self,text):
        from py_mini_racer import MiniRacer
        js=MiniRacer()
        js.eval((Path(__file__).parent/'resources/sina_trade_calendar.js').read_text(encoding='utf-8'))
        dates=js.call('d',text.split('=')[1].split(';')[0].replace('"',''))
        return {day(value) for value in dates}

    def load_calendar(self, *, cached=False):
        if cached:
            with self.db.connection() as conn:
                row=conn.execute("SELECT id FROM collection_sources WHERE dataset='index.calendar' "
                    "AND parse_status='complete' ORDER BY id DESC LIMIT 1").fetchone()
            if row:
                raw,_=read_source(self.db,row[0])
                dates=self.decode_calendar(raw.decode('utf-8'))
                today=datetime.now(ZoneInfo('Asia/Shanghai')).date()
                if len(dates)>=1000 and max(dates)>=today.isoformat():
                    self.trading_days=dates
                    return
        batch=begin_batch(self.db,self.operation_id,'index.calendar','sina')
        try:
            response,source=self.http.get(batch,'sina','index.calendar',CALENDAR,expect='text')
            # AKShare's vendored public decoder executes only its fixed source, not network JS.
            self.trading_days=self.decode_calendar(response.text)
            if len(self.trading_days)<1000:
                raise ValueError('交易日历响应不完整')
            parsed_source(self.db,source,fields=['trading_date'])
            finish_batch(self.db,batch,'complete','unknown',result=dict(count=len(self.trading_days),source_id=source))
        except Exception as exc:
            if 'source' in locals():
                parsed_source(self.db,source,error=exc)
            finish_batch(self.db,batch,'failed',error=exc,result=self.http.report())
            raise

    def collect_cs(self,start,end,mode):
        candidate_codes=getattr(self,'planned_cs_candidates',None)
        if candidate_codes is None:candidate_codes=self.accepted_cs_candidates() if mode not in ('full','backfill') else CS_CANDIDATES
        planned=[*CS_CODES]
        if mode not in ('full','backfill'):planned.extend(candidate_codes)
        elif mode in ('full','backfill'):planned.extend(CS_CANDIDATES)
        for code in planned:self.register_work_unit('csindex','history:'+code,'中证指数历史 '+code)
        self.register_work_unit('csindex','dashboard','中证板块看板')
        self.register_work_unit('csindex','recent-xls','中证近期估值文件')
        self.collect_cs_codes(start,end,mode,CS_CODES)
        if mode not in ('full','backfill'):
            self.collect_cs_codes(start,end,mode,candidate_codes)
        self.execute_work_unit('csindex','dashboard','中证板块看板',lambda:self.collect_board(start,end,mode))
        self.execute_work_unit('csindex','recent-xls','中证近期估值文件',lambda:self.collect_recent(start,end,mode))
        if mode in ('full','backfill'):
            # Candidate coverage remains unconfirmed; never treat a refusal as no data.
            self.collect_cs_codes(start,end,mode,CS_CANDIDATES)

    def accepted_cs_candidates(self):
        with self.db.connection() as conn:
            supported={r[0] for r in conn.execute("SELECT DISTINCT s.index_code FROM index_valuation_series s "
                "WHERE s.provider='csindex' AND s.series_key='history.rolling' AND EXISTS("
                "SELECT 1 FROM index_valuation_observations f WHERE f.series_id=s.id AND f.is_latest=1 AND f.pe_rolling IS NOT NULL)")}
        # Keep the curated non-theme universe; a directory entry alone is not evidence.
        return {code:name for code,name in CS_CANDIDATES.items() if canonical(code,'csindex') in supported}

    def incremental_start(self,baseline,end,provider,series_keys,index_code, *, new_only=False):
        """Overlap the last stored observation; keep each series' own watermark."""
        with self.db.connection() as conn:
            dates=[r[0] for r in conn.execute(
                'SELECT max(f.observation_date) FROM index_valuation_series s '
                'JOIN index_valuation_observations f ON f.series_id=s.id '
                'JOIN collection_batches b ON b.id=f.batch_id '
                'WHERE s.provider=? AND s.index_code=? AND s.series_key IN ('+
                ','.join('?' for _ in series_keys)+') AND f.is_latest=1 AND f.observation_date<=? '
                +("AND b.status='complete' " if new_only else '')+
                'GROUP BY s.series_key',(provider,index_code,*series_keys,end))]
        if new_only:
            if len(dates)<len(series_keys):
                return baseline
            last=min(dates)
            if provider=='legu' and last<end:
                # Monthly APIs return moving snapshots, with no remote date filter.
                return last[:7]+'-01'
            return min((d for d in self.trading_days if last<d<=end),default=(date.fromisoformat(end)+timedelta(days=1)).isoformat())
        last=min(dates,default=baseline)
        if provider=='legu':
            last=last[:7]+'-01'
        return min(baseline,last)

    def sw_incremental_starts(self,baseline,end):
        # Per-index watermarks would drag retired / source-gap series backwards
        # forever. SW requests publish all industries for each month and level.
        with self.db.connection() as conn:
            dates={r[0]:r[1] for r in conn.execute(
                'SELECT c.industry_level,max(f.observation_date) FROM index_valuation_observations f '
                'JOIN index_valuation_series s ON s.id=f.series_id '
                'JOIN index_catalog c ON c.index_code=s.index_code '
                'JOIN collection_batches b ON b.id=f.batch_id '
                "WHERE s.provider='sws' AND s.series_key='analysis.daily' "
                "AND b.status='complete' AND f.is_latest=1 AND f.observation_date<=? GROUP BY c.industry_level",(end,))}
        return {level:min((d for d in self.trading_days if last<d<=end),default=(date.fromisoformat(end)+timedelta(days=1)).isoformat())
                if (last:=dates.get(level)) else baseline for level in (1,2)}

    def collect_cs_codes(self,start,end,mode,codes):
        for code in codes:self.register_work_unit('csindex','history:'+code,'中证指数历史 '+code)
        def collect(code,worker):
            return worker.execute_work_unit('csindex','history:'+code,'中证指数历史 '+code,
                                            lambda:collect_one(code,worker))
        def collect_one(code,worker):
            code_start=worker.incremental_start(start,end,'csindex',['history.rolling'],canonical(code,'csindex'),new_only=True) if getattr(worker,'auto_incremental',False) else start
            worker.http.check_cancelled()
            if code_start>end:
                worker.progress('中证 '+code+' 日期已最新，跳过请求')
                return SKIPPED_WORK
            scope=dict(code=code)
            if worker.completed('index.cs.history','csindex',start,end,scope,mode):
                return SKIPPED_WORK
            batch=begin_batch(worker.db,worker.operation_id,'index.cs.history','csindex',code_start,end,scope=scope,frequency='daily',trigger=mode)
            source=None
            try:
                worker.progress('中证日频滚动 PE · '+code)
                raw,source=worker.request(batch,'csindex','index.cs.history',CS_HISTORY,
                    dict(indexCode=code,startDate=code_start.replace('-',''),endDate=end.replace('-','')))
                rows,identities,anomalies=parse_rows(raw,provider='csindex',series_keys=['history.rolling'],code_key='indexCode',
                    name_key='indexNameCn',date_key='tradeDate',start=code_start,end=end,trading_days=worker.trading_days,
                    batch_id=batch,source_id=source,expected_code=code)
                prices,price_anomalies=parse_prices(raw,provider='csindex',series_key='history.points',code_key='indexCode',
                    date_key='tradeDate',start=code_start,end=end,trading_days=worker.trading_days,batch_id=batch,source_id=source,expected_code=code)
                parsed_source(worker.db,source,fields=set().union(*(r.keys() for r in raw)) if raw else [])
                state,coverage=coverage_summary(rows,worker.trading_days,code_start,end,
                    expected_codes=[canonical(code,'csindex')] if code in CS_CODES else None)
                if state!='complete' and rows and code in CS_CODES:
                    state='partial'
                publish_segment(worker.db,batch,rows,identities,dict(coverage=coverage,anomalies=anomalies,price_anomalies=price_anomalies,**worker.http.report()),coverage=state,check_cancelled=worker.http.check_cancelled,prices=prices)
            except Exception as exc:
                worker.failed(batch,source,exc)
                raise
        if not self.owns_http or len(codes)<2:
            for code in codes:collect(code,self)
            return
        def fresh_worker():
            worker=IndexCollector(self.db,self.operation_id,progress=self.progress,sw_page_size=self.sw_page_size)
            worker.trading_days=self.trading_days
            worker.auto_incremental=getattr(self,'auto_incremental',False)
            worker.task_progress=self.task_progress
            return worker
        failures=[]
        workers=[]
        with ThreadPoolExecutor(max_workers=2,thread_name_prefix='index-csindex') as pool:
            jobs={code:fresh_worker() for code in codes}
            futures={pool.submit(collect,code,worker):(code,worker) for code,worker in jobs.items()}
            for future in as_completed(futures):
                try:future.result()
                except Exception as exc:failures.append(exc)
                workers.append(futures[future][1])
        for worker in workers:
            self.http.requests+=worker.http.requests
            self.http.retries+=worker.http.retries
            self.http.halted.update(worker.http.halted)
        self.close_worker_sessions(workers)
        if failures:raise failures[0]

    def collect_recent(self,start,end,mode):
        if getattr(self,'auto_incremental',False):
            start=self.incremental_start(start,end,'csindex',['indicator.total_shares','indicator.calculation_shares'],'cn:000300',new_only=True)
            if start>end:
                self.progress('中证近期估值日期已最新，跳过请求')
                return SKIPPED_WORK
        # Only the empirically verified CSI300 file enters this initial dataset.
        batch=begin_batch(self.db,self.operation_id,'index.cs.indicator','csindex',start,end,scope=dict(code='000300'),frequency='daily',trigger=mode)
        source=None
        try:
            response,source=self.http.get(batch,'csindex','index.cs.indicator',
                'https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/indicator/000300indicator.xls',expect='binary',index_parallel=True)
            import xlrd
            sheet=xlrd.open_workbook(file_contents=response.content).sheet_by_index(0)
            if sheet.ncols!=10 or not all(token in str(sheet.cell_value(0,col)) for token,col in (('P/E1',6),('P/E2',7),('D/P1',8),('D/P2',9))):
                raise ValueError('中证近期估值文件字段变化')
            keys=('date','code','full_name','name','full_name_en','name_en','pe1','pe2','dp1','dp2')
            records=[dict(zip(keys,sheet.row_values(i))) for i in range(1,sheet.nrows)]
            rows,identities,anomalies=parse_rows(records,provider='csindex',series_keys=['indicator.total_shares','indicator.calculation_shares'],
                code_key='code',name_key='name',date_key='date',start=start,end=end,trading_days=self.trading_days,batch_id=batch,source_id=source,expected_code='000300')
            parsed_source(self.db,source,fields=keys)
            publish_segment(self.db,batch,rows,identities,dict(history_supported=False,anomalies=anomalies,**self.http.report()),coverage='unknown' if rows else 'empty',check_cancelled=self.http.check_cancelled)
        except Exception as exc:
            self.failed(batch,source,exc)
            raise

    def collect_board(self,start,end,mode):
        if getattr(self,'auto_incremental',False):
            start=min(self.incremental_start(start,end,'csindex',['dashboard'],canonical(code,'csindex'),new_only=True) for code,name in BOARD_CODES.values())
            if start>end:
                self.progress('中证看板日期已最新，跳过请求')
                return SKIPPED_WORK
        batch=begin_batch(self.db,self.operation_id,'index.cs.dashboard','csindex',start,end,frequency='daily',trigger=mode)
        source=None
        try:
            raw,source=self.request(batch,'csindex','index.cs.dashboard',CS_BOARD)
            if not isinstance(raw,dict) or not isinstance(raw.get('indexValuations'),list):
                raise ValueError('中证最新板块结构变化')
            observed=day(raw['tradeDate'])
            records=[]
            for item in raw['indexValuations']:
                name=item.get('name') or item.get('indexName')
                if name not in BOARD_CODES:
                    raise ValueError('中证最新板块名称需重新核对映射')
                code,name=BOARD_CODES[name]
                records.append({**item,'indexCode':code,'indexNameCn':name,'tradeDate':observed})
            rows,identities,anomalies=parse_rows(records,provider='csindex',series_keys=['dashboard'],code_key='indexCode',
                name_key='indexNameCn',date_key='tradeDate',start=start,end=end,trading_days=self.trading_days,batch_id=batch,source_id=source)
            parsed_source(self.db,source,fields=set().union(*(r.keys() for r in records)))
            publish_segment(self.db,batch,rows,identities,dict(snapshot_date=observed,history_supported=False,anomalies=anomalies,**self.http.report()),coverage='unknown' if rows else 'empty',check_cancelled=self.http.check_cancelled)
        except Exception as exc:
            self.failed(batch,source,exc)
            raise

    def setup_legu(self,batch):
        if self.lg_headers is not None:
            return
        response,source=self.http.get(batch,'legu','index.legu.cookie','https://legulegu.com/stockdata/sz50-ttm-lyr',expect='html',index_parallel=True)
        parser=CsrfParser()
        parser.feed(response.text)
        if not parser.token:
            parsed_source(self.db,source,error='缺少 CSRF meta')
            raise ValueError('乐咕初始化结构变化')
        parsed_source(self.db,source,fields=['meta:_csrf'])
        self.lg_headers={'X-CSRF-Token':parser.token}
        # Matches the provider's public date token (verified against AKShare's hex JS).
        self.lg_token=hashlib.md5(datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat().encode()).hexdigest()

    def collect_legu(self,start,end,mode):
        jobs=[]
        for code,name in LG_CODES.items():
            for dataset in ('pe','pb'):
                task_key=code+':'+dataset;label=f'乐咕 {name} {dataset.upper()}'
                self.register_work_unit('legu',task_key,label)
                dataset_start=self.incremental_start(start,end,'legu',[dataset+'.'+m for m in ('ordinary','equal','median')],canonical(code,'legu'),new_only=True) if getattr(self,'auto_incremental',False) else start
                try:self.http.check_cancelled()
                except Exception as exc:
                    self.close_open_batches([item[4] for item in jobs],exc)
                    raise
                if dataset_start>end:
                    self.progress('乐咕 '+code+' '+dataset+' 日期已最新，跳过请求')
                    if self.task_progress:self.task_progress.transition('legu:'+task_key,'skipped','日期已最新')
                    continue
                scope=dict(code=code);full_dataset='index.legu.'+dataset
                if self.completed(full_dataset,'legu',start,end,scope,mode):
                    if self.task_progress:self.task_progress.transition('legu:'+task_key,'skipped','该分段已完成')
                    continue
                batch=begin_batch(self.db,self.operation_id,full_dataset,'legu',dataset_start,end,scope=scope,frequency='monthly',trigger=mode)
                jobs.append((code,name,dataset,dataset_start,batch,task_key,label))
        if not jobs:return
        # Fetch the daily CSRF token once, then give each independent data task its own Session.
        try:self.setup_legu(jobs[0][4])
        except Exception as exc:
            self.close_open_batches([item[4] for item in jobs],exc)
            if self.task_progress:
                for item in jobs:self.task_progress.transition('legu:'+item[5],
                    'cancelled' if isinstance(exc,Cancelled) else 'failed',
                    '已停止' if isinstance(exc,Cancelled) else '初始化失败')
            raise
        headers,token=self.lg_headers,self.lg_token
        def collect(item,worker):
            code,name,dataset,dataset_start,batch,task_key,label=item
            worker.lg_headers=dict(headers);worker.lg_token=token
            return worker.execute_work_unit('legu',task_key,label,
                lambda:worker._collect_legu_item(code,name,dataset,dataset_start,end,batch))
        if not self.owns_http or len(jobs)<2:
            try:
                for item in jobs:collect(item,self)
            except Exception as exc:
                self.close_open_batches([item[4] for item in jobs],exc)
                raise
            return
        workers=[];failures=[]
        with ThreadPoolExecutor(max_workers=2,thread_name_prefix='index-legu') as pool:
            tasks={}
            for item in jobs:
                worker=self.new_worker()
                tasks[pool.submit(collect,item,worker)]=worker
            for future in as_completed(tasks):
                try:future.result()
                except Exception as exc:failures.append(exc)
                workers.append(tasks[future])
        # Keep all request/denial statistics even when one item fails.
        for worker in workers:
            self.http.requests+=worker.http.requests
            self.http.retries+=worker.http.retries
            self.http.halted.update(worker.http.halted)
        self.close_worker_sessions(workers)
        if failures:raise failures[0]

    def _collect_legu_item(self,code,name,dataset,dataset_start,end,batch):
        full_dataset='index.legu.'+dataset;source=None
        try:
            self.progress(f'乐咕月频 {dataset.upper()} · {name}')
            raw,source=self.request(batch,'legu',full_dataset,'https://legulegu.com/api/stockdata/index-basic-'+dataset,
                dict(token=self.lg_token,indexCode=code),headers=self.lg_headers)
            if not isinstance(raw,list):raise ValueError('乐咕缺少数据列表')
            if any('indexCode' in r and r['indexCode']!=code for r in raw):raise ValueError('乐咕返回其他指数')
            records=[{**r,'_code':code,'_name':name} for r in raw]
            rows,identities,anomalies=parse_rows(records,provider='legu',series_keys=[dataset+'.'+method for method in ('ordinary','equal','median')],
                code_key='_code',name_key='_name',date_key='date',start=dataset_start,end=end,trading_days=self.trading_days,batch_id=batch,source_id=source)
            prices,price_anomalies=parse_prices(records,provider='legu',series_key=dataset+'.points',code_key='_code',
                date_key='date',start=dataset_start,end=end,trading_days=self.trading_days,batch_id=batch,source_id=source,expected_code=code)
            parsed_source(self.db,source,fields=set().union(*(r.keys() for r in raw)) if raw else [])
            state,coverage=coverage_summary(rows,self.trading_days,dataset_start,end,monthly=True)
            publish_segment(self.db,batch,rows,identities,dict(coverage=coverage,anomalies=anomalies,price_anomalies=price_anomalies,**self.http.report()),coverage=state,check_cancelled=self.http.check_cancelled,prices=prices)
        except Exception as exc:
            self.failed(batch,source,exc);raise

    def collect_sw(self,start,end,mode):
        # Latest month first establishes current navigation; old names cannot replace it.
        starts=self.sw_incremental_starts(start,end) if getattr(self,'auto_incremental',False) else {1:start,2:start}
        if min(starts.values())>end:
            self.progress('申万一、二级日期已最新，跳过请求')
            return
        start=min(starts.values())
        ranges={1:[],2:[]}
        for month_begin,finish in reversed(list(segments(start,end))):
            if not any(month_begin<=d<=finish for d in self.trading_days):
                continue
            for level,label in ((1,'一级行业'),(2,'二级行业')):
                level_begin=max(month_begin,starts[level])
                if level_begin>finish:
                    continue
                self.http.check_cancelled()
                scope=dict(level=level)
                task_key=f'{level}:{level_begin}:{finish}'
                self.register_work_unit('sws',task_key,f'申万{label} {level_begin}—{finish}')
                if self.completed('index.sw.analysis','sws',level_begin,finish,scope,mode):
                    if self.task_progress:self.task_progress.transition('sws:'+task_key,'skipped','该分段已完成')
                    continue
                ranges[level].append((level_begin,finish,level,label,mode,task_key))
        jobs=[(level,label,items) for level,label in ((1,'一级行业'),(2,'二级行业')) if (items:=ranges[level])]
        if not jobs:return
        def collect(job,worker):
            level,label,items=job
            for item in items:
                begin,finish,level,label,mode,task_key=item
                worker.execute_work_unit('sws',task_key,f'申万{label} {begin}—{finish}',
                    lambda:worker._collect_sw_task(begin,finish,level,label,mode))
        if not self.owns_http or len(jobs)<2:
            for job in jobs:collect(job,self)
            return
        workers=[];failures=[]
        with ThreadPoolExecutor(max_workers=2,thread_name_prefix='index-sws') as pool:
            tasks={}
            for job in jobs:
                worker=self.new_worker()
                tasks[pool.submit(collect,job,worker)]=worker
            for future in as_completed(tasks):
                try:future.result()
                except Exception as exc:failures.append(exc)
                workers.append(tasks[future])
        for worker in workers:
            self.http.requests+=worker.http.requests
            self.http.retries+=worker.http.retries
            self.http.halted.update(worker.http.halted)
        self.close_worker_sessions(workers)
        if failures:raise failures[0]

    def _collect_sw_task(self,begin,finish,level,label,mode):
        self.http.check_cancelled()
        batch=begin_batch(self.db,self.operation_id,'index.sw.analysis','sws',begin,finish,
            scope=dict(level=level),frequency='daily',trigger=mode)
        sources=[]
        try:
            self.progress(f'申万{label} · {begin}—{finish}')
            all_rows=[];all_prices=[];price_anomalies=[];identities={};anomalies=[]
            page=1;total=None;returned=0;seen=set()
            while total is None or returned<total:
                params=dict(page=page,page_size=self.sw_page_size,index_type=label,start_date=begin,end_date=finish,type='DAY',swindexcode='all')
                raw,source=self.request(batch,'sws','index.sw.analysis',SW_ANALYSIS,params)
                sources.append(source)
                if not isinstance(raw,dict) or not isinstance(raw.get('results'),list) or type(raw.get('count')) is not int or raw['count']<0:
                    raise ValueError('申万分页结构变化')
                if total is None:total=raw['count']
                if raw['count']!=total or len(raw['results'])>self.sw_page_size:
                    raise ValueError('申万分页总数变化或超出页面大小')
                if len(raw['results'])!=min(self.sw_page_size,total-returned):
                    raise ValueError('申万分页截断／空页')
                if total==0:
                    parsed_source(self.db,source,fields=[]);break
                rows,cats,bad=parse_rows(raw['results'],provider='sws',series_keys=['analysis.daily'],
                    code_key='swindexcode',name_key='swindexname',date_key='bargaindate',start=begin,end=finish,
                    trading_days=self.trading_days,batch_id=batch,source_id=source,level=level)
                prices,price_bad=parse_prices(raw['results'],provider='sws',series_key='analysis.points',code_key='swindexcode',
                    date_key='bargaindate',start=begin,end=finish,trading_days=self.trading_days,batch_id=batch,source_id=source)
                all_prices+=prices;price_anomalies+=price_bad
                for row in raw['results']:
                    key=(str(row['swindexcode']),day(row['bargaindate']))
                    if key in seen:raise ValueError('申万跨页重复记录')
                    seen.add(key)
                for item in cats:
                    if item['code'] not in identities or item['observed_on']>identities[item['code']]['observed_on']:
                        identities[item['code']]=item
                all_rows+=rows;anomalies+=bad;returned+=len(raw['results'])
                parsed_source(self.db,source,fields=set().union(*(r.keys() for r in raw['results'])))
                page+=1
            state,coverage=coverage_summary(all_rows,self.trading_days,begin,finish)
            publish_segment(self.db,batch,all_rows,list(identities.values()),dict(coverage=coverage,anomalies=anomalies,
                returned=returned,reported_total=total,price_anomalies=price_anomalies,classification_history='pending',**self.http.report()),coverage=state,check_cancelled=self.http.check_cancelled,prices=all_prices,compact_index_coverage=True)
        except Exception as exc:
            self.failed(batch,sources[-1] if sources else None,exc)
            raise

    def failed(self,batch,source,exc):
        if source is not None:
            with self.db.connection() as conn:
                parse_state=conn.execute('SELECT parse_status FROM collection_sources WHERE id=?',(source,)).fetchone()[0]
            if parse_state=='pending':
                parsed_source(self.db,source,error=exc)
        with self.db.connection() as conn:
            row=conn.execute('SELECT result_json FROM collection_batches WHERE id=?',(batch,)).fetchone()
        previous=json.loads(row[0])
        finish_batch(self.db,batch,'cancelled' if isinstance(exc,Cancelled) else 'failed',
                     'partial' if previous.get('inserted',0)+previous.get('unchanged',0)>0 else 'unknown',
                     error=exc,result={**previous,**self.http.report()})

    def completed(self,dataset,provider,start,end,scope,mode):
        if mode not in ('full','backfill'):
            return False
        with self.db.connection() as conn:
            row=conn.execute("SELECT id FROM collection_batches WHERE dataset=? AND source_system=? AND scope_start=? AND scope_end=? "
                "AND scope_json=? AND status='complete' AND coverage_status NOT IN ('partial','empty') LIMIT 1",
                (dataset,provider,start,end,encoded(scope))).fetchone()
        return row is not None


def start_operation(db,mode='full',providers=('csindex','legu','sws'),start=None,end=None):
    if mode not in ('full','incremental','backfill','audit','review') or not isinstance(providers,(list,tuple)) or not providers or not set(providers)<= {'csindex','legu','sws'}:
        raise ValueError('指数采集模式／来源无效')
    if start:
        day(start)
    if end:
        day(end)
    if start and end and start>end:
        raise ValueError('采集日期范围倒置')
    operation=uuid4().hex
    with db.connection(write=True) as conn:
        if conn.execute("SELECT 1 FROM index_collection_operations WHERE status='running'").fetchone():
            raise ValueError('已有指数任务正在运行，请先等待或取消')
        conn.execute("INSERT INTO index_collection_operations(operation_id,mode,scope_json,status,started_at) VALUES(?,?,?,'running',?)",
            (operation,mode,encoded(dict(providers=list(dict.fromkeys(providers)),start=start,end=end)),utc_now()))
    return operation


def run_operation(db,operation,*,collector=None):
    tracker=OperationProgress(db,operation)
    def progress(message,provider=None):
        with db.connection(write=True) as conn:
            current=conn.execute('SELECT result_json FROM index_collection_operations WHERE operation_id=?',(operation,)).fetchone()
            result=json.loads(current[0])
            result['message']=message
            if provider:
                result.setdefault('providers',{})[provider]=message
            conn.execute('UPDATE index_collection_operations SET result_json=? WHERE operation_id=?',(encoded(result),operation))
        if provider:tracker.update_message(provider,message)
        print(message,flush=True)
    with db.connection() as conn:
        row=dict(conn.execute('SELECT * FROM index_collection_operations WHERE operation_id=?',(operation,)).fetchone())
    scope=json.loads(row['scope_json'])
    injected_collector=collector is not None
    collector=collector or IndexCollector(db,operation,progress=progress)
    try:collector.task_progress=tracker
    except (AttributeError,TypeError):pass
    errors={}
    status='complete'
    try:
        with instance_lock(db.path.parent/'collection_sources'/'index-operation'):
            collector.load_calendar(cached=row['mode'] in ('incremental','review'))
            today=datetime.now(ZoneInfo('Asia/Shanghai')).date()
            # Never query the still-open current trading day as a completed observation.
            eligible=[d for d in collector.trading_days if d<today.isoformat()]
            if not eligible:
                raise ValueError('交易日历没有已完成交易日')
            end=min(scope['end'] or max(eligible),max(eligible))
            start=scope['start'] or rolling_start(today).isoformat()
            if row['mode'] in ('incremental','review') and not scope['start']:
                start=sorted(d for d in collector.trading_days if d<=end)[-10]
            collector.auto_incremental=row['mode']=='incremental' and not scope['start']
            if row['mode']=='audit' and not scope['start']:
                # Rotate one older month per manual monthly audit; quarter-wide overview is read-only.
                candidates=list(segments(start,end))[:-4]
                with db.connection() as conn:
                    audit_number=conn.execute("SELECT count(*) FROM index_collection_operations WHERE mode='audit' AND operation_id<>? AND status='complete'",(operation,)).fetchone()[0]
                if candidates:
                    start,end=candidates[audit_number%len(candidates)]
            if start>end:
                raise ValueError('滚动窗口没有已完成交易日')
            progress(f'指数采集范围 {start}—{end}；主题指数未纳入')
            legu_plan_start=start
            if row['mode'] in ('incremental','review') and not scope['start']:
                current=date.fromisoformat(end).replace(day=1)
                for _ in range(3):current=(current-timedelta(days=1)).replace(day=1)
                legu_plan_start=current.isoformat()
            planner=getattr(collector,'plan_operation_work_units',None)
            if callable(planner):planner(start,end,row['mode'],scope['providers'],legu_plan_start)
            tracker.set_phase('running')
            def run_provider(provider,worker):
                if provider=='csindex':
                    worker.collect_cs(start,end,row['mode'])
                elif provider=='legu':
                    lg_start=start
                    if row['mode'] in ('incremental','review') and not scope['start']:
                        current=date.fromisoformat(end).replace(day=1)
                        for _ in range(3):
                            current=(current-timedelta(days=1)).replace(day=1)
                        lg_start=current.isoformat()
                    worker.collect_legu(lg_start,end,row['mode'])
                else:
                    worker.collect_sw(start,end,row['mode'])
                return worker
            workers=[]
            if injected_collector:
                # Keep custom collectors deterministic for tests and callers that inject a session.
                jobs=[(provider,collector) for provider in scope['providers']]
                for provider,worker in jobs:
                    try:
                        run_provider(provider,worker)
                    except Cancelled:
                        raise
                    except Exception as exc:
                        errors[provider]=str(exc)
                        progress(f'{provider} 停止：{exc}；其他来源独立继续')
            else:
                def worker_for(provider):
                    worker=IndexCollector(db,operation,progress=lambda message,p=provider:progress(message,p))
                    worker.trading_days=collector.trading_days
                    worker.auto_incremental=collector.auto_incremental
                    try:worker.task_progress=tracker
                    except (AttributeError,TypeError):pass
                    if hasattr(collector,'planned_cs_candidates'):
                        worker.planned_cs_candidates=collector.planned_cs_candidates
                    return worker
                cancelled=False
                with ThreadPoolExecutor(max_workers=max(1,len(scope['providers'])),thread_name_prefix='index-provider') as pool:
                    jobs={provider:worker_for(provider) for provider in scope['providers']}
                    futures={pool.submit(run_provider,provider,worker):(provider,worker) for provider,worker in jobs.items()}
                    for future in as_completed(futures):
                        provider,worker=futures[future]
                        try:
                            future.result()
                        except Cancelled:
                            cancelled=True
                        except Exception as exc:
                            errors[provider]=str(exc)
                            progress(f'{provider} 停止：{exc}；其他来源独立继续')
                        workers.append(worker)
                for worker in workers:
                    collector.http.requests+=worker.http.requests
                    collector.http.retries+=worker.http.retries
                    collector.http.halted.update(worker.http.halted)
                close_worker_sessions(workers)
                if cancelled:
                    raise Cancelled('用户取消采集；已发布分段保留')
            with db.connection() as conn:
                failed=conn.execute("SELECT count(*) FROM collection_batches WHERE operation_id=? AND status='failed'",(operation,)).fetchone()[0]
            if errors or failed:
                status='failed'
    except Cancelled as exc:
        status='cancelled'
        errors['operation']=str(exc)
    except Exception as exc:
        status='failed'
        errors['operation']=str(exc)
    tracker.finish(status)
    with db.connection(write=True) as conn:
        current=conn.execute('SELECT result_json FROM index_collection_operations WHERE operation_id=?',(operation,)).fetchone()
        progress_result=json.loads(current[0]) if current and current[0] else {}
        totals=[dict(r) for r in conn.execute('SELECT status,coverage_status,count(*) AS batches FROM collection_batches WHERE operation_id=? '
                                            'GROUP BY status,coverage_status',(operation,))]
        valuation_batches=conn.execute("SELECT count(*) FROM collection_batches WHERE operation_id=? AND dataset<>'index.calendar'",(operation,)).fetchone()[0]
        conn.execute('UPDATE index_collection_operations SET status=?,finished_at=?,result_json=?,error=? WHERE operation_id=?',
            (status,utc_now(),encoded(dict(message='日期已最新，未请求估值接口' if status=='complete' and row['mode']=='incremental' and not valuation_batches else '指数采集'+status,
                providers=progress_result.get('providers',{}),work_progress=progress_result.get('work_progress'),
                batches=totals,errors=errors,**collector.http.report())),
             encoded(errors) if errors else None,operation))
    report=db.path.parent/'verification'/'reports'/('index-collection-'+operation+'.json')
    with db.connection() as conn:
        payload=dict(conn.execute('SELECT * FROM index_collection_operations WHERE operation_id=?',(operation,)).fetchone())
    atomic_write(report,encoded(payload).encode())
    return payload


def cancel_operation(db):
    with db.connection(write=True) as conn:
        conn.execute("UPDATE index_collection_operations SET cancel_requested=1 WHERE status='running'")


def recover_operations(db):
    # Startup recovery only: a CLI collector may also be alive, so inspect the same OS lock.
    try:
        with instance_lock(db.path.parent/'collection_sources'/'index-operation'):
            with db.connection(write=True) as conn:
                operations=[r[0] for r in conn.execute("SELECT operation_id FROM index_collection_operations WHERE status='running'")]
                for operation in operations:
                    conn.execute("UPDATE index_collection_operations SET status='failed',finished_at=?,error='后台重启，分段保留；可恢复全量' WHERE operation_id=?",(utc_now(),operation))
                    conn.execute("UPDATE collection_batches SET status='failed',finished_at=?,error='采集中断，已归档来源保留' WHERE operation_id=? AND status='running'",(utc_now(),operation))
    except Exception as exc:
        from .storage import StorageError
        if not isinstance(exc,StorageError):
            raise


def main():
    parser=argparse.ArgumentParser(description='指数估值采集；不迁移旧采集，不采主题指数')
    parser.add_argument('--database',type=Path,default=DEFAULT_DATABASE)
    parser.add_argument('--mode',choices=['full','incremental','backfill','audit','review'],default='full')
    parser.add_argument('--providers',default='csindex,legu,sws')
    parser.add_argument('--start')
    parser.add_argument('--end')
    args=parser.parse_args()
    db=Database(args.database)
    db.check()
    recover_operations(db)
    operation=start_operation(db,args.mode,args.providers.split(','),args.start,args.end)
    print('operation_id='+operation,flush=True)
    result=run_operation(db,operation)
    print(encoded(result),flush=True)


if __name__=='__main__':
    main()
