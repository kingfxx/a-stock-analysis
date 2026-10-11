"""Import hash-verified research responses through the production index parser.

This imports new index data, not legacy collection tables. It retains the actual
original request time and original parameters. No network requests are made.
"""
from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path

import requests

from .collection import archive_source, begin_batch, digest, encoded, finish_batch, parsed_source
from .index_collection import (CS_CODES, LG_CODES, IndexCollector, coverage_summary, day, json_data,
                               parse_rows, publish_segment, start_operation)
from .index_definitions import canonical
from .storage import DEFAULT_DATABASE, Database, instance_lock, utc_now


def import_probe(db, reports, *, tag, start, end):
    reports=Path(reports).resolve()
    repo=reports.parents[2]
    sample_root=repo/'data'/'verification'/'samples'
    operation=start_operation(db,'backfill',start=start,end=end)
    results=[]
    try:
        with instance_lock(db.path.parent/'collection_sources'/'index-operation'):
            def load(provider):
                return json.loads((reports/f'{tag}-index-valuation-{provider}.json').read_text(encoding='utf-8'))

            def reuse(item,batch,provider,dataset):
                path=(repo/item['file_path']).resolve()
                if not path.is_relative_to(sample_root.resolve()):
                    raise ValueError('验证样本路径超出 samples 目录')
                stored=path.read_bytes()
                raw=gzip.decompress(stored)
                if digest(stored)!=item['stored_sha256'] or digest(raw)!=item['raw_sha256']:
                    raise ValueError('验证样本双哈希不符')
                if not item.get('obtained_at'):
                    raise ValueError('验证样本缺少真实采集时间，不虚构')
                request=dict(transport='http',method='GET',url=item['url'],params=item.get('params',{}),
                    cache_reuse=dict(original_sample=path.relative_to(repo).as_posix(),original_raw_sha256=item['raw_sha256']))
                response=requests.Response()
                response.status_code=200
                response._content=raw
                response.headers['Content-Type']='text/plain' if provider=='sina' else 'application/json'
                sid=archive_source(db,batch,provider,dataset,request,1,raw=raw,response=response,
                                   request_status='reused',obtained_at=item['obtained_at'])
                return response,sid

            cal=load('calendar')
            request=next(r for r in cal['requests'] if r['http_status']==200)
            batch=begin_batch(db,operation,'index.calendar','sina')
            response,sid=reuse(request,batch,'sina','index.calendar')
            from py_mini_racer import MiniRacer
            js=MiniRacer()
            js.eval((Path(__file__).parent/'resources/sina_trade_calendar.js').read_text(encoding='utf-8'))
            calendar={day(d) for d in js.call('d',response.text.split('=')[1].split(';')[0].replace('"',''))}
            parsed_source(db,sid,fields=['trading_date'])
            finish_batch(db,batch,'complete',result=dict(cache_reused=True,count=len(calendar)))
            for provider,codes,datasets in [('csindex',CS_CODES,['history']),('legu',LG_CODES,['pe','pb'])]:
                report=load(provider)
                for code,name in codes.items():
                    for kind in datasets:
                        candidates=[r for r in report['requests'] if r.get('http_status')==200 and
                                    r.get('params',{}).get('indexCode')==code and
                                    (provider=='csindex' or r['url'].endswith('index-basic-'+kind))]
                        if provider=='csindex':
                            candidates=[r for r in candidates if r['params'].get('startDate','99999999')<=start.replace('-','')
                                        and r['params'].get('endDate','00000000')>=end.replace('-','')]
                        if not candidates:
                            raise ValueError(f'没有可核验的 {provider}/{code}/{kind} 来源')
                        item=candidates[-1]
                        dataset='index.cs.history' if provider=='csindex' else 'index.legu.'+kind
                        batch=begin_batch(db,operation,dataset,provider,start,end,scope=dict(code=code),
                                          frequency='daily' if provider=='csindex' else 'monthly',trigger='backfill')
                        response,sid=reuse(item,batch,provider,dataset)
                        raw=json_data(response)
                        records=raw if provider=='csindex' else [{**row,'_code':code,'_name':name} for row in raw]
                        rows,identities,anomalies=parse_rows(records,provider=provider,
                            series_keys=['history.rolling'] if provider=='csindex' else [kind+'.'+method for method in ('ordinary','equal','median')],
                            code_key='indexCode' if provider=='csindex' else '_code',name_key='indexNameCn' if provider=='csindex' else '_name',
                            date_key='tradeDate' if provider=='csindex' else 'date',start=start,end=end,trading_days=calendar,
                            batch_id=batch,source_id=sid,expected_code=code)
                        parsed_source(db,sid,fields=set().union(*(r.keys() for r in raw)))
                        state,coverage=coverage_summary(rows,calendar,start,end,expected_codes=[canonical(code,provider)] if provider=='csindex' else None,
                                                        monthly=provider=='legu')
                        result=publish_segment(db,batch,rows,identities,dict(cache_reused=True,network_requests=0,coverage=coverage,anomalies=anomalies),coverage=state)
                        results.append(dict(provider=provider,code=code,dataset=dataset,**result))
            with db.connection(write=True) as conn:
                conn.execute("UPDATE index_collection_operations SET status='complete',finished_at=?,result_json=? WHERE operation_id=?",
                             (utc_now(),encoded(dict(message='已核验样本导入完成；保留原始采集时间',network_requests=0,results=results)),operation))
    except Exception as exc:
        with db.connection(write=True) as conn:
            conn.execute("UPDATE index_collection_operations SET status='failed',finished_at=?,error=? WHERE operation_id=?",(utc_now(),str(exc),operation))
            conn.execute("UPDATE collection_batches SET status='failed',finished_at=?,error=? WHERE operation_id=? AND status='running'",(utc_now(),str(exc),operation))
        raise
    return dict(operation_id=operation,results=results)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,default=DEFAULT_DATABASE)
    parser.add_argument('--reports',type=Path,default=Path(__file__).parent.parent/'data/verification/reports')
    parser.add_argument('--tag',required=True)
    parser.add_argument('--start',required=True)
    parser.add_argument('--end',required=True)
    args=parser.parse_args()
    db=Database(args.database)
    db.check()
    result=import_probe(db,args.reports,tag=args.tag,start=args.start,end=args.end)
    print(encoded(dict(operation_id=result['operation_id'],datasets=len(result['results']),
                       observations=sum(r['observations'] for r in result['results']),network_requests=0)),flush=True)


if __name__=='__main__':
    main()
