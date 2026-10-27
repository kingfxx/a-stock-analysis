"""Bounded provider channels and in-memory, validated multi-stock responses."""
import json
import math
import re
from decimal import Decimal
from concurrent.futures import Future
from queue import Queue, Empty
from threading import Thread

import requests

from stock_analysis.collection.collection import RequestCollector, parsed_source
from stock_analysis.collection.execution_context import current_execution, execution_context
from stock_analysis.domains.stocks.group_batches import GroupBatches, MAX_SYMBOLS

FILTER = re.compile(r"\((\w+)(>=|<=|=)[\"']([^\"']*)[\"']\)")


def clauses(value):
    found = FILTER.findall(value)
    if ''.join(m.group(0) for m in FILTER.finditer(value)) != value:
        raise ValueError('不支持合并此筛选表达式')
    return found


def request_key(job):
    batch, provider, dataset, method, url, params, options, context = job
    values = options.get('data') if method=='POST' else params
    values = dict(values or {})
    if 'filter' in values:
        parsed = clauses(values['filter'])
        values['filter'] = [(f,op,'CODE' if f in ('SECURITY_CODE','SCODE','SECUCODE') else
                            value if op=='=' else 'DATE') for f,op,value in parsed]
    if 'stock' in values:
        values['stock'] = 'CODES'
        values['seDate'] = 'RANGE'
    return batch, method, url, json.dumps(values,sort_keys=True)


def response_copy(payload, original, source_ids=None):
    response = requests.Response()
    response.status_code = 200
    response.headers = original.headers.copy()
    response._content = json.dumps(payload,ensure_ascii=False,separators=(',',':'),default=str).encode('utf-8')
    response.encoding = 'utf-8'
    response.stock_source_ids = source_ids
    return response


class ProviderChannel:
    def __init__(self, owner, provider):
        self.owner, self.provider = owner, provider
        self.queue = Queue(maxsize=64)
        self.collector = owner.factory(owner.db, cancelled=owner.cancelled)
        self.cache = {}
        self.thread = Thread(target=self.run, name='stock-source-'+provider)
        self.thread.start()

    def run(self):
        try:
            while True:
                item = self.queue.get()
                if self.cache:
                    batches={key[0] for key in self.cache}
                    with self.owner.db.connection() as conn:
                        finished={r[0] for r in conn.execute('SELECT id FROM collection_batches WHERE id IN ('+','.join('?' for _ in batches)+") AND status NOT IN ('pending','running')",tuple(batches))}
                    for key in list(self.cache):
                        if key[0] in finished:del self.cache[key]
                if item is None:
                    return
                items = [item]
                # Coalesce concurrently prepared stock work, without a fixed membership barrier.
                while len(items) < MAX_SYMBOLS * 4:
                    try:
                        following = self.queue.get(timeout=.5)
                    except Empty:
                        break
                    if following is None:
                        self.queue.put(None)
                        break
                    items.append(following)
                groups = {}
                for kind, payload, future in items:
                    try:
                        if kind=='begin':
                            call=payload; scope=call[6].get('scope') or {}; ctx=call[7]
                            key=(kind,call[2],scope.get('source_interface',''),call[4][:7] if call[4] else None,
                                 call[5],scope.get('mode'),ctx.get('scope',{}).get('requested_mode'),
                                 json.dumps(scope.get('category_codes')),call[6].get('parser_version'),call[6].get('definition_version'),call[6].get('frequency'))
                        else:
                            try:key=(kind,request_key(payload))
                            except ValueError:key=(kind,id(future))
                        groups.setdefault(key,[]).append((payload,future))
                    except Exception as exc:
                        future.set_exception(exc)
                chunks=[(key,values[i:i+MAX_SYMBOLS]) for key,values in groups.items() for i in range(0,len(values),MAX_SYMBOLS)]
                for key, group in chunks:
                    try:
                        self.collector.check_cancelled()
                        if key[0]=='begin':
                            results=self.owner.batches.merge([p for p,f in group])
                        else:
                            results=self.requests([p for p,f in group])
                        for (_,future),result in zip(group,results):future.set_result(result)
                    except BaseException as exc:
                        if key[0]=='request':
                            with self.owner.db.connection(write=True) as conn:
                                for batch in {job[0] for job,future in group}:
                                    conn.execute("UPDATE collection_sources SET parse_status='failed',error=coalesce(error,?) WHERE batch_id=? AND parse_status='pending'",(type(exc).__name__,batch))
                        for _,future in group:future.set_exception(exc)
        finally:
            self.cache.clear()
            self.collector.session.close()

    def exchange(self, job, params=None, data=None):
        batch, provider, dataset, method, url, original, options, context = job
        options = dict(options)
        if params is not None:
            original=params
        if data is not None:
            options['data']=data
        with execution_context(**context):
            return self.collector.request(batch,provider,dataset,method,url,params=original,**options)

    def requests(self, jobs):
        first=jobs[0]
        # Each original business request is deterministic within this operation;
        # already validated in-memory pages are reused, never new fake sources.
        cache_key=lambda j: (j[0],j[3],j[4],json.dumps(j[5],sort_keys=True),json.dumps(j[6].get('data'),sort_keys=True))
        if all(cache_key(j) in self.cache for j in jobs):
            return [self.cache[cache_key(j)] for j in jobs]
        if len(jobs)==1:
            return [self.exchange(first)]
        if self.collector.state(self.provider).get('cooldown_until'):
            return [self.exchange(j) for j in jobs]
        if 'filter' in (first[5] or {}):
            results=self.eastmoney(jobs)
        elif first[3]=='POST' and 'stock' in (first[6].get('data') or {}):
            results=self.cninfo(jobs)
        elif '/new/data/szse_stock.json' in first[4]:
            results=[self.exchange(first)]*len(jobs)
        else:
            return [self.exchange(j) for j in jobs]
        for job,result in zip(jobs,results):self.cache[cache_key(job)]=result
        return results

    def eastmoney(self, jobs):
        specs=[clauses(job[5]['filter']) for job in jobs]
        code_fields=[[(f,v) for f,op,v in spec if f in ('SECURITY_CODE','SCODE','SECUCODE')] for spec in specs]
        if any(len(fields)!=1 for fields in code_fields):
            return [self.exchange(j) for j in jobs]
        field=code_fields[0][0][0]; codes=[v[0][1] for v in code_fields]
        if len(set(codes))!=len(codes):return [self.exchange(j) for j in jobs]
        merged='('+field+' in ('+','.join('"'+code+'"' for code in codes)+'))'
        for position,(name,op,value) in enumerate(specs[0]):
            if name==field:continue
            values=[spec[position][2] for spec in specs]
            value=min(values) if op=='>=' else max(values) if op=='<=' else value
            merged+=f"({name}{op}'{value}')"
        params={**jobs[0][5],'filter':merged,'pageNumber':1}
        size=int(params.get('pageSize') or 500)
        params['pageSize']=size
        rows=[]; expected=None; original=None; first_rows=None
        seen=set(); sources=[]
        for page in range(1,101):
            response,sid=self.exchange(jobs[0],params={**params,'pageNumber':page})
            decimal=jobs[0][5].get('reportName')=='RPT_F10_FN_MAINOP'
            payload=response.json(parse_float=Decimal) if decimal else response.json(); result=payload.get('result') or {}
            if payload.get('code')==9201 and page==1:
                result=dict(data=[],count=0,pages=0)
            elif payload.get('success') is not True:
                raise ValueError('共享东财查询失败')
            count,pages,data=result.get('count'),result.get('pages'),result.get('data')
            if type(count) is not int or type(pages) is not int or not isinstance(data,list) or count<0:
                raise ValueError('共享东财分页结构无效')
            if pages not in ((0,1) if not count else (math.ceil(count/size),)) or len(data)!=min(size,max(0,count-(page-1)*size)):
                raise ValueError('共享东财分页截断')
            if pages>100 and len(jobs)>1:
                parsed_source(self.owner.db,sid,fields=('result','data','count','pages'))
                middle=len(jobs)//2
                return self.eastmoney(jobs[:middle])+self.eastmoney(jobs[middle:])
            if expected is None:expected=(count,pages);first_rows=data;original=response
            if expected!=(count,pages):raise ValueError('共享东财分页总数变化')
            for row in data:
                if row.get(field) not in codes:raise ValueError('共享东财响应包含名单外股票')
                token=json.dumps(row,sort_keys=True,default=str)
                if token in seen:raise ValueError('共享东财分页重复')
                seen.add(token);rows.append(row);sources.append(sid)
            parsed_source(self.owner.db,sid,fields=('result','data','count','pages'))
            if page>=pages:break
        else:raise ValueError('共享东财分页超过100页')
        if len(rows)!=expected[0]:raise ValueError('共享东财全页总数不符')
        if expected[1]>1:
            check,check_sid=self.exchange(jobs[0],params=params)
            current=(check.json(parse_float=Decimal) if decimal else check.json()).get('result') or {}
            if (current.get('count'),current.get('pages'))!=expected or current.get('data')!=first_rows:
                raise ValueError('共享东财查询期间首页变化')
            parsed_source(self.owner.db,check_sid,fields=('result','data'))
        output=[]
        for job,spec,code in zip(jobs,specs,codes):
            selected=[]; ids=[]
            for row,sid in zip(rows,sources):
                if row.get(field)!=code:continue
                valid=True
                for name,op,value in spec:
                    if name==field:continue
                    actual=str(row.get(name) or '')[:10]
                    valid=valid and bool(actual) and (actual>=value if op=='>=' else actual<=value if op=='<=' else actual==value)
                if valid:selected.append(row);ids.append(sid)
            if not selected:
                # Missing list members need a real single-symbol control before no-data.
                output.append(self.exchange(job));continue
            count=len(selected)
            body=dict(success=True,code=0,result=dict(count=count,pages=math.ceil(count/size),data=selected[:size]))
            response=response_copy(body,original,ids[:size])
            output.append((response,ids[0]))
            # Keep subsequent virtual pages in memory, tied to their real source IDs.
            for page in range(2,math.ceil(count/size)+1):
                a=(page-1)*size;b=page*size;body=dict(success=True,code=0,result=dict(count=count,pages=math.ceil(count/size),data=selected[a:b]))
                clone=response_copy(body,original,ids[a:b])
                key=(job[0],job[3],job[4],json.dumps({**job[5],'pageNumber':page},sort_keys=True),json.dumps(job[6].get('data'),sort_keys=True))
                self.cache[key]=(clone,ids[a])
        return output

    def cninfo(self,jobs):
        forms=[j[6]['data'] for j in jobs]
        stocks=[f['stock'] for f in forms]
        if len(set(stocks))!=len(stocks):return [self.exchange(j) for j in jobs]
        codes=[s.split(',')[0] for s in stocks]
        ranges=[f['seDate'].split('~') for f in forms]
        form={**forms[0],'stock':';'.join(stocks),'seDate':min(r[0] for r in ranges)+'~'+max(r[1] for r in ranges),'pageNum':1}
        size=int(form['pageSize']);rows=[];ids=[];total=None;seen=set();first=None;original=None
        for page in range(1,1001):
            response,sid=self.exchange(jobs[0],data={**form,'pageNum':page})
            obj=response.json(); data=obj.get('announcements');n=obj.get('totalAnnouncement')
            if type(n) is not int or n<0 or not isinstance(data,list):raise ValueError('共享公告分页结构无效')
            if total is None:total=n;first=data;original=response
            pages=max(1,math.ceil(n/size))
            if obj.get('totalpages') not in (n//size,pages):raise ValueError('共享公告分页总数不符')
            if obj.get('hasMore') not in (True,False,'true','false',None):raise ValueError('共享公告hasMore格式无效')
            if obj.get('hasMore') is not None and (obj['hasMore'] in (True,'true'))!=(page<pages):raise ValueError('共享公告hasMore不符')
            if n!=total or len(data)!=min(size,max(0,total-(page-1)*size)):raise ValueError('共享公告分页截断')
            for row in data:
                if row.get('secCode') not in codes:raise ValueError('共享公告包含名单外股票')
                token=(row.get('secCode'),row.get('announcementId'))
                if token in seen:raise ValueError('共享公告分页重复')
                seen.add(token);rows.append(row);ids.append(sid)
            parsed_source(self.owner.db,sid,fields=('announcements','totalAnnouncement'))
            if page>=max(1,math.ceil(total/size)):break
        else:raise ValueError('共享公告分页过多')
        if total>size:
            response,sid=self.exchange(jobs[0],data=form);obj=response.json()
            if obj.get('totalAnnouncement')!=total or obj.get('announcements')!=first:raise ValueError('共享公告期间首页变化')
            parsed_source(self.owner.db,sid,fields=('announcements',))
        output=[]
        for job,code,bounds in zip(jobs,codes,ranges):
            pairs=[(r,s) for r,s in zip(rows,ids) if r['secCode']==code]
            if not pairs:output.append(self.exchange(job));continue
            # Original domain code checks dates; only keep the requested per-stock interval.
            from datetime import datetime
            from zoneinfo import ZoneInfo
            pairs=[(r,s) for r,s in pairs if bounds[0]<=datetime.fromtimestamp(r['announcementTime']/1000,ZoneInfo('Asia/Shanghai')).date().isoformat()<=bounds[1]]
            if not pairs:output.append(self.exchange(job));continue
            n=len(pairs)
            for page in range(1,max(1,math.ceil(n/size))+1):
                part=pairs[(page-1)*size:page*size]
                body=dict(announcements=[r for r,s in part],totalAnnouncement=n,totalpages=n//size,hasMore=page<math.ceil(n/size))
                result=(response_copy(body,original,[s for r,s in part]),part[0][1])
                if page==1:output.append(result)
                else:
                    key=(job[0],job[3],job[4],json.dumps(job[5],sort_keys=True),json.dumps({**job[6]['data'],'pageNum':page},sort_keys=True))
                    self.cache[key]=result
        return output


class GroupRequests:
    def __init__(self,db,cancelled,factory=RequestCollector):
        self.db,self.cancelled,self.factory=db,cancelled,factory
        self.channels={};self.batches=GroupBatches(self)

    def submit(self,provider,kind,payload):
        future=Future();self.channels[provider].queue.put((kind,payload,future))
        return future.result()

    def request(self,batch,provider,dataset,method,url,params=None,**kwargs):
        return self.submit(provider,'request',(batch,provider,dataset,method,url,params,kwargs,dict(current_execution.get() or {})))

    def get(self,*args,**kwargs):return self.request(*args[:3],'GET',*args[3:],**kwargs)
    def post(self,*args,data=None,**kwargs):return self.request(*args[:3],'POST',*args[3:],data=data,**kwargs)
    def check_cancelled(self):
        from stock_analysis.collection.collection import Cancelled
        if self.cancelled():raise Cancelled('用户取消采集')
    def state(self,provider):return self.channels[provider].collector.state(provider)
    @property
    def clock(self):
        import time
        return time.time
    def confirm_recovery(self,provider,token):return self.channels[provider].collector.confirm_recovery(provider,token)
    def abort_recovery(self,provider,token,reason):return self.channels[provider].collector.abort_recovery(provider,token,reason)
    def open(self,providers):
        for provider in providers:self.channels[provider]=ProviderChannel(self,provider)
    def close(self):
        for channel in self.channels.values():channel.queue.put(None)
        for channel in self.channels.values():channel.thread.join()
