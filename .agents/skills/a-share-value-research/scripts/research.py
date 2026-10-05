"""Independent, standard-library research pack tools. Never import the investment app."""
import argparse
from datetime import date, datetime, timezone, timedelta
import hashlib
import gzip
import html
import json
import math
from pathlib import Path
import re
import sqlite3

SCHEMA = 'equity_research_facts_v1'
VERSION = 'project_research_v2'
# Codes, not ambiguous display labels. gjzb is preferred only for applicable fields.
FIELDS = {
 'revenue': [('gjzb','BIZTOTINCO'),('lrb','BIZTOTINCO')],
 'operating_revenue': [('gjzb','BIZINCO'),('lrb','BIZINCO')],
 'operating_cost': [('gjzb','BIZCOST'),('lrb','BIZCOST')],
 'parent_profit': [('gjzb','PARENETP'),('lrb','PARENETP')],
 'net_profit': [('gjzb','NETPROFIT'),('lrb','NETPROFIT')],
 'deduct_parent_profit': [('gjzb','NPCUT')],
 'operating_profit': [('gjzb','PERPROFIT'),('lrb','PERPROFIT')],
 'cfo': [('gjzb','MANANETR'),('llb','MANANETR')],
 'capex': [('gjzb','ACQUASSETCASH'),('llb','ACQUASSETCASH')],
 'eps_basic': [('gjzb','EPSBASIC'),('lrb','BASICEPS')],
 'source_fcff_per_share': [('gjzb','FCFFPS')],
 'source_fcfe_per_share': [('gjzb','FCFEPS')],
 'roe_weighted': [('gjzb','ROEWEIGHTED')],
 'cash': [('gjzb','CURFDS'),('fzb','CURFDS')],
 'trading_assets': [('gjzb','TRADFINASSET'),('fzb','TRADFINASSET')],
 'receivables': [('gjzb','ACCORECE'),('fzb','ACCORECE')],
 'inventory': [('gjzb','INVE'),('fzb','INVE')],
 'parent_equity': [('gjzb','PARESHARRIGH'),('fzb','PARESHARRIGH')],
 'assets': [('gjzb','TOTASSET'),('fzb','TOTASSET')],
}
POINTS={'cash','trading_assets','receivables','inventory','parent_equity','assets'}
OVERRIDES={'revenue_ytd':'operating_revenue','profit_ytd':'parent_profit',
 'net_profit_ytd':'net_profit','operating_cost_ytd':'operating_cost',
 'operating_cash_flow_ytd':'cfo','capex_ytd':'capex','monetary_funds':'cash','equity':'parent_equity'}


def number(value):
    if isinstance(value,bool):return None
    try:
        result=float(value)
        return result if math.isfinite(result) else None
    except (TypeError,ValueError):return None


def load(path):
    path=Path(path)
    if path.suffix=='.gz':
        with gzip.open(path,'rt',encoding='utf-8-sig') as stream:return json.load(stream)
    return json.loads(path.read_text(encoding='utf-8-sig'))
def save(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    if path.suffix=='.gz':
        raw=json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
        path.write_bytes(gzip.compress(raw,compresslevel=9,mtime=0))
    else:path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
def sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
def inside(root,relative):
    root=Path(root).resolve();target=(root/relative).resolve()
    if not target.is_relative_to(root):raise ValueError('Source path escapes reports root')
    return target
def fact_id(period,metric):return 'fin.'+period+'.'+metric
def rowdict(row):return dict(row)


def readonly(path):
    path=Path(path).resolve()
    if not path.is_file():raise ValueError('Database does not exist: '+str(path))
    conn=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    conn.row_factory=sqlite3.Row;conn.execute('PRAGMA query_only=ON')
    allowed={sqlite3.SQLITE_SELECT,sqlite3.SQLITE_READ,sqlite3.SQLITE_FUNCTION,sqlite3.SQLITE_TRANSACTION}
    conn.set_authorizer(lambda action,*args: sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY)
    conn.execute('BEGIN')
    return conn


def validate_pack(pack):
    errors=[]
    if pack.get('schema_version')!=SCHEMA:errors.append('Unsupported schema_version')
    try:date.fromisoformat(pack['as_of'])
    except (KeyError,ValueError,TypeError):errors.append('Invalid as_of')
    sources={s.get('id'):s for s in pack.get('sources',[])}
    if len(sources)!=len(pack.get('sources',[])):errors.append('Duplicate source ids')
    seen=set()
    for f in pack.get('facts',[]):
        if not all(k in f for k in ('id','metric','period','value','unit','scope','status','source_id')):
            errors.append('Incomplete fact: '+str(f.get('id')));continue
        if f['id'] in seen:errors.append('Duplicate fact: '+f['id'])
        seen.add(f['id'])
        if f['value'] is not None and number(f['value']) is None and f['status'] not in ('excerpt','statement'):
            errors.append('Invalid numeric fact: '+f['id'])
        if f.get('status') in ('direct','manual','excerpt','statement','forecast') and f['source_id'] not in sources:
            errors.append('Missing source: '+f['id'])
        if f.get('period') and str(f['period'])>pack.get('as_of',''):errors.append('Future period: '+f['id'])
    for s in sources.values():
        if s.get('published_on') and s['published_on'][:10]>pack.get('as_of',''):errors.append('Future publication: '+str(s['id']))
    for f in pack.get('facts',[]):
        for key in f.get('inputs',[]):
            if key not in seen:errors.append('Missing calculation input: '+key)
        if 'raw_unit' in f and f.get('value') is not None:
            scales={'元':1,'万元':1e4,'亿元':1e8}
            raw=number(f.get('raw_value'))
            if f['raw_unit'] in scales and f['unit'] in scales:
                expected=None if raw is None else raw*scales[f['raw_unit']]/scales[f['unit']]
                if expected is None or not math.isclose(expected,f['value'],rel_tol=1e-9,abs_tol=.01):errors.append('Raw unit conversion mismatch: '+f['id'])
    lookup={f['id']:f for f in pack.get('facts',[]) if 'id' in f}
    for check in pack.get('reconciliations',[]):
        ids=check.get('inputs',[])
        if any(x not in lookup or lookup[x].get('value') is None for x in ids):
            errors.append('Missing reconciliation input: '+check.get('id',''));continue
        values=[lookup[x]['value'] for x in ids]
        if check.get('kind')=='gross_margin' and len(values)==3:
            revenue,cost,margin=values
            if revenue<=0 or not math.isclose((revenue-cost)/revenue*100,margin,abs_tol=check.get('tolerance',.02)):
                errors.append('Revenue/cost/margin reconciliation failed: '+check['id'])
        else:errors.append('Unsupported reconciliation: '+check.get('id',''))
    return errors



def project_root(explicit=None):
    """Locate checkout independently of CWD; support copied/user-installed skills."""
    if explicit:
        root = Path(explicit).expanduser().resolve()
        if not root.is_dir():
            raise ValueError('Project root does not exist: ' + str(root))
        return root
    script = Path(__file__).resolve()
    for root in script.parents:
        if root != Path.home().resolve() and script == root/'.agents/skills/a-share-value-research/scripts/research.py':
            return root
    start = Path.cwd().resolve()
    for root in (start, *start.parents):
        if (root/'.git').exists() or (root != Path.home().resolve() and (root/'.agents/skills').is_dir()):
            return root
    raise ValueError('Cannot locate project root; pass --project-root <checkout>')


def profile_paths(profile, explicit=None):
    root = project_root(explicit)
    paths = {}
    for key in ('database', 'reports_root', 'output_root'):
        value = Path(profile[key]).expanduser()
        paths[key] = value.resolve() if value.is_absolute() else (root/value).resolve()
    return root, paths


def doctor(args):
    import sys
    profile = load(args.profile)
    root, paths = profile_paths(profile, args.project_root)
    errors = []
    warnings = []
    if sys.version_info < (3, 11):
        errors.append('Python 3.11+ required')
    db = Path(args.database).expanduser().resolve() if args.database else paths['database']
    reports = Path(args.reports_root).expanduser().resolve() if args.reports_root else paths['reports_root']
    if profile.get('adapter') != 'investment-sina-v1':
        errors.append('Unknown adapter')
    if not db.is_file():
        errors.append('Missing database: restore project data or use collect --input <normalized JSON>')
    else:
        try:
            conn = readonly(db)
            try:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {'instruments', 'financial_reports'}.issubset(tables):
                    errors.append('Database schema mismatch')
            finally:
                conn.close()
        except (sqlite3.Error, ValueError) as exc:
            errors.append(str(exc))
    if not reports.is_dir():
        warnings.append('Missing company_reports: restore PDFs for original-page verification')
    if not (Path(__file__).resolve().parent.parent/'assets/report.html').is_file():
        errors.append('Missing report template')
    result = dict(passed=not errors, python=sys.version.split()[0], project_root=str(root),
                  database=str(db), reports_root=str(reports), output_root=str(paths['output_root']),
                  errors=errors, warnings=warnings)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if errors:
        raise SystemExit(1)


def collect(args):
    out=Path(args.out).resolve()
    if (out/'facts.json').exists():raise ValueError('Output pack already exists; choose a new report directory')
    date.fromisoformat(args.as_of)
    if args.input:
        pack=load(args.input)
        if pack.get('as_of')!=args.as_of or pack.get('company',{}).get('code')!=args.code:
            raise ValueError('Normalized input code/as_of mismatch')
        errors=validate_pack(pack)
        if errors:raise ValueError('; '.join(errors))
        pack['adapter']='normalized-json';save(out/'facts.json',pack)
        save(out/'inventory.json',{'adapter':'normalized-json','facts':len(pack['facts']),'warnings':pack.get('warnings',[])})
        return
    profile=load(args.profile)
    if profile.get('adapter')!='investment-sina-v1':raise ValueError('Unknown adapter; use normalized input')
    _, paths = profile_paths(profile, getattr(args, 'project_root', None))
    db=Path(args.database).expanduser().resolve() if args.database else paths['database']
    root=Path(args.reports_root).expanduser().resolve() if args.reports_root else paths['reports_root']
    if out==db.parent or out.is_relative_to(root):raise ValueError('Output must not overwrite database or report originals')
    conn=readonly(db)
    try:
        pack=_extract(conn,db,root,args.code,args.as_of,int(profile.get('history_years',10)),out)
    finally:conn.close()
    errors=validate_pack(pack)
    if errors:raise ValueError('; '.join(errors))
    save(out/'facts.json',pack)
    save(out/'inventory.json',{'company':pack['company'],'as_of':args.as_of,'adapter':pack['adapter'],
        'financial_periods':sorted({f['period'] for f in pack['facts'] if f['id'].startswith('fin.')}),
        'documents':[s for s in pack['sources'] if s.get('kind')=='pdf'],
        'source_count':len(pack['sources']),'fact_count':len(pack['facts']),
        'warnings':pack['warnings'],'network_gaps':['最新事件和调研记录','同业可比资料','独立行业统计','机构预测与关键假设'],
        'database_read_only':True,'project_imports':False})


def _extract(conn,db,root,code,as_of,years,out):
    tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'instruments','financial_reports'}.issubset(tables):raise ValueError('Schema mismatch; use normalized input')
    company=conn.execute('SELECT * FROM instruments WHERE code=?',(code,)).fetchone()
    if not company:raise ValueError('Company missing from local database')
    company=dict(company);identity=company['id'];sources=[];facts=[];warnings=[];raw=[]
    pack={'schema_version':SCHEMA,'calculation_version':VERSION,'adapter':'investment-sina-v1',
          'company':company,'as_of':as_of,'built_at':datetime.now(timezone.utc).isoformat(),
          'database':str(db),'sources':sources,'facts':facts,'warnings':warnings}
    start=str(int(as_of[:4])-years)+'-01-01'
    by_period={}
    for rr in conn.execute('SELECT * FROM financial_reports WHERE instrument_id=? AND period>=? AND period<=? ORDER BY period,report_type,source',(identity,start,as_of)):
        r=dict(rr)
        if not r.get('publish_date'):
            warnings.append('披露日缺失，未选入事实：'+r['period']+' '+r['report_type']);continue
        if r['publish_date']>as_of or not r['source'].startswith('sina:'):continue
        j=json.loads(r['raw_json']);sid='db.'+r['report_type']+'.'+r['period']+'.'+r['content_hash'][:12]
        sources.append({'id':sid,'kind':'sqlite','title':company['name']+' '+r['period']+' '+r['report_type'],
          'location':str(db),'table':'financial_reports','key':{'instrument_id':identity,'source':r['source'],'report_type':r['report_type'],'period':r['period']},
          'published_on':r['publish_date'],'obtained_at':r['obtained_at'],'sha256':r['content_hash'],
          'currency':j.get('rCurrency'),'report_scope':j.get('rType')})
        raw.append({'source_id':sid,'payload':j})
        if j.get('rCurrency')!='CNY' or j.get('rType')!='合并期末':
            warnings.append('币种/合并范围未适配：'+sid);continue
        by_period.setdefault(r['period'],{}).setdefault(r['report_type'],[]).append((sid,j))
        if r['obtained_at'][:10]>as_of: warnings.append('当前保存版本采集晚于研究日，非严格历史时点：'+sid)
    for period,reports in sorted(by_period.items()):
        for metric,candidates in FIELDS.items():
            chosen=None;conflict=[]
            for typ,field in candidates:
                values=[]
                for sid,j in reports.get(typ,[]):
                    for item in j.get('data',[]):
                        value=number(item.get('item_value'))
                        if item.get('item_field')==field and value is not None:values.append((value,sid,item))
                if values:
                    if len({x[0] for x in values})>1:conflict=values;break
                    chosen=values[0];break
            unit='%' if metric=='roe_weighted' else '元/股' if metric in ('eps_basic','source_fcff_per_share','source_fcfe_per_share') else '元'
            f={'id':fact_id(period,metric),'metric':metric,'period':period,'value':chosen[0] if chosen else None,
               'unit':unit,'scope':'归母' if metric in ('parent_profit','deduct_parent_profit','parent_equity','eps_basic','roe_weighted') else '合并',
               'window':'期末' if metric in POINTS else '累计','status':'direct' if chosen else 'conflict' if conflict else 'missing',
               'source_id':chosen[1] if chosen else None}
            if chosen:f.update(field=chosen[2]['item_field'],source_title=chosen[2]['item_title'],raw_value=chosen[2]['item_value'])
            if metric in ('source_fcff_per_share','source_fcfe_per_share'):
                f.update(scope='来源计算的每股指标，股数分母及合并/归母权益边界待核验',definition_verified=False,valuation_ready=False)
            if conflict:
                f['candidates']=[{'value':x[0],'source_id':x[1],'field':x[2]['item_field']} for x in conflict]
                warnings.append('同口径候选冲突，未自动回退：'+f['id'])
            facts.append(f)
    if 'report_overrides' in tables:
        for rr in conn.execute('SELECT * FROM report_overrides WHERE instrument_id=? AND period>=? AND period<=?',(identity,start,as_of)):
            r=dict(rr);metric=OVERRIDES.get(r['field_name']);v=number(json.loads(r['value_json']))
            if not metric or v is None or r['updated_at'][:10]>as_of:continue
            sid='override.'+r['period']+'.'+r['field_name']
            sources.append({'id':sid,'kind':'manual','title':'人工核验覆盖 '+r['field_name'],'location':r.get('source_path') or str(db),'published_on':r['updated_at'][:10],'obtained_at':r['updated_at'],'origin':r.get('origin')})
            f=next((f for f in facts if f['id']==fact_id(r['period'],metric)),None)
            if f:
                f['before_override']={'value':f['value'],'source_id':f['source_id']};f.update(value=v,status='manual',source_id=sid,field=r['field_name'])
    save(out/'sources/financial-raw.json.gz',raw)
    _market(conn,tables,identity,as_of,db,sources,facts,warnings)
    _dividends(conn,tables,identity,as_of,db,sources,facts,warnings)
    _documents(conn,tables,identity,as_of,root,sources,warnings,out)
    return pack


def _market(conn,tables,identity,as_of,db,sources,facts,warnings):
    prices=[]
    if 'raw_daily_prices' in tables:
        prices=[dict(r) for r in conn.execute('SELECT trade_date,close,high,low,source FROM raw_daily_prices WHERE instrument_id=? AND trade_date<=? ORDER BY trade_date',(identity,as_of))]
    if prices:
        p=prices[-1];sid='market.price.'+p['trade_date'];sources.append({'id':sid,'kind':'sqlite','title':'未复权日行情 '+p['trade_date'],'location':str(db),'table':'raw_daily_prices','published_on':p['trade_date'],'source':p['source']})
        facts.append({'id':'market.close','metric':'close','period':p['trade_date'],'value':p['close'],'unit':'元/股','scope':'未复权收盘','status':'direct','source_id':sid})
    if 'valuation_observations' in tables:
        for metric,unit in [('market_cap','亿元'),('pe','倍'),('pb','倍')]:
            limit=prices[-1]['trade_date'] if prices else as_of
            rows=[dict(r) for r in conn.execute('SELECT * FROM valuation_observations WHERE instrument_id=? AND metric=? AND observed_on<=? ORDER BY observed_on DESC,source',(identity,metric,limit))]
            if not rows:continue
            last=[r for r in rows if r['observed_on']==rows[0]['observed_on']];vals={number(r['value']) for r in last}
            r=last[0];sid='market.'+metric+'.'+r['observed_on']
            sources.append({'id':sid,'kind':'sqlite','title':metric+' 观察值 '+r['observed_on'],'location':str(db),'table':'valuation_observations','source':r['source'],'published_on':r['observed_on'],'obtained_at':r['obtained_at'],'raw_json':json.loads(r['raw_json'])})
            conflict=len(vals)>1
            facts.append({'id':'market.'+metric,'metric':metric,'period':r['observed_on'],'value':None if conflict else number(r['value']),'unit':unit,'scope':'行情源原口径','status':'conflict' if conflict else 'direct','source_id':sid})
            if conflict:warnings.append('行情来源冲突：'+metric)
            if prices and r['observed_on']!=prices[-1]['trade_date']:warnings.append('估值观察日与最新收盘日不同：'+metric)


def _dividends(conn,tables,identity,as_of,db,sources,facts,warnings):
    if 'dividend_events' not in tables:return
    events={}
    rows=[dict(r) for r in conn.execute("SELECT * FROM dividend_events WHERE instrument_id=? AND status='implemented' AND ex_dividend_date<=? ORDER BY report_period,ex_dividend_date,source",(identity,as_of))]
    for r in rows:events.setdefault((r['report_period'],r['ex_dividend_date']),[]).append(r)
    for (period,ex_date),group in events.items():
        group.sort(key=lambda r:not r['source'].startswith('eastmoney:'))
        r=group[0];v=number(r['cash_per_ten']);sid='dividend.'+str(r['id'])
        if any(number(x['cash_per_ten']) is not None and number(x['cash_per_ten'])!=v for x in group):
            warnings.append('分红事件金额冲突：'+str((period,ex_date)));continue
        if r.get('notice_date') and r['notice_date']>as_of:continue
        raw=json.loads(r['raw_json']);sources.append({'id':sid,'kind':'sqlite','title':'已实施分红 '+str(period)+' / '+ex_date,'location':str(db),'table':'dividend_events','key':{'id':r['id']},'published_on':r.get('notice_date') or ex_date,'obtained_at':r['obtained_at'],'sha256':r['content_hash'],'raw':raw})
        if v is None:continue
        facts.append({'id':sid+'.dps','metric':'cash_dividend_per_share','period':period or ex_date,'payment_date':ex_date,'value':v/10,'unit':'元/股','scope':'该次分红基准股本','status':'direct','source_id':sid,'share_change':bool(number(raw.get('BONUS_IT_RATIO')) or number(raw.get('IT_RATIO')) or number(raw.get('BONUS_RATIO')))})
        shares=number(raw.get('TOTAL_SHARES'))
        if shares is not None and shares>0:
            facts.append({'id':sid+'.shares','metric':'dividend_base_shares','period':ex_date,'value':shares,'unit':'股','scope':'该次分红基准股数，非自动认定最新股本','status':'direct','source_id':sid})
            facts.append({'id':sid+'.amount','metric':'dividend_amount','period':period or ex_date,'payment_date':ex_date,'value':v/10*shares,'unit':'元','scope':'该次现金分红估算，须核对回购股扣除','status':'calculated','source_id':None,'formula':'cash_per_ten / 10 * dividend_base_shares','inputs':[sid+'.dps',sid+'.shares']})


def _documents(conn,tables,identity,as_of,root,sources,warnings,out):
    pages=[]
    if not {'company_report_documents','company_report_parses'}.issubset(tables):return
    for rr in conn.execute('SELECT * FROM company_report_documents WHERE instrument_id=? AND report_period<=? AND published_on<=? ORDER BY report_period,id',(identity,as_of,as_of)):
        d=dict(rr);pdf=inside(root,d['relative_path']);sid='pdf.'+str(d['id'])
        if not pdf.is_file() or sha(pdf)!=d['content_hash']:
            warnings.append('PDF 缺失或 SHA256 不符，拒绝该文档：'+sid);continue
        sources.append({'id':sid,'kind':'pdf','title':d['report_period']+' '+d['report_type'],'location':str(pdf),'url':d['source_url'],'published_on':d['published_on'],'obtained_at':d['obtained_at'],'sha256':d['content_hash'],'document_id':d['id'],'period':d['report_period'],'supersedes_id':d['supersedes_id']})
        parse=conn.execute("SELECT * FROM company_report_parses WHERE document_id=? AND status='succeeded' ORDER BY created_at DESC,id DESC LIMIT 1",(d['id'],)).fetchone()
        if not parse:
            warnings.append('未找到成功 parse：'+sid);continue
        path=inside(root,parse['relative_path'])
        if not path.is_file():warnings.append('parse 文件缺失：'+sid);continue
        parse_hash=sha(path)
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if not line:continue
            p=json.loads(line)
            pages.append({'source_id':sid,'report_period':d['report_period'],'pdf_page':p['pdf_page'],'text':p['text'],'parser_version':parse['parser_version'],'parse_sha256':parse_hash})
    target=out/'sources/pages.jsonl';target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text('\n'.join(json.dumps(p,ensure_ascii=False) for p in pages),encoding='utf-8')


def calculate(args):
    out=Path(args.out);pack=load(out/'facts.json')
    pack['facts']=[f for f in pack['facts'] if not f['id'].startswith('calc.')]
    lookup={f['id']:f for f in pack['facts']};facts=pack['facts']
    periods=sorted({f['period'] for f in facts if f['id'].startswith('fin.')})
    def get(p,m):return lookup.get(fact_id(p,m),{}).get('value')
    def add(p,m,v,unit,inputs,formula,scope):
        facts.append({'id':'calc.'+p+'.'+m,'metric':m,'period':p,'value':v,'unit':unit,'scope':scope,'status':'calculated','source_id':None,'inputs':inputs,'formula':formula})
    for p in periods:
        for m,inputs,formula,scope in [
          ('gross_margin',['operating_revenue','operating_cost'],'(operating_revenue - operating_cost) / operating_revenue * 100','合并营业毛利率'),
          ('net_margin',['revenue','net_profit'],'net_profit / revenue * 100','合并净利率'),
          ('parent_margin',['revenue','parent_profit'],'parent_profit / revenue * 100','归母利润 / 合并收入'),
          ('cash_conversion',['parent_profit','cfo'],'cfo / parent_profit * 100','合并CFO / 归母利润，非严格归母现金流'),
        ]:
            a,b=(get(p,x) for x in inputs)
            if a is not None and a>0 and b is not None:
                v=(a-b)/a*100 if m=='gross_margin' else b/a*100
                add(p,m,v,'%',list(map(lambda x:fact_id(p,x),inputs)),formula,scope)
        cfo,capex=get(p,'cfo'),get(p,'capex')
        if cfo is not None and capex is not None:add(p,'fcf_proxy',cfo-capex,'元',[fact_id(p,'cfo'),fact_id(p,'capex')],'cfo - capex','合并简化FCF，非已重建FCFF/FCFE')
        prev=str(int(p[:4])-1)+p[4:]
        for m in ('revenue','parent_profit','deduct_parent_profit','cfo'):
            a,b=get(prev,m),get(p,m)
            if a is not None and a>0 and b is not None:add(p,m+'_yoy',(b/a-1)*100,'%',[fact_id(prev,m),fact_id(p,m)],'(current / prior_same_period - 1) * 100','同累计期间同比')
    dividends=[f for f in facts if f['metric']=='cash_dividend_per_share']
    for year in sorted({f['period'][:4] for f in dividends}):
        selected=[f for f in dividends if f['period'].startswith(year)]
        if any(f.get('share_change') for f in selected):continue
        amount=[f for f in facts if f['metric']=='dividend_amount' and f['period'].startswith(year)]
        add(year+'-12-31','dividend_dps',sum(f['value'] for f in selected),'元/股',[f['id'] for f in selected],'sum(implemented DPS attributed to year)','已实施、归属年度每股分红；未作长期送转股复权') if year<'%04d'%int(pack['as_of'][:4]) else None
        profit=get(year+'-12-31','parent_profit')
        if profit is not None and profit>0 and len(amount)==len(selected):
            add(year+'-12-31','dividend_payout',sum(f['value'] for f in amount)/profit*100,'%', [f['id'] for f in amount]+[fact_id(year+'-12-31','parent_profit')], 'sum(implemented dividends attributed to year) / annual parent_profit * 100','年度派息率估算；归属年度与支付年度分开')
    close=lookup.get('market.close')
    if close and close['value']>0:
        end=date.fromisoformat(close['period']);start=end-timedelta(days=365)
        selected=[f for f in dividends if start.isoformat()<f['payment_date']<=end.isoformat()]
        if selected and not any(f.get('share_change') for f in selected):
            add(close['period'],'dividend_yield_paid_12m',sum(f['value'] for f in selected)/close['value']*100,'%', [f['id'] for f in selected]+['market.close'],'sum(DPS paid in previous 365 days) / close * 100','按除息日代理支付日，税前，非未来承诺分红')
    errors=validate_pack(pack)
    if errors:raise ValueError('; '.join(errors))
    save(out/'facts.json',pack);save(out/'calculations.json',[f for f in facts if f['status']=='calculated'])


def add_evidence(args):
    out=Path(args.out);pack=load(out/'facts.json');new=load(args.input)
    source_ids={s['id'] for s in pack['sources']};fact_ids={f['id'] for f in pack['facts']}
    for s in new.get('sources',[]):
        if s['id'] in source_ids:raise ValueError('Duplicate source id: '+s['id'])
        source_ids.add(s['id']);pack['sources'].append(s)
    for f in new.get('facts',[]):
        if f['id'] in fact_ids:raise ValueError('Duplicate fact id: '+f['id'])
        fact_ids.add(f['id']);pack['facts'].append(f)
    pack.setdefault('reconciliations',[]).extend(new.get('reconciliations',[]))
    errors=validate_pack(pack)
    if errors:raise ValueError('; '.join(errors))
    save(out/'facts.json',pack)


TOKEN=re.compile(r'\{\{fact:([^}|]+)(?:\|([^}]+))?\}\}')
def format_fact(f,unit=None):
    value=f['value'];original=f['unit'];unit=unit or original
    if value is None:return '待核对'
    if isinstance(value,str):
        if unit!=original:raise ValueError('Cannot convert text fact')
        return value
    scales={'元':1,'万元':1e4,'亿元':1e8}
    if original in scales and unit in scales:value=value*scales[original]/scales[unit]
    elif unit!=original:raise ValueError('Incompatible units: '+original+' -> '+unit)
    return format(value,',.0f' if unit in ('股','辆','家','人') and value==int(value) else ',.2f')+unit


def expand_report(text,pack):
    # Reject damaged placeholders before Markdown can split unit pipes into cells.
    if re.search(r'\{+\s*fact:', TOKEN.sub('',text)):
        raise ValueError('Malformed fact placeholder: use {{fact:id|unit}}')
    lookup={f['id']:f for f in pack['facts']}
    sources={s['id']:s for s in pack['sources']};used=[];sections=[]
    # Only numbered main chapters start a new source group; subheadings stay together.
    parts=re.split(r'(?=^## [一二三四五六七八]、)',text,flags=re.M)
    for part in parts:
        if not part.strip():continue
        chapter_sources=[]
        def add_source(sid):
            if sid not in sources:raise ValueError('Unknown source: '+sid)
            if sid not in chapter_sources:chapter_sources.append(sid)
        def evidence(fid,visited=None):
            visited=set(visited or ())
            if fid in visited:raise ValueError('Circular calculation: '+fid)
            if fid not in lookup:raise ValueError('Unknown fact: '+fid)
            visited.add(fid);f=lookup[fid]
            if f.get('source_id'):add_source(f['source_id'])
            for child in f.get('inputs',[]):evidence(child,visited)
        def replace(m):
            if m[1] not in lookup:raise ValueError('Unknown fact: '+m[1])
            used.append(m[1]);evidence(m[1])
            return format_fact(lookup[m[1]],m[2])
        expanded=TOKEN.sub(replace,part)
        def source(m):
            add_source(m[1]);return ''
        expanded=re.sub(r'\[S:([^\]]+)\]',source,expanded)
        # Remove marker-only source lines; one consolidated line is generated below.
        expanded=re.sub(r'^\s*(?:\*\*)?数据来源[：:](?:\*\*)?\s*$', '', expanded, flags=re.M)
        grouped={}
        for sid in chapter_sources:
            src=sources[sid];key=src.get('url') or src.get('location') or sid
            grouped.setdefault(key,[]).append(src)
        labels=[]
        for group in grouped.values():
            src=group[0];location=src.get('url') or src.get('location') or ''
            title=re.sub(r' · PDF第\d+页$', '', src.get('title',src['id']))
            if src.get('kind')=='sqlite':title='本地已验证财务、行情与已实施分红'
            url=location if location.startswith(('https://','http://')) else Path(location).resolve().as_uri() if location else ''
            label='['+title+']('+url+')' if url else title
            pages=sorted({x['pdf_page'] for x in group if x.get('pdf_page')})
            dates=sorted({str(x['published_on']) for x in group if x.get('published_on')})
            details=[]
            if dates:details.append('、'.join(dates))
            if pages:details.append('PDF 第 '+ '、'.join(map(str,pages)) +' 页')
            if details:label+='（'+'；'.join(details)+'）'
            labels.append(label)
        if labels:expanded=expanded.rstrip()+'\n\n**数据来源：** '+'；'.join(labels)+'。\n'
        sections.append(expanded)
    return '\n'.join(sections),list(dict.fromkeys(used))


def inline(text):
    text=html.escape(text)
    text=re.sub(r'\[([^\]]+)\]\(([^)]+)\)',lambda m:'<a href="'+html.escape(m[2],quote=True)+'">'+m[1]+'</a>' if re.match(r'^(?:#|https?://|file:///)',html.unescape(m[2])) else m[1],text)
    return re.sub(r'\*\*([^*]+)\*\*',r'<strong>\1</strong>',text)


def markdown(text):
    lines=text.splitlines();chunks=[];i=0
    while i<len(lines):
        line=lines[i].strip()
        if not line:i+=1;continue
        if line.startswith('|'):
            table=[]
            while i<len(lines) and lines[i].strip().startswith('|'):
                cells=lines[i].strip().strip('|').split('|');i+=1
                if all(re.fullmatch(r'\s*:?-+:?\s*',x) for x in cells):continue
                table.append(cells)
            chunks.append('<div class="table-wrap"><table>'+''.join('<tr>'+''.join('<'+('th' if j==0 else 'td')+'>'+inline(x.strip())+'</'+('th' if j==0 else 'td')+'>' for x in cells)+'</tr>' for j,cells in enumerate(table))+'</table></div>');continue
        heading=re.match(r'^(#{1,6})\s+(.+)',line)
        if heading:chunks.append('<h'+str(len(heading[1]))+'>'+inline(heading[2])+'</h'+str(len(heading[1]))+'>')
        elif line.startswith('- '):
            items=[]
            while i<len(lines) and lines[i].strip().startswith('- '):items.append('<li>'+inline(lines[i].strip()[2:])+'</li>');i+=1
            chunks.append('<ul>'+''.join(items)+'</ul>');continue
        else:chunks.append('<p>'+inline(line)+'</p>')
        i+=1
    return '\n'.join(chunks)



# Match table headers, never table index: missing/extra company rows cannot shift styling.
REFERENCE_WIDTHS = {
    ('检查项','评分','理由'): (15,15,70),
    ('维度','问题','答案','评分'): (15,27,48,10),
    ('时间','决策','结果','评分'): (15,32,43,10),
    ('类型','验证方法','是否具备','具体证据','变宽/变窄'): (15,15,10,45,15),
    ('风险','概率','影响','说明'): (30,10,10,50),
    ('对手','市场份额','竞争策略'): (25,25,50),
    ('类型','问题','答案'): (15,24,61),
    ('指标','当前值','解读'): (30,20,50),
    ('参数','取值','说明'): (30,20,50),
    ('情景（FCF复合增速g）','每股内在价值','现价折溢价','判断'): (25,25,20,30),
    ('#','问题','是/否'): (4,45,51),
    ('年度','营收','净利润（归母）','增速','EPS'): (12,28,24,18,18),
    ('指标','数值','说明'): (30,25,45),
    ('状态','建议'): (15,85),
    ('期间','每股现金分红','派息率','当前价对应股息率','分红构成及状态','持续性判断'): (10,13,13,16,28,20),
    ('期间','每股现金分红','派息率','期末市值口径股息率','分红构成及状态','持续性判断'): (10,13,13,16,28,20),
}


def reference_style(body):
    def normalize(value):
        return re.sub(r'\s+', '', html.unescape(re.sub(r'<[^>]+>', '', value)))
    def table_style(match):
        table = match[0]
        head = re.search(r'<tr>(.*?)</tr>', table, flags=re.S)
        headers = tuple(normalize(x) for x in re.findall(r'<th>(.*?)</th>', head[1], flags=re.S))
        widths = REFERENCE_WIDTHS.get(headers)
        if widths:
            columns = '<colgroup>'+''.join('<col style="width:'+str(w)+'%">' for w in widths)+'</colgroup>'
            table = table.replace('<table>', '<table>'+columns, 1)
        if headers == ('状态','建议'):
            table = table.replace('class="table-wrap"', 'class="table-wrap decision-table"', 1)
        return table
    body = re.sub(r'<div class="table-wrap"><table>.*?</table></div>', table_style, body, flags=re.S)
    def badges(match):
        parts = match[1].split('｜')
        if len(parts) != 3:
            return match[0]
        return '<div class="report-tags">'+''.join('<span class="report-tag tag-'+str(i)+'">'+t.strip()+'</span>' for i,t in enumerate(parts))+'</div>'
    body = re.sub(r'<p>(信息丰富度评级：.*?)</p>', badges, body)
    body = re.sub(r'<p>(<strong>数据来源：</strong>.*?)</p>', r'<p class="chapter-sources">\1</p>', body)
    body = re.sub(r'<p>(<strong>(?:数据交叉验证提示：|一句话本质：).*?)</p>', r'<p class="callout">\1</p>', body)
    body = re.sub(r'(<p><strong>最终结论.*?</p>\s*<p>必须保留.*?</p>\s*<p><strong>一句话：.*?</p>)', r'<div class="conclusion-box">\1</div>', body, flags=re.S)
    body = re.sub(r'([★☆]{2,5})', r'<span class="stars">\1</span>', body)
    return body


def report_navigation(body):
    entries=[]
    def heading(match):
        level=int(match[1]);label=html.unescape(re.sub(r'<[^>]+>','',match[2]))
        anchor='section-'+str(len(entries)+1)
        entries.append({'level':level,'id':anchor,'label':label})
        return '<h'+str(level)+' id="'+anchor+'">'+match[2]+'</h'+str(level)+'>'
    body=re.sub(r'<h([123])>(.*?)</h\1>',heading,body,flags=re.S)
    links=''.join('<li class="toc-level-'+str(row['level'])+'"><a href="#'+row['id']+'">'+html.escape('报告开头' if row['level']==1 else row['label'])+'</a></li>' for row in entries)
    toc='<aside class="report-sidebar"><details class="report-toc" open><summary>文档目录</summary><nav aria-label="研报章节目录"><ol>'+links+'</ol></nav></details></aside>'
    return body,toc,entries


def render(args):
    out=Path(args.out);pack=load(out/'facts.json');text=(out/'report.md').read_text(encoding='utf-8-sig')
    expanded,used=expand_report(text,pack);body=reference_style(markdown(expanded))
    body,toc,headings=report_navigation(body)
    template=(Path(__file__).parent.parent/'assets/report.html').read_text(encoding='utf-8')
    result=template.replace('@@TITLE@@',html.escape(pack['company']['name']+' · 价值投资研究')).replace('@@AS_OF@@',html.escape(pack['as_of'])).replace('@@BODY@@',body).replace('@@TOC@@',toc)
    (out/'report.html').write_text(result,encoding='utf-8')
    (out/'report-readable.md').write_text(expanded,encoding='utf-8')
    save(out/'render.json',{'referenced_facts':used,'renderer_version':VERSION,'navigation':headings})


def validate(args):
    out=Path(args.out);pack=load(out/'facts.json');errors=validate_pack(pack)
    text=(out/'report.md').read_text(encoding='utf-8-sig') if (out/'report.md').exists() else ''
    if not text:errors.append('Missing report.md')
    try:_,used=expand_report(text,pack)
    except ValueError as exc:errors.append(str(exc));used=[]
    if '{{' in TOKEN.sub('',text) or re.search(r'\{+\s*fact:',TOKEN.sub('',text)):errors.append('Unresolved template token')
    for name in ('report-readable.md','report.html'):
        path=out/name
        if path.exists() and re.search(r'\{+\s*fact:',path.read_text(encoding='utf-8')):
            errors.append('Unresolved fact placeholder in '+name)
    if not (out/'report.html').exists():errors.append('Missing rendered HTML')
    errors=list(dict.fromkeys(errors))
    result={'passed':not errors,'errors':errors,'facts':len(pack['facts']),'referenced_facts':len(used),'sources':len(pack['sources']),
            'checks':['schema','source references','fact references','unit conversions','publication cutoff','calculation input references','raw unit scaling','revenue-cost-margin reconciliation'],
            'limitations':['不自动核验叙述中的手工数字','不验证模型判断、外部出处真实性或估值参数合理性','HTML视觉需另行检查'],
            'warnings':pack.get('warnings',[])}
    save(out/'validation.json',result)
    print(json.dumps({k:result[k] for k in ('passed','errors','facts','referenced_facts','sources')},ensure_ascii=False))
    if errors:raise SystemExit(1)


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('collect');p.add_argument('--profile',default=str(Path(__file__).parent.parent/'references/local-profile.json'));p.add_argument('--project-root');p.add_argument('--database');p.add_argument('--reports-root');p.add_argument('--input');p.add_argument('--code',required=True);p.add_argument('--as-of',required=True);p.add_argument('--out',required=True);p.set_defaults(func=collect)
    p=sub.add_parser('doctor');p.add_argument('--profile',default=str(Path(__file__).parent.parent/'references/local-profile.json'));p.add_argument('--project-root');p.add_argument('--database');p.add_argument('--reports-root');p.set_defaults(func=doctor)
    for command,func in [('calculate',calculate),('render',render),('validate',validate),('add-evidence',add_evidence)]:
        p=sub.add_parser(command);p.add_argument('--out',required=True);p.set_defaults(func=func)
        if command=='add-evidence':p.add_argument('--input',required=True)
    args=parser.parse_args()
    if hasattr(args,'code') and not re.fullmatch(r'\d{6}',args.code):parser.error('code must contain six digits')
    args.func(args)


if __name__=='__main__':main()
