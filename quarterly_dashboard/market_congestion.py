"""Legu history initialization and HTML-only daily checks; reads never collect."""
from __future__ import annotations
from contextlib import nullcontext

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
import math
import re
import sqlite3
from threading import Event, Thread
from uuid import uuid4
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from .collection import (Cancelled, PacingPolicy, RequestCollector, RECOVERY_PENDING,
                         begin_batch, digest, encoded, finish_batch, parsed_source,
                         read_source, reusable_source)
from .storage import instance_lock, utc_now
from .collection_schedule import latest_daily_date

ZONE = ZoneInfo('Asia/Shanghai')
TASK = 'market_congestion'
PAGE = 'https://legulegu.com/stockdata/ashares-congestion'
API = 'https://legulegu.com/api/stockdata/ashares-congestion'
DATASETS = {'api_history':'market.legu.congestion', 'html_recent_table':'market.legu.congestion.page'}
CALCULATED_CHANNEL = 'bigquant_calculated'
CALCULATED_DATASET = 'market.congestion.bigquant_calculated'
CALCULATED_IDENTITY = ('ashares_congestion','all_a','bigquant',CALCULATED_CHANNEL,'daily','amount_rank_top5_v1')
IDENTITY_FIELDS = ('indicator_key','scope_key','provider','channel','frequency','definition_version')
VALUE_FIELDS = ('value','close','top5_amount_yi','total_amount_yi')
DEFINITIONS = {('ashares_congestion','all_a','legu',channel,'daily','v1'):dict(
    name='大盘拥挤度', unit='ratio', minimum=0, maximum=1,
    formula='成交额排名前5%个股成交额 / 全部A股成交额；不含大宗交易',
    close_basis='来源未明确标注基准身份', amount_unit='亿元') for channel in DATASETS}
DEFINITIONS[CALCULATED_IDENTITY] = dict(name='大盘拥挤度（BigQuant离线补值）',unit='ratio',minimum=0,maximum=1,
    formula='BigQuant当日非空amount证券，按成交额降序取ceil(N/20)只，前K成交额/有效成交额总额',
    close_basis='本地中证上证指数日频同日收盘点数',amount_unit='亿元',
    limitation='独立自算口径；空成交额排除并留证据，不保证与Legu完全一致')


def local_now():
    return datetime.now(ZONE)


def identity(channel):
    if channel==CALCULATED_CHANNEL:
        return CALCULATED_IDENTITY
    return ('ashares_congestion','all_a','legu',channel,'daily','v1')


def day(value):
    if not isinstance(value,str) or date.fromisoformat(value).isoformat()!=value:
        raise ValueError('观察日期须为ISO日期')
    if value>local_now().date().isoformat():
        raise ValueError('来源观察日期在未来')
    return value


def numeric(value):
    if value is None or value in ('','—','--','-'):
        return None
    if isinstance(value,bool):
        raise ValueError('布尔值不是指标数值')
    try:
        result=float(Decimal(str(value).replace(',','').strip()))
    except (InvalidOperation,ValueError,TypeError):
        raise ValueError('无效的指标数值') from None
    if not math.isfinite(result):
        raise ValueError('指标数值必须有限')
    return result


def validate_rows(rows, ident):
    definition=DEFINITIONS.get(ident)
    if definition is None:
        raise ValueError('未登记的指标定义')
    seen=set()
    for row in rows:
        observed=day(row['observation_date'])
        if observed in seen:
            raise ValueError('来源有重复观察日期')
        seen.add(observed)
        for key in VALUE_FIELDS:
            row[key]=numeric(row.get(key))
        value=row['value']
        if value is not None and not definition['minimum']<=value<=definition['maximum']:
            raise ValueError('拥挤度超出定义范围')
        if row['close'] is not None and row['close']<=0:
            raise ValueError('收盘点位须为正数')
        for key in ('top5_amount_yi','total_amount_yi'):
            if row[key] is not None and row[key]<0:
                raise ValueError('成交额不能为负数')
        top,total=row['top5_amount_yi'],row['total_amount_yi']
        if top is not None and total is not None and top>total+0.02:
            raise ValueError('前5%成交额大于全A成交额')
        if value is not None and top is not None and total and abs(top/total-value)>0.00015:
            raise ValueError('拥挤度与成交额比例不一致')
        row['value_status']='observed' if value is not None else 'source_missing'
    if not rows:
        raise ValueError('来源未返回任何日期')
    return sorted(rows,key=lambda row:row['observation_date'])


def parse_api(payload):
    if not isinstance(payload,dict) or not isinstance(payload.get('items'),list):
        raise ValueError('拥挤度API缺少items数组')
    rows=[]
    for item in payload['items']:
        if not isinstance(item,dict) or 'date' not in item:
            raise ValueError('API记录缺少日期')
        value=numeric(item.get('congestion'))
        percent=numeric(item.get('congestionPercent'))
        if value is None and percent is not None:
            value=float(Decimal(str(percent))/100)
        elif value is not None and percent is not None and abs(value*100-percent)>0.00001:
            raise ValueError('API比例和百分数字段矛盾')
        rows.append(dict(observation_date=item['date'],value=value,close=item.get('close'),
                         top5_amount_yi=item.get('top5AmountYi'),total_amount_yi=item.get('totalAmountYi'),precision_digits=4))
    rows=validate_rows(rows,identity('api_history'))
    if not any(row['value'] is not None for row in rows):
        raise ValueError('API没有有效历史值')
    return rows


def parse_html(text):
    soup=BeautifulSoup(text,'html.parser')
    table=soup.select_one('table#tableID')
    if table is None:
        raise ValueError('页面缺少近期数据表格')
    headings=[re.sub(r'\s+','',th.get_text()).replace('（','(').replace('）',')') for th in table.select('th')]
    expected=('日期','收盘价','成交额排名前5%的个股的成交额(亿元)','全部A股成交额(亿元)','拥挤度(%)')
    if any(headings.count(key)!=1 for key in expected):
        raise ValueError('近期表格表头或单位变化')
    positions=[headings.index(key) for key in expected]
    rows=[]
    for tr in table.select('tr'):
        cells=[td.get_text(' ',strip=True) for td in tr.select('td')]
        if not cells:
            continue
        if len(cells)!=len(headings):
            raise ValueError('近期表格列数不完整')
        observed,close,top,total,percent=[cells[i] for i in positions]
        value=numeric(percent)
        rows.append(dict(observation_date=observed,close=close,top5_amount_yi=top,total_amount_yi=total,
                         value=float(Decimal(str(value))/100) if value is not None else None,precision_digits=4))
    rows=validate_rows(rows,identity('html_recent_table'))
    if not any(row['value'] is not None for row in rows):
        raise ValueError('近期表格没有有效拥挤度')
    summaries=set()
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            obj=json.loads(script.string or script.get_text())
        except ValueError:
            continue
        if not isinstance(obj,dict):
            continue
        for item in obj.get('variableMeasured',[]):
            description=item.get('description','') if isinstance(item,dict) else ''
            match=re.search(r'(\d{4}-\d{2}-\d{2})[，,\s]*大盘拥挤度[：:]\s*([\d.]+)%',description)
            if match:
                summaries.add((match[1],float(Decimal(match[2])/100)))
    if len(summaries)>1:
        raise ValueError('页面摘要互相矛盾')
    if summaries:
        observed,value=next(iter(summaries))
        latest=rows[-1]
        if observed!=latest['observation_date'] or latest['value'] is None or abs(latest['value']-value)>0.00005:
            raise ValueError('页面摘要与表格最新值矛盾')
    return rows


def publish(conn, rows, channel, batch, source):
    """Caller owns the short transaction; source must have passed parsing."""
    ident=identity(channel)
    rows=validate_rows([dict(row) for row in rows],ident)
    ref=conn.execute('SELECT s.*,b.status FROM collection_sources s JOIN collection_batches b ON b.id=s.batch_id '
                     'WHERE s.id=? AND s.batch_id=?',(source,batch)).fetchone()
    dataset=CALCULATED_DATASET if channel==CALCULATED_CHANNEL else DATASETS[channel]
    if not ref or ref['source_system']!=ident[2] or ref['dataset']!=dataset or ref['parse_status']!='complete' or ref['status']!='running':
        raise ValueError('事实来源、提供方或批次未通过校验')
    counts=dict(inserted=0,revised=0,unchanged=0,withheld_dates=[])
    where=' AND '.join(key+'=?' for key in IDENTITY_FIELDS)
    for row in rows:
        old=conn.execute('SELECT * FROM market_indicator_observations WHERE '+where+
                         ' AND observation_date=? AND is_latest=1',(*ident,row['observation_date'])).fetchone()
        if old and any(old[key] is not None and row[key] is None for key in VALUE_FIELDS):
            counts['withheld_dates'].append(row['observation_date'])
            continue  # Permission/window changes are not confirmed withdrawals.
        content={key:row[key] for key in (*VALUE_FIELDS,'value_status','precision_digits')}
        hashed=digest(encoded(dict(identity=ident,**content)).encode())
        if old and old['content_hash']==hashed:
            counts['unchanged']+=1
            continue
        if old:
            conn.execute('UPDATE market_indicator_observations SET is_latest=0 WHERE id=?',(old['id'],))
        record=dict(zip(IDENTITY_FIELDS,ident),observation_date=row['observation_date'],**content,
                    version=old['version']+1 if old else 1,is_latest=1,content_hash=hashed,
                    source_id=source,batch_id=batch,obtained_at=ref['obtained_at'],updated_at=utc_now())
        conn.execute('INSERT INTO market_indicator_observations('+','.join(record)+') VALUES ('+
                     ','.join('?' for _ in record)+')',tuple(record.values()))
        counts['revised' if old else 'inserted']+=1
    return counts


def initialized(conn):
    # Both steps must have committed; placeholders alone do not initialize history.
    return all(conn.execute('SELECT 1 FROM market_indicator_observations WHERE indicator_key=? AND scope_key=? '
                            'AND provider=? AND channel=? AND frequency=? AND definition_version=? '
                            'AND is_latest=1 AND value IS NOT NULL LIMIT 1',identity(ch)).fetchone() for ch in DATASETS)


def prepare_index_dependency(db, operation_id, target_date, cancelled=lambda:False):
    """One metadata-only HTML check inside an existing index operation's lock."""
    from .collection import begin_batch, finish_batch, parsed_source
    with db.connection() as conn:
        existing=conn.execute("SELECT total_amount_yi FROM market_indicator_observations WHERE indicator_key='ashares_congestion' "
            "AND scope_key='all_a' AND provider='legu' AND definition_version='v1' AND observation_date=? AND is_latest=1 "
            "AND channel IN ('html_recent_table','api_history') ORDER BY CASE channel WHEN 'html_recent_table' THEN 0 ELSE 1 END",
            (target_date,)).fetchall()
        if any(r['total_amount_yi'] is not None and math.isfinite(float(r['total_amount_yi'])) and r['total_amount_yi']>0 for r in existing):
            return {'status':'reused','target_date':target_date,'network_requests':0}
        if not initialized(conn):
            return {'status':'not_initialized','target_date':target_date,'network_requests':0}
        schedule=conn.execute('SELECT enabled FROM collection_schedules WHERE task_key=?',(TASK,)).fetchone()
        if schedule and not schedule['enabled']:
            return {'status':'paused','target_date':target_date,'network_requests':0}
        schedule_row=conn.execute('SELECT local_time FROM collection_schedules WHERE task_key=?',(TASK,)).fetchone()
        threshold=schedule_row['local_time'] if schedule_row else '18:00'
        now_local=local_now()
        checks=conn.execute("SELECT b.started_at,b.trigger_type FROM collection_batches b WHERE b.dataset=? AND b.source_system='legu' "
            "AND b.status IN ('complete','failed','cancelled') AND b.trigger_type IN ('manual','scheduled','index_dependency') "
            "AND substr(datetime(b.started_at,'+8 hours'),1,10)=? "
            "AND (b.trigger_type<>'index_dependency' OR json_extract(b.scope_json,'$.purpose')='index_derivation_dependency') "
            "ORDER BY b.id DESC",(DATASETS['html_recent_table'],now_local.date().isoformat())).fetchall()
        for prior in checks:
            checked_at=datetime.fromisoformat(prior['started_at']).astimezone(ZONE)
            if now_local.strftime('%H:%M')<threshold or checked_at.strftime('%H:%M')>=threshold:
                return {'status':'already_checked','target_date':target_date,'network_requests':0,
                    'reused_check_trigger':prior['trigger_type'],'checked_at':prior['started_at']}
    batch=begin_batch(db,operation_id,DATASETS['html_recent_table'],'legu',target_date,target_date,
        scope={'channel':'html_recent_table','range':'all_returned','evidence_mode':'metadata_only','purpose':'index_derivation_dependency'},
        frequency='daily',trigger='index_dependency',parser_version='congestion-v1',definition_version='v1')
    source=None
    http=RequestCollector(db,policy=PacingPolicy(max_attempts=1),cancelled=cancelled)
    try:
        response,source=http.get(batch,'legu',DATASETS['html_recent_table'],PAGE,expect='html',evidence_mode='metadata_only',
            recovery_token='congestion-'+operation_id,
            healthy_response=lambda r: RECOVERY_PENDING if parse_html(r.text) else False)
        rows=parse_html(response.text)
        http.check_cancelled()
        parsed_source(db,source,fields=['date',*VALUE_FIELDS])
        with db.connection(write=True) as conn:
            counts=publish(conn,rows,'html_recent_table',batch,source)
        finish_batch(db,batch,'complete','partial' if counts['withheld_dates'] else 'unknown',
            result={'network_requests':1,'dependency_target':target_date,**counts})
        if http.state('legu').get('reason')=='recovery_pending:congestion-'+operation_id:
            http.confirm_recovery('legu','congestion-'+operation_id)
        exact=next((r for r in rows if r['observation_date']==target_date and r['total_amount_yi'] is not None and r['total_amount_yi']>0),None)
        return {'status':'available' if exact else 'html_not_updated','target_date':target_date,'network_requests':1,
            'rows':len(rows),'source_id':source,
            'first':min(r['observation_date'] for r in rows),'last':max(r['observation_date'] for r in rows)}
    except Exception as exc:
        if source is not None: parsed_source(db,source,error=exc)
        finish_batch(db,batch,'cancelled' if isinstance(exc,Cancelled) else 'failed','unknown',error=exc)
        if isinstance(exc,Cancelled): raise
        return {'status':'failed','target_date':target_date,
            'network_requests':http.requests,'error':str(exc)}
    finally:
        http.session.close()


def configure(db, command):
    if not isinstance(command,dict) or set(command)-{'enabled','local_time'}:
        raise ValueError('每日检查配置参数无效')
    with db.connection(write=True) as conn:
        old=conn.execute('SELECT * FROM collection_schedules WHERE task_key=?',(TASK,)).fetchone()
        enabled=command.get('enabled',bool(old['enabled']) if old else False)
        clock=command.get('local_time',old['local_time'] if old else '18:00')
        if type(enabled) is not bool or not isinstance(clock,str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',clock):
            raise ValueError('每日检查须指定布尔开关及HH:MM时间')
        if enabled and not initialized(conn):
            raise ValueError('请先完成历史初始化')
        conn.execute('INSERT INTO collection_schedules VALUES(?,?,?,?,?) ON CONFLICT(task_key) DO UPDATE SET '
                     'enabled=excluded.enabled,local_time=excluded.local_time,updated_at=excluded.updated_at',
                     (TASK,int(enabled),'Asia/Shanghai',clock,utc_now()))
    return dict(enabled=enabled,local_time=clock,timezone='Asia/Shanghai')


def start(db, mode='incremental', *, scheduled_date=None, connection=None):
    if mode not in ('full','incremental','review'):
        raise ValueError('拥挤度采集模式无效')
    operation=uuid4().hex
    with (nullcontext(connection) if connection is not None else db.connection(write=True)) as conn:
        if conn.execute("SELECT 1 FROM index_collection_operations WHERE status='running'").fetchone():
            raise ValueError('已有指数采集任务正在运行')
        if mode!='full' and not initialized(conn):
            raise ValueError('请先完成拥挤度历史初始化')
        if scheduled_date and conn.execute("SELECT 1 FROM index_collection_operations WHERE "
                "json_extract(scope_json,'$.dataset')=? AND json_extract(scope_json,'$.scheduled_date')=?",
                (TASK,scheduled_date)).fetchone():
            return None
        scope=dict(dataset=TASK,providers=['legu'],trigger='scheduled' if scheduled_date else 'manual',
                   scheduled_date=scheduled_date)
        conn.execute("INSERT INTO index_collection_operations(operation_id,mode,scope_json,status,started_at) VALUES(?,?,?,'running',?)",
                     (operation,mode,encoded(scope),utc_now()))
    return operation


def _archived(db, dataset, parser):
    with db.connection() as conn:
        candidates=[r[0] for r in conn.execute("SELECT id FROM collection_sources WHERE dataset=? AND source_system='legu' "
            "AND request_status IN ('success','reused') AND parse_status='complete' AND file_path IS NOT NULL ORDER BY obtained_at DESC,id DESC LIMIT 10",(dataset,))]
    for source in candidates:
        try:
            raw,item=read_source(db,source)
            return source,parser(json.loads(raw) if dataset==DATASETS['api_history'] else raw.decode('utf-8'))
        except (ValueError,OSError,TypeError,KeyError):
            continue
    return None


def run(db, operation, *, http=None):
    from .index_collection import OperationProgress
    tracker=OperationProgress(db,operation)
    with db.connection() as conn:
        op=dict(conn.execute('SELECT * FROM index_collection_operations WHERE operation_id=?',(operation,)).fetchone())
    scope=json.loads(op['scope_json']);full=op['mode']=='full'
    def cancelled():
        with db.connection() as conn:
            return bool(conn.execute('SELECT cancel_requested FROM index_collection_operations WHERE operation_id=?',(operation,)).fetchone()[0])
    owns=http is None
    http=http or RequestCollector(db,policy=PacingPolicy(max_attempts=1),cancelled=cancelled)
    completed=[];status='complete';error=None;batch=None;source=None;channel=None
    recovery='congestion-'+operation
    try:
        with instance_lock(db.path.parent/'collection_sources'/'index-operation'):
            channels=list(DATASETS) if full else ['html_recent_table']
            for channel in channels:
                tracker.register('legu',channel,'大盘拥挤度 · '+('API历史' if channel=='api_history' else 'HTML近期表格'))
            tracker.set_phase('running')
            # HTML first supplies credentials for the one-time API request.
            for channel in reversed(channels):
                batch=None;source=None
                http.check_cancelled()
                tracker.transition('legu:'+channel,'running',message='解析／采集大盘拥挤度')
                with db.connection() as conn:
                    exists=conn.execute('SELECT 1 FROM market_indicator_observations WHERE '+
                        ' AND '.join(key+'=?' for key in IDENTITY_FIELDS)+
                        ' AND is_latest=1 AND value IS NOT NULL LIMIT 1',identity(channel)).fetchone()
                if full and exists:
                    tracker.transition('legu:'+channel,'skipped',message='该初始化步骤已完成')
                    continue
                dataset=DATASETS[channel];mode='archive' if full else 'metadata_only'
                batch=begin_batch(db,operation,dataset,'legu',scope={'channel':channel,'range':'all_returned','evidence_mode':mode},
                    frequency='daily',trigger=scope['trigger'],domain='market',parser_version='congestion-v1',definition_version='v1')
                parser=parse_api if channel=='api_history' else parse_html
                archive=_archived(db,dataset,parser) if full else None
                if archive:
                    original,rows=archive
                    source=reusable_source(db,batch,original)
                else:
                    if channel=='api_history':
                        # A reused/previously completed HTML batch has no live cookies.
                        if not getattr(http,'congestion_csrf',None):
                            response,sid=http.get(batch,'legu',dataset,PAGE,expect='html',recovery_token=recovery,
                                healthy_response=lambda r:RECOVERY_PENDING if BeautifulSoup(r.text,'html.parser').select_one('meta[name="_csrf"]') else False)
                            token=BeautifulSoup(response.text,'html.parser').select_one('meta[name="_csrf"]')
                            if not token or not token.get('content'):
                                parsed_source(db,sid,error='缺少CSRF');raise ValueError('页面缺少CSRF')
                            http.congestion_csrf=token['content'];parsed_source(db,sid,fields=['meta:_csrf'])
                        import hashlib
                        token=hashlib.md5(local_now().date().isoformat().encode()).hexdigest()
                        response,source=http.get(batch,'legu',dataset,API,params={'token':token},headers={'X-CSRF-Token':http.congestion_csrf,'Referer':PAGE},
                            recovery_token=recovery,healthy_response=lambda r:RECOVERY_PENDING if parse_api(r.json()) else False)
                        rows=parse_api(response.json())
                    else:
                        def healthy(r):
                            parse_html(r.text)
                            return RECOVERY_PENDING if full else True
                        response,source=http.get(batch,'legu',dataset,PAGE,expect='html',evidence_mode=mode,
                            recovery_token=recovery,healthy_response=healthy)
                        rows=parse_html(response.text)
                        meta=BeautifulSoup(response.text,'html.parser').select_one('meta[name="_csrf"]')
                        http.congestion_csrf=meta.get('content') if meta else None
                    parsed_source(db,source,fields=['date',*VALUE_FIELDS])
                http.check_cancelled()
                with db.connection(write=True) as conn:
                    counts=publish(conn,rows,channel,batch,source)
                    result=dict(channel=channel,rows=len(rows),valid=sum(r['value'] is not None for r in rows),
                                first=rows[0]['observation_date'],last=rows[-1]['observation_date'],**counts)
                    conn.execute("UPDATE collection_batches SET status='complete',coverage_status=?,finished_at=?,result_json=? WHERE id=?",
                        ('partial' if result['valid']<result['rows'] or counts['withheld_dates'] else 'unknown',utc_now(),encoded(result),batch))
                completed.append(result);batch=None
                tracker.transition('legu:'+channel,'success',message=f"已保存{result['valid']}条有值观察；来源覆盖单独记录")
            if http.state('legu').get('reason')=='recovery_pending:'+recovery:
                http.confirm_recovery('legu',recovery)
            with db.connection(write=True) as conn:
                if initialized(conn):
                    conn.execute('INSERT OR IGNORE INTO collection_schedules VALUES(?,?,?,?,?)',(TASK,1,'Asia/Shanghai','18:00',utc_now()))
            try:
                from .index_price_derived import recompute
                latest=max((item.get('last') for item in completed if item.get('last')),default=None)
                earliest=min((item.get('first') for item in completed if item.get('first')),default=latest)
                derived_result=recompute(db,operation,start=earliest,through=latest,cancelled=cancelled) if completed else {'status':'current','facts':0,'network_requests':0}
            except Cancelled:
                raise
            except Exception as exc:
                # Keep the source refresh successful; local estimates can be retried independently.
                derived_result={'status':'failed','facts':0,'network_requests':0,'error':str(exc)}
    except Exception as exc:
        status='cancelled' if isinstance(exc,Cancelled) else 'failed';error=str(exc)
        if batch is not None:
            if source is not None:
                parsed_source(db,source,error=error)
            finish_batch(db,batch,status,'unknown',error=error)
        if http.state('legu').get('reason')=='recovery_pending:'+recovery:
            http.abort_recovery('legu',recovery,'恢复检查未完成')
        if channel:
            tracker.transition('legu:'+channel,status if status=='cancelled' else 'failed',message=error)
    finally:
        tracker.finish(status)
        with db.connection(write=True) as conn:
            current=json.loads(conn.execute('SELECT result_json FROM index_collection_operations WHERE operation_id=?',(operation,)).fetchone()[0])
            current.update(message='大盘拥挤度：'+('检查完成，缺值保留' if status=='complete' else error or status),
                           channels=completed,derived=locals().get('derived_result',{'status':'pending','facts':0,'network_requests':0}),
                           errors={'legu':error} if error else {})
            conn.execute('UPDATE index_collection_operations SET status=?,finished_at=?,result_json=?,error=? WHERE operation_id=?',
                         (status,utc_now(),encoded(current),error,operation))
        if owns:
            http.session.close()
    # Deliberately no per-operation report file; metadata and results are in SQLite.
    return dict(operation_id=operation,status=status,error=error,channels=completed)


def read(db, *, start=None, end=None, index_code='cn:000001'):
    from .index_prices import read_overlay
    for boundary in (start,end):
        if boundary is not None:
            day(boundary)
    if start and end and start>end:
        raise ValueError('时间范围倒置')
    with db.connection() as conn:
        rows=[dict(r) for r in conn.execute('SELECT * FROM market_indicator_observations WHERE indicator_key=? AND scope_key=? '
            'AND frequency=? AND ((provider=? AND definition_version=?) OR '
            '(provider=? AND channel=? AND definition_version=?)) AND is_latest=1 ORDER BY observation_date,channel',
            ('ashares_congestion','all_a','daily','legu','v1',CALCULATED_IDENTITY[2],CALCULATED_CHANNEL,CALCULATED_IDENTITY[5]))]
        broad=[dict(r) for r in conn.execute("SELECT index_code,max(name) AS name FROM index_catalog WHERE index_type='broad' GROUP BY index_code ORDER BY name")]
        schedule=conn.execute('SELECT * FROM collection_schedules WHERE task_key=?',(TASK,)).fetchone()
        operations=[dict(r) for r in conn.execute("SELECT * FROM index_collection_operations WHERE json_extract(scope_json,'$.dataset')=? ORDER BY started_at DESC LIMIT 1",(TASK,))]
        running=bool(conn.execute("SELECT 1 FROM index_collection_operations WHERE status='running'").fetchone())
        check=conn.execute("SELECT s.obtained_at,s.parse_status,s.http_status,s.file_path FROM collection_sources s JOIN collection_batches b ON b.id=s.batch_id "
            "WHERE s.dataset=? AND b.domain='market' ORDER BY s.obtained_at DESC,s.id DESC LIMIT 1",(DATASETS['html_recent_table'],)).fetchone()
        ready=initialized(conn)
        revision=conn.execute('SELECT revision FROM index_read_revisions WHERE scope=?',('indices',)).fetchone()
    if index_code not in {r['index_code'] for r in broad} and index_code!='cn:000001':
        raise ValueError('叠加指数须为宽基指数')
    overlay=read_overlay(db,index_code)
    if overlay['available']:
        with db.connection() as conn:
            # Keep explicit NULL closes rather than silently connecting across them.
            overlay['rows']=[dict(r) for r in conn.execute('SELECT f.observation_date,f.close FROM index_price_observations f '
                'JOIN index_price_series s ON s.id=f.series_id WHERE s.index_code=? AND s.provider=? AND s.series_key=? '
                'AND s.frequency=? AND s.return_type=? AND s.definition_version=? ORDER BY f.observation_date',
                (index_code,overlay['provider'],overlay['series_key'],overlay['frequency'],overlay['return_type'],overlay['definition_version']))]
        from .index_price_codec import decode_day
        for item in overlay['rows']:item['observation_date']=decode_day(item['observation_date'])
        if overlay['frequency']=='monthly':
            overlay['rows']=list({r['observation_date'][:7]:r for r in overlay['rows']}.values())
    valid={r['observation_date'] for r in rows if r['value'] is not None}
    missing=sorted({r['observation_date'] for r in rows}-valid)
    channels={}
    for channel in (*DATASETS,CALCULATED_CHANNEL):
        all_rows=[r for r in rows if r['channel']==channel]
        values=[r for r in all_rows if r['value'] is not None]
        channels[channel]=dict(rows=[r for r in all_rows if (not start or r['observation_date']>=start) and (not end or r['observation_date']<=end)],
            count=len(all_rows),valid=len(values),last_value_date=values[-1]['observation_date'] if values else None,
            field_valid={key:sum(r[key] is not None for r in all_rows) for key in VALUE_FIELDS})
    actual=[r for r in rows if r['value'] is not None]
    latest=max(actual,key=lambda r:(r['observation_date'],r['channel']=='html_recent_table')) if actual else None
    by_date={}
    for row in actual:
        if row['provider']=='legu':by_date.setdefault(row['observation_date'],[]).append(row)
    conflicts=[d for d,items in by_date.items() if len(items)>1 and max(r['value'] for r in items)-min(r['value'] for r in items)>0.00005]
    conflicts=[dict(date=d,values={r['channel']:r['value'] for r in by_date[d]}) for d in conflicts]
    legu_valid={r['observation_date'] for r in actual if r['provider']=='legu'}
    supplemented=sorted({r['observation_date'] for r in actual if r['channel']==CALCULATED_CHANNEL}-legu_valid)
    return dict(channels=channels,latest=latest,known_dates=len({r['observation_date'] for r in rows}),valid_dates=len(valid),
        supplemented_dates=supplemented,source_missing_dates=sorted({r['observation_date'] for r in rows if r['provider']=='legu'}-legu_valid),
        missing_dates=missing,coverage='unknown',conflict_dates=[r['date'] for r in conflicts],conflicts=conflicts,initialized=ready,running=running,
        schedule=dict(schedule) if schedule else dict(enabled=0,local_time='18:00',timezone='Asia/Shanghai'),
        last_check=dict(check) if check else None,operations=operations,broad_indices=broad,overlay=overlay,
        point_revision=revision[0] if revision else None,definition=DEFINITIONS[identity('api_history')])


def scheduled_tick(db, *, now=None, execute=run):
    now=now or local_now()
    with db.connection() as conn:
        schedule=conn.execute('SELECT * FROM collection_schedules WHERE task_key=?',(TASK,)).fetchone()
        if not schedule or not schedule['enabled'] or not initialized(conn):
            return None
    due_date=latest_daily_date(now,schedule['local_time']).isoformat()
    due_at=datetime.fromisoformat(due_date+'T'+schedule['local_time']).replace(tzinfo=ZONE)
    if datetime.fromisoformat(schedule['updated_at'])>due_at:
        return None  # Saving/enabling starts with a future slot, as with other tasks.
    try:
        operation=start(db,'incremental',scheduled_date=due_date)
    except ValueError:  # Another index operation owns the slot; try when idle.
        return None
    if not operation:
        return None
    with db.connection() as conn:
        blocked=conn.execute("SELECT cooldown_until,reason FROM collection_provider_state WHERE source_system='legu'").fetchone()
        manual=conn.execute("SELECT 1 FROM collection_sources s JOIN collection_batches b ON b.id=s.batch_id WHERE s.dataset=? "
            "AND s.parse_status='complete' AND b.status='complete' AND b.trigger_type IN ('manual','scheduled','index_dependency') AND datetime(s.obtained_at)>=datetime(?) AND datetime(s.obtained_at)<=datetime(?) LIMIT 1",
            (DATASETS['html_recent_table'],due_at.isoformat(),now.isoformat())).fetchone()
    cooling=blocked and blocked['cooldown_until'] and datetime.fromisoformat(blocked['cooldown_until'])>now
    if cooling or manual:
        message='本次到期检查因来源冷却跳过' if cooling else '本次到期时段已完成HTML检查，复用检查结果'
        with db.connection(write=True) as conn:
            conn.execute("UPDATE index_collection_operations SET status='complete',finished_at=?,result_json=? WHERE operation_id=?",
                         (utc_now(),encoded(dict(message=message,skipped=True)),operation))
        return dict(operation_id=operation,status='complete',skipped=True)
    return execute(db,operation)


class DailyScheduler:
    def __init__(self, db, *, gate=None):
        self.db=db;self.gate=gate;self.stop_event=Event();self.thread=Thread(target=self._loop,name='congestion-daily',daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop_event.set()
        # Cancel only this scheduler's running operation so database restore can drain it.
        with self.db.connection(write=True) as conn:
            conn.execute("UPDATE index_collection_operations SET cancel_requested=1 WHERE status='running' "
                         "AND json_extract(scope_json,'$.dataset')=? AND json_extract(scope_json,'$.trigger')='scheduled'",(TASK,))
        self.thread.join(timeout=90)
        if self.thread.is_alive():
            raise RuntimeError('拥挤度每日检查尚未停止，不能关闭数据库')

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                admitted=self.gate is None or self.gate.enter()
                if admitted:
                    try:scheduled_tick(self.db)
                    finally:
                        if self.gate:self.gate.leave()
            except (ValueError,OSError,sqlite3.DatabaseError) as exc:
                print('拥挤度每日检查：'+type(exc).__name__,flush=True)
            self.stop_event.wait(30)
