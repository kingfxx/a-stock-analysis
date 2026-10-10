"""Read-only concise checklist evidence; deterministic finance and trends."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
import json
import math
from .analysis_snapshot import digest, SnapshotError
from .storage import utc_now
from .sources import normalize_code
from .valuation import valuation_summary_observations
from .fundamental_service import FundamentalService
from .core import build_period_rows, view_rows
from .checklist_validation import ITEMS
from .company_report_service import PARSER_VERSION, EXTRACTION_VERSION
INPUT_VERSION="stock_checklist_input_v1"
CALCULATION_VERSION="stock_checklist_calc_v4"
PROFILE={"style":"qualitative_checklist","chips_window":"one_year_weekly","holders_periods":3,"valuation_years":10}
FIELDS={'revenue':'BIZTOTINCO','profit':'PARENETP','profit_cut':'NPCUT','cash_flow':'MANANETR','roe':'ROEWEIGHTED','debt_ratio':'ASSLIABRT','cash':'CURFDS'}
FALLBACK={'revenue':'revenue_ytd','profit':'profit_ytd','cash_flow':'operating_cash_flow_ytd','cash':'monetary_funds'}

def number(value):
    try:
        result=float(value)
        return result if math.isfinite(result) else None
    except (ValueError,TypeError):return None

def capture(db, code, *, as_of=None):
    code=normalize_code(code);today=as_of or datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat();evidence=[];manifest={}
    def add(key,metric,value,observed_on,source,**extra):
        evidence.append({'id':key,'metric':metric,'value':value,'observed_on':observed_on,'source':source,**extra})
    with db.shared_reader() as reader:
        conn=reader.conn
        instrument=conn.execute('SELECT * FROM instruments WHERE code=?',(code,)).fetchone()
        if not instrument:raise SnapshotError('没有本地股票数据，请先刷新股票')
        identity=instrument['id']
        def rows(table,order):return [dict(r) for r in conn.execute(f'SELECT * FROM {table} WHERE instrument_id=? ORDER BY {order}',(identity,))]
        reports=[r for r in rows('financial_reports','period,source,report_type') if r['period']<=today and (not r['publish_date'] or r['publish_date']<=today)]
        periods=sorted({r['period'] for r in reports})
        local=FundamentalService(reader,Path('.')).read(code)['reports']
        local_by_period={r['period']:r for r in local}
        selected=[]
        if periods:
            latest=periods[-1];prior=str(int(latest[:4])-1)+latest[4:]
            selected=[p for p in [latest,prior, str(int(latest[:4])-1)+'-12-31'] if p in periods]
        for period in dict.fromkeys(selected):
            values={};basis={}
            for kind in ('gjzb','lrb','fzb','llb'):
                rr=[r for r in reports if r['period']==period and r['report_type']==kind and r['source'].startswith('sina:')]
                for r in rr:
                    for item in json.loads(r['raw_json']).get('data',[]):
                        for name,field in FIELDS.items():
                            v=number(item.get('item_value'))
                            if item.get('item_field')==field and v is not None and name not in values:
                                values[name]=v;basis[name]={'field':field,'report_type':kind,'unit':'%' if name in ('roe','debt_ratio') else '元'}
            fallback=local_by_period.get(period,{})
            for name,field in FALLBACK.items():
                if name not in values and number(fallback.get(field)) is not None:values[name]=number(fallback[field]);basis[name]={'field':field,'report_type':'normalized_local','unit':'元'}
            overrides=conn.execute('SELECT field_name,value_json FROM report_overrides WHERE instrument_id=? AND period=?',(identity,period)).fetchall()
            for override in overrides:
                for name,field in FALLBACK.items():
                    if override['field_name']==field and number(json.loads(override['value_json'])) is not None:values[name]=number(json.loads(override['value_json']));basis[name]={'field':field,'source':'manual','unit':'元'}
            add('financial.'+period,'financial_period',values,period,'sina/normalized/manual',basis=basis,methodology='累计财务金额、加权ROE；半年值仅与同期间比较，不将加权ROE相加为TTM')
        published_local=[r for r in local if r['period']<=today and (not r.get('publish_date') or r['publish_date']<=today)]
        ttm=view_rows(build_period_rows(published_local,[],[]),'ttm')
        if ttm:
            latest_ttm=ttm[-1]
            values={name:latest_ttm[name] for name in ('roe','roic','gross_margin','net_margin','capex','free_cash_flow','interest_bearing_debt','net_cash') if number(latest_ttm.get(name)) is not None}
            if values:add('financial.calculated.ttm','financial_period',values,latest_ttm['period'],'existing_local_calculation',period_type='ttm',methodology='复用页面计算：ROE与ROIC为TTM简化估算，不是gjzb加权ROE；债务、净现金为期末值，CapEx/自由现金流为TTM。优先展示gjzb同期加权ROE；估算只作补充，现金及债务口径不包含全部附注理财或定存。')
        if len(selected)>=2:
            current=evidence[0]['value'];base=next((e['value'] for e in evidence if e['observed_on']==str(int(selected[0][:4])-1)+selected[0][4:]),{})
            changes={k:100*(v/base[k]-1) for k,v in current.items() if k in ('revenue','profit','profit_cut','cash_flow') and base.get(k) is not None and base[k]>0}
            if changes:add('financial.yoy','yoy',changes,selected[0],'local_calculation',baseline_on=str(int(selected[0][:4])-1)+selected[0][4:],unit='%')
        dividends=[r for r in rows('dividend_events','report_period,source,id') if r['status']=='implemented' and r['ex_dividend_date'] and r['ex_dividend_date']<=today]
        distinct={}
        for r in sorted(dividends,key=lambda x:x['source']=='legacy'):
            distinct.setdefault((r['report_period'],r['ex_dividend_date']),r)
        annual={};missing_dividends=[]
        for r in distinct.values():
            if not r['report_period']:continue
            y=r['report_period'][:4]
            if y>=today[:4]:continue
            if r['cash_per_ten'] is None:missing_dividends.append(r['report_period']);continue
            annual[y]=annual.get(y,0)+r['cash_per_ten']/10
        if annual:add('dividend.annual','cash_dividend',dict(sorted(annual.items())[-5:]),max(r['ex_dividend_date'] for r in dividends),'local_dividend_events',unit='元/股',missing_amounts=missing_dividends,methodology='已实施事件去重，按归属年度汇总，包括季中及特别分红；不代表送转股调整后的历史同股本口径')
        raw=[r for r in rows('raw_daily_prices','trade_date') if r['trade_date']<=today];trade_dates={r['trade_date'] for r in raw}
        observations={}
        for r in rows('valuation_observations','observed_on,source'):
            if r['observed_on']>today or (trade_dates and r['observed_on']>raw[-1]['trade_date']):continue
            point=observations.setdefault(r['observed_on'],{'date':r['observed_on']});point[r['metric']]=r['value'];point[r['metric']+'_date']=r['observed_on']
            if r['metric']=='pe':point['pe_raw']=r['value']
        obs=list(observations.values());end=raw[-1]['trade_date'] if raw else today
        for metric in ('pe','pb'):
            summary=valuation_summary_observations(obs,metric,10,{},as_of=end,trading_dates=trade_dates)
            summary={k:v for k,v in summary.items() if k not in ('rows','rows_by_frequency','negative_pe_ranges')}
            summary.update(percentile_unit='%',percentile_scale='0-100',percentile_methodology='低于当前估值的有效历史样本占比；0.8表示0.8%，不是80%')
            latest_metric=next((r for r in reversed(obs) if metric in r),None)
            if latest_metric and (number(latest_metric[metric]) is None or latest_metric[metric]<=0):
                summary.update(current=None,current_date=latest_metric['date'],percentile=None,unavailable_reason='最新值非正数，不能解释正值历史分位')
            if latest_metric:add('valuation.'+metric+'.10y',metric,summary,summary['current_date'],'local_valuation',years=10)
        if obs and obs[-1].get('market_cap') is not None:add('valuation.market_cap','market_cap',obs[-1]['market_cap'],obs[-1]['date'],'local_valuation',unit='亿元')
        version=conn.execute("SELECT id FROM adjusted_price_versions WHERE instrument_id=? AND status='complete' ORDER BY coverage_end DESC,id DESC LIMIT 1",(identity,)).fetchone()
        if version:
            qfq=[dict(r) for r in conn.execute('SELECT trade_date,close FROM adjusted_daily_prices WHERE version_id=? AND trade_date<=? ORDER BY trade_date',(version[0],today))]
            if qfq:
                from .analysis_chips import months_before
                start=months_before(qfq[-1]['trade_date'],3);qfq=[r for r in qfq if r['trade_date']>=start]
                add('price.qfq.trend','price_trend',qfq,qfq[-1]['trade_date'],'local_qfq',version_id=version[0],adjustment='qfq')
        holders=[r for r in rows('shareholder_observations','stat_date,source') if r['stat_date']<=today and (not r['announced_on'] or r['announced_on']<=today) and r['holder_scope']!='unknown']
        if holders:
            chosen=min({(r['source'],r['holder_scope']) for r in holders},key=lambda k:(-max(int(r['stat_date'].replace('-','')) for r in holders if (r['source'],r['holder_scope'])==k),k[0].startswith('legacy'),k))
            h={r['stat_date']:r for r in holders if (r['source'],r['holder_scope'])==chosen};h=[h[d] for d in sorted(h)[-3:]]
            add('shareholders.latest3','holders',[{'date':r['stat_date'],'holders':r['holders'],'change_pct':100*(r['holders']/h[j-1]['holders']-1) if j and h[j-1]['holders'] else None} for j,r in enumerate(h)],h[-1]['stat_date'],chosen[0],scope=chosen[1],methodology='最近三个不同统计日、相同来源和口径；非必须季末')
        financing=[r for r in rows('financing_daily','trade_date,source') if r['trade_date']<=today]
        if financing:
            source=financing[-1]['source'];f=[r for r in financing if r['source']==source];end=date.fromisoformat(f[-1]['trade_date']);start=end.replace(year=end.year-1) if not (end.month==2 and end.day==29) else end.replace(year=end.year-1,day=28);f=[r for r in f if r['trade_date']>=start.isoformat()];weeks={}
            for r in f:
                day=date.fromisoformat(r['trade_date']);key=(day-timedelta(days=day.weekday())).isoformat();w=weeks.setdefault(key,{'week_start':key,'date':r['trade_date'],'balance':r['margin_balance'],'net_buy':0,'days':0,'net_buy_days':0});w.update(date=r['trade_date'],balance=r['margin_balance']);w['days']+=1
                if r['net_buy'] is not None:w['net_buy']+=r['net_buy'];w['net_buy_days']+=1
            weekly=list(weeks.values())
            for w in weekly:
                if w['net_buy_days']!=w['days']:w['net_buy']=None
                w['partial_week']=w['week_start']<start.isoformat() or date.fromisoformat(w['week_start'])+timedelta(days=6)>end
            add('financing.year.weekly','financing',{'weekly':weekly,'start_on':f[0]['trade_date'],'end_on':f[-1]['trade_date'],'change_pct':100*(f[-1]['margin_balance']/f[0]['margin_balance']-1) if f[0]['margin_balance']>0 else None,'observed_days':len(f),'missing_known_days':sorted(d for d in trade_dates if start.isoformat()<=d<=end.isoformat() and d not in {r['trade_date'] for r in f})},f[-1]['trade_date'],source,unit='元',methodology='融资余额取周末最后有效观测，净买入按周求和，缺失不按零；缺周不补值')
        documents=[dict(r) for r in conn.execute("SELECT d.*,p.id AS parse_id FROM company_report_documents d JOIN company_report_parses p ON p.document_id=d.id WHERE d.instrument_id=? AND p.status='succeeded' AND p.parser_version=? AND d.report_period<=? AND (d.published_on IS NULL OR d.published_on<=?) ORDER BY d.report_period DESC,d.id DESC",(identity,PARSER_VERSION,today,today))]
        used=set();selected_docs=[];seen_topics=set()
        for d in documents:
            if d['report_type'] in used:continue
            used.add(d['report_type']);selected_docs.append(d)
            doc_pages={}
            for fact in conn.execute('SELECT * FROM company_report_facts WHERE parse_id=? AND extraction_version=? ORDER BY topic',(d['parse_id'],EXTRACTION_VERSION)):
                values=json.loads(fact['facts_json'])
                if d['report_type']=='annual' and fact['topic'] in seen_topics and fact['topic'] not in ('strategy','control','bargaining','products','market','competition'):continue
                if values:seen_topics.add(fact['topic'])
                for page in values:
                    record=doc_pages.setdefault(page['pdf_page'],{'page':page,'topics':[]})
                    record['topics'].append(fact['topic'])
            for page_no,record in sorted(doc_pages.items()):
                add('company.page.'+str(d['id'])+'.'+str(page_no),'report_excerpt',[record['page']],d['report_period'],d['source_url'],topics=record['topics'],label='财报 PDF 第 '+str(page_no)+' 页',file_hash=d['content_hash'],published_on=d['published_on'],relative_path=d['relative_path'],document_id=d['id'],parser_version=PARSER_VERSION,extraction_version=EXTRACTION_VERSION)
        updating=bool(conn.execute("SELECT 1 FROM sync_runs WHERE instrument_id=? AND status='running'",(identity,)).fetchone())
        manifest={'company_documents':selected_docs,'financial_records':[{k:r[k] for k in ('source','report_type','period','content_hash')} for r in reports if r['period'] in selected],'qfq_version':version[0] if version else None}
    add('company.identity','company',{'code':code,'name':instrument['name']},None,'local_instrument')
    ids={e['id'] for e in evidence};missing=[]
    topics={topic for e in evidence for topic in e.get('topics',[])}
    for key,_ in ITEMS:
        if key in ('strategy','products','market','control','cycle','competition','value_chain','bargaining') and key not in topics:missing.append(key)
    financial_current=next((e['value'] for e in evidence if e['metric']=='financial_period'),{})
    checks={'revenue':'revenue' in financial_current,'profit':'profit' in financial_current,'balance':'debt_ratio' in financial_current or 'cash' in financial_current,'roe':'roe' in financial_current,'dividend':'dividend.annual' in ids,'market_cap':'valuation.market_cap' in ids,'valuation':any(i.startswith('valuation.pe') or i.startswith('valuation.pb') for i in ids),'price':'price.qfq.trend' in ids,'chips':any(e['metric']=='holders' and len(e['value'])==3 for e in evidence) and any(e['metric']=='financing' and len(e['value']['weekly'])>=48 and not e['value']['missing_known_days'] for e in evidence)}
    missing += [k for k,v in checks.items() if not v]
    quality={'limited':bool(missing),'missing_items':missing,'missing':missing,'updating':updating,'source_errors':[],'dates':{'financial':periods[-1] if periods else None,'price':raw[-1]['trade_date'] if raw else None,'valuation':obs[-1]['date'] if obs else None,'shareholders':next((e['observed_on'] for e in evidence if e['metric']=='holders'),None),'financing':next((e['observed_on'] for e in evidence if e['metric']=='financing'),None)}}
    add('context.data_quality','data_quality',quality,None,'local')
    if not selected and not selected_docs:raise SnapshotError('没有可用财务或公司资料，请先更新数据或准备财报')
    data={'schema_version':INPUT_VERSION,'calculation_version':CALCULATION_VERSION,'instrument':{'code':code,'name':instrument['name']},'analysis_profile':PROFILE,'checklist_items':[{'id':i,'title':t} for i,t in ITEMS],'evidence':evidence,'allowed_evidence_ids':[e['id'] for e in evidence],'quality':quality,'limitations':['每项一至两句，缺失明确标注；报告期景气不等于实时景气；不产生投资评分或买卖结论。']}
    return {'instrument_id':identity,'input':data,'hash':digest(data),'manifest':manifest,'quality':quality,'captured_at':utc_now()}
