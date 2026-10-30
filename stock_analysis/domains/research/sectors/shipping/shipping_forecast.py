"""Local shipping facts and deterministic, versioned COSCO scenario calculations."""
from calendar import monthrange
from datetime import date, timedelta
import hashlib
import json
import math
import re
import numpy as np

from stock_analysis.collection.collection import encoded
from stock_analysis.domains.financials.financial_storage import decompress_json
from stock_analysis.infrastructure.storage import utc_now

MODEL = 'shipping-linear-anchor-v1'
RATE_KEY = ('index_name','frequency','route','statistic','period_start','period_end','channel')


def publish_rates(db, points, batch, source):
    # Validate the whole segment before the short publication transaction.
    seen = set()
    for p in points:
        key = tuple(p[k] for k in RATE_KEY)
        if key in seen or isinstance(p['value'],bool) or not math.isfinite(p['value']) or p['value'] <= 0:
            raise ValueError('重复键或无效指数')
        if p.get('change_value') is not None and (isinstance(p['change_value'],bool) or not math.isfinite(p['change_value'])):
            raise ValueError('公布变动须为有限数值；零和负值保留')
        seen.add(key)
        if date.fromisoformat(p['period_start']) > date.fromisoformat(p['period_end']):
            raise ValueError('统计区间倒置')
    count = 0
    with db.connection(write=True) as conn:
        if not conn.execute("SELECT 1 FROM collection_sources WHERE id=? AND batch_id=? AND request_status IN ('success','reused')",(source,batch)).fetchone():
            raise ValueError('事实来源与批次不匹配或请求失败')
        for p in points:
            key = tuple(p[k] for k in RATE_KEY)
            previous = conn.execute('SELECT * FROM research_shipping_rate_points WHERE '+
                ' AND '.join(k+'=?' for k in RATE_KEY)+' ORDER BY revision DESC LIMIT 1', key).fetchone()
            # Acquisition/publication time is evidence, not a change in the fact.
            compared = ('value','unit','change_value','change_kind','quality')
            if previous and all(previous[k] == p.get(k) for k in compared):
                continue
            fields = dict(p, revision=previous['revision']+1 if previous else 1,
                supersedes_id=previous['id'] if previous else None, batch_id=batch, source_id=source, created_at=utc_now())
            conn.execute('INSERT INTO research_shipping_rate_points('+','.join(fields)+') VALUES ('+
                         ','.join('?' for _ in fields)+')', tuple(fields.values()))
            count += 1
    return count


def current_rates(conn):
    return [dict(r) for r in conn.execute('SELECT p.* FROM research_shipping_rate_points p WHERE NOT EXISTS '
        '(SELECT 1 FROM research_shipping_rate_points n WHERE n.supersedes_id=p.id) ORDER BY period_start,id')]


def financials(conn):
    reports = {}
    for r in conn.execute("SELECT f.* FROM financial_reports f JOIN instruments i ON i.id=f.instrument_id WHERE i.code='601919' AND f.report_type IN ('gjzb','lrb') ORDER BY period,source"):
        data = json.loads(decompress_json(r['raw_json']))['data']
        reports[r['period'],r['report_type']] = ({x['item_field']: x.get('item_value') for x in data},
                                               dict(hash=r['content_hash'],field_source=r['source'],obtained_at=r['obtained_at']))
    rows = []
    for period in sorted({p for p,k in reports}):
        q = int(period[5:7])//3
        if not q or period[5:] not in ('03-31','06-30','09-30','12-31'):
            continue
        previous = period[:4]+'-'+('03-31','06-30','09-30')[q-2] if q>1 else None
        row = dict(period=period, label=period[:4]+'Q'+str(q), sources={})
        for name,field in [('profit','PARENETP'),('revenue','BIZTOTINCO'),('cost','BIZCOST'),('pretax','TOTPROFIT')]:
            def get(p):
                for kind in (['lrb'] if name in ('cost','pretax') else ['gjzb','lrb']):
                    data, provenance = reports.get((p,kind), ({},{}))
                    v = data.get(field)
                    if v not in (None,'','--'):
                        try:
                            number = float(v)/1e8
                        except (ValueError,TypeError):
                            continue
                        if math.isfinite(number):
                            return number, dict(provenance,period=p,kind=kind,field=field)
                return None, None
            value, origin = get(period)
            prior, prior_origin = get(previous) if previous else (0,None)
            row[name] = value-prior if value is not None and prior is not None else None
            row['sources'][name] = dict(current=origin,previous=prior_origin)
        rows.append(row)
    return rows


def _profit_trend(conn):
    """Return actual single-quarter and directly reported YTD parent profit."""
    reports = {}
    for r in conn.execute("SELECT f.* FROM financial_reports f JOIN instruments i ON i.id=f.instrument_id WHERE i.code='601919' AND f.report_type IN ('gjzb','lrb') ORDER BY period,source"):
        data = json.loads(decompress_json(r['raw_json']))['data']
        reports.setdefault(r['period'], {})[r['report_type']] = (data, dict(source=r['source'], hash=r['content_hash'], obtained_at=r['obtained_at']))
    cumulative = {}
    for period, kinds in reports.items():
        for kind in ('gjzb', 'lrb'):
            data, provenance = kinds.get(kind, ({}, {}))
            field = next((x for x in data if x.get('item_field') == 'PARENETP' and x.get('item_value') not in (None, '', '--')), None)
            if field:
                try: value = float(field['item_value']) / 1e8
                except (TypeError, ValueError): continue
                if math.isfinite(value):
                    cumulative[period] = dict(value=value, source=provenance, kind=kind, field='PARENETP')
                    break
    rows = []
    for period, current in sorted(cumulative.items()):
        q = int(period[5:7]) // 3
        if period[5:] not in ('03-31', '06-30', '09-30', '12-31'):
            continue
        previous_period = period[:4] + '-' + ('03-31', '06-30', '09-30')[q-2] if q > 1 else None
        previous = cumulative.get(previous_period) if previous_period else None
        quarter = current['value'] if q == 1 else current['value']-previous['value'] if previous else None
        rows.append(dict(period=period, label=period[:4]+'Q'+str(q), quarter=quarter,
                         ytd=current['value'], quarter_source=current if q == 1 or previous else None, ytd_source=current))
    return rows


def _canonical_route(route):
    # Weekly source decorates the exact route name with “航线 (…”. Keep
    # geographic names intact: 西非 and 东西非 are different observations.
    return re.sub(r'航线\s*\(.*$', '', route).strip()


def read_trend(db, *, index='CCFI', frequency='monthly', route='综合', years='all', quality='official', profit='none', selected_years=None):
    if index not in ('CCFI', 'SCFI') or frequency not in ('monthly', 'weekly') or years not in ('all', '10', '5', '3', '1') or quality not in ('official', 'preferred') or profit not in ('none', 'quarter', 'ytd'):
        raise ValueError('趋势筛选参数无效')
    statistic = 'monthly_mean' if frequency == 'monthly' else 'release'
    with db.connection() as conn:
        base = ''' FROM research_shipping_rate_points p
            WHERE p.index_name=? AND p.frequency=? AND p.statistic=?
            AND NOT EXISTS (SELECT 1 FROM research_shipping_rate_points n WHERE n.supersedes_id=p.id)'''
        params = [index, frequency, statistic]
        quality_clause = " AND p.quality='official'" if quality == 'official' else ''
        route_rows = conn.execute('SELECT DISTINCT p.route'+base+quality_clause, params)
        routes = sorted({_canonical_route(r['route']) for r in route_rows}, key=lambda v: (v != '综合', v))
        if route not in routes and route != '综合': route = '综合'
        route_clause = ' AND p.route=?' if route == '综合' else ' AND (p.route=? OR p.route LIKE ?)'
        route_params = params + ([route] if route == '综合' else [route, route+'航线 (%'])
        rows = [dict(r) for r in conn.execute('''SELECT p.*,s.obtained_at FROM research_shipping_rate_points p
            LEFT JOIN collection_sources s ON s.id=p.source_id
            WHERE p.index_name=? AND p.frequency=? AND p.statistic=?
            AND NOT EXISTS (SELECT 1 FROM research_shipping_rate_points n WHERE n.supersedes_id=p.id)'''+quality_clause+route_clause+' ORDER BY p.period_start,p.id', route_params)]
        rows.sort(key=lambda r: (r['period_start'], r['id']))
        # Resolve duplicates from equivalent weekly/monthly names only by
        # official preference, then newest revision; retain source diagnostics.
        grouped = {}
        for r in rows:
            key = (r['period_start'], r['period_end'])
            old = grouped.get(key)
            if old is None or (r['quality'] == 'official', r['revision'], r['id']) > (old['quality'] == 'official', old['revision'], old['id']): grouped[key] = r
        rows = list(grouped.values())
        cutoff = None
        if rows:
            latest_date = max(r['period_end'] if frequency == 'monthly' else r['published_on'] or r['period_start'] for r in rows)
            if years != 'all':
                latest = date.fromisoformat(latest_date)
                cutoff_year = latest.year-int(years)
                cutoff = date(cutoff_year, latest.month, min(latest.day, monthrange(cutoff_year, latest.month)[1]))
                rows = [r for r in rows if (r['period_end'] if frequency == 'monthly' else r['published_on'] or r['period_start']) >= cutoff.isoformat()]
        points = []
        for r in rows:
            observed = r['published_on'] or r['period_start']
            d = date.fromisoformat(observed)
            iso = d.isocalendar()
            points.append(dict(id=r['id'], date=(r['period_end'] if frequency == 'monthly' else observed), period_start=r['period_start'], period_end=r['period_end'],
                year=(int(r['period_start'][:4]) if frequency == 'monthly' else iso.year), x=(int(r['period_start'][5:7]) if frequency == 'monthly' else iso.week),
                value=r['value'], unit=r['unit'], route=r['route'], quality=r['quality'], channel=r['channel'], revision=r['revision'], published_on=r['published_on'],
                source_id=r['source_id'], obtained_at=r['obtained_at'], created_at=r['created_at']))
        available_years = sorted({p['year'] for p in points})
        year_selection = [int(y) for y in selected_years or [] if str(y).isdigit() and int(y) in available_years]
        if not selected_years:
            year_selection = available_years[-4:]
        comparison = [p for p in points if p['year'] in year_selection]
        profit_rows = _profit_trend(conn) if profit != 'none' else []
        if cutoff:
            profit_rows = [r for r in profit_rows if r['period'] >= cutoff.isoformat()]
        if rows and profit != 'none':
            rate_start = min(r['period_end'] if frequency == 'monthly' else r['published_on'] or r['period_start'] for r in rows)
            rate_end = max(r['period_end'] if frequency == 'monthly' else r['published_on'] or r['period_start'] for r in rows)
            profit_rows = [r for r in profit_rows if rate_start <= r['period'] <= rate_end]
        elif profit != 'none':
            profit_rows = []
    compare_points = [dict(p, compare_x=(int(p['period_start'][5:7]) if frequency == 'monthly' else p['x'])) for p in comparison]
    profit_rows = [dict(r, value=r['quarter'] if profit == 'quarter' else r['ytd'], x=int(r['label'][-1]), year=int(r['label'][:4])) for r in profit_rows if (r['quarter'] if profit == 'quarter' else r['ytd']) is not None]
    return dict(index=index, frequency=frequency, statistic=statistic, route=route, routes=routes, years=available_years, selected_years=year_selection,
        points=points, comparison=compare_points, profit=profit_rows, quality=quality, profit_mode=profit,
        coverage=dict(count=len(points), first=points[0]['date'] if points else None, last=points[-1]['date'] if points else None,
                      latest_obtained=max((p['obtained_at'] or '' for p in points), default=None),
                      note='范围按最新有效观察滚动截取；首末期不代表连续覆盖。月度为整月均值，周度按真实发布日期，缺期不插值。'))


def business_quarters(conn):
    cumulative = {}
    refs = []
    for r in conn.execute("SELECT m.*,d.report_period FROM research_shipping_report_metrics m JOIN company_report_documents d ON d.id=m.document_id WHERE m.scope IN ('total','domestic') AND m.basis='cumulative' ORDER BY d.report_period,d.id,m.id"):
        r = dict(r)
        cumulative.setdefault(r['report_period'], {})[r['metric']+'_'+r['scope']] = r['value']
        refs.append(r)
    rows = {}
    for p, values in cumulative.items():
        q = int(p[5:7])//3
        prev = p[:4]+'-'+('03-31','06-30','09-30')[q-2] if q>1 else None
        if prev and prev not in cumulative:
            continue
        quarter = {k:v-cumulative[prev][k] if prev and k in cumulative[prev] else v if not prev else None for k,v in values.items()}
        if any(quarter.get(k) is None for k in ('rmb_total','rmb_domestic','teu_total','teu_domestic')):
            continue
        if all(k in quarter for k in ('rmb_total','rmb_domestic','teu_total','teu_domestic')):
            quarter['international'] = (quarter['rmb_total']-quarter['rmb_domestic'])/1e5
            quarter['domestic'] = quarter['rmb_domestic']/1e5
            quarter['teu'] = quarter['teu_total']-quarter['teu_domestic']
            if quarter['teu']>0:
                quarter['unit_rmb'] = quarter['international']*1e8/quarter['teu']
                rows[p] = quarter
    return rows, refs


def months_for(target, lag=1):
    y,q = map(int,target.split('Q'))
    start = y*12+(q-1)*3-lag
    return [f'{n//12:04d}-{n%12+1:02d}' for n in range(start,start+3)]


def rate_window(points, target, *, weekly=False, lead_weeks=2):
    if weekly:
        if lead_weeks not in (2, 3, 4):
            raise ValueError('周度提前周数须为2、3或4周')
        y,q = map(int,target.split('Q'))
        shift=timedelta(weeks=lead_weeks)
        start = date(y,(q-1)*3+1,1)-shift
        end = date(y+1,1,1) if q==4 else date(y,q*3+1,1)
        end -= shift
        selected = [p for p in points if p['frequency']=='weekly' and start.isoformat()<=p['period_start']<end.isoformat()]
        byday = {}
        for p in sorted(selected,key=lambda p:(p['quality']=='official',p['id'])):
            byday[p['period_start']] = p
        values = list(byday.values())
        # Calendars vary; disclose gaps rather than invent holiday observations.
        complete = bool(values) and (date.fromisoformat(values[0]['period_start'])-start).days<=10 and (end-date.fromisoformat(values[-1]['period_start'])).days<=10 and all((date.fromisoformat(b['period_start'])-date.fromisoformat(a['period_start'])).days<=14 for a,b in zip(values,values[1:])) and end<=date.today()
        return dict(value=sum(p['value'] for p in values)/len(values) if values and complete else None,
                    observed_mean=sum(p['value'] for p in values)/len(values) if values else None,
                    count=len(values), complete=complete, start=start.isoformat(),end_exclusive=end.isoformat(),lead_weeks=lead_weeks,
                    refs=[p['id'] for p in values],note=f'整季前移{lead_weeks}周；日期含日历推定，停发/漏周覆盖待核验')
    months = months_for(target)
    selected = {}
    for p in sorted(points,key=lambda p:(p['quality']=='official',p['id'])):
        if p['frequency']=='monthly' and p['statistic']=='monthly_mean' and p['period_start'][:7] in months:
            selected[p['period_start'][:7]] = p
    complete = len(selected)==3
    return dict(value=sum(p['value'] for p in selected.values())/3 if complete else None,
        count=len(selected), complete=complete, months=months, missing=[m for m in months if m not in selected],
        refs=[p['id'] for p in selected.values()], qualities=[p['quality'] for p in selected.values()])


def fit(rows):
    x = np.array([r['rate'] for r in rows]); y = np.array([r['unit_rmb'] for r in rows])
    if len(rows)<5 or np.ptp(x)==0:
        return None
    a,b = np.linalg.lstsq(np.column_stack([np.ones(len(x)),x]), y, rcond=None)[0]
    return dict(intercept=float(a),slope=float(b),n=len(rows),training_end=rows[-1]['period'])


def model_context(db, target, weekly_lead_weeks=2):
    if not isinstance(target,str) or not re.fullmatch(r'20\d{2}Q[1-4]',target):
        raise ValueError('目标季度须为YYYYQn')
    if weekly_lead_weeks not in (2,3,4):
        raise ValueError('周度提前周数须为2、3或4周')
    with db.connection() as conn:
        rates = current_rates(conn)
        history = financials(conn)
        business, metric_refs = business_quarters(conn)
    # Only the business model's actual sample and target-year cumulative inputs
    # belong in a forecast. Older stock history is not a forecast dependency.
    first_period=min(business,default=target[:4]+'-01-01')
    history=[r for r in history if r['period']>=first_period and r['label']<target]
    points = [r for r in rates if r['index_name']=='CCFI' and r['route']=='综合']
    prior = [r for r in history if r['label']<target and r['period'] in business and all(r[k] is not None for k in ('profit','revenue','cost','pretax'))]
    anchor = prior[-1] if prior else None
    used_rates=set()
    for label in [target]+[r['label'] for r in prior]:
        used_rates.update(rate_window(points,label)['refs'])
        used_rates.update(rate_window(points,label,weekly=True,lead_weeks=weekly_lead_weeks)['refs'])
    rows = []
    for r in prior:
        window = rate_window(points,r['label'])
        if window['complete']:
            rows.append(dict(period=r['period'],label=r['label'],rate=window['value'],unit_rmb=business[r['period']]['unit_rmb'],profit=r['profit']))
    model = fit(rows)
    common=[];weekly_rows=[]
    for r in rows:
        weekly=rate_window(points,r['label'],weekly=True,lead_weeks=weekly_lead_weeks)
        if weekly['complete']:
            common.append(dict(r,weekly_rate=weekly['value']))
    for r in prior:
        weekly=rate_window(points,r['label'],weekly=True,lead_weeks=weekly_lead_weeks)
        if weekly['complete'] and r['period'] in business:
            weekly_rows.append(dict(period=r['period'],label=r['label'],rate=weekly['value'],unit_rmb=business[r['period']]['unit_rmb'],profit=r['profit']))
    comparison={}
    for mode in ('monthly','weekly'):
        samples=[dict(r,rate=r['weekly_rate'] if mode=='weekly' else r['rate']) for r in common]
        tests=[]
        for i in range(5,len(samples)):
            candidate=fit(samples[:i]);prev=samples[i-1];actual=samples[i]
            if candidate:
                prediction=prev['unit_rmb']+candidate['slope']*(actual['rate']-prev['rate'])
                tests.append(dict(target=actual['label'],training_end=candidate['training_end'],actual=actual['unit_rmb'],prediction=prediction,error=prediction-actual['unit_rmb']))
        comparison[mode]=dict(model=fit(samples),samples=[r['label'] for r in samples],backtests=tests,
            mae=float(np.mean([abs(r['error']) for r in tests])) if tests else None)
    backtests = []
    for i in range(5,len(rows)):
        m = fit(rows[:i]); prev = rows[i-1]; actual = rows[i]
        if not m:
            continue
        prediction = prev['unit_rmb']+m['slope']*(actual['rate']-prev['rate'])
        backtests.append(dict(target=actual['label'],actual=actual['unit_rmb'],prediction=prediction,
            error=prediction-actual['unit_rmb'],baseline_error=prev['unit_rmb']-actual['unit_rmb'],training_end=m['training_end']))
    forecast_window=rate_window(points,target,weekly=True,lead_weeks=weekly_lead_weeks)
    anchor_weekly_window=rate_window(points,anchor['label'],weekly=True,lead_weeks=weekly_lead_weeks) if anchor else dict(value=None,refs=[])
    context = dict(target=target,model_version=MODEL,model=model,forecast_model=fit(weekly_rows),anchor=anchor,
        business=business.get(anchor['period']) if anchor else None,
        target_window=rate_window(points,target),weekly_window=forecast_window,forecast_window=forecast_window,
        weekly_lead_weeks=weekly_lead_weeks,anchor_window=rate_window(points,anchor['label']) if anchor else None,
        forecast_anchor_window=anchor_weekly_window,
        history=[{k:v for k,v in r.items() if k!='sources'} for r in history],backtests=backtests,window_comparison=comparison,
        validation=dict(mae=float(np.mean([abs(r['error']) for r in backtests])) if backtests else None,
                        baseline_mae=float(np.mean([abs(r['baseline_error']) for r in backtests])) if backtests else None,
                        note='时间滚动的单箱收入验证；现有修订财报，非完整当时可得快照；不验证主观情景'),
        inputs=dict(rate_ids=sorted(used_rates),report_metric_ids=[r['id'] for r in metric_refs if r['report_period']<target[:4]+'-'+('03-31','06-30','09-30','12-31')[int(target[-1])-1]],
                    financial_sources=[r['sources'] for r in history]))
    context['input_hash'] = hashlib.sha256(encoded(context).encode()).hexdigest()
    return context


def defaults(context):
    anchor = context['anchor']; b = context['business']
    if not anchor or not b or anchor['pretax']==0:
        return {}
    common = dict(rate=context.get('forecast_window',context['target_window'])['value'],fx=0,extra=0,override=None)
    return dict(bear=dict(common,revenue_model='manual',elasticity=.55,volume=0,cost=6,below=-18,conversion=72),
        base=dict(common,revenue_model='linear',elasticity=.65,volume=2,cost=4.5,
                  below=anchor['pretax']-(anchor['revenue']-anchor['cost']),conversion=anchor['profit']/anchor['pretax']*100),
        bull=dict(common,revenue_model='manual',elasticity=.75,volume=4,cost=3,below=-10,conversion=75))


def calculate(context, params):
    if not context['anchor'] or not context['business']:
        raise ValueError('缺少前期财务及业务底数')
    expected = set(defaults(context)['base'])
    if not isinstance(params,dict) or set(params)!=expected:
        raise ValueError('情景参数字段无效')
    for k,v in params.items():
        if k=='revenue_model':
            if v not in ('linear','manual'):raise ValueError('收入模型无效')
        elif k in ('override','rate') and v is None:
            continue
        elif isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or abs(v)>1e6:
            raise ValueError('参数须为有限数字')
    if params['volume']<=-100 or params['cost']<=-100 or params['rate'] is not None and params['rate']<=0:
        raise ValueError('箱量、单位成本或指数范围无效')
    if params['rate'] is None and params['override'] is None:
        return dict(profit=None,reason='未来窗口/来源缺值，填写CCFI情景假设或收入覆盖后计算')
    p=params;b=context['business'];a=context['anchor'];
    m=context['forecast_model'] if 'forecast_model' in context else context['model']
    aw=context.get('forecast_anchor_window',context['anchor_window'])['value']
    if p['override'] is not None:
        growth=p['override']/100
    elif p['revenue_model']=='linear':
        if not m or aw is None:raise ValueError('线性模型样本或锚点运价不足')
        growth=(1+m['slope']*(p['rate']-aw)/b['unit_rmb'])*(1+p['fx']/100)-1
    else:
        if aw is None:raise ValueError('锚点运价不足')
        growth=(1+p['elasticity']*(p['rate']/aw-1))*(1+p['fx']/100)-1
    revenue=(b['international']*(1+growth)+a['revenue']-b['international'])*(1+p['volume']/100)
    cost=a['cost']*(1+p['volume']/100)*(1+p['cost']/100)
    pretax=revenue-cost+p['below'];profit=pretax*p['conversion']/100+p['extra']
    cumulative=[r['profit'] for r in context['history'] if r['label']<context['target'] and r['label'][:4]==context['target'][:4]]
    quarter=int(context['target'][-1])
    return dict(growth=growth*100,revenue=revenue,cost=cost,pretax=pretax,profit=profit,
                cumulative=sum(cumulative)+profit if len(cumulative)==quarter-1 and all(v is not None for v in cumulative) else None)


def save_version(db, command):
    if not isinstance(command,dict) or set(command)-{'target','name','scenarios','input_hash','kind','weekly_lead_weeks'}:
        raise ValueError('保存参数无效')
    lead_weeks=command.get('weekly_lead_weeks',2)
    context=model_context(db,command.get('target'),lead_weeks)
    if command.get('input_hash')!=context['input_hash']:
        raise ValueError('本地输入已有修订，请重新载入后另存版本')
    scenarios=command.get('scenarios')
    if not isinstance(scenarios,dict) or set(scenarios)!={'bear','base','bull'}:
        raise ValueError('须保存完整三情景')
    result={k:calculate(context,v) for k,v in scenarios.items()}
    name=command.get('name','方案')
    kind=command.get('kind','forecast')
    if not isinstance(name,str) or not 1<=len(name.strip())<=100 or kind not in ('forecast','calibration'):
        raise ValueError('版本名称或类型无效')
    snapshot=dict(context=context,scenarios=scenarios,results=result)
    with db.connection(write=True) as conn:
        id=conn.execute('INSERT INTO research_shipping_forecast_versions(target,name,kind,model_version,input_hash,snapshot_json,created_at) VALUES(?,?,?,?,?,?,?)',
            (context['target'],name,kind,MODEL,context['input_hash'],encoded(snapshot),utc_now())).lastrowid
    return dict(id=id,results=result)


def read_forecast(db,target='2026Q3',weekly_lead_weeks=2):
    context=model_context(db,target,weekly_lead_weeks)
    with db.connection() as conn:
        versions=[dict(r) for r in conn.execute('SELECT id,target,name,kind,model_version,input_hash,created_at FROM research_shipping_forecast_versions ORDER BY id DESC LIMIT 100')]
        coverage=[dict(r) for r in conn.execute('SELECT index_name,frequency,statistic,channel,quality,count(*) AS rows,min(period_start) AS first,max(period_end) AS last FROM research_shipping_rate_points GROUP BY index_name,frequency,statistic,channel,quality')]
        industry=[dict(r) for r in conn.execute('SELECT p.* FROM research_shipping_industry_metrics p WHERE NOT EXISTS '
            '(SELECT 1 FROM research_shipping_industry_metrics n WHERE n.metric=p.metric AND n.scope=p.scope AND n.period=p.period AND n.basis=p.basis AND n.revision>p.revision) ORDER BY period DESC,id DESC LIMIT 36')]
        sources=[dict(r) for r in conn.execute("SELECT DISTINCT s.id,s.obtained_at,s.request_json,s.raw_sha256,s.parse_status,s.error FROM collection_sources s JOIN collection_batches b ON b.id=s.batch_id WHERE b.domain='research_shipping' ORDER BY s.id DESC LIMIT 60")]
    return dict(context=context,defaults=defaults(context),versions=versions,coverage=coverage,industry=industry,sources=sources,
        limitations=['官方优先仅替换已核验月份；Excel转录及推定日期保留独立身份。','SCFI、航线权重、吞吐量及非线性未获增益证据，不进入默认模型。','累计业务收入/箱量分别差分后相除；模型取目标季度之前的财务，历史回测仍有报告修订可得时点限制。'])


def read_version(db,id):
    with db.connection() as conn:
        row=conn.execute('SELECT * FROM research_shipping_forecast_versions WHERE id=?',(int(id),)).fetchone()
        if not row:raise ValueError('版本不存在')
        row=dict(row);row['snapshot']=json.loads(row.pop('snapshot_json'))
        actual=next((r['profit'] for r in financials(conn) if r['label']==row['target']),None)
    row['actual_profit']=actual
    row['errors']={k:v['profit']-actual if actual is not None and v.get('profit') is not None else None for k,v in row['snapshot']['results'].items()}
    return row


def delete_version(db, id):
    try:
        version_id = int(id)
    except (TypeError, ValueError):
        raise ValueError('版本编号无效')
    if version_id <= 0:
        raise ValueError('版本编号无效')
    with db.connection(write=True) as conn:
        cursor = conn.execute('DELETE FROM research_shipping_forecast_versions WHERE id=?', (version_id,))
        if cursor.rowcount != 1:
            raise ValueError('版本不存在')
    return {'deleted': version_id}
