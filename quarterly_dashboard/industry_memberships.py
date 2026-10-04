"""Official listing dates and a once-per-Shanghai-day membership check."""
from __future__ import annotations

import hashlib
import io
import json
import re
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from .industry_sources import SW_BASE, SWAdapter, parse_memberships, parse_taxonomy
from .network import create_data_session

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


def fetch_directory(directory, asof, progress, *, classification=True):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    files=[]
    def get(session,name,url,params=None,headers=None):
        progress('检查全市场名单 · '+name)
        response=session.get(url,params=params,headers=headers,timeout=30)
        response.raise_for_status()
        raw=response.content;path=directory/name;path.write_bytes(raw)
        files.append({'key':name,'file':str(path.resolve()),'url':url,'params':params,
            'sha256':hashlib.sha256(raw).hexdigest(),'obtained_at':datetime.now(timezone.utc).isoformat(),
            'date_field':('A股上市日期' if name.endswith('.xlsx') else 'LIST_DATE') if name in
                ('sse_main.json','sse_star.json','szse_a.xlsx') else None,
            'definition':'对应代码的 A 股上市日期' if name in
                ('sse_main.json','sse_star.json','szse_a.xlsx') else '申万行业分类及变更',
            'parser_version':'listing_dates_v1'})
        time.sleep(1)
        return raw
    with create_data_session() as session:
        session.mount('https://www.swsresearch.com/',SWAdapter())
        for board,name in [('1','sse_main.json'),('8','sse_star.json')]:
            get(session,name,SSE_URL,{'sqlId':'COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L','STOCK_TYPE':board,
                'COMPANY_STATUS':'2,4,5,7,8','type':'inParams','isPagination':'true',
                'pageHelp.pageSize':'10000','pageHelp.pageNo':'1'},
                {'Referer':'https://www.sse.com.cn/assortment/stock/list/share/'})
        get(session,'szse_a.xlsx',SZSE_URL,{'SHOWTYPE':'xlsx','CATALOGID':'1110','TABKEY':'tab1'},
            {'Referer':'https://www.szse.cn/market/product/stock/list/index.html'})
        if classification:
            taxonomy=parse_taxonomy(get(session,'taxonomy.xls',SW_BASE+'SwClassCode_2021.xls'))
            members_raw=get(session,'members.xls',SW_BASE+'StockClassifyUse_stock.xls')
    listings=parse_listing_files(directory,asof)
    if not 4000<=len(listings)<=10000:
        raise ValueError('官方沪深 A 股名单规模异常')
    if not classification:
        return {'listings':listings,'files':files}
    members,history=parse_memberships(members_raw,taxonomy,{r['stock_code']:r['name'] for r in listings},asof)
    return {'taxonomy':taxonomy,'members':members,'membership_history':history,
            'listings':listings,'files':files}


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


def ensure_daily_memberships(service, progress=lambda message:None, *, today=None):
    day=today or datetime.now(SHANGHAI).date().isoformat()
    now=datetime.now(timezone.utc).isoformat()
    with service.db.connection(write=True) as conn:
        old=conn.execute('SELECT * FROM sw_membership_checks WHERE check_date=?',(day,)).fetchone()
        if old:
            return {'check_date':day,'status':old['status'],'reused':True,
                    'warning':old['error'] or ('今日名单检查尚未完成，沿用已保存名单' if old['status']=='running' else None)}
        conn.execute("INSERT INTO sw_membership_checks(check_date,checked_at,status) VALUES(?,?,'running')",(day,now))
    result={}
    try:
        old=service.foundation()
        service.db.daily_backup(verify_existing=False)
        directory=service.directory/'membership_checks'/day
        directory.mkdir(parents=True,exist_ok=True)
        bundle=fetch_directory(directory,day,progress)
        old_by_code={r['stock_code']:r for r in old['members']}
        new_by_code={r['stock_code']:r for r in bundle['members']}
        if len(new_by_code)<len(old_by_code)*.95:
            raise ValueError('来源名单数量骤减，沿用已保存名单')
        missing=sorted(old_by_code.keys()-new_by_code.keys())
        # A temporarily missing company is retained until an explicit delisting policy is adopted.
        members=list(new_by_code.values())+[old_by_code[s] for s in missing]
        history={(r['stock_code'],r['effective_date'],r['industry_code']):r for r in old['membership_history']}
        history.update({(r['stock_code'],r['effective_date'],r['industry_code']):r for r in bundle['membership_history']})
        for row in members:
            previous=old_by_code.get(row['stock_code'],{})
            if row['industry_code'] is None and previous.get('industry_code'):
                for key in ('industry_code','effective_date','source_update'):
                    row[key]=previous[key]
        imported={**bundle,'members':members,'membership_history':list(history.values()),
            'asof':day,'obtained_at':now,'financials':[],'caps':[],
            'manifest':{'standard':'SW2021','scope':'classification','action':'daily_membership_check',
                'files':bundle['files'],'membership_count':len(members),'pilot_industries':[]}}
        service.import_bundle(imported,capture_local=False)
        result=backfill_listing_dates(service,bundle['listings'],bundle['files'])
        result.update(status='complete',check_date=day,added=sorted(new_by_code.keys()-old_by_code.keys()),
            retained_missing=missing,files=bundle['files'],
            warning=f'来源暂缺 {len(missing)} 家，已保留原名单' if missing else None)
        (directory/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    except Exception as exc:
        result={'status':'failed','check_date':day,'warning':'名单检查失败，沿用已保存名单：'+str(exc)}
    with service.db.connection(write=True) as conn:
        conn.execute('UPDATE sw_membership_checks SET finished_at=?,status=?,member_import_id=?,result_json=?,error=? WHERE check_date=?',
            (datetime.now(timezone.utc).isoformat(),result['status'],result.get('member_import_id'),
             json.dumps(result,ensure_ascii=False),result.get('warning'),day))
    progress(result.get('warning') or '今日全市场名单检查完成')
    return result


if __name__ == '__main__':
    import argparse
    from .industry_service import IndustryService
    from .storage import Database, DEFAULT_DATABASE, instance_lock
    parser=argparse.ArgumentParser(description='直接补齐当前名单上市日期，不发布新名单版本')
    parser.add_argument('--database',type=Path,default=DEFAULT_DATABASE)
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    with instance_lock(args.database):
        db=Database(args.database);db.initialize()
        backup=db.backup(db.path.parent/'backups'/('before-listing-dates-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.sqlite3'))
        service=IndustryService(db)
        bundle=fetch_directory(args.directory,datetime.now(SHANGHAI).date().isoformat(),
            lambda message:print(message,flush=True),classification=False)
        with db.connection() as conn:
            before={name:conn.execute('SELECT count(*) FROM '+name).fetchone()[0] for name in
                ('sw_imports','sw_memberships','sw_financial_facts','sw_cap_facts','sw_cap_quarter_members')}
            rosters=[tuple(r) for r in conn.execute('SELECT * FROM sw_cap_quarter_rosters ORDER BY quarter')]
        result=backfill_listing_dates(service,bundle['listings'],bundle['files'])
        with db.connection() as conn:
            assert before=={name:conn.execute('SELECT count(*) FROM '+name).fetchone()[0] for name in before}
            assert rosters==[tuple(r) for r in conn.execute('SELECT * FROM sw_cap_quarter_rosters ORDER BY quarter')]
            assert not conn.execute('PRAGMA foreign_key_check').fetchall()
            result['coverage']=dict(conn.execute('SELECT count(*) total,count(listing_date) known FROM sw_memberships WHERE import_id=?',
                (result['member_import_id'],)).fetchone())
        result['files']=bundle['files'];result['unchanged_table_counts']=before;result['backup']=str(backup.resolve())
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({k:v for k,v in result.items() if k not in {'changes','files'}},ensure_ascii=False))
