"""CNINFO metadata-only identity and announcement pagination adapter."""
from __future__ import annotations

from datetime import date, timedelta
import hashlib
import json

from stock_analysis.collection.collection import ProviderBlocked, parsed_source
from stock_analysis.domains.stocks.disclosure_dates import classify

HEADERS={'User-Agent':'Mozilla/5.0','Referer':'https://www.cninfo.com.cn/'}
INDEX_URLS=('https://www.cninfo.com.cn/new/data/szse_stock.json',)
QUERY_URL='https://www.cninfo.com.cn/new/hisAnnouncement/query'
PAGE_SIZE=30

# Explicitly probe-verified CNINFO codes only. The source's forecast category
# also contains express/earnings reports; qyfpxzcs covers proposal, execution,
# and investor-meeting notices. These source categories are not redefined here.
CATEGORY_REGISTRY={
    'annual':'category_ndbg_szsh',
    'interim':'category_bndbg_szsh',
    'q1':'category_yjdbg_szsh',
    'q3':'category_sjdbg_szsh',
    'forecast':'category_yjygjxz_szsh',
    'dividend':'category_qyfpxzcs_szsh',
}
DEFAULT_CATEGORIES=tuple(CATEGORY_REGISTRY)

class AnnouncementSources:
    def __init__(self, collector): self.collector=collector

    def identity(self,batch_id,dataset,code):
        matches=[]; ids=[]
        for url in INDEX_URLS:
            try:
                response,sid=self.collector.get(batch_id,'cninfo',dataset,url,headers=HEADERS,
                    evidence_mode='metadata_only',healthy_response=lambda r: isinstance(r.json(),dict) and isinstance(r.json().get('stockList'),list))
            except ProviderBlocked:
                raise
            except ValueError as exc:
                # A missing exchange-specific index can be skipped; transport/schema errors stop this source run.
                if 'HTTP 404' not in str(exc): raise
                continue
            ids.append(sid); payload=response.json();
            for row in payload['stockList']:
                if str(row.get('code') or '')==code and row.get('orgId'):
                    matches.append(str(row['orgId']))
            parsed_source(self.collector.db,sid,fields=('stockList',))
            if len(set(matches))==1:
                return next(iter(set(matches))),ids,'matched'
        unique=set(matches)
        if len(unique)!=1: return None,ids,('missing' if not unique else 'ambiguous')
        return next(iter(unique)),ids,'matched'

    def query_period(self,batch_id,dataset,code,org_id,start,end,*,categories=None):
        if date.fromisoformat(start)>date.fromisoformat(end): raise ValueError('公告范围无效')
        categories=DEFAULT_CATEGORIES if categories is None else tuple(categories)
        if not categories or len(set(categories))!=len(categories) or any(x not in CATEGORY_REGISTRY for x in categories):
            raise ValueError('公告分类集合无效')
        data={'stock':f'{code},{org_id}','tabName':'fulltext','pageSize':PAGE_SIZE,'pageNum':1,
              'column':'','category':';'.join(CATEGORY_REGISTRY[x] for x in categories)+';',
              'plate':'','seDate':f'{start}~{end}','searchkey':'','isHLtitle':'false'}
        source_ids=[]
        def fetch(page):
            payload={**data,'pageNum':page}
            response,sid=self.collector.post(batch_id,'cninfo',dataset,QUERY_URL,data=payload,headers=HEADERS,
                evidence_mode='metadata_only',healthy_response=lambda r: isinstance(r.json(),dict)
                and isinstance(r.json().get('announcements'),list))
            obj=response.json()
            if not isinstance(obj,dict) or not isinstance(obj.get('announcements'),list):
                parsed_source(self.collector.db,sid,error='公告响应结构无效'); raise ValueError('巨潮公告响应结构无效')
            source_ids.append(sid)
            obj['_source_id']=sid
            for item in obj['announcements']:
                item['_source_id']=sid
                with self.collector.db.connection() as conn:
                    source=conn.execute('SELECT obtained_at FROM collection_sources WHERE id=?',(sid,)).fetchone()
                item['_obtained_at']=source['obtained_at'] if source else None
            return obj,sid
        first,_=fetch(1)
        try: total=int(first.get('totalAnnouncement',0))
        except (TypeError,ValueError): raise ValueError('巨潮总数无效')
        if total<0: raise ValueError('巨潮总数无效')
        try: reported_pages=int(first.get('totalpages',0) or 0)
        except (TypeError,ValueError): raise ValueError('巨潮分页数无效')
        pages=max(1,(total+PAGE_SIZE-1)//PAGE_SIZE)
        page_floor=total//PAGE_SIZE
        if reported_pages not in (page_floor,pages): raise ValueError('巨潮分页数与总数不符')
        if first.get('hasMore') not in (True,False,'true','false',None): raise ValueError('巨潮hasMore格式无效')
        if first.get('hasMore') is not None and (first.get('hasMore') in (True,'true')) != (pages>1):
            raise ValueError('巨潮hasMore与总数不符')
        items=list(first['announcements'])
        if len(items)>PAGE_SIZE or len(items)<min(PAGE_SIZE,total): raise ValueError('巨潮首分页条数与总数不符')
        if total==0 and items: raise ValueError('巨潮总数与首分页不一致')
        prior_ids={str(x.get('announcementId')) for x in items}
        if len(prior_ids)!=len(items): raise ValueError('巨潮首页公告重复')
        for page in range(2,pages+1):
            obj,_=fetch(page)
            if int(obj.get('totalAnnouncement',-1))!=total: raise ValueError('巨潮分页总数变化')
            try: current_pages=int(obj.get('totalpages',0) or 0)
            except (TypeError,ValueError): raise ValueError('巨潮分页数无效')
            if current_pages not in (page_floor,pages): raise ValueError('巨潮分页数变化')
            rows=obj['announcements']
            if not rows or len(rows)>PAGE_SIZE or len(rows)<min(PAGE_SIZE,total-(page-1)*PAGE_SIZE):
                raise ValueError('巨潮分页截断')
            if obj.get('hasMore') not in (True,False,'true','false',None): raise ValueError('巨潮hasMore格式无效')
            if (obj.get('hasMore') in (True,'true')) != (page<pages): raise ValueError('巨潮hasMore与页码不符')
            ids=[str(x.get('announcementId')) for x in rows]
            if any(x in prior_ids for x in ids) or len(set(ids))!=len(ids): raise ValueError('巨潮公告跨页重复')
            prior_ids.update(ids); items.extend(rows)
        if len(items)!=total: raise ValueError('巨潮分页记录总数不符')
        if first.get('hasMore') is True or first.get('hasMore')=='true':
            if pages==1: raise ValueError('巨潮hasMore与总数不符')
        if pages>1:
            verify,_=fetch(1)
            def clean(rows): return [{k:v for k,v in x.items() if not k.startswith('_')} for x in rows]
            if int(verify.get('totalAnnouncement',-1))!=total or clean(verify.get('announcements',[]))!=clean(first.get('announcements',[])) or verify.get('hasMore')!=first.get('hasMore'):
                raise ValueError('巨潮分页期间边界变化')
        parsed=[]
        for item in items:
            if str(item.get('secCode') or '')!=code: raise ValueError('巨潮公告股票代码不符')
            found=classify(item,code)
            found['category_profile']=list(categories)
            if not start<=found['published_on']<=end: raise ValueError('巨潮公告日期超出查询范围')
            found['_source_id']=item['_source_id']
            found['_obtained_at']=item['_obtained_at']
            # announcementTime is authoritative; adjunct path date only flags a discrepancy.
            import re
            path_dates=re.findall(r'(?<!\d)(20\d{2})[-/]?(\d{2})[-/]?(\d{2})(?!\d)',found['adjunct_url'])
            found['date_conflict']=bool(path_dates and any(''.join(x)!=found['published_on'].replace('-','') for x in path_dates))
            parsed.append(found)
        for sid in source_ids:
            parsed_source(self.collector.db,sid,fields=('announcements','totalAnnouncement','totalpages','hasMore'))
        return parsed
