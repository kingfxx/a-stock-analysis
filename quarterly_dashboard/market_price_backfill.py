"""Read-only collection, verified rehearsal, then atomic sparse-price import.

Run collection in the existing BigQuant SDK environment. Credentials remain in
memory; SDK diagnostics are suppressed.
"""
from __future__ import annotations

import argparse
import contextlib
from datetime import date, timedelta
import gzip
import hashlib
import io
import json
import math
from pathlib import Path
import re
import sqlite3
import sys

if sys.prefix != sys.base_prefix:
    sys.path.append(str(Path(sys.base_prefix)/'Lib/site-packages'))

from .storage import Database, DEFAULT_DATABASE, instance_lock, utc_now
from .sources import TENCENT_URL, parse_price_history
from .network import create_data_session

ROOT = Path(__file__).resolve().parents[1]
SOURCE = 'bigquant:cn_stock_real_bar1d'


def encoded(value):
    return json.dumps(value,ensure_ascii=False,allow_nan=False,sort_keys=True,separators=(',',':'))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(encoded(value),encoding='utf-8');temp.replace(path)


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def archive(folder,name,payload,source,dataset,params):
    path=folder/(name+'.json.gz')
    path.write_bytes(gzip.compress(encoded(payload).encode('utf-8'),mtime=0))
    return {'source':source,'dataset':dataset,'params':params,
            'file_path':path.relative_to(ROOT).as_posix(),'content_hash':digest(path),'obtained_at':utc_now()}


def periods(start,end):
    result=[]
    for year in range(date.fromisoformat(start).year,date.fromisoformat(end).year+1):
        for suffix in ('03-31','06-30','09-30','12-31'):
            day=f'{year}-{suffix}'
            if start<=day<=end:result.append(day)
    if not result or result[0]!=start or result[-1]!=end or end>=date.today().isoformat():
        raise ValueError('范围必须是已结束的季度末日期')
    return result


def inventory(db_path,folder,start,end):
    conn=sqlite3.connect(db_path.resolve().as_uri()+'?mode=ro',uri=True)
    conn.row_factory=sqlite3.Row
    try:
        member_id=conn.execute('SELECT member_import_id FROM sw_imports ORDER BY id DESC LIMIT 1').fetchone()[0]
        members=[dict(r) for r in conn.execute('SELECT stock_code,name,listing_date FROM sw_memberships WHERE import_id=? ORDER BY stock_code',(member_id,))]
        frozen=dict(conn.execute('SELECT quarter,target_date FROM sw_cap_quarter_rosters'))
    finally:conn.close()
    targets=[];sources=[]
    selected=periods(start,end)
    with create_data_session() as session:
        for year in sorted({p[:4] for p in selected}):
            path=folder/f'calendar-{year}.json'
            if path.exists():
                stored=load(path);sources.append(stored['source']);days=stored['days']
            else:
                params={'param':f'sh000001,day,,{max(p for p in selected if p.startswith(year))},365,'}
                response=session.get(TENCENT_URL,params=params,headers={'Referer':'https://gu.qq.com/'},timeout=18)
                response.raise_for_status();payload=response.json()
                days=[r['date'] for r in parse_price_history(payload,'sh000001','') if r['date'].startswith(year)]
                source=archive(folder,'calendar-'+year,payload,'tencent:index','sh000001.day',params)
                save(path,{'days':days,'source':source});sources.append(source)
            for period in (p for p in selected if p.startswith(year)):
                candidates=[d for d in days if (date.fromisoformat(period)-timedelta(days=20)).isoformat()<=d<=period]
                if not candidates:raise ValueError(f'{period} 无法确认最后交易日')
                actual=max(candidates)
                quarter=f'{year}Q{int(period[5:7])//3}'
                if frozen.get(quarter,actual)!=actual:
                    raise ValueError(f'{quarter} 原市值交易日与指数日历不符，需核对，未改变原值')
                targets.append({'quarter_end':period,'trade_date':actual})
    result={'member_import_id':member_id,'members':members,'targets':targets,'calendar_sources':sources,
            'basis':'current SSE/SZSE members backfill, not historical constituents'}
    save(folder/'inventory.json',result)
    return result


def collect(folder,credentials):
    inv=load(folder/'inventory.json')
    secret=credentials.read_text(encoding='utf-8-sig').strip()
    ak,sep,sk=secret.partition('.')
    if not sep or not ak or not sk or '\n' in secret:raise ValueError('BigQuant 凭据格式无效')
    # Ask the service on each collection: credentials may have been renewed.
    with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
        try:
            import bigquant
            from bigquant import dai
            bigquant.init(ak=ak,sk=sk)
            for year in sorted({r['quarter_end'][:4] for r in inv['targets']}):
                path=folder/f'prices-{year}.json'
                if path.exists():continue
                dates=[r['trade_date'] for r in inv['targets'] if r['quarter_end'].startswith(year)]
                sql='SELECT date,instrument,close FROM cn_stock_real_bar1d WHERE date IN ('+','.join("'"+d+"'" for d in dates)+') ORDER BY date,instrument'
                filters={'date':[min(dates),max(dates)]}
                frame=dai.query(sql,filters=filters).df()
                records=json.loads(frame.to_json(orient='records',date_format='iso'))
                source=archive(folder,'prices-'+year,records,SOURCE,'cn_stock_real_bar1d',{'sql':sql,'filters':filters})
                save(path,{'source':source})
        except Exception as exc:
            message=str(exc)
            for token in (secret,ak,sk):message=message.replace(token,'[REDACTED]')
            raise ValueError(type(exc).__name__+': '+message[:600]) from None
    return prepare(folder)


def positive(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value>0


def prepare(folder):
    inv=load(folder/'inventory.json');sources=list(inv['calendar_sources']);observations={}
    dates={r['trade_date'] for r in inv['targets']}
    for year in sorted({r['quarter_end'][:4] for r in inv['targets']}):
        item=load(folder/f'prices-{year}.json');source_index=len(sources);sources.append(item['source'])
        response=ROOT/item['source']['file_path']
        if not response.resolve().is_relative_to(ROOT) or digest(response)!=item['source']['content_hash']:
            raise ValueError('来源响应哈希不符')
        records=json.loads(gzip.decompress(response.read_bytes()).decode('utf-8'))
        for r in records:
            day=str(r['date'])[:10];symbol=r['instrument']
            if day not in dates or not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)',symbol):
                raise ValueError('来源返回了未请求日期或无效证券代码')
            key=(symbol,day)
            if key in observations:raise ValueError('来源股票日期重复')
            if r['close'] is not None and not positive(r['close']):raise ValueError('来源包含非正或无效价格')
            observations[key]=(r['close'],source_index)
    candidates=[];coverage=[];missing=[]
    for target in inv['targets']:
        counts={**target,'expected':0,'valid':0,'missing':0,'not_listed':0}
        for member in inv['members']:
            code=member['stock_code'];listing=member['listing_date']
            if not listing:raise ValueError('名单缺少上市日期，不能确定适用范围')
            if listing>target['trade_date']:counts['not_listed']+=1;continue
            counts['expected']+=1
            exchange='sh' if code.startswith('6') else 'sz'
            value,source_index=observations.get((code+'.'+exchange.upper(),target['trade_date']),(None,None))
            if value is None:
                counts['missing']+=1;missing.append({'security_code':code,**target,'reason':'exact-date close missing; suspension not established'})
            else:
                counts['valid']+=1;candidates.append({'security_code':code,'exchange':exchange,**target,'close':value,'source_index':source_index})
        coverage.append(counts)
    for source in sources:
        path=ROOT/source['file_path']
        if not path.resolve().is_relative_to(ROOT) or digest(path)!=source['content_hash']:
            raise ValueError('来源响应哈希不符')
    candidate={'member_import_id':inv['member_import_id'],'sources':sources,'rows':candidates,'coverage':coverage}
    save(folder/'candidate.json',candidate);save(folder/'missing.json',missing)
    return {'quarters':len(coverage),'rows':len(candidates),'missing':len(missing),'candidate_hash':digest(folder/'candidate.json')}


def publish(db,candidate,content_hash):
    with db.connection(write=True) as conn:
        existing=conn.execute('SELECT id,result_json FROM market_price_batches WHERE content_hash=? AND status=\'complete\'',(content_hash,)).fetchone()
        if existing:return {**json.loads(existing['result_json']),'batch_id':existing['id'],'reused':True}
        if not conn.execute('SELECT 1 FROM sw_imports WHERE id=?',(candidate['member_import_id'],)).fetchone():
            raise ValueError('来源名单版本不存在')
        now=utc_now();coverage=candidate['coverage']
        batch=conn.execute("INSERT INTO market_price_batches(content_hash,member_import_id,start_period,end_period,started_at,status) VALUES(?,?,?,?,?,'running')",
            (content_hash,candidate['member_import_id'],coverage[0]['quarter_end'],coverage[-1]['quarter_end'],now)).lastrowid
        sources=[]
        for source in candidate['sources']:
            sources.append(conn.execute('INSERT INTO market_price_sources(batch_id,source,dataset,params_json,file_path,content_hash,obtained_at) VALUES(?,?,?,?,?,?,?)',
                (batch,source['source'],source['dataset'],encoded(source['params']),source['file_path'],source['content_hash'],source['obtained_at'])).lastrowid)
        inserted=0;skipped=0
        for row in candidate['rows']:
            if not positive(row['close']) or row['trade_date']>row['quarter_end']:
                raise ValueError('候选价格或日期无效')
            prior=conn.execute('SELECT trade_date,close FROM market_quarterly_prices WHERE security_code=? AND exchange=? AND quarter_end=?',
                               (row['security_code'],row['exchange'],row['quarter_end'])).fetchone()
            if prior:
                if prior['trade_date']!=row['trade_date'] or abs(prior['close']-row['close'])>.000001:
                    raise ValueError('已有季末价格冲突，未覆盖')
                skipped+=1;continue
            conn.execute('INSERT INTO market_quarterly_prices(security_code,exchange,quarter_end,trade_date,close,source_id,batch_id,obtained_at) VALUES(?,?,?,?,?,?,?,?)',
                (row['security_code'],row['exchange'],row['quarter_end'],row['trade_date'],row['close'],sources[row['source_index']],batch,now))
            inserted+=1
        result={'inserted':inserted,'skipped':skipped,'coverage':coverage}
        conn.execute("UPDATE market_price_batches SET status='complete',finished_at=?,result_json=? WHERE id=?",(utc_now(),encoded(result),batch))
        return {**result,'batch_id':batch,'reused':False}


def verify_overlap(db_path,candidate):
    values={(r['security_code'],r['trade_date']):r['close'] for r in candidate['rows']}
    conn=sqlite3.connect(db_path.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        matched=0;differences=[]
        for code,day,close in conn.execute("SELECT i.code,p.trade_date,p.close FROM raw_daily_prices p JOIN instruments i ON i.id=p.instrument_id WHERE p.source='tencent'"):
            value=values.get((code,day))
            if value is not None:
                matched+=1
                if abs(value-close)>.000001:differences.append({'code':code,'day':day,'bigquant':value,'tencent':close})
    finally:conn.close()
    if differences:raise ValueError('与本地腾讯价格有差异：'+encoded(differences[:10]))
    return matched


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('inventory','collect','rehearse','import'))
    parser.add_argument('--db',type=Path,default=DEFAULT_DATABASE)
    parser.add_argument('--folder',type=Path,required=True)
    parser.add_argument('--start',default='2014-09-30');parser.add_argument('--end',default='2026-09-30')
    parser.add_argument('--credentials',type=Path,default=ROOT/'data/credentials/bigquant.txt')
    parser.add_argument('--test-db',type=Path)
    args=parser.parse_args();folder=args.folder.resolve();folder.mkdir(parents=True,exist_ok=True)
    if not folder.is_relative_to(ROOT/'data/verification/samples'):raise ValueError('采集目录须位于 data/verification/samples')
    if args.action=='inventory':
        inv=inventory(args.db,folder,args.start,args.end)
        result={'quarters':len(inv['targets']),'members':len(inv['members'])}
    elif args.action=='collect':result=collect(folder,args.credentials)
    else:
        prepare(folder);path=folder/'candidate.json';candidate=load(path);hashed=digest(path)
        overlap=verify_overlap(args.db,candidate)
        if args.action=='rehearse':
            if not args.test_db or args.test_db.exists():raise ValueError('演练需要不存在的新临时数据库')
            original=Database(args.db);original.backup(args.test_db)
            test=Database(args.test_db);test.initialize()
            result=publish(test,candidate,hashed);test.check()
            result.update(candidate_hash=hashed,matched_tencent=overlap,passed=True)
            save(folder/'rehearsal.json',result)
        else:
            rehearsal=load(folder/'rehearsal.json')
            if not rehearsal.get('passed') or rehearsal.get('candidate_hash')!=hashed:raise ValueError('需先完成同一候选文件的隔离演练')
            with instance_lock(args.db):
                db=Database(args.db)
                backup=args.db.parent/'backups'/('before-quarter-price-'+utc_now().replace(':','').replace('+','')+'.sqlite3')
                db.backup(backup);db.initialize();result=publish(db,candidate,hashed);db.check()
            result.update(backup=str(backup),candidate_hash=hashed,matched_tencent=overlap)
            save(folder/'import-result.json',result)
    # Only summary fields; sources and full coverage remain in the report files.
    print(encoded({k:v for k,v in result.items() if k!='coverage'}))


if __name__=='__main__':main()
