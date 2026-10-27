"""Official listing dates and a once-per-Shanghai-day membership check."""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from stock_analysis.domains.industries.industry_sources import SW_BASE, SWAdapter, parse_memberships, parse_taxonomy
from stock_analysis.collection.collection import begin_batch, finish_batch, parsed_source, read_source, reusable_source

SSE_URL = 'https://query.sse.com.cn/sseQuery/commonQuery.do'
SZSE_URL = 'https://www.szse.cn/api/report/ShowReport'
SHANGHAI = timezone(timedelta(hours=8))
A_CODE = re.compile(r'(?:60\d|688|00\d|30\d)\d{3}')


def normalize_date(value, asof):
    if value is None or str(value).strip() in {'','-','--'}:
        return None
    value = str(value).strip()
    parsed = datetime.strptime(value,'%Y%m%d' if re.fullmatch(r'\d{8}',value) else '%Y-%m-%d').date()
    if parsed.isoformat() > asof:
        raise ValueError('上市日期晚于采集日期')
    return parsed.isoformat()


def parse_sse(payload, asof):
    rows = payload.get('result')
    if not isinstance(rows,list) or not rows:
        raise ValueError('沪市股票列表为空或结构变化')
    total = (payload.get('pageHelp') or {}).get('total')
    if total is not None and int(total)!=len(rows):
        raise ValueError('沪市股票列表分页不完整')
    return [{'stock_code':r['A_STOCK_CODE'],'name':r['SEC_NAME_CN'],
             'listing_date':normalize_date(r.get('LIST_DATE'),asof)}
            for r in rows if A_CODE.fullmatch(r.get('A_STOCK_CODE',''))]


def parse_szse(content, asof):
    ns={'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(io.BytesIO(content)) as book:
        strings=[''.join(t.text or '' for t in item.findall('.//m:t',ns))
                 for item in ET.fromstring(book.read('xl/sharedStrings.xml')).findall('m:si',ns)] \
                if 'xl/sharedStrings.xml' in book.namelist() else []
        rows=[]
        for row in ET.fromstring(book.read('xl/worksheets/sheet1.xml')).findall('.//m:row',ns):
            values={}
            for cell in row.findall('m:c',ns):
                column=re.sub(r'\d','',cell.attrib['r'])
                value=cell.find('m:v',ns)
                value=value.text if value is not None else ''.join(t.text or '' for t in cell.findall('.//m:t',ns))
                values[column]=strings[int(value)] if cell.get('t')=='s' else value
            rows.append(values)
    if not rows:
        raise ValueError('深市股票列表为空')
    columns={v:k for k,v in rows[0].items()}
    if not {'A股代码','A股简称','A股上市日期'}<=columns.keys():
        raise ValueError('深市股票列表字段变化')
    result=[]
    for row in rows[1:]:
        code=(row.get(columns['A股代码']) or '').zfill(6)
        if A_CODE.fullmatch(code):
            result.append({'stock_code':code,'name':row[columns['A股简称']],
                'listing_date':normalize_date(row.get(columns['A股上市日期']),asof)})
    return result


def parse_listing_files(directory, asof):
    directory=Path(directory)
    result=[]
    for name in ('sse_main.json','sse_star.json','szse_a.xlsx'):
        raw=(directory/name).read_bytes()
        rows=parse_szse(raw,asof) if name.endswith('.xlsx') else parse_sse(json.loads(raw),asof)
        result.extend({**r,'source_key':name} for r in rows)
    if len({r['stock_code'] for r in result})!=len(result):
        raise ValueError('上市日期来源股票代码重复')
    return result


def fetch_directory(directory, asof, progress, *, classification=True, db=None,
                    operation_id=None, collector=None):
    """Archive every official response through the shared provider collector first."""
    if db is None or not operation_id or collector is None:
        raise ValueError('名单采集必须提供数据库、operation_id 和公共 RequestCollector')
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    collector.session.mount('https://www.swsresearch.com/',SWAdapter())
    batches={};files=[];raw_by_key={};source_ids={}
    def get(provider,dataset,name,url,params=None,headers=None,expect='binary',health=None):
        if (provider,dataset) not in batches:
            batches[(provider,dataset)]=begin_batch(db,operation_id,dataset,provider,asof,asof,
                scope={'asof':asof,'universe':'current_sse_szse_a'},frequency='daily',trigger='manual',
                domain='market',parser_version='industry-memberships-v1',
                definition_version='sse-szse-listings-v1' if dataset.startswith('market.listings.') else 'sw2021-v1',
                universe_version='current-sse-szse-a-v1')
        progress('归档名单来源 · '+name)
        response,source_id=collector.get(batches[(provider,dataset)],provider,dataset,url,params,
            headers=headers,expect=expect,healthy_response=health)
        raw,record=read_source(db,source_id)
        raw_by_key[name]=raw;source_ids[name]=source_id
        files.append({'key':name,'url':url,'params':params,'sha256':record['raw_sha256'],
            'obtained_at':record['obtained_at'],'collection_batch_id':record['batch_id'],
            'collection_source_id':source_id,'provider':provider,'dataset':dataset,
            'date_field':('A股上市日期' if name.endswith('.xlsx') else 'LIST_DATE') if name in
                ('sse_main.json','sse_star.json','szse_a.xlsx') else None,
            'definition':'对应代码的 A 股上市日期' if name in
                ('sse_main.json','sse_star.json','szse_a.xlsx') else '申万行业分类及变更',
            'parser_version':'listing_dates_v1'})
    try:
        for board,name in [('1','sse_main.json'),('8','sse_star.json')]:
            get('sse','market.listings.sse',name,SSE_URL,{'sqlId':'COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L',
                'STOCK_TYPE':board,'COMPANY_STATUS':'2,4,5,7,8','type':'inParams','isPagination':'true',
                'pageHelp.pageSize':'10000','pageHelp.pageNo':'1'},
                {'Referer':'https://www.sse.com.cn/assortment/stock/list/share/'},'json',
                lambda response: isinstance(response.json(),dict) and isinstance(response.json().get('result'),list))
        get('szse','market.listings.szse','szse_a.xlsx',SZSE_URL,
            {'SHOWTYPE':'xlsx','CATALOGID':'1110','TABKEY':'tab1'},
            {'Referer':'https://www.szse.cn/market/product/stock/list/index.html'},'binary',_xlsx_health)
        if classification:
            get('sws','industry.classification.taxonomy','taxonomy.xls',SW_BASE+'SwClassCode_2021.xls',
                expect='binary',health=_xls_health)
            get('sws','industry.classification.members','members.xls',SW_BASE+'StockClassifyUse_stock.xls',
                expect='binary',health=_xls_health)
        # All request bodies above are now safely archived. Parse and validate the set before publish.
        sse_rows=[];listing_sources={}
        for name in ('sse_main.json','sse_star.json'):
            board_rows=parse_sse(json.loads(raw_by_key[name]),asof)
            sse_rows.extend(board_rows)
            listing_sources.update({r['stock_code']:source_ids[name] for r in board_rows})
        szse_rows=parse_szse(raw_by_key['szse_a.xlsx'],asof)
        listing_sources.update({r['stock_code']:source_ids['szse_a.xlsx'] for r in szse_rows})
        listings=sse_rows+szse_rows
        if len({r['stock_code'] for r in listings})!=len(listings):
            raise ValueError('上市日期来源股票代码重复')
        if not 4000<=len(listings)<=10000:
            raise ValueError('官方沪深 A 股名单规模异常')
        taxonomy=members=history=None
        if classification:
            taxonomy=parse_taxonomy(raw_by_key['taxonomy.xls'])
            members,history=parse_memberships(raw_by_key['members.xls'],taxonomy,
                {r['stock_code']:r['name'] for r in listings},asof)
        field_sets={'sse_main.json':['A_STOCK_CODE','SEC_NAME_CN','LIST_DATE'],
            'sse_star.json':['A_STOCK_CODE','SEC_NAME_CN','LIST_DATE'],
            'szse_a.xlsx':['A股代码','A股简称','A股上市日期'],
            'taxonomy.xls':['industry_code','industry_name','level','parent_code'],
            'members.xls':['stock_code','effective_date','industry_code','source_update']}
        for name,sid in source_ids.items():
            parsed_source(db,sid,fields=field_sets[name])
    except Exception as exc:
        for sid in source_ids.values():
            try: parsed_source(db,sid,error='roster validation failed: '+type(exc).__name__)
            except Exception: pass
        for batch_id in batches.values():
            finish_batch(db,batch_id,'failed','unknown',error=type(exc).__name__+': '+str(exc))
        raise
    # Mark per-stock listing origin by exchange provider; retain source references in the manifest.
    source_by_id={f['collection_source_id']:f for f in files}
    listing_refs={r['stock_code']:{'collection_batch_id':source_by_id[listing_sources[r['stock_code']]]['collection_batch_id'],
        'collection_source_id':listing_sources[r['stock_code']]} for r in listings}
    for row in listings:
        row.update(listing_refs[row['stock_code']])
        row['source_key']=next(f['key'] for f in files if f['collection_source_id']==row['collection_source_id'])
    if not classification:
        return {'listings':listings,'files':files,'collection_batches':{f'{p}:{d}':b for (p,d),b in batches.items()}}
    return {'taxonomy':taxonomy,'members':members,'membership_history':history,
        'listings':listings,'files':files,'collection_batches':{f'{p}:{d}':b for (p,d),b in batches.items()}}


def _xlsx_health(content):
    try:
        content=content.content if hasattr(content,'content') else content
        parse_szse(content,'9999-12-31');return True
    except Exception:return False


def _xls_health(response):
    try:
        import xlrd
        book=xlrd.open_workbook(file_contents=response.content)
        return book.nsheets>0 and book.sheet_by_index(0).nrows>1 and book.sheet_by_index(0).ncols>=4
    except Exception:return False


def backfill_listing_dates(service, listings, files):
    """Only the current roster is enriched; its identity and old versions stay put."""
    source_by_key={f['key']:f for f in files}
    updates=[]
    with service.db.connection(write=True) as conn:
        member_id=conn.execute('SELECT member_import_id FROM sw_imports ORDER BY id DESC LIMIT 1').fetchone()[0]
        existing={r['stock_code']:r['listing_date'] for r in conn.execute(
            'SELECT stock_code,listing_date FROM sw_memberships WHERE import_id=?',(member_id,))}
        sources={}
        for row in listings:
            code=row['stock_code'];value=row['listing_date']
            if code not in existing or value is None or value==existing[code]:
                continue
            key=row['source_key']
            if key not in sources:
                source=source_by_key[key];payload=json.dumps(source,ensure_ascii=False,sort_keys=True,separators=(',',':'))
                digest=hashlib.sha256(payload.encode()).hexdigest()
                conn.execute('INSERT OR IGNORE INTO sw_listing_sources(content_hash,source_json,obtained_at) VALUES(?,?,?)',
                    (digest,payload,source['obtained_at']))
                sources[key]=conn.execute('SELECT id FROM sw_listing_sources WHERE content_hash=?',(digest,)).fetchone()[0]
            conn.execute('UPDATE sw_memberships SET listing_date=?,listing_source_id=? WHERE import_id=? AND stock_code=?',
                (value,sources[key],member_id,code))
            updates.append({'stock_code':code,'before':existing[code],'after':value})
    with service._lock:
        service._cache=None
    return {'member_import_id':member_id,'updated_count':len(updates),'changes':updates}


def ensure_daily_memberships(service, progress=lambda message:None, *, today=None, run_id=None):
    day=today or datetime.now(SHANGHAI).date().isoformat()
    now=datetime.now(timezone.utc).isoformat()
    with service.db.connection(write=True) as conn:
        old=conn.execute('SELECT * FROM sw_membership_checks WHERE check_date=?',(day,)).fetchone()
        if old and old['status'] in {'complete','running'}:
            return {'check_date':day,'status':old['status'],'reused':True,
                    'warning':old['error'] or ('今日名单检查尚未完成，沿用已保存名单' if old['status']=='running' else None)}
        if old:
            conn.execute("UPDATE sw_membership_checks SET checked_at=?,finished_at=NULL,status='running',error=NULL,result_json=NULL WHERE check_date=?",
                         (now,day))
        else:
            conn.execute("INSERT INTO sw_membership_checks(check_date,checked_at,status) VALUES(?,?,'running')",(day,now))
    result={}
    operation_id='sw_update_runs:'+str(run_id) if run_id is not None else 'membership:'+day+':'+uuid4().hex
    try:
        with service.db.connection() as conn:
            has_foundation=conn.execute('SELECT 1 FROM sw_imports LIMIT 1').fetchone() is not None
        # A clean database can initialize only the current official roster/classification.
        old=service.foundation() if has_foundation else {'members':[],'membership_history':[]}
        directory=service.directory/'membership_checks'/day
        directory.mkdir(parents=True,exist_ok=True)
        from stock_analysis.collection.collection import RequestCollector
        collector=RequestCollector(service.db,progress=progress,cancelled=lambda:service.collection_cancelled(run_id))
        bundle=fetch_directory(directory,day,progress,db=service.db,operation_id=operation_id,collector=collector)
        old_by_code={r['stock_code']:r for r in old['members']}
        new_by_code={r['stock_code']:r for r in bundle['members']}
        if len(new_by_code)<len(old_by_code)*.95:
            raise ValueError('来源名单数量骤减，沿用已保存名单')
        missing=sorted(old_by_code.keys()-new_by_code.keys())
        listing_by_code={r['stock_code']:r for r in bundle['listings']}
        retained_sources=[]
        reused_listing_sources={}
        def retain_listing_source(previous):
            source_id=previous.get('collection_source_id')
            batch_id=previous.get('collection_batch_id')
            if source_id is None:
                return batch_id,None
            with service.db.connection() as conn:
                source=conn.execute('SELECT source_system,dataset FROM collection_sources WHERE id=?',(source_id,)).fetchone()
            if not source:
                raise ValueError('待保留的上市日期公共来源不存在')
            read_source(service.db,source_id)
            batch_key=f"{source['source_system']}:{source['dataset']}"
            target_batch=bundle.get('collection_batches',{}).get(batch_key)
            if target_batch is None:
                raise ValueError('本次名单采集没有可关联的上市日期提供方批次')
            key=(target_batch,source_id)
            if key not in reused_listing_sources:
                reused_listing_sources[key]=reusable_source(service.db,target_batch,source_id)
            return target_batch,reused_listing_sources[key]
        members=[]
        for code,row in new_by_code.items():
            row=dict(row);previous=old_by_code.get(code,{})
            listing=listing_by_code.get(code,{})
            if listing.get('listing_date'):
                row['listing_date']=listing['listing_date']
                if listing['listing_date']!=previous.get('listing_date'):
                    row['collection_batch_id']=listing['collection_batch_id']
                    row['collection_source_id']=listing['collection_source_id']
                    row['listing_source_id']=None
                else:
                    row['listing_date']=previous.get('listing_date') or listing['listing_date']
                    row['listing_source_id']=previous.get('listing_source_id')
                    if previous.get('collection_source_id'):
                        row['collection_batch_id'],row['collection_source_id']=retain_listing_source(previous)
                    else:
                        row['collection_batch_id']=row['collection_source_id']=None
                    if previous.get('collection_source_id'):
                        retained_sources.append({'stock_code':code,'retained':True,
                            'collection_batch_id':row['collection_batch_id'],
                            'collection_source_id':row['collection_source_id'],
                            'reused_source_id':previous['collection_source_id']})
            else:
                prior_batch=previous.get('collection_batch_id');prior_source=previous.get('collection_source_id')
                row['listing_date']=previous.get('listing_date')
                row['listing_source_id']=previous.get('listing_source_id')
                if prior_source:
                    row['collection_batch_id'],row['collection_source_id']=retain_listing_source(previous)
                    retained_sources.append({'stock_code':code,'retained':True,
                        'collection_batch_id':row['collection_batch_id'],'collection_source_id':row['collection_source_id'],
                        'reused_source_id':prior_source})
                else:
                    row['collection_batch_id']=row['collection_source_id']=None
            if row.get('industry_code') is None and previous.get('industry_code'):
                for key in ('industry_code','effective_date','source_update'):
                    row[key]=previous[key]
            members.append(row)
        # Keep temporarily absent companies as explicitly local retained membership rows.
        for code in missing:
            row=dict(old_by_code[code]);prior_batch=row.get('collection_batch_id');prior_source=row.get('collection_source_id')
            if prior_source:
                row['collection_batch_id'],row['collection_source_id']=retain_listing_source(row)
                retained_sources.append({'stock_code':code,'retained':True,
                    'collection_batch_id':row['collection_batch_id'],'collection_source_id':row['collection_source_id'],
                    'reused_source_id':prior_source})
            else:
                row['collection_batch_id']=row['collection_source_id']=None
            members.append(row)
        history={(r['stock_code'],r['effective_date'],r['industry_code']):r for r in old['membership_history']}
        history.update({(r['stock_code'],r['effective_date'],r['industry_code']):r for r in bundle['membership_history']})
        imported={**bundle,'members':members,'membership_history':list(history.values()),
            'asof':day,'obtained_at':now,'financials':[],'caps':[],
            'manifest':{'standard':'SW2021','scope':'classification','action':'daily_membership_check',
                'collection_operation_id':operation_id,'collection_batches':bundle['collection_batches'],
                'source_refs':bundle['files'],'retained_local_members':missing,
                'retained_prior_listing_sources':retained_sources,
                'listing_source_policy':'新值按交易所公共来源引用；来源缺失沿用旧值并在本manifest标记为retained',
                'membership_count':len(members),'pilot_industries':[]}}
        def mark_publication(conn):
            published=conn.execute("SELECT max(id) FROM sw_imports WHERE json_extract(source_manifest_json,'$.collection_operation_id')=?",
                (operation_id,)).fetchone()[0]
            for batch_key,batch_id in bundle['collection_batches'].items():
                provider,dataset=batch_key.split(':',1)
                source_files=[f for f in bundle['files'] if f['provider']==provider and f['dataset']==dataset]
                conn.execute("UPDATE collection_batches SET status='complete',coverage_status='unknown',finished_at=?,result_json=? WHERE id=?",
                    (datetime.now(timezone.utc).isoformat(),json.dumps({
                        'request_count':len(source_files),'source_count':len(source_files),
                        'verified_count':len(bundle['listings']) if dataset.startswith('market.listings.') else len(bundle['members']),
                        'published_import_id':published,'source_ids':[f['collection_source_id'] for f in source_files]},
                        ensure_ascii=False),batch_id))
        import_id=service.import_bundle(imported,capture_local=False,publish=mark_publication)
        current=service.foundation()
        result={'import_id':import_id,'member_import_id':current['member_import_id'],
            'status':'complete','check_date':day,'operation_id':operation_id,
            'added':sorted(new_by_code.keys()-old_by_code.keys()),
            'retained_missing':missing,'files':bundle['files'],
            'warning':f'来源暂缺 {len(missing)} 家，已保留原名单' if missing else None}
        (directory/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    except Exception as exc:
        with service.db.connection() as conn:
            pending=[r[0] for r in conn.execute("SELECT id FROM collection_batches WHERE operation_id=? AND status='running'",
                (operation_id,))]
        for batch_id in pending:
            finish_batch(service.db,batch_id,'failed','unknown',error=type(exc).__name__+': '+str(exc))
        result={'status':'failed','check_date':day,'warning':'名单检查失败，沿用已保存名单：'+str(exc)}
    with service.db.connection(write=True) as conn:
        conn.execute('UPDATE sw_membership_checks SET finished_at=?,status=?,member_import_id=?,result_json=?,error=? WHERE check_date=?',
            (datetime.now(timezone.utc).isoformat(),result['status'],result.get('member_import_id'),
             json.dumps(result,ensure_ascii=False),result.get('warning'),day))
    progress(result.get('warning') or '今日全市场名单检查完成')
    return result


if __name__ == '__main__':
    import argparse
    from stock_analysis.domains.industries.industry_service import IndustryService
    from stock_analysis.infrastructure.storage import Database, DEFAULT_DATABASE, instance_lock
    parser=argparse.ArgumentParser(description='通过公共采集更新申万分类及官方沪深名单')
    parser.add_argument('--database',type=Path,default=DEFAULT_DATABASE)
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    with instance_lock(args.database):
        db=Database(args.database);db.initialize()
        service=IndustryService(db,args.directory)
        day=datetime.now(SHANGHAI).date().isoformat()
        with db.connection(write=True) as conn:
            run_id=conn.execute("INSERT INTO collection_operations(action,target,recheck,started_at,status) "
                "VALUES('classification',?,1,?,'running')",(day,datetime.now(timezone.utc).isoformat())).lastrowid
        result=ensure_daily_memberships(service,lambda message:print(message,flush=True),today=day,run_id=run_id)
        with db.connection(write=True) as conn:
            conn.execute('UPDATE collection_operations SET finished_at=?,status=?,result_json=?,error=? WHERE id=?',
                (datetime.now(timezone.utc).isoformat(),result['status'],json.dumps(result,ensure_ascii=False),
                 result.get('warning'),run_id))
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=False))
