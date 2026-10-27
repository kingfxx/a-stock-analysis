"""Shipping adapters using shared operation control and the public request budget."""
from stock_analysis.paths import PROJECT_ROOT
from datetime import datetime
from calendar import monthrange
from uuid import uuid4
import json
import math
from urllib.parse import urljoin, urlparse
from stock_analysis.collection.collection import (RequestCollector, Cancelled, ProviderBlocked, begin_batch, finish_batch,
                         parsed_source, read_source, reusable_source, encoded, archive_source)
from stock_analysis.domains.research.sectors.shipping.shipping_sources import (SSE,MOT,parse_sse,discover_mot,parse_mot,parse_throughput,mot_body,
                               ImageTableRequired,NoThroughputDisclosure,mot_month_metadata,monthly_points)
from stock_analysis.domains.research.sectors.shipping.shipping_forecast import publish_rates
from stock_analysis.infrastructure.storage import utc_now


def reserve(conn,key,params):
    if conn.execute("SELECT 1 FROM collection_operations WHERE action IS NULL AND task_key<>'stocks' AND status='running'").fetchone():
        raise ValueError('已有采集任务正在运行，请先等待或停止')
    ref=uuid4().hex
    conn.execute("INSERT INTO collection_operations(operation_id,mode,scope_json,status,started_at,task_key) VALUES(?,?,?,'running',?,?)",
                 (ref,params['mode'],encoded(dict(dataset=key,**params)),utc_now(),key))
    return ref


def reuse_month_segment(db,ref,scope,month,kind,url,previous):
    """Record recovery under this operation, preserving original acquisition time.

    A skipped validated segment is still part of the new operation's evidence.
    In particular, reviewed images must not be parsed again as HTML or fetched
    again just to replace an earlier failed operation summary.
    """
    dataset='shipping.mot_'+kind
    year,number=map(int,month.split('-'))
    batch=begin_batch(db,ref,dataset,'mot',month+'-01',f'{month}-{monthrange(year,number)[1]}',
        scope=dict(scope,url=url,month=month,reused_batch_id=previous['id']),domain='research_shipping',
        parser_version='shipping-v1',frequency='monthly')
    try:
        with db.connection() as conn:
            sources=[dict(r) for r in conn.execute('SELECT * FROM collection_sources WHERE batch_id=?',(previous['id'],))]
        primary=None
        for source in sources:
            if source['file_path']:
                read_source(db,source['id'])  # Verify both hashes, including image evidence.
            if source['dataset']==dataset and source['parse_status']=='complete' and source['file_path'] and source['request_status'] in ('success','reused'):
                primary=source
        prior_result=json.loads(previous['result_json'] or '{}')
        review_id=prior_result.get('review_source_id')
        if review_id:
            raw,_=read_source(db,review_id)
            review=json.loads(raw)
            read_source(db,int(review['image_source_id']))
            read_source(db,int(review['html_source_id']))
        if not primary:
            raise ValueError('已完成分段缺少可核验的原始来源，请使用修订核对')
        reusable_source(db,batch,primary['id'])
        result=dict(month=month,kind=kind,reused_batch_id=previous['id'],published=0,
                    note='复用已校验分段及原取得时间；未重新联网，覆盖判定不扩大')
        if review_id:
            result['review_source_id']=review_id
        finish_batch(db,batch,'complete',previous['coverage_status'],result=result)
    except Exception as exc:
        finish_batch(db,batch,'failed','unknown',error=exc)
        raise
    return dict(month=month,kind=kind,skipped=True,reused_batch_id=previous['id'],batch_id=batch)



def run(db,ref):
    with db.connection() as conn:
        operation=dict(conn.execute('SELECT * FROM collection_operations WHERE operation_id=?',(ref,)).fetchone())
    scope=json.loads(operation['scope_json']);key=scope['dataset']
    def cancelled():
        with db.connection() as conn:
            return bool(conn.execute('SELECT cancel_requested FROM collection_operations WHERE operation_id=?',(ref,)).fetchone()[0])
    collector=RequestCollector(db,cancelled=cancelled)
    results=[];errors=[];batch=None;total=0;completed=0
    def progress():
        skipped=sum(bool(r.get('skipped')) for r in results)
        counts=dict(total=total,success=max(0,completed-skipped-len(errors)),skipped=skipped,
                    failed=len(errors),cancelled=0,running=int(completed<total),
                    pending=max(0,total-completed-int(completed<total)))
        result=dict(channels=[] if key=='shipping_monthly' else results,errors=errors[:3],error_count=len(errors),work_progress=counts,**collector.report())
        with db.connection(write=True) as conn:
            conn.execute("UPDATE collection_operations SET result_json=? WHERE operation_id=? AND status='running'",(encoded(result),ref))
        return counts
    def fetch(url,dataset,provider, *, cached=False):
        nonlocal batch
        collector.check_cancelled()
        if urlparse(url).hostname not in ('www.sse.net.cn','www.sse.sh.cn','www.mot.gov.cn'):
            raise ValueError('航运来源地址不在官方白名单')
        batch=begin_batch(db,ref,dataset,provider,scope.get('start'),scope.get('end'),scope=dict(scope,url=url),domain='research_shipping',
                          parser_version='shipping-analysis-v2' if dataset=='shipping.mot_analysis' else 'shipping-v1',frequency='weekly' if key=='shipping_weekly' else 'monthly')
        if cached:
            with db.connection() as conn:
                saved=conn.execute("SELECT s.id,s.parse_status FROM collection_sources s JOIN collection_batches b ON b.id=s.batch_id WHERE b.dataset=? AND json_extract(s.request_json,'$.url')=? AND s.file_path IS NOT NULL AND s.request_status IN ('success','reused') AND s.parse_status IN ('complete','failed') ORDER BY s.id DESC LIMIT 1",(dataset,url)).fetchone()
            if saved:
                raw,_=read_source(db,saved[0]);source=reusable_source(db,batch,saved[0],reparse=saved['parse_status']=='failed')
                return raw.decode('utf8'),source
        response,source=collector.get(batch,provider,dataset,url,expect='html')
        response.encoding='utf8'
        return response.text,source
    status='complete'
    try:
        if key=='shipping_research_import':
            from pathlib import Path
            from stock_analysis.domains.research.sectors.shipping.shipping_import import import_research
            results.append(import_research(db,PROJECT_ROOT,scope['file'],operation_id=ref,cancelled=cancelled))
        elif key=='shipping_image_review':
            from stock_analysis.domains.research.sectors.shipping.shipping_image_review import import_review
            results.append(import_review(db,scope['file']))
        elif key=='shipping_weekly':
            for index in ('CCFI','SCFI'):
                collector.check_cancelled()
                source=None
                try:
                    # A same-day successful release check is reused by ordinary incrementals.
                    with db.connection() as conn:
                        checked=conn.execute("SELECT 1 FROM collection_batches b WHERE b.dataset=? AND b.status='complete' AND substr(b.finished_at,1,10)=? LIMIT 1",('shipping.'+index.lower(),utc_now()[:10])).fetchone()
                    if checked and scope['mode']=='incremental':
                        results.append(dict(index=index,skipped='今日已核验'));continue
                    html,source=fetch(SSE+index.lower(),'shipping.'+index.lower(),'sse')
                    points=parse_sse(html,index)
                    count=publish_rates(db,points,batch,source)
                    parsed_source(db,source,fields=['routes','previous','current','release_dates'])
                    finish_batch(db,batch,'complete','partial',result=dict(published=count,visible_points=len(points),note='仅当期及上期；全历史覆盖未知'))
                    results.append(dict(index=index,published=count))
                except (Cancelled,ProviderBlocked):raise
                except Exception as exc:
                    if source:parsed_source(db,source,error=exc)
                    if batch:finish_batch(db,batch,'failed','unknown',error=exc)
                    errors.append(str(exc))
        else:
            html,source=fetch(MOT,'shipping.mot_directory','mot',cached=scope['mode']=='backfill')
            links=discover_mot(html)
            parsed_source(db,source,fields=['links']);finish_batch(db,batch,'complete','unknown',result=dict(discovered=len(links)))
            start=scope.get('start','2022-12-01')[:7];end=scope.get('end',datetime.now().strftime('%Y-%m-%d'))[:7]
            selected=sorted((m,k,u) for (m,k),u in links.items() if start<=m<=end)
            total=len(selected)
            if not selected:raise ValueError('目录未发现目标月份，覆盖未知')
            first_year,first_month=map(int,start.split('-'));last_year,last_month=map(int,end.split('-'))
            expected=[f'{n//12:04d}-{n%12+1:02d}' for n in range(first_year*12+first_month-1,last_year*12+last_month)]
            missing=[month for month in expected if (month,'rates') not in links]
            with db.connection(write=True) as conn:
                saved=json.loads(conn.execute('SELECT result_json FROM collection_batches WHERE id=?',(batch,)).fetchone()[0])
                saved.update(missing_rate_months=missing,expected_months=len(expected))
                conn.execute('UPDATE collection_batches SET result_json=? WHERE id=?',(encoded(saved),batch))
            results.append(dict(discovered_months=len({m for m,k,u in selected}),missing_rate_months=missing,note='目录缺项不当成指数不存在；需定向核验'))
            for month,kind,url in selected:
                collector.check_cancelled()
                progress()
                dataset='shipping.mot_'+kind
                with db.connection() as conn:
                    done=conn.execute("SELECT * FROM collection_batches WHERE dataset=? AND status='complete' AND json_extract(scope_json,'$.url')=? ORDER BY id DESC LIMIT 1",(dataset,url)).fetchone()
                if done and scope['mode']!='review':
                    try:
                        results.append(reuse_month_segment(db,ref,scope,month,kind,url,dict(done)))
                    except Exception as exc:
                        errors.append(dict(month=month,kind=kind,error=str(exc)))
                    completed+=1;progress()
                    continue
                source=None
                try:
                    html,source=fetch(url,dataset,'mot',cached=scope['mode']!='review')
                    extraction={};coverage='complete'
                    if kind=='rates':
                        try:
                            points=parse_mot(html,month)
                        except ImageTableRequired:
                            _,body=mot_body(html)
                            images=list(dict.fromkeys(urljoin(url,i['src']) for i in body.find_all('img',src=True)))
                            archived=[]
                            for image in images:
                                if urlparse(image).hostname!='www.mot.gov.cn':raise ValueError('月表图片不是已确认的官方来源')
                                response,image_source=collector.get(batch,'mot','shipping.mot_image',image,expect='binary')
                                archived.append((response.content,image_source))
                            if len(archived)!=1:raise ValueError('月表图片数量不为1，已归档，留待核验')
                            raw,image_source=archived[0]
                            try:
                                from stock_analysis.domains.research.sectors.shipping.shipping_ocr import extract_month_table
                                rows,evidence=extract_month_table(raw,month)
                                _,published_on=mot_month_metadata(html,month)
                                points=monthly_points(rows,month,published_on)
                            except Exception as exc:
                                parsed_source(db,image_source,error=exc);raise
                            evidence.update(image_source_id=image_source,html_source_id=source,month=month)
                            ocr_source=archive_source(db,batch,'mot','shipping.mot_ocr',dict(transport='local_ocr',
                                extractor='mot-local-ocr-v1',image_source_id=image_source),1,raw=encoded(evidence).encode())
                            # Facts point to the actual image, not the derived OCR document.
                            published=publish_rates(db,points,batch,image_source)
                            parsed_source(db,image_source,fields=['validated_month_table','ocr_source_id:'+str(ocr_source)])
                            parsed_source(db,ocr_source,fields=['rows','model_hashes','consensus_validation'])
                            extraction=dict(extractor='mot-local-ocr-v1',ocr_source_id=ocr_source,image_source_id=image_source)
                        else:
                            published=publish_rates(db,points,batch,source)
                    else:
                        try:
                            metrics=parse_throughput(html,month)
                            groups={}
                            for metric in metrics:
                                groups.setdefault((metric['period'],metric['basis']),[]).append(metric)
                            if any(len(rows)!=3 or len({row['scope'][2:] for row in rows})!=1 or any(row['yoy'] is None for row in rows) for rows in groups.values()):
                                coverage='partial';extraction=dict(note='仅发布原文明确披露的范围及字段；未披露保持缺失')
                        except NoThroughputDisclosure as exc:
                            metrics=[];coverage='empty';extraction=dict(note=str(exc))
                        published=publish_industry(db,metrics,batch,source)
                    parsed_source(db,source,fields=['month','routes' if kind=='rates' else 'throughput'])
                    result=dict(month=month,published=published,note='仅此详情页可见指标校验通过，不代表全历史')
                    result.update(extraction)
                    finish_batch(db,batch,'complete',coverage,result=result)
                    results.append(dict(month=month,kind=kind,published=published,coverage=coverage))
                except (Cancelled,ProviderBlocked):raise
                except Exception as exc:
                    if source:parsed_source(db,source,error=exc)
                    if batch:finish_batch(db,batch,'failed','partial',error=exc)
                    errors.append(dict(month=month,kind=kind,error=str(exc)))
                completed+=1;progress()
    except Cancelled:
        status='cancelled'
        if batch:finish_batch(db,batch,'cancelled','partial',error='用户停止，已提交分段保留')
    except Exception as exc:
        status='failed';errors.append(str(exc))
        if batch:finish_batch(db,batch,'failed','unknown',error=exc)
    if errors:status='failed'
    counts=progress() if key=='shipping_monthly' else None
    with db.connection(write=True) as conn:
        conn.execute('UPDATE collection_operations SET status=?,finished_at=?,result_json=?,error=? WHERE operation_id=?',
                     (status,utc_now(),encoded(dict(channels=[] if key=='shipping_monthly' else results,errors=errors[:3],error_count=len(errors),
                        **({'work_progress':counts} if counts is not None else {}),
                        message='所选范围执行完成；已校验分段保留原覆盖判定，全历史覆盖未知' if status=='complete' else '运行未完成，请查看分段诊断',
                        **collector.report())),encoded(errors[:3]) if errors else None,ref))
    return dict(status=status,results=results,errors=errors)


def publish_industry(db,metrics,batch,source):
    for m in metrics:
        if not isinstance(m['value'],(float,int)) or not math.isfinite(m['value']) or m['value']<0 or m['yoy'] is not None and not math.isfinite(m['yoy']):
            raise ValueError('吞吐量或同比无效；有效零/负同比保留')
    count=0
    with db.connection(write=True) as conn:
        if not conn.execute("SELECT 1 FROM collection_sources WHERE id=? AND batch_id=? AND request_status IN ('success','reused')",(source,batch)).fetchone():
            raise ValueError('行业事实来源与批次不匹配')
        for m in metrics:
            previous=conn.execute('SELECT * FROM research_shipping_industry_metrics WHERE metric=? AND scope=? AND period=? AND basis=? ORDER BY revision DESC LIMIT 1',
                                 tuple(m[k] for k in ('metric','scope','period','basis'))).fetchone()
            if previous and all(previous[k]==m[k] for k in ('value','yoy','unit')):continue
            fields=dict(m,revision=previous['revision']+1 if previous else 1,batch_id=batch,source_id=source,created_at=utc_now())
            conn.execute('INSERT INTO research_shipping_industry_metrics('+','.join(fields)+') VALUES('+','.join('?' for _ in fields)+')',tuple(fields.values()));count+=1
    return count
