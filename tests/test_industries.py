import copy
import json

import pytest

from quarterly_dashboard.industry_service import IndustryService, aggregate, value_for
from quarterly_dashboard.industry_sources import financial_rows, parse_quotes
from quarterly_dashboard.storage import Database
from industry_financial_fixture import PublishedIndustryService


def fixture_bundle():
    return {'obtained_at':'2026-10-03T00:00:00+00:00','asof':'2026-10-03',
            'taxonomy':[{'code':'630000','name':'电力设备','level':1,'parent_code':None},
                        {'code':'630700','name':'电池','level':2,'parent_code':'630000'},
                        {'code':'630701','name':'锂电池','level':3,'parent_code':'630700'},
                        {'code':'630702','name':'电池其他','level':3,'parent_code':'630700'}],
            'members':[{'stock_code':s,'name':s,'industry_code':c,'effective_date':'2021-07-30','source_update':'2026-09-29'}
                       for s,c in [('300750','630701'),('000001','630701'),('000002','630702')]],
            'membership_history':[],
            'financials':[{'stock_code':s,'period':p,'revenue':v,'parent_profit':v/10 if v is not None else None,
                           'notice_date':'2026-08-30','provenance':{'source':'test','field':'TOTAL_OPERATE_INCOME'}}
                          for s,p,v in [('300750','2024-06-30',10),('000001','2024-06-30',20),('000002','2024-06-30',5),
                                        ('300750','2025-06-30',15),('000001','2025-06-30',30),('000002','2025-06-30',8)]],
            'caps':[{'stock_code':s,'trade_date':'2026-09-30','total_cap':v,'provenance':{'source':'tencent','field':'45'}}
                    for s,v in [('300750',100),('000001',200),('000002',50)]],
            'manifest':{'pilot_industries':['630701','630702'],'scope':'pilot'}}


@pytest.fixture
def service(tmp_path,monkeypatch):
    monkeypatch.setattr('quarterly_dashboard.industry_memberships.ensure_daily_memberships',
        lambda *a,**k:{'status':'complete','reused':True})
    db=Database(tmp_path/'industry.sqlite3');db.initialize()
    return PublishedIndustryService(db)


def test_missing_zero_quarter_ttm_and_same_cohort():
    facts={('a','2024-03-31'):{'revenue':0},('a','2024-06-30'):{'revenue':40},
           ('a','2024-12-31'):{'revenue':100},('a','2025-06-30'):{'revenue':60},
           ('b','2025-06-30'):{'revenue':900}}
    assert value_for(facts,'a','2024-06-30','revenue','quarter')==40
    assert value_for(facts,'a','2025-06-30','revenue','quarter') is None
    assert value_for(facts,'a','2025-06-30','revenue','ttm')==120
    assert value_for(facts,'b','2025-06-30','revenue','ttm') is None
    r=aggregate(['a','b'],facts,'2025-06-30','ytd')
    assert r['revenue_known']==960 and r['revenue_yoy']==50
    assert r['revenue_matched_count']==1 and not r['rank_eligible']
    r=aggregate(['a','b'],facts,'2024-03-31','ytd')
    assert r['revenue'] is None and r['revenue_known']==0 and r['revenue_count']==1


def test_gross_margin_periods_weighting_and_missing():
    facts={('a',p):{'operating_revenue':r,'operating_cost':c}
           for p,r,c in [('2024-06-30',100,80),('2024-12-31',240,180),
                         ('2025-03-31',60,45),('2025-06-30',160,100)]}
    assert aggregate(['a'],facts,'2025-06-30','ytd')['gross_margin']==37.5
    assert aggregate(['a'],facts,'2025-06-30','quarter')['gross_margin']==45
    assert aggregate(['a'],facts,'2025-06-30','ttm')['gross_margin']==pytest.approx(100/3)
    assert aggregate(['a'],facts,'2024-12-31','annual')['gross_margin']==25
    facts[('b','2025-06-30')]={'operating_revenue':40,'operating_cost':40}
    facts[('missing','2025-06-30')]={'operating_revenue':1000}
    facts[('zero','2025-06-30')]={'operating_revenue':0,'operating_cost':0}
    facts[('bank','2025-06-30')]={'operating_revenue':500,'operating_cost':0,'gross_margin_applicable':False}
    result=aggregate(['a','b','missing','zero','bank'],facts,'2025-06-30','ytd')
    assert result['gross_margin']==30
    assert result['gross_margin_count']==2 and result['gross_margin_coverage']==.4
    assert aggregate(['b'],facts,'2025-06-30','ytd')['gross_margin']==0
    assert aggregate(['bank'],facts,'2025-06-30','ytd')['gross_margin'] is None
    facts[('a','2025-03-31')]['operating_cost']=None
    assert aggregate(['a'],facts,'2025-06-30','quarter')['gross_margin'] is None
    facts[('fallback','2025-06-30')]={'revenue':100,'operating_cost':60}
    assert aggregate(['fallback'],facts,'2025-06-30','ytd')['gross_margin'] is None
    facts[('fallback','2025-06-30')]['gross_margin_total_revenue_equivalent']=True
    assert aggregate(['fallback'],facts,'2025-06-30','ytd')['gross_margin']==40
    facts[('fallback','2025-06-30')]['operating_revenue']=80
    assert aggregate(['fallback'],facts,'2025-06-30','ytd')['gross_margin']==25


def test_company_and_industry_gross_margin_and_financial_exclusion(service):
    bundle=fixture_bundle()
    for row in bundle['financials']:
        row.update(operating_revenue=row['revenue'],operating_cost=row['revenue']*.6)
    service.import_bundle(bundle)
    result=service.read(code='300750',mode='ytd',period='2025-06-30')
    assert result['companies'][0]['metrics']['gross_margin']==40
    assert result['ranking'][0]['gross_margin']==40
    assert result['series'][-1]['gross_margin_count']==2
    bank=copy.deepcopy(bundle)
    bank['taxonomy'][0]['name']='银行'
    service.import_bundle(bank)
    result=service.read(code='300750',mode='ytd',period='2025-06-30')
    assert all(c['metrics']['gross_margin'] is None for c in result['companies'])
    assert all(r['gross_margin'] is None for r in result['ranking'])


def test_child_ranking_uses_selected_parent_and_same_metrics(service):
    bundle=fixture_bundle()
    for row in bundle['financials']:
        row.update(operating_revenue=row['revenue'],operating_cost=row['revenue']*.6)
    service.import_bundle(bundle)
    first=service.read(industry='630000',level=1,mode='ytd',period='2025-06-30')
    assert [r['code'] for r in first['child_ranking']]==['630700']
    second=service.read(industry='630700',level=2,mode='ytd',period='2025-06-30')
    assert {r['code'] for r in second['child_ranking']}=={'630701','630702'}
    third=service.read(industry='630701',level=3,mode='ytd',period='2025-06-30')
    assert third['child_ranking']==[]
    same_level={r['code']:r for r in third['ranking']}
    for row in second['child_ranking']:
        assert row==same_level[row['code']]
    assert sum(r['revenue_known'] for r in second['child_ranking'])==first['child_ranking'][0]['revenue_known']


@pytest.mark.parametrize('mode,current,baseline', [('ytd','2025-06-30','2024-06-30'),
    ('quarter','2025-06-30','2024-06-30'),('ttm','2025-06-30','2024-06-30'),
    ('annual','2025-12-31','2024-12-31')])
def test_gross_margin_yoy_same_cohort_and_period(mode,current,baseline):
    facts={}
    for year in (2023,2024,2025):
        for suffix,revenue in [('03-31',50),('06-30',100),('12-31',200)]:
            facts[('a',f'{year}-{suffix}') ]={'operating_revenue':revenue,
                'operating_cost':revenue*(.7 if year==2025 else .8)}
    facts[('new',current)]={'operating_revenue':900,'operating_cost':0}
    result=aggregate(['a','new'],facts,current,mode)
    assert result['gross_margin_yoy']==pytest.approx(10 if mode!='ttm' else 5)
    assert result['gross_margin_matched_count']==1
    assert result['gross_margin_matched_coverage']==.5
    assert result['gross_margin_baseline']==pytest.approx(20)
    assert aggregate(['new'],facts,current,mode)['gross_margin_yoy'] is None
    facts[('a',baseline)]['operating_cost']=None
    assert aggregate(['a'],facts,current,mode)['gross_margin_yoy'] is None


@pytest.mark.parametrize('baseline_cost,expected', [(100,30),(120,50)])
def test_gross_margin_yoy_zero_or_negative_baseline(baseline_cost,expected):
    facts={('a','2024-06-30'):{'operating_revenue':100,'operating_cost':baseline_cost},
           ('a','2025-06-30'):{'operating_revenue':100,'operating_cost':70}}
    assert aggregate(['a'],facts,'2025-06-30','ytd')['gross_margin_yoy']==pytest.approx(expected)


def test_upward_sum_classification_comparison_and_quarter_cap(service):
    service.import_bundle(fixture_bundle())
    third=service.read(industry='630701',mode='ytd',period='2025-06-30')
    assert third['series'][-1]['revenue']==45
    assert third['market_series'][0]['total_cap']==300
    assert third['market_series'][0]['quarter']=='2026Q3'
    parent=service.read(industry='630700',level=2,mode='ytd',period='2025-06-30')
    first=service.read(code='300750',industry='630000',level=1,mode='ytd',period='2025-06-30')
    assert parent['series'][-1]['revenue']==53==first['series'][-1]['revenue']
    assert parent['market_series'][0]['total_cap']==350==first['market_series'][0]['total_cap']
    assert [r['name'] for r in first['stock_path']]==['电力设备','电池','锂电池']
    with service.db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM instruments').fetchone()[0]==0
        assert conn.execute('SELECT count(*) FROM ai_analysis_runs').fetchone()[0]==0


def test_versions_same_quarter_and_failed_import_preserve_old(service):
    b=fixture_bundle();one=service.import_bundle(b)
    assert service.import_bundle(b)==one
    changed=copy.deepcopy(b)
    changed['caps'][0]['total_cap']=120
    changed['caps'][0]['trade_date']='2026-09-29'
    changed['caps'][1]['trade_date']='2026-09-29'
    changed['caps'][2]['trade_date']='2026-09-29'
    changed['financials'][-1]['revenue']=9
    service.import_bundle(changed)
    r=service.read(industry='630701',mode='ytd')
    assert len(r['market_series'])==1 and r['market_series'][0]['trade_date']=='2026-09-30'
    with service.db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM sw_financial_facts').fetchone()[0]==7
    invalid=copy.deepcopy(b);invalid['members']=invalid['members'][:1]
    with pytest.raises(ValueError,match='骤减'):
        service.import_bundle(invalid)
    assert service.read(industry='630702',mode='ytd')['series'][-1]['revenue']==9


def test_partial_cap_is_not_total(service):
    b=fixture_bundle();b['caps']=b['caps'][:1];service.import_bundle(b)
    r=service.read(industry='630701',mode='ytd')['market_series'][0]
    assert r['total_cap'] is None and r['known_cap']==100 and r['coverage']==.5


def test_old_float_cap_parser_version_excluded_even_if_date_is_later(service):
    b=fixture_bundle();one=service.import_bundle(b)
    with service.db.connection(write=True) as conn:
        conn.execute("UPDATE sw_cap_facts SET provenance_json=? WHERE import_id=? AND stock_code='300750'",
                     (json.dumps({'source':'tencent:qt.gtimg.cn','field':'44'}),one))
    corrected=copy.deepcopy(b)
    corrected['caps'][0]['total_cap']=120
    for r in corrected['caps']:r['trade_date']='2026-09-29'
    service.import_bundle(corrected)
    market=service.read(industry='630701',mode='ytd')['market_series']
    assert len(market)==1 and market[0]['trade_date']=='2026-09-29' and market[0]['total_cap']==320
    with service.db.connection() as conn:
        manifest=json.loads(conn.execute('SELECT source_manifest_json FROM sw_imports WHERE id=?',(one,)).fetchone()[0])
    assert manifest['cap_status']=='rejected'


def test_quote_actual_date_and_unit_no_adjusted_price():
    fields=['']*50;fields[2]='300750';fields[30]='20260930150000';fields[44]='80';fields[45]='123.45';fields[3]='100'
    r=parse_quotes('v_sz300750="'+'~'.join(fields)+'";')[0]
    assert r['total_cap']==123.45e8 and r['trade_date']=='2026-09-30'
    fields[30]='20261332150000'
    assert parse_quotes('v_sz300750="'+'~'.join(fields)+'";')==[]


def test_total_cap_independent_sina_sample_and_reject_float_field(service):
    fields=['']*50;fields[2]='300750';fields[30]='20260930150000'
    fields[44]='12403.08';fields[45]='13470.39'
    quote=parse_quotes('v_sz300750="'+'~'.join(fields)+'";')[0]
    # Independent Sina universe response: mktcap in 万元. Tencent rounds to .01 亿元.
    sina_total=134703865.60915*1e4
    assert abs(quote['total_cap']-sina_total)<.005e8
    assert quote['provenance']['field']=='45'
    wrong=fixture_bundle();wrong['caps'][0]['provenance']['field']='44'
    with pytest.raises(ValueError,match='流通市值'):
        service.import_bundle(wrong)


def test_financial_zero_missing_and_future_notice():
    rows=[{'SECURITY_CODE':'300750','REPORT_DATE':'2026-06-30 00:00:00','NOTICE_DATE':'2026-08-30',
           'TOTAL_OPERATE_INCOME':0,'PARENT_NETPROFIT':None}]
    r=financial_rows(rows,'2026-10-03')[0]
    assert r['revenue']==0 and r['parent_profit'] is None
    assert financial_rows(rows,'2026-07-01')==[]


def test_local_priority_equivalent_scope_and_zero(service):
    from quarterly_dashboard.industry_service import local_financials
    class Conn:
        def execute(self,*_):
            return [{'code':'300750','period':'2025-06-30','report_type':kind,'source':'sina',
                     'raw_json':json.dumps({'rCurrency':'CNY','rType':'合并期末','data':[
                         {'item_source':'lrb','item_field':'BIZTOTINCO','item_value':value,'item_title':'营业总收入'}]})}
                    for kind,value in [('gjzb',0),('lrb',99)]]
    r=local_financials(Conn())[('300750','2025-06-30')]
    assert r['revenue']==0 and r['revenue_provenance']['report_type']=='gjzb'


def test_api_read_validation_and_assets(service,monkeypatch):
    from quarterly_dashboard import server
    from http.server import ThreadingHTTPServer
    from threading import Thread
    import urllib.request
    monkeypatch.setattr(server,'industry_service',lambda:service)
    service.import_bundle(fixture_bundle())
    http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
    thread=Thread(target=http.serve_forever,daemon=True);thread.start()
    try:
        root=f'http://127.0.0.1:{http.server_port}'
        with urllib.request.urlopen(root+'/api/industry?industry=630701&mode=ytd') as response:
            assert json.load(response)['selected']['name']=='锂电池'
        with urllib.request.urlopen(root+'/industry.js') as response:
            assert 'initIndustry' in response.read().decode()
        from urllib.error import HTTPError
        with pytest.raises(HTTPError) as err:
            urllib.request.urlopen(root+'/api/industry?level=5')
        assert err.value.code==400
    finally:
        http.shutdown();http.server_close();thread.join()


def test_quarter_history_exact_dates_no_forward_fill_and_reference_validation():
    from quarterly_dashboard.industry_cap_history import quarter_facts,parse_history
    payload={'Result':[{'DisplayData':{'resultData':{'tplData':{'result':{'chartInfo':[
        {'header':['总市值'],'type':'近一年','body':[['2026-03-30','90'],['2026-06-30','110'],['2026-09-30','120']]}
    ]}}}}}]}
    values=parse_history(payload)
    reference={'trade_date':'2026-09-30','total_cap':120e8}
    facts=quarter_facts('300750',values,['2026-03-31','2026-06-30'],reference,{})
    assert len(facts)==1 and facts[0]['trade_date']=='2026-06-30' and facts[0]['total_cap']==110e8
    assert facts[0]['provenance']['composition']=='current_constituents_backfill'
    with pytest.raises(ValueError,match='不一致'):
        quarter_facts('300750',values,['2026-06-30'],{'trade_date':'2026-09-30','total_cap':130e8},{})
    with pytest.raises(ValueError,match='精确季末'):
        quarter_facts('300750',values,['2026-03-30'],reference,{})


def test_four_quarter_cap_points_persist_and_upward_sum(service):
    b=fixture_bundle()
    base=copy.deepcopy(b['caps'])
    for date,scale in [('2025-12-31',.8),('2026-03-31',.9),('2026-06-30',1.1)]:
        for r in base:
            b['caps'].append({**r,'trade_date':date,'total_cap':r['total_cap']*scale,
                'provenance':{'source':'baidu:opendata','field':'总市值','composition':'current_constituents_backfill'}})
    service.import_bundle(b)
    leaf=service.read(industry='630701',mode='ytd')['market_series']
    parent=service.read(industry='630700',level=2,mode='ytd')['market_series']
    assert [r['quarter'] for r in leaf]==['2025Q4','2026Q1','2026Q2','2026Q3']
    assert [r['total_cap'] for r in leaf]==pytest.approx([240,270,330,300])
    assert parent[0]['total_cap']==280
    with service.db.connection() as conn:
        assert conn.execute('SELECT count(DISTINCT quarter) FROM sw_industry_cap_quarters').fetchone()[0]==4


def test_market_chart_distinguishes_snapshot_from_trend():
    import shutil,subprocess
    from pathlib import Path
    node=shutil.which('node')
    if not node:pytest.skip('Node.js needed for frontend format checks')
    result=subprocess.run([node,str(Path(__file__).with_name('industry_ui.cjs'))],capture_output=True,text=True,encoding='utf-8')
    assert result.returncode==0,result.stdout+result.stderr


def save_local_financial(db, revenue, profit):
    from quarterly_dashboard.storage import SyncKey,SyncResult
    identity=db.ensure_instrument('300750','测试企业')
    key=SyncKey(identity,'financial:lrb','sina')
    run=db.start_sync(key,parser_version='test',methodology_version='test',trigger_reason='test')
    raw={'rCurrency':'CNY','rType':'合并期末','data':[
        {'item_source':'lrb','item_field':field,'item_value':value,'item_title':field}
        for field,value in [('BIZTOTINCO',revenue),('PARENETP',profit)]]}
    db.complete_sync(run,SyncResult(1,'2025-06-30','2025-06-30','2025-06-30'),
        lambda conn:db.upsert_financial_reports(conn,key,run,[{'period':'2025-06-30','raw_json':raw}]))


def test_stock_financial_refresh_does_not_collect_or_change_industry_snapshot(service,monkeypatch):
    from quarterly_dashboard import server
    save_local_financial(service.db,100,10)
    service.import_bundle(fixture_bundle())
    before=service.read(industry='630701',mode='ytd',period='2025-06-30')
    # Industry reads Eastmoney observations even when individual Sina values differ.
    assert before['series'][-1]['revenue']==45
    class Fundamentals:
        db=service.db
        def update(self,code,**options):
            assert options['refresh'] and options['full']
            save_local_financial(service.db,999,99)
            return {'reports':[]}
    class Dividends:
        def read(self,code):return []
    monkeypatch.setattr(server,'DATABASE_PATH',service.db.path)
    monkeypatch.setattr(server,'p4_services',lambda:(Fundamentals(),Dividends(),None))
    monkeypatch.setattr(server,'industry_service',lambda:pytest.fail('stock refresh reached industry service'))
    monkeypatch.setattr(server,'_financial_payload',lambda value:value)
    monkeypatch.setattr(server,'_add_sqlite_missing_name',lambda db,value:value)
    monkeypatch.setattr(server,'cached_stocks',lambda:[])
    server.load_chart_data('300750','financial',True,full=True)
    # Reloading the service also remains fixed; this is not merely an in-memory cache.
    after=IndustryService(service.db).read(industry='630701',mode='ytd',period='2025-06-30')
    assert after['series']==before['series'] and after['market_series']==before['market_series']
    assert after['import_id']==before['import_id']
    with service.db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM sw_update_runs').fetchone()[0]==0


def test_financial_task_one_period_reuses_local_and_never_fetches_caps(service,monkeypatch,tmp_path):
    from quarterly_dashboard import industry_updates as updates
    service.directory=tmp_path/'sources'
    save_local_financial(service.db,0,10)
    service.import_bundle(fixture_bundle())
    before=service.read(industry='630701',mode='ytd')['market_series']
    requested=[]
    def fetch(directory,stocks,period):
        requested.append((stocks,period))
        return [{'stock_code':'000001','period':period,'notice_date':None,'revenue':50,'parent_profit':5,
                 'provenance':{'source':'test'}}]
    monkeypatch.setattr(updates,'fetch_financial_period',fetch)
    monkeypatch.setattr(updates,'fetch_cap_quarter',lambda *a,**k:pytest.fail('finance requested caps'))
    result=updates.perform(service,'financial_period','2025-06-30',False,lambda _:None)
    assert requested==[(['000001'],'2025-06-30')]
    assert not result['failures']
    assert service.read(industry='630701',mode='ytd')['market_series']==before
    with service.db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM sw_memberships').fetchone()[0]==3
        assert conn.execute('SELECT count(*) FROM sw_cap_facts').fetchone()[0]==3


def test_financial_repeat_ignores_response_file_changes_and_preserves_valid_values(service):
    b=fixture_bundle();service.import_bundle(b)
    repeated=copy.deepcopy(b)
    for row in repeated['financials']:row['provenance']['file']='another-response.json'
    assert service.import_bundle(repeated)==1
    changed=copy.deepcopy(b);changed['financials'][0]['revenue']=None
    changed['financials'][0]['parent_profit']=0
    service.import_bundle(changed)
    with service.db.connection() as conn:
        row=conn.execute("SELECT * FROM sw_financial_facts WHERE stock_code='300750' AND period='2024-06-30' ORDER BY import_id DESC LIMIT 1").fetchone()
    assert row['revenue']==10 and row['parent_profit']==0


def test_quarter_cap_default_skip_and_revision_only_changed_company(service,monkeypatch,tmp_path):
    from quarterly_dashboard import industry_updates as updates
    b=fixture_bundle();service.import_bundle(b);service.directory=tmp_path/'sources'
    monkeypatch.setattr(updates,'target_trade_date',lambda *a:'2026-09-30')
    calls=[]
    def fetch(db,directory,stocks,target,progress,**options):
        calls.append((stocks,target,options['reuse_local']))
        return ([{**b['caps'][0],'total_cap':120}] if stocks else []),[]
    monkeypatch.setattr(updates,'fetch_cap_quarter',fetch)
    monkeypatch.setattr(updates,'fetch_financial_period',lambda *a:pytest.fail('cap requested finance'))
    updates.perform(service,'cap_quarter','2026Q3',False,lambda _:None)
    assert calls[-1]==([],'2026-09-30',True)
    updates.perform(service,'cap_quarter','2026Q3',True,lambda _:None)
    assert calls[-1]==(['000001','300750'],'2026-09-30',False)
    assert service.read(industry='630701',mode='ytd')['market_series'][-1]['total_cap']==320
    with service.db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM sw_cap_facts').fetchone()[0]==4
        assert conn.execute('SELECT count(*) FROM sw_financial_facts').fetchone()[0]==6
        assert conn.execute('SELECT count(*) FROM sw_memberships').fetchone()[0]==3
    # No changed value means no new import or facts, even on explicit recheck.
    previous=service.read(industry='630701',mode='ytd')['import_id']
    result=updates.perform(service,'cap_quarter','2026Q3',True,lambda _:None)
    assert result['import_id']==previous
    # A revision may legitimately return to an older numeric value.
    monkeypatch.setattr(updates,'fetch_cap_quarter',lambda *a,**k:([b['caps'][0]],[]))
    updates.perform(service,'cap_quarter','2026Q3',True,lambda _:None)
    assert service.read(industry='630701',mode='ytd')['market_series'][-1]['total_cap']==300


def test_partial_cap_retry_keeps_old_values_and_frozen_members(service,monkeypatch,tmp_path):
    from quarterly_dashboard import industry_updates as updates
    b=fixture_bundle();b['caps']=b['caps'][:1];service.import_bundle(b);service.directory=tmp_path/'sources'
    monkeypatch.setattr(updates,'target_trade_date',lambda *a:'2026-09-30')
    def fetch(db,directory,stocks,target,progress,**options):
        assert stocks==['000001']
        return [],[{'stock_code':'000001','error':'source unavailable'}]
    monkeypatch.setattr(updates,'fetch_cap_quarter',fetch)
    result=updates.perform(service,'cap_quarter','2026Q3',False,lambda _:None)
    assert len(result['failures'])==1
    market=service.read(industry='630701',mode='ytd')['market_series'][-1]
    assert market['known_cap']==100 and market['total_cap'] is None and market['coverage']==.5
    def retry(db,directory,stocks,target,progress,**options):
        assert stocks==['000001']
        return [fixture_bundle()['caps'][1]],[]
    monkeypatch.setattr(updates,'fetch_cap_quarter',retry)
    updates.perform(service,'cap_quarter','2026Q3',False,lambda _:None)
    assert service.read(industry='630701',mode='ytd')['market_series'][-1]['total_cap']==300
    changed=copy.deepcopy(b);changed['caps']=[];changed['financials']=[]
    changed['members'][1]['industry_code']='630702'
    service.import_bundle(changed)
    assert updates.cap_roster(service.db,service.foundation(),'2026Q3','2026-09-30')['members'][0]['industry_code']=='630701'


def test_ended_quarter_validation_calendar_weekend_and_exact_target(service,monkeypatch,tmp_path):
    from datetime import date
    from quarterly_dashboard import industry_updates as updates
    for action,target in [('refresh_pilot','2026Q3'),('financial_period','2026-07-01'),('cap_quarter','2026Q4')]:
        with pytest.raises(ValueError):updates.validate_request(action,target,today=date(2026,10,3))
    # 2023Q4 ended on Sunday: confirm Friday from the index calendar, without updating a stock.
    def index_calendar(session, directory, url, params):
        assert params['param'] == 'sh000001,day,,2023-12-31,30,'
        return {'code':0,'data':{'sh000001':{'day':[['2023-12-29','1','1']]}}},{}
    monkeypatch.setattr(updates,'_response',index_calendar)
    assert updates.target_trade_date(service.db,'2023Q4',tmp_path)=='2023-12-29'
    b=fixture_bundle();one=service.import_bundle(b)
    bad={**service.foundation(),'financials':[],'caps':[dict(b['caps'][0],trade_date='2026-10-01')],
         'obtained_at':b['obtained_at'],'manifest':{},'cap_roster':{'quarter':'2026Q3','target_date':'2026-09-30',
         'composition':'test','member_import_id':one,'members':[{'stock_code':'300750','industry_code':'630701'}]}}
    with pytest.raises(ValueError,match='固定目标'):service.import_bundle(bad,capture_local=False)
    assert service.read(industry='630701',mode='ytd')['import_id']==one


def test_target_period_source_filter_and_content_addressed_samples(monkeypatch,tmp_path):
    from quarterly_dashboard import industry_updates as updates
    payload={'success':True,'result':{'pages':1,'count':1,'data':[{'SECURITY_CODE':'300750',
        'REPORT_DATE':'2025-06-30','REPORTDATE':'2025-06-30','NOTICE_DATE':'2025-08-01','TOTAL_OPERATE_INCOME':1,'PARENT_NETPROFIT':0,'WEIGHTAVG_ROE':0}]}}
    params=[]
    class Response:
        def raise_for_status(self):pass
        def json(self):return payload
    class Session:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def get(self,url,**options):params.append(options['params']);return Response()
    monkeypatch.setattr(updates,'create_data_session',Session)
    one=updates.fetch_financial_period(tmp_path,['300750'],'2025-06-30')
    updates.fetch_financial_period(tmp_path,['300750'],'2025-06-30')
    assert params[0]['filter']=='(REPORT_DATE=\'2025-06-30\')(SECURITY_CODE in ("300750"))'
    assert one[0]['parent_profit']==0 and 'raw' not in one[0]['provenance']
    assert one[0]['weighted_roe']==0
    assert params[1]['reportName']=='RPT_LICO_FN_CPD'
    assert len(list((tmp_path/'responses').iterdir()))==1


def test_industry_api_requires_explicit_task_stock_routes_stay_separate(service,monkeypatch):
    from http.server import ThreadingHTTPServer
    from threading import Thread
    from urllib.request import Request,urlopen
    from urllib.error import HTTPError
    from quarterly_dashboard import server
    calls=[]
    monkeypatch.setattr(server,'industry_service',lambda:service)
    monkeypatch.setattr(service,'start_refresh',lambda *args:calls.append(args) or {'running':True})
    stock_calls=[]
    monkeypatch.setattr(server,'load_chart_data',lambda code,section,refresh,**options:
                        stock_calls.append((code,section,refresh,options['full'])) or {})
    monkeypatch.setattr(server,'price_bundle',lambda code,**options:stock_calls.append((code,'prices',options['refresh'],options['full'])) or {})
    http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
    thread=Thread(target=http.serve_forever,daemon=True);thread.start()
    try:
        root=f'http://127.0.0.1:{http.server_port}'
        for section in ('financial','valuation','dividends','shareholders','financing','prices'):
            with urlopen(root+f'/api/{section}?code=300750&refresh=1&full=1') as response:assert response.status==200
        assert len(stock_calls)==6 and not calls
        for command in ({'action':'refresh_pilot'},{'action':'cap_quarter','target':'2026Q3','code':'300750'}):
            with pytest.raises(HTTPError) as err:
                urlopen(Request(root+'/api/industry/refresh',data=json.dumps(command).encode(),headers={'Content-Type':'application/json'}))
            assert err.value.code==400
        command={'action':'cap_quarter','target':'2026Q3','recheck':False}
        with urlopen(Request(root+'/api/industry/refresh',data=json.dumps(command).encode(),headers={'Content-Type':'application/json'})) as response:
            assert json.load(response)['running']
        assert calls==[('cap_quarter','2026Q3',False)]
    finally:
        http.shutdown();http.server_close();thread.join()


def test_industry_job_audits_success_and_failure(service,monkeypatch,tmp_path):
    from quarterly_dashboard import industry_updates as updates
    service.import_bundle(fixture_bundle());service.directory=tmp_path/'sources'
    monkeypatch.setattr(updates,'perform',lambda *a:{'requested_count':0,'failures':[]})
    service._refresh('cap_quarter','2026Q3',False)
    assert service.status()['error'] is None
    monkeypatch.setattr(updates,'perform',lambda *a:(_ for _ in ()).throw(ValueError('source failed')))
    service._refresh('financial_period','2026-06-30',False)
    assert 'source failed' in service.status()['error'] and not service.status()['running']
    with service.db.connection() as conn:
        assert [r[0] for r in conn.execute('SELECT status FROM sw_update_runs ORDER BY id')]==['complete','failed']
        assert conn.execute('SELECT count(*) FROM sw_imports').fetchone()[0]==1
    with service.db.connection(write=True) as conn:
        conn.execute("INSERT INTO sw_update_runs(action,target,recheck,started_at,status) VALUES('cap_quarter','2026Q3',0,'2026-10-03','running')")
    assert service.recover_interrupted_runs()==1
    assert service.recover_interrupted_runs()==0


def test_populated_schema9_migration_preserves_cap_values_and_rejected_versions(monkeypatch,tmp_path):
    from quarterly_dashboard import storage
    migrations=storage.MIGRATIONS
    monkeypatch.setattr(storage,'MIGRATIONS',migrations[:9])
    db=Database(tmp_path/'old.sqlite3');db.initialize();b=fixture_bundle()
    with db.connection(write=True) as conn:
        for row in b['taxonomy']:
            conn.execute('INSERT INTO sw_industries(code,name,level,parent_code) VALUES(?,?,?,?)',
                         tuple(row[k] for k in ('code','name','level','parent_code')))
        for version,status in [(1,{}),(2,{'cap_status':'rejected'})]:
            conn.execute("INSERT INTO sw_imports VALUES(?,?,?,?,'complete')",(version,b['obtained_at'],str(version),json.dumps(status)))
            for row in b['members']:
                conn.execute('INSERT INTO sw_memberships VALUES(?,?,?,?,?,?)',(version,*[row[k] for k in ('stock_code','name','industry_code','effective_date','source_update')]))
            conn.execute('INSERT INTO sw_industry_cap_quarters VALUES(?,?,?,?,?,?,?,?)',
                (version,'630701','2026Q3','2026-09-30',2,2,300 if version==1 else 999,300 if version==1 else 999))
    monkeypatch.setattr(storage,'MIGRATIONS',migrations)
    db.initialize()
    with db.connection() as conn:
        roster=dict(conn.execute('SELECT * FROM sw_cap_quarter_rosters').fetchone())
        assert roster['member_import_id']==1 and roster['target_date']=='2026-09-30'
        assert conn.execute('SELECT count(*) FROM sw_cap_quarter_members').fetchone()[0]==3
        assert conn.execute('SELECT count(*) FROM sw_industry_cap_quarters').fetchone()[0]==2
    assert IndustryService(db).read(industry='630701')['market_series'][0]['total_cap']==300


def test_classification_recheck_advances_source_date_without_copying_data(service):
    service.import_bundle(fixture_bundle())
    b={**service.foundation(),'obtained_at':'2027-01-03T00:00:00+00:00','asof':'2027-01-03',
       'financials':[],'caps':[],'manifest':{'scope':'classification'}}
    service.import_bundle(b,capture_local=False)
    assert service.foundation()['member_obtained_at']==b['obtained_at']
    service.import_bundle(b,capture_local=False)
    with service.db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM sw_imports').fetchone()[0]==1
        assert conn.execute('SELECT count(*) FROM sw_memberships').fetchone()[0]==3
        assert conn.execute('SELECT count(*) FROM sw_update_runs').fetchone()[0]==1
