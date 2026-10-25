from copy import deepcopy
from pathlib import Path
import json
import sqlite3
import pytest
from quarterly_dashboard.storage import Database
from quarterly_dashboard.collection import begin_batch,archive_source,parsed_source
from quarterly_dashboard.shipping_sources import parse_sse,parse_mot,discover_mot,parse_throughput
from quarterly_dashboard.shipping_forecast import (publish_rates,current_rates,months_for,rate_window,calculate,
    defaults,save_version,read_version,model_context,read_forecast,read_trend)
from quarterly_dashboard.collection_management import CollectionManagement,validate_params,TASKS
from quarterly_dashboard.maintenance import DESCRIPTIONS,table_time
from quarterly_dashboard import shipping_collection

FIX=Path(__file__).parent/'fixtures/shipping'


@pytest.fixture
def db(tmp_path):
    db=Database(tmp_path/'test.sqlite3');db.initialize();return db


def evidence(db, provider='sse'):
    batch=begin_batch(db,'test-op','shipping.test',provider,domain='research_shipping')
    source=archive_source(db,batch,provider,'shipping.test',{'url':'https://example.invalid/test'},1,raw=b'evidence')
    parsed_source(db,source,fields=['test'])
    return batch,source


def test_real_free_weekly_shape():
    ccfi=parse_sse((FIX/'ccfi.html').read_text(encoding='utf8'),'CCFI')
    scfi=parse_sse((FIX/'scfi.html').read_text(encoding='utf8'),'SCFI')
    assert len(ccfi)==26 and len(scfi)==2
    assert len({p['route'] for p in ccfi})==13
    assert all(p['quality']=='official' for p in ccfi)
    assert scfi[-1]['change_kind']=='points'
    assert len({p['period_start'] for p in ccfi})==2


def test_month_statistics_and_route_identity():
    points=parse_mot((FIX/'month.html').read_text(encoding='utf8'),'2024-01')
    assert len(points)==39
    assert next(p['value'] for p in points if p['route']=='综合' and p['statistic']=='monthly_mean')==1191.91
    assert {p['statistic'] for p in points}=={'monthly_mean','annual_mean','ytd_mean'}
    assert '西非' in {p['route'] for p in points} and '东西非' not in {p['route'] for p in points}
    with pytest.raises(ValueError):parse_mot((FIX/'month.html').read_text(encoding='utf8'),'2024-02')
    with pytest.raises(ValueError):parse_mot('<title>2024年1月中国出口集装箱运价指数</title>2024-02-08<img src="table.png">','2024-01')


def test_source_revision_and_segment_atomicity(db):
    b,s=evidence(db);points=parse_sse((FIX/'ccfi.html').read_text(encoding='utf8'),'CCFI')
    assert publish_rates(db,points,b,s)==26
    assert publish_rates(db,points,b,s)==0
    updated=deepcopy(points[:1]);updated[0]['value']+=1
    assert publish_rates(db,updated,b,s)==1
    with db.connection() as conn:
        assert len(current_rates(conn))==26
        assert conn.execute('SELECT count(*) FROM research_shipping_rate_points WHERE supersedes_id IS NOT NULL').fetchone()[0]==1
    bad=deepcopy(points);bad[-1]['value']=float('nan')
    with pytest.raises(ValueError):publish_rates(db,bad,b,s)
    with db.connection() as conn:assert conn.execute('SELECT count(*) FROM research_shipping_rate_points').fetchone()[0]==27
    other,foreign=evidence(db)
    with pytest.raises(ValueError):publish_rates(db,updated,other,s)


def test_trend_filtering_statistics_routes_quality_years_and_profit(db):
    batch,source=evidence(db)
    monthly=parse_mot((FIX/'month.html').read_text(encoding='utf8'),'2024-01')
    publish_rates(db,monthly,batch,source)
    dec=deepcopy(next(p for p in monthly if p['route']=='综合' and p['statistic']=='monthly_mean'))
    dec.update(period_start='2022-12-01',period_end='2022-12-31',published_on='2023-01-15',value=900)
    feb=deepcopy(dec);feb.update(period_start='2024-02-01',period_end='2024-02-29',published_on='2024-03-15',value=1511.91)
    publish_rates(db,[dec,feb],batch,source)
    weekly=parse_sse((FIX/'ccfi.html').read_text(encoding='utf8'),'CCFI')
    publish_rates(db,weekly,batch,source)
    iso_weeks=[]
    for d,w in [('2021-01-01',1800),('2021-01-08',1810),('2022-01-01',1820)]:
        p=deepcopy(weekly[0]);p.update(route='综合',period_start=d,period_end=d,published_on=d,value=w)
        iso_weeks.append(p)
    publish_rates(db,iso_weeks,batch,source)
    # Supplement conflicts with official for the same month; it is only used
    # when supplement mode is selected and cannot overwrite the official fact.
    supplement=deepcopy(next(p for p in monthly if p['route']=='综合' and p['statistic']=='monthly_mean'))
    supplement.update(channel='user_excel',quality='transcribed_unverified',value=1441)
    fallback=deepcopy(supplement);fallback.update(period_start='2024-03-01',period_end='2024-03-31',value=1300)
    publish_rates(db,[supplement,fallback],batch,source)
    official=read_trend(db,index='CCFI',frequency='monthly',route='综合',quality='official')
    preferred=read_trend(db,index='CCFI',frequency='monthly',route='综合',quality='preferred')
    assert [p['value'] for p in official['points']]==[900,1191.91,1511.91]
    assert [p['value'] for p in preferred['points']]==[900,1191.91,1511.91,1300]
    assert preferred['points'][-1]['quality']=='transcribed_unverified'
    recent=read_trend(db,index='CCFI',frequency='monthly',route='综合',years='1')
    assert recent['coverage']['count']==2 and recent['coverage']['first']=='2024-01-31'
    assert '西非' in official['routes']
    assert official['statistic']=='monthly_mean'
    # No ambiguous fuzzy merge: West Africa and East-West Africa remain separate.
    assert '西非' in official['routes'] and '东西非' not in official['routes']
    assert read_trend(db,index='SCFI',frequency='monthly')['points']==[]
    weekly_trend=read_trend(db,index='CCFI',frequency='weekly',route='综合')
    assert weekly_trend['statistic']=='release' and len(weekly_trend['points'])==5
    assert {p['year']:p['x'] for p in weekly_trend['points'] if p['period_start'] in ('2021-01-01','2021-01-08','2022-01-01')}=={2020:53,2021:1,2021:52}
    assert '西非' not in weekly_trend['routes'] and '东西非' in weekly_trend['routes']


def test_profit_trend_uses_gjzb_first_lrb_field_fallback_and_cumulative_difference(db):
    batch,source=evidence(db)
    publish_rates(db,[dict(index_name='CCFI',frequency='monthly',route='综合',statistic='monthly_mean',period_start=d,period_end=d,
        value=1500,unit='点',change_value=None,change_kind=None,published_on=d,quality='official',channel='mot_sse')
        for d in ('2026-03-31','2026-06-30')],batch,source)
    with db.connection(write=True) as c:
        instrument=c.execute("INSERT INTO instruments(exchange,code,name,created_at) VALUES('sh','601919','中远海控','2026-10-25T00:00:00Z')").lastrowid
        runs={kind:c.execute("INSERT INTO sync_runs(instrument_id,dataset,source,adjustment,parser_version,methodology_version,trigger_reason,started_at,status,base_revision,record_count,finished_at) VALUES(?,?, 'sina','raw','test','test','manual','2026-10-25T00:00:00Z','success',0,3,'2026-10-25T00:00:00Z')",(instrument,'financial:'+kind)).lastrowid for kind in ('gjzb','lrb')}
        payloads=[('2026-03-31','gjzb','0'),('2026-06-30','lrb','-50000000'),('2026-06-30','gjzb',None)]
        for period,kind,value in payloads:
            field=[] if value is None else [{'item_field':'PARENETP','item_value':value}]
            raw=json.dumps({'data':field})
            c.execute("INSERT INTO financial_reports(instrument_id,source,report_type,period,raw_json,content_hash,obtained_at,run_id) VALUES(?,?,?,?,?,?,?,?)",
                (instrument,'sina',kind,period,raw,'hash-'+period+kind,'2026-10-25T00:00:00Z',runs[kind]))
    result=read_trend(db,profit='quarter')
    assert [(r['label'],r['value']) for r in result['profit']]==[('2026Q1',0.0),('2026Q2',-0.5)]
    cumulative=read_trend(db,profit='ytd')
    assert [r['value'] for r in cumulative['profit']]==[0.0,-0.5]


def test_shipping_page_has_tabs_and_trend_controls():
    html=(Path(__file__).parents[1]/'web/shipping-forecast.html').read_text(encoding='utf8')
    js=(Path(__file__).parents[1]/'web/shipping-forecast.js').read_text(encoding='utf8')
    assert 'tab-forecast' in html and 'tab-trend' in html
    assert all(f'id="trend-{name}"' in html for name in ('index','frequency','route','range','quality','profit','years'))
    assert '/api/shipping-trend?' in js and "hovermode:'x unified'" in js


def test_official_priority_and_incomplete_future(db):
    b,s=evidence(db)
    official=parse_mot((FIX/'month.html').read_text(encoding='utf8'),'2024-01')
    publish_rates(db,official,b,s)
    transcribed=deepcopy([p for p in official if p['route']=='综合' and p['statistic']=='monthly_mean'][0])
    transcribed.update(channel='user_excel',quality='transcribed_unverified',value=1441)
    publish_rates(db,[transcribed],b,s)
    with db.connection() as conn:points=[p for p in current_rates(conn) if p['route']=='综合']
    w=rate_window(points,'2024Q1')
    assert w['count']==1 and not w['complete'] and w['value'] is None
    assert w['refs']==[next(p['id'] for p in points if p['channel']=='mot_sse' and p['statistic']=='monthly_mean')]
    coverage=read_forecast(db,'2024Q1')['coverage']
    official_month=next(r for r in coverage if r['channel']=='mot_sse' and r['statistic']=='monthly_mean')
    assert official_month['first']=='2024-01-01'  # Annual reference must not extend month coverage.
    assert months_for('2024Q1')==['2023-12','2024-01','2024-02']
    week=rate_window(points,'2026Q3',weekly=True)
    assert week['start']=='2026-06-17' and week['end_exclusive']=='2026-09-17'


def test_scenarios_zero_override_and_missing():
    context=dict(target='2026Q3',anchor=dict(revenue=100,cost=70,pretax=20,profit=15),business=dict(international=80,unit_rmb=5000),
                 model=dict(slope=2),anchor_window=dict(value=1000),target_window=dict(value=1200),history=[dict(label='2026Q1',profit=10),dict(label='2026Q2',profit=15)])
    p=defaults(context)['base'];p.update(override=0,volume=0,cost=0,below=-10,conversion=75)
    result=calculate(context,p)
    assert result['profit']==15 and result['cumulative']==40 and result['growth']==0
    p.update(override=None,rate=None)
    assert calculate(context,p)['profit'] is None
    p['fx']=float('inf')
    with pytest.raises(ValueError):calculate(context,p)


def test_management_registration_defaults_and_isolation(db):
    manager=CollectionManagement(db,None)
    with db.connection() as conn:
        schedules=manager.schedules(conn)
        for key in ('shipping_weekly','shipping_monthly'):
            assert not schedules[key]['enabled']
            assert key in TASKS
    assert validate_params('shipping_monthly',dict(mode='backfill',start='2022-12-01',end='2026-09-30'))
    with pytest.raises(ValueError):validate_params('shipping_monthly',dict(start='2026-10-01'),scheduled=True)
    with pytest.raises(ValueError):validate_params('shipping_weekly',dict(mode='full'))
    with db.connection(write=True) as conn:
        ref=manager.reserve(conn,'shipping_weekly',dict(mode='incremental'))
        with pytest.raises(ValueError):manager.reserve(conn,'shipping_monthly',dict(mode='incremental'))
    data=manager.read()
    shipping=next(t for t in data['tasks'] if t['task_key']=='shipping_weekly')
    indices=next(t for t in data['tasks'] if t['task_key']=='indices')
    assert shipping['running'] and not indices['history']
    assert manager.detail('shipping_weekly',ref)['scope']['dataset']=='shipping_weekly'
    with pytest.raises(ValueError):manager.detail('indices',ref)
    manager.cancel('shipping_weekly')
    with db.connection() as conn:assert conn.execute('SELECT cancel_requested FROM index_collection_operations WHERE operation_id=?',(ref,)).fetchone()[0]==1


def test_new_tables_maintenance_empty_and_time(db):
    with db.connection() as conn:
        for name in ('rate_points','report_metrics','forecast_versions','industry_metrics'):
            table='research_shipping_'+name
            assert DESCRIPTIONS[table][0]=='行业研究'
            columns=[r['name'] for r in conn.execute('PRAGMA table_info('+table+')')]
            value,basis=table_time(conn,table,columns)
            assert value is None and basis=='created_at'


def test_directory_title_filter_and_real_month():
    links=discover_mot('<a href="202512/t.html">2024年1月中国出口集装箱运价指数</a><a href="analysis.html">2026年9月集装箱运输市场分析</a><a href="bad.html">2024年1月上海出口集装箱运价指数</a>')
    assert len(links)==2 and ('2024-01','rates') in links and ('2026-09','analysis') in links


def test_throughput_publication_month_and_negative_values():
    july=parse_throughput((FIX/'analysis-2026-08.html').read_text(encoding='utf8'),'2026-08')
    august=parse_throughput((FIX/'analysis-2026-09.html').read_text(encoding='utf8'),'2026-09')
    assert [r['value'] for r in july]==[2990,2642,348]
    assert [r['yoy'] for r in july]==[-.2,-.1,-.9]
    assert [r['value'] for r in august]==[3012,2668,343]
    assert august[0]['period']=='2026-08' and august[0]['report_month']=='2026-09'
    assert august[0]['published_on']=='2026-10-15'
    assert sum(r['value'] for r in august[1:])!=august[0]['value']  # Preserve official rounding.


def test_formal_page_and_read_only_contract(db,monkeypatch):
    from quarterly_dashboard.collection import RequestCollector
    monkeypatch.setattr(RequestCollector,'get',lambda *a,**k:pytest.fail('GET不得采集'))
    payload=read_forecast(db,'2026Q3')
    assert not payload['coverage'] and payload['context']['model'] is None
    html=(Path(__file__).parents[1]/'web/shipping-forecast.html').read_text(encoding='utf8')
    assert '前往采集管理' in html and '运行所选范围' not in html
    with pytest.raises(ValueError):model_context(db,'2026Q5')


def test_save_snapshot_and_stale_input(db,monkeypatch):
    import quarterly_dashboard.shipping_forecast as module
    context=dict(target='2026Q3',model_version=module.MODEL,input_hash='abc',anchor=dict(revenue=100,cost=70,pretax=20,profit=15),
        business=dict(international=80,unit_rmb=5000),model=dict(slope=2),anchor_window=dict(value=1000),target_window=dict(value=1200),history=[])
    monkeypatch.setattr(module,'model_context',lambda *a:deepcopy(context))
    command=dict(target='2026Q3',name='测试',input_hash='abc',scenarios=defaults(context))
    id=save_version(db,command)['id']
    with db.connection(write=True) as conn:
        with pytest.raises(sqlite3.IntegrityError):conn.execute('UPDATE research_shipping_forecast_versions SET name=? WHERE id=?',('修改',id))
    original=read_version(db,id)
    context['input_hash']='new'
    with pytest.raises(ValueError):save_version(db,command)
    assert read_version(db,id)['snapshot']==original['snapshot']


def test_weekly_managed_incremental_and_refusal_stops_shared_source(db,monkeypatch):
    from quarterly_dashboard.collection import ProviderBlocked
    sent=[]
    class Collector:
        def __init__(self,db,**kwargs):self.db=db
        def check_cancelled(self):pass
        def report(self):return dict(network_requests=len(sent))
        def get(self,batch,provider,dataset,url,**kwargs):
            sent.append(url)
            html=(FIX/('scfi.html' if 'scfi' in url else 'ccfi.html')).read_text(encoding='utf8')
            source=archive_source(self.db,batch,provider,dataset,dict(url=url),1,raw=html.encode())
            return type('Response',(),dict(text=html))(),source
    monkeypatch.setattr(shipping_collection,'RequestCollector',Collector)
    def run(mode):
        with db.connection(write=True) as c:ref=shipping_collection.reserve(c,'shipping_weekly',dict(mode=mode))
        return shipping_collection.run(db,ref)
    assert run('incremental')['status']=='complete' and len(sent)==2
    assert run('incremental')['status']=='complete' and len(sent)==2
    assert run('review')['status']=='complete' and len(sent)==4
    with db.connection() as c:assert c.execute('SELECT count(*) FROM research_shipping_rate_points').fetchone()[0]==28
    def blocked(*a,**k):sent.append('blocked');raise ProviderBlocked('拒绝后冷却')
    monkeypatch.setattr(Collector,'get',blocked)
    assert run('review')['status']=='failed' and sent.count('blocked')==1


def test_cancel_before_source_keeps_no_facts(db):
    with db.connection(write=True) as c:
        ref=shipping_collection.reserve(c,'shipping_weekly',dict(mode='review'))
        c.execute('UPDATE index_collection_operations SET cancel_requested=1 WHERE operation_id=?',(ref,))
    assert shipping_collection.run(db,ref)['status']=='cancelled'
    with db.connection() as c:assert c.execute('SELECT count(*) FROM research_shipping_rate_points').fetchone()[0]==0


def test_image_review_preserves_source_and_is_idempotent(db,tmp_path):
    from quarterly_dashboard.shipping_image_review import import_review
    b=begin_batch(db,'image-test','shipping.mot_rates','mot',domain='research_shipping')
    html=b'<title>2026\xe5\xb9\xb48\xe6\x9c\x88\xe4\xb8\xad\xe5\x9b\xbd\xe5\x87\xba\xe5\x8f\xa3\xe9\x9b\x86\xe8\xa3\x85\xe7\xae\xb1\xe8\xbf\x90\xe4\xbb\xb7\xe6\x8c\x87\xe6\x95\xb0</title><p>2026-09-03</p><img src="table.png">'
    hs=archive_source(db,b,'mot','shipping.mot_rates',dict(url='https://www.mot.gov.cn/month.html'),1,raw=html)
    image=archive_source(db,b,'mot','shipping.mot_image',dict(url='https://www.mot.gov.cn/table.png'),1,raw=b'\x89PNGfixture')
    review=dict(image_source_id=image,html_source_id=hs,month='2026-08',published_on='2026-09-03',verified=True,
                rows=[dict(route='综合',annual=1197.6,ytd=1414.52,monthly=1835.55,change=-1.9)])
    file=tmp_path/'review.json';file.write_text(json.dumps(review),encoding='utf8')
    assert import_review(db,file)['published']==3
    assert import_review(db,file)['published']==0
    with db.connection() as c:
        r=c.execute("SELECT * FROM research_shipping_rate_points WHERE statistic='monthly_mean'").fetchone()
        assert r['source_id']==image and r['change_value']==-1.9
    review['verified']=False;file.write_text(json.dumps(review),encoding='utf8')
    with pytest.raises(ValueError):import_review(db,file)


def test_formal_quarter_prepare_reuses_local_pdf_and_no_network(db,monkeypatch):
    from quarterly_dashboard.company_report_service import CompanyReportService
    service=CompanyReportService(db)
    file=db.path.parent/'q1.pdf';file.write_bytes(b'%PDF-test')
    doc=service.import_report('601919','2026-03-31',file,url='https://static.cninfo.com.cn/q1.pdf')
    monkeypatch.setattr(service,'parse',lambda d:dict(id=101,document_id=d['id']))
    prepared=service.prepare_period('601919','2026-03-31')
    assert prepared['cached'] and prepared['document']['id']==doc['id']
    with pytest.raises(ValueError):service.prepare_period('601919','2026-09-30')


def test_monthly_recovery_logs_reused_segments_and_keeps_failed_history(db,monkeypatch):
    from quarterly_dashboard.collection import finish_batch, RequestCollector
    from quarterly_dashboard.shipping_sources import MOT
    month='2026-08';scope=dict(mode='backfill',start=month+'-01',end=month+'-31')
    with db.connection(write=True) as c:old=shipping_collection.reserve(c,'shipping_monthly',scope)
    directory=f'<a href="rates.html">2026年8月中国出口集装箱运价指数</a><a href="analysis.html">2026年8月集装箱运输市场分析</a>'
    stamp='2026-10-25T01:56:47+00:00'
    originals=[]
    for kind,html in [('directory',directory),('rates','verified monthly page'),('analysis','verified throughput page')]:
        url=MOT if kind=='directory' else MOT+kind+'.html'
        batch=begin_batch(db,old,'shipping.mot_'+kind,'mot',scope=dict(scope,url=url),domain='research_shipping')
        source=archive_source(db,batch,'mot','shipping.mot_'+kind,dict(url=url),1,raw=html.encode(),obtained_at=stamp)
        parsed_source(db,source,fields=['verified'])
        finish_batch(db,batch,'complete','partial' if kind=='rates' else 'unknown' if kind=='directory' else 'complete')
        originals.append(batch)
    with db.connection(write=True) as c:
        c.execute("UPDATE index_collection_operations SET status='failed',finished_at=?,error='首次解析失败' WHERE operation_id=?",(stamp,old))
    monkeypatch.setattr(RequestCollector,'get',lambda *a,**k:pytest.fail('已校验分段恢复不得联网'))
    for _ in range(2):
        with db.connection(write=True) as c:ref=shipping_collection.reserve(c,'shipping_monthly',scope)
        result=shipping_collection.run(db,ref)
        assert result['status']=='complete' and not result['errors']
        with db.connection() as c:
            batches=[dict(r) for r in c.execute('SELECT * FROM collection_batches WHERE operation_id=?',(ref,))]
            assert len(batches)==3
            assert sorted(r['coverage_status'] for r in batches)==['complete','partial','unknown']
            assert all(r['status']=='complete' for r in batches)
            assert c.execute('SELECT status FROM index_collection_operations WHERE operation_id=?',(old,)).fetchone()[0]=='failed'
            assert c.execute("SELECT min(obtained_at),max(obtained_at) FROM collection_sources WHERE batch_id IN (SELECT id FROM collection_batches WHERE operation_id=?)",(ref,)).fetchone()[:]==(stamp,stamp)
            assert c.execute('SELECT count(*) FROM research_shipping_rate_points').fetchone()[0]==0
        task=next(t for t in CollectionManagement(db,None).read()['tasks'] if t['task_key']=='shipping_monthly')
        assert task['history'][0]['status']=='complete' and task['last_success'] and task['coverage']=='partial'


def test_reused_month_rejects_corrupt_archive_and_records_failure(db):
    from quarterly_dashboard.collection import finish_batch
    b=begin_batch(db,'old','shipping.mot_rates','mot',scope=dict(url='https://www.mot.gov.cn/test.html'),domain='research_shipping')
    source=archive_source(db,b,'mot','shipping.mot_rates',dict(url='https://www.mot.gov.cn/test.html'),1,raw=b'original')
    parsed_source(db,source,fields=['verified']);finish_batch(db,b,'complete','partial')
    with db.connection() as c:
        previous=dict(c.execute('SELECT * FROM collection_batches WHERE id=?',(b,)).fetchone())
        path=db.path.parent/c.execute('SELECT file_path FROM collection_sources WHERE id=?',(source,)).fetchone()[0]
    path.write_bytes(b'corrupt')
    with pytest.raises(ValueError,match='哈希'):
        shipping_collection.reuse_month_segment(db,'new',dict(mode='backfill'),'2026-09','rates','https://www.mot.gov.cn/test.html',previous)
    with db.connection() as c:
        new=c.execute("SELECT * FROM collection_batches WHERE operation_id='new'").fetchone()
        assert new['status']=='failed' and new['coverage_status']=='unknown' and new['scope_end']=='2026-09-30'
