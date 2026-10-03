"""Bounded pilot backfill: three exact quarter ends, never full-market history."""
from __future__ import annotations

import hashlib
import json
import time
from calendar import monthrange
from datetime import date, datetime, timezone
from pathlib import Path

from .industry_sources import PILOT_INDUSTRIES, number
from .network import create_data_session
from .valuation import BAIDU_URL

TARGETS = ('2025-12-31', '2026-03-31', '2026-06-30')


def parse_history(payload):
    try:
        chart=payload['Result'][0]['DisplayData']['resultData']['tplData']['result']['chartInfo'][0]
        if chart['header'][0] != '总市值' or chart.get('type','近一年') != '近一年':
            raise ValueError('返回的指标或窗口不同')
        rows=chart['body']
        if not isinstance(rows,list) or not rows:
            raise ValueError('历史为空')
        values={}
        for observed,value in rows:
            date.fromisoformat(observed)
            if observed in values:
                raise ValueError('日期重复')
            value=number(value)
            values[observed]=value if value is not None and value>0 else None
        return values
    except (KeyError,IndexError,TypeError,ValueError) as exc:
        raise ValueError('百度总市值历史格式或口径无效') from exc


def quarter_facts(code, values, targets, reference, provenance):
    """Exact quarter dates only; independently compare the common current date."""
    common=values.get(reference['trade_date'])
    if common is None or abs(common-reference['total_cap']/1e8)>.020001:
        raise ValueError(f'{code} 同日总市值与腾讯不一致，未回填')
    facts=[]
    for target in targets:
        parsed=date.fromisoformat(target)
        if parsed.month not in {3,6,9,12} or parsed.day!=monthrange(parsed.year,parsed.month)[1] or target>=reference['trade_date']:
            raise ValueError('仅允许回填更早的精确季末日期')
        value=values.get(target)
        if value is not None:
            facts.append({'stock_code':code,'trade_date':target,'total_cap':value*1e8,
                'provenance':{**provenance,'source':'baidu:opendata','field':'总市值','source_unit':'亿元',
                    'method':'来源精确季末总市值 × 1e8','source_date':target,'composition':'current_constituents_backfill',
                    'validation_date':reference['trade_date'],'validation_tencent_field':'45',
                    'validation_difference_yi':abs(common-reference['total_cap']/1e8)}})
    return facts


def build(db, source_bundle, directory, *, progress=lambda message:None):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    bundle=json.loads(Path(source_bundle).read_text(encoding='utf-8'))
    stocks=sorted(m['stock_code'] for m in bundle['members'] if m['industry_code'] in PILOT_INDUSTRIES)
    if len(stocks)>60:
        raise ValueError('超过 60 家试运行上限，不扩大抓取')
    references={r['stock_code']:r for r in bundle['caps'] if r['provenance'].get('source')=='tencent:qt.gtimg.cn' and r['provenance'].get('field')=='45'}
    cached={}
    dates=(*TARGETS,*{r['trade_date'] for r in references.values()})
    with db.connection() as conn:
        known_days={r[0] for r in conn.execute('SELECT DISTINCT trade_date FROM raw_daily_prices WHERE trade_date IN ('+
                       ','.join('?' for _ in TARGETS)+')',TARGETS)}
        if set(TARGETS)-known_days:
            raise ValueError('季末日期未在本地真实交易日资料中确认')
        for r in conn.execute("SELECT i.code,v.observed_on,v.value,v.content_hash FROM valuation_observations v "
                "JOIN instruments i ON i.id=v.instrument_id WHERE v.metric='market_cap' AND v.source='baidu:opendata' "
                'AND v.observed_on IN ('+','.join('?' for _ in dates)+')',dates):
            cached.setdefault(r['code'],{})[r['observed_on']]=(r['value'],r['content_hash'])
    added=[];checks=[];files=[];failures=[];network_count=0
    with create_data_session() as session:
        for index,code in enumerate(stocks,1):
            progress(f'历史市值小范围验证 {index}/{len(stocks)} · {code}')
            reference=references.get(code)
            if not reference:
                failures.append({'code':code,'error':'没有同日腾讯总市值，未回填'});continue
            try:
                local=cached.get(code,{})
                required=(*TARGETS,reference['trade_date'])
                if all(d in local for d in required):
                    values={d:v[0] for d,v in local.items()}
                    provenance={'reuse':'local valuation_observations','row_hashes':{d:local[d][1] for d in required}}
                else:
                    params={'openapi':'1','dspName':'iphone','tn':'tangram','client':'app','query':'总市值',
                            'code':code,'word':'','resource_id':'51171','market':'ab','tag':'总市值',
                            'chart_select':'近一年','industry_select':'','skip_industry':'1','finClientType':'pc'}
                    path=directory/(code+'.json')
                    if not path.exists():
                        response=session.get(BAIDU_URL,params=params,timeout=20);response.raise_for_status()
                        payload=response.json();parse_history(payload)
                        path.write_text(json.dumps(payload,ensure_ascii=False),encoding='utf-8')
                        network_count+=1;time.sleep(.5)
                    values=parse_history(json.loads(path.read_text(encoding='utf-8')))
                    provenance={'url':BAIDU_URL,'params':params,'file':str(path.resolve()),
                                'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
                    files.append(provenance)
                facts=quarter_facts(code,values,TARGETS,reference,provenance)
                added.extend(facts)
                checks.append({'code':code,'reference_date':reference['trade_date'],
                               'difference_yi':abs(values[reference['trade_date']]-reference['total_cap']/1e8),
                               'quarter_count':len(facts),'reused_local':all(d in local for d in required)})
            except Exception as exc:
                failures.append({'code':code,'error':str(exc)})
    report={'targets':list(TARGETS),'stock_count':len(stocks),'network_count':network_count,
            'quarter_records':len(added),'checks':checks,'failures':failures}
    (directory/'validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    # Publish only if the two complete pilot industries remain complete across all target dates.
    if len(added)!=len(stocks)*len(TARGETS):
        raise ValueError('试运行季末市值未全部通过验证；原数据不变，详情见 validation.json')
    existing={(r['stock_code'],r['trade_date']):r for r in bundle['caps']}
    existing.update({(r['stock_code'],r['trade_date']):r for r in added})
    bundle['caps']=list(existing.values())
    bundle['obtained_at']=datetime.now(timezone.utc).isoformat()
    bundle['manifest'].update(cap_history={'target_dates':list(TARGETS),'composition':'current_constituents_backfill',
        'source':'baidu:opendata','reference_source':'tencent:qt.gtimg.cn:field45','network_count':network_count,
        'files':files,'validation_file':str((directory/'validation.json').resolve())})
    bundle['manifest']['cap_count']=len(bundle['caps'])
    (directory/'bundle.json').write_text(json.dumps(bundle,ensure_ascii=False,allow_nan=False),encoding='utf-8')
    return bundle


if __name__=='__main__':
    import argparse
    from .storage import Database,DEFAULT_DATABASE
    parser=argparse.ArgumentParser();parser.add_argument('bundle',type=Path);parser.add_argument('directory',type=Path)
    parser.add_argument('--database',type=Path,default=DEFAULT_DATABASE)
    args=parser.parse_args()
    build(Database(args.database),args.bundle,args.directory,progress=lambda msg:print(msg,flush=True))
