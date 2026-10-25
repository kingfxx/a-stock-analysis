"""Backfill index points from existing hash-verified archives; zero network calls."""
import argparse
import json
from pathlib import Path
import re
from uuid import uuid4

from stock_analysis.collection.collection import begin_batch, encoded, atomic_write, read_source, finish_batch, Cancelled
from stock_analysis.domains.indices.index_collection import IndexCollector, publish_segment, start_operation
from stock_analysis.domains.indices.index_prices import parse_prices, version_for
from stock_analysis.domains.indices.index_repository import catalog
from stock_analysis.infrastructure.storage import Database, DEFAULT_DATABASE, instance_lock, utc_now

DATASETS={
 'index.cs.history':('csindex','history.points','indexCode','tradeDate'),
 'index.sw.analysis':('sws','analysis.points','swindexcode','bargaindate'),
 'index.legu.pe':('legu','pe.points','_code','date'),
 'index.legu.pb':('legu','pb.points','_code','date'),
 'index.price.range_probe':('sina','history.points','_code','date'),
}


def reused_source(db, original, batch):
    """Reference the same immutable file, preserve its actual original timestamp."""
    if not original['obtained_at']:
        raise ValueError('原始来源没有真实取得时间')
    fields=('source_system','dataset','request_key','request_json','http_status','file_path','content_type',
            'compression','raw_sha256','stored_sha256','hash_basis','raw_bytes','stored_bytes','fields_json','obtained_at')
    values={field:original[field] for field in fields}
    values.update(batch_id=batch,attempt_no=1,request_status='reused',parse_status='complete',reused_source_id=original['id'])
    with db.connection(write=True) as conn:
        return conn.execute('INSERT INTO collection_sources('+','.join(values)+') VALUES('+','.join('?' for _ in values)+')',tuple(values.values())).lastrowid


def replay(db, *, start='2016-10-11', end='2026-10-09', report_path=None):
    from stock_analysis.domains.indices.index_collection import day
    day(start);day(end)
    if start>end:
        raise ValueError('点位回放范围倒置')
    report=dict(start=start,end=end,network_requests=0,results=[],skipped_batches=0)
    with instance_lock(db.path.parent/'collection_sources'/'index-operation'):
        operation=start_operation(db,'backfill',start=start,end=end)
        report['operation_id']=operation
        def cancelled():
            with db.connection() as conn:
                row=conn.execute('SELECT cancel_requested FROM index_collection_operations WHERE operation_id=?',(operation,)).fetchone()
            return not row or bool(row[0])
        def check_cancelled():
            if cancelled(): raise Cancelled('点位回放收到取消请求；已发布分段保留')
        try:
            with db.connection() as conn:
                batches=[dict(r) for r in conn.execute('SELECT * FROM collection_batches WHERE status=\'complete\' AND dataset IN ('+
                    ','.join('?' for _ in DATASETS)+') ORDER BY id',tuple(DATASETS))]
                cal=conn.execute("SELECT id FROM collection_sources WHERE dataset='index.calendar' AND parse_status='complete' ORDER BY id DESC LIMIT 1").fetchone()
            if not cal:
                raise ValueError('没有已归档交易日历，不自动联网')
            raw,_=read_source(db,cal[0])
            calendar=IndexCollector.decode_calendar(None,raw.decode('utf-8'))
            report['calendar_source_id']=cal[0]
            from py_mini_racer import MiniRacer
            js=MiniRacer()
            js.eval((Path(__file__).parent/'resources/sina_index_history.js').read_text(encoding='utf-8'))
            for original_batch in batches:
                check_cancelled()
                original_id=original_batch['id']
                provider,key,code_key,date_key=DATASETS[original_batch['dataset']]
                begin=max(start,original_batch['scope_start'] or start)
                finish=min(end,original_batch['scope_end'] or end)
                if begin>finish:
                    continue
                point_version=version_for(*({'index.cs.history':('csindex','history.points'),'index.sw.analysis':('sws','analysis.points'),
                    'index.legu.pe':('legu','pe.points'),'index.legu.pb':('legu','pb.points'),
                    'index.price.range_probe':('sina','history.points')}[original_batch['dataset']]))
                idempotency=encoded(dict(original_batch=original_id,start=begin,end=finish,parser='points-'+point_version,definition=point_version))
                with db.connection() as conn:
                    completed=conn.execute("SELECT id FROM collection_batches WHERE dataset='index.points.replay' AND source_system=? AND idempotency_key=? AND status='complete'",(provider,idempotency)).fetchone()
                    source_ids=[r[0] for r in conn.execute('SELECT s.id FROM collection_sources s WHERE s.batch_id=? AND s.parse_status=\'complete\' '
                        'AND s.dataset=? AND NOT EXISTS(SELECT 1 FROM collection_sources n WHERE n.batch_id=s.batch_id AND n.request_key=s.request_key '
                        "AND n.dataset=s.dataset AND n.parse_status='complete' AND n.id>s.id) ORDER BY s.id",(original_id,original_batch['dataset']))]
                if completed:
                    report['skipped_batches']+=1
                    continue
                batch=begin_batch(db,operation,'index.points.replay',provider,begin,finish,
                    scope=dict(original_batch=original_id,original_dataset=original_batch['dataset']),
                    frequency='monthly' if provider=='legu' else 'daily',trigger='backfill',idempotency_key=idempotency,
                    parser_version='points-'+point_version,definition_version=point_version)
                with db.connection(write=True) as conn:
                    conn.execute('UPDATE collection_batches SET parser_version=? WHERE id=?',('index-points-'+point_version,batch))
                all_prices,anomalies,totals,seen=[],[],[],set()
                page_lengths=[];page_numbers=[];page_sizes=[];index_types=[]
                original_scope=json.loads(original_batch['scope_json'] or '{}')
                try:
                    if not source_ids:
                        raise ValueError('成功批次缺少可校验的原始来源')
                    for original_source in source_ids:
                        check_cancelled()
                        raw,source=read_source(db,original_source)
                        if source['source_system']!=provider:
                            raise ValueError('原始提供方与数据集不匹配')
                        request=json.loads(source['request_json'])
                        expected=None
                        if provider=='sina':
                            match=re.search(r'/company/((?:sh|sz)\d{6})/',request['url'])
                            if not match:
                                raise ValueError('新浪样本没有可校验的指数代码')
                            symbol=match[1];expected=symbol[2:]
                            rows=[{**r,'_code':expected} for r in js.call('d',raw.decode('utf-8').split('=',1)[1].split(';',1)[0].replace('"',''))]
                        else:
                            payload=json.loads(raw)
                            if str(payload.get('code','200')) not in ('200','0') or payload.get('success') is False:
                                raise ValueError('缓存业务状态不是成功')
                            data=payload['data']
                            if provider=='csindex':
                                params=request.get('params') or {}
                                if (params.get('indexCode')!=original_scope.get('code') or not params.get('startDate') or not params.get('endDate')
                                    or day(params['startDate'])>begin or day(params['endDate'])<finish):
                                    raise ValueError('中证缓存实际请求代码／范围与回放范围不匹配')
                                rows=data;expected=params['indexCode']
                            elif provider=='sws':
                                rows=data['results'];totals.append(data['count'])
                                params=request.get('params') or {}
                                if (type(params.get('page')) is not int or params['page']<1 or type(params.get('page_size')) is not int
                                    or params['page_size']<1 or params['page_size']>500):
                                    raise ValueError('申万缓存缺少可核验页号／页容量')
                                if params.get('type')!='DAY' or params.get('swindexcode')!='all':
                                    raise ValueError('申万缓存请求类型／指数范围与回放要求不匹配')
                                if (not params.get('start_date') or not params.get('end_date')
                                    or day(params['start_date'])>begin or day(params['end_date'])<finish):
                                    raise ValueError('申万缓存实际请求范围与回放范围不匹配')
                                page_numbers.append(params['page']);page_sizes.append(params['page_size']);page_lengths.append(len(rows))
                                index_types.append(params.get('index_type'))
                            elif provider=='legu':
                                expected=request['params']['indexCode']
                                rows=[{**r,'_code':expected} for r in data]
                            else:
                                raise ValueError('未支持的行情归档提供方')
                        if not isinstance(rows,list):
                            raise ValueError('原始响应缺少记录列表')
                        for row in rows:
                            identity=(str(row[code_key]),day(row[date_key]))
                            if identity in seen:
                                raise ValueError('原批次存在跨页重复代码／日期')
                            seen.add(identity)
                        sid=reused_source(db,source,batch)
                        points,bad=parse_prices(rows,provider=provider,series_key=key,code_key=code_key,date_key=date_key,
                            start=begin,end=finish,trading_days=calendar,batch_id=batch,source_id=sid,expected_code=expected)
                        if provider=='sina' and points:
                            with db.connection(write=True) as conn:
                                current=conn.execute('SELECT * FROM index_catalog WHERE index_code=? ORDER BY id DESC LIMIT 1',(points[0]['index_code'],)).fetchone()
                                if not current:
                                    raise ValueError('新浪点位没有现有指数目录身份')
                                catalog(conn,current['index_code'],current['name'],current['index_type'],provider='sina',provider_code=symbol,source_id=sid)
                        all_prices+=points;anomalies+=bad
                    if provider=='sws':
                        scope=json.loads(original_batch['scope_json'] or '{}')
                        labels={1:'一级行业',2:'二级行业'}
                        if scope.get('level') not in (1,2) or set(index_types)!={labels[scope['level']]}:
                            raise ValueError('申万原批次没有明确层级，不能作为完整回放分段')
                        if (len(set(totals))!=1 or len(seen)!=totals[0] or len(set(page_sizes))!=1
                            or sorted(page_numbers)!=list(range(1,len(page_numbers)+1))):
                            raise ValueError('申万缓存分页数量、页号或层级不完整')
                        size=page_sizes[0]
                        if any(length!=min(size,max(totals[0]-(page-1)*size,0)) for page,length in zip(page_numbers,page_lengths)):
                            raise ValueError('申万缓存页长与总数不符')
                    result=publish_segment(db,batch,[],[],dict(original_batch=original_id,network_requests=0,
                        raw_rows=len(seen),price_anomalies=anomalies),prices=all_prices,coverage='unknown' if all_prices else 'empty',
                        compact_index_coverage=provider=='sws',check_cancelled=check_cancelled)
                    report['results'].append(dict(batch_id=batch,provider=provider,**result))
                except Exception as exc:
                    with db.connection() as conn:
                        previous=json.loads(conn.execute('SELECT result_json FROM collection_batches WHERE id=?',(batch,)).fetchone()[0])
                    finish_batch(db,batch,'cancelled' if isinstance(exc,Cancelled) else 'failed',
                        'partial' if any(previous.get('price_counts',{}).values()) else 'unknown',result=previous,error=exc)
                    raise
            report['status']='complete'
        except BaseException as exc:
            report.update(status='cancelled' if isinstance(exc,Cancelled) else 'failed',error=str(exc))
            raise
        finally:
            with db.connection(write=True) as conn:
                conn.execute('UPDATE index_collection_operations SET status=?,finished_at=?,result_json=?,error=? WHERE operation_id=?',
                    (report['status'],utc_now(),encoded(report),report.get('error'),operation))
            atomic_write(report_path or db.path.parent/'verification/reports'/('index-points-replay-'+operation+'.json'),encoded(report).encode('utf-8'))
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,default=DEFAULT_DATABASE)
    parser.add_argument('--start',default='2016-10-11')
    parser.add_argument('--end',default='2026-10-09')
    parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    db=Database(args.database);db.check()
    result=replay(db,start=args.start,end=args.end,report_path=args.report)
    print(encoded(dict(status=result['status'],batches=len(result['results']),skipped=result['skipped_batches'],
        points=sum(r['price_observations'] for r in result['results']),network_requests=0)),flush=True)


if __name__=='__main__':
    main()
