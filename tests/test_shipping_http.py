"""Isolated HTTP and browser scenario parity; fixture owns and closes its temporary port."""
import json
from copy import deepcopy
from datetime import date,timedelta
import requests
from test_collection_management_ui import live
from stock_analysis.domains.research.sectors.shipping import shipping_forecast as forecast
from stock_analysis.collection.collection import begin_batch,archive_source


def seed(db,monkeypatch):
    b=begin_batch(db,'browser-fixture','shipping.fixture','user_excel',domain='research_shipping')
    source=archive_source(db,b,'user_excel','shipping.fixture',dict(transport='fixture'),1,raw=b'isolated fixture')
    points=[]
    from calendar import monthrange
    for n in range(46):
        total=2022*12+11+n;y,m=total//12,total%12+1
        points.append(dict(index_name='CCFI',frequency='monthly',route='综合',statistic='monthly_mean',
            period_start=f'{y}-{m:02d}-01',period_end=f'{y}-{m:02d}-{monthrange(y,m)[1]}',value=1000+n*20,
            unit='点',quality='transcribed_unverified',channel='user_excel',published_on=None))
    forecast.publish_rates(db,points,b,source)
    weekly=[];day=date(2022,11,4);stop=date(2026,10,30);offset=0
    while day<=stop:
        weekly.append(dict(index_name='CCFI',frequency='weekly',route='综合',statistic='release',period_start=day.isoformat(),period_end=day.isoformat(),
            value=1500+offset*.4,unit='点',quality='transcribed_unverified',channel='user_excel',published_on=day.isoformat()))
        day+=timedelta(days=7);offset+=1
    forecast.publish_rates(db,weekly,b,source)
    history=[];business={}
    for n in range(14):
        y,q=2023+n//4,n%4+1;period=f'{y}-'+('03-31','06-30','09-30','12-31')[q-1]
        label=f'{y}Q{q}'
        history.append(dict(period=period,label=label,profit=20+n,revenue=160+n,cost=100,pretax=30,sources={}))
        business[period]=dict(international=70+n,domestic=20,teu=1000000,unit_rmb=7000+n*100)
    monkeypatch.setattr(forecast,'financials',lambda c:deepcopy(history))
    monkeypatch.setattr(forecast,'business_quarters',lambda c:(deepcopy(business),[]))
    with db.connection(write=True) as c:
        for scope,value in (('全国',3012),('沿海',2668),('内河',343)):
            c.execute("INSERT INTO research_shipping_industry_metrics(metric,scope,period,report_month,basis,value,unit,yoy,published_on,revision,batch_id,source_id,created_at) VALUES('throughput',?,'2026-08','2026-09','monthly',?,'万TEU',-4.4,'2026-10-15',1,?,?, '2026-10-25T00:00:00Z')",(scope,value,b,source))
        instrument=c.execute("INSERT INTO instruments(exchange,code,name,created_at) VALUES('sh','601919','中远海控','2026-10-25T00:00:00Z')").lastrowid
        run=c.execute("INSERT INTO sync_runs(instrument_id,dataset,source,adjustment,parser_version,methodology_version,trigger_reason,started_at,status,base_revision,record_count,finished_at) VALUES(?,'financial:gjzb','sina','raw','test','test','manual','2026-10-25T00:00:00Z','success',0,14,'2026-10-25T00:00:00Z')",(instrument,)).lastrowid
        for row in history:
            quarter=int(row['label'][-1]);year=row['label'][:4];period=row['period'];cumulative=[20,43,69,98][quarter-1]
            c.execute("INSERT INTO financial_reports(instrument_id,source,report_type,period,raw_json,content_hash,obtained_at,run_id) VALUES(?,'sina','gjzb',?,?,?,'2026-10-25T00:00:00Z',?)",
                (instrument,period,json.dumps({'data':[{'item_field':'PARENETP','item_value':str((cumulative+int(year[-1]))*1e8)}]}),'hash-'+period,run))


def test_http_read_only_auth_and_saved_snapshot(live,monkeypatch):
    base,db,state,web=live;seed(db,monkeypatch)
    s=requests.Session();s.trust_env=False
    payload=s.get(base+'/api/shipping-forecast',timeout=5).json()
    assert payload['context']['model']['n']==14
    with db.connection() as c:assert c.execute('SELECT count(*) FROM research_shipping_forecast_versions').fetchone()[0]==0
    command=dict(target='2026Q3',name='隔离验收',weekly_lead_weeks=2,input_hash=payload['context']['input_hash'],scenarios=payload['defaults'])
    for _ in range(5):
        assert s.post(base+'/api/shipping-forecast/save',json=command,timeout=5).status_code==403
    with db.connection() as c:
        assert c.execute('SELECT count(*) FROM research_shipping_forecast_versions').fetchone()[0]==0
    headers=dict(Origin=base,**{'X-Local-Session':payload['session_token']})
    saved=s.post(base+'/api/shipping-forecast/save',json=command,headers=headers,timeout=5)
    assert saved.ok
    version=s.get(base+'/api/shipping-forecast?version='+str(saved.json()['id']),timeout=5).json()
    assert version['snapshot']['results']==saved.json()['results'] and version['actual_profit'] is None
    assert s.get(base+'/api/shipping-forecast?code=600900',timeout=5).status_code==400
    three=s.get(base+'/api/shipping-forecast?target=2026Q3&weekly_lead_weeks=3',timeout=5).json()
    assert three['context']['weekly_window']['lead_weeks']==3
    assert three['context']['weekly_window']['start']=='2026-06-10'
    assert three['defaults']['base']['rate']==three['context']['weekly_window']['value']
    trend=s.get(base+'/api/shipping-trend?index=CCFI&frequency=monthly&quality=preferred&profit=quarter',timeout=5)
    assert trend.ok
    data=trend.json()
    assert data['statistic']=='monthly_mean' and len(data['points'])==46
    assert all(p['quality']=='transcribed_unverified' and p['channel']=='user_excel' for p in data['points'])
    assert len(data['profit'])>0 and all(r['value'] is not None for r in data['profit'])
    assert s.get(base+'/api/shipping-trend?index=SCFI&frequency=monthly',timeout=5).json()['points']==[]
    assert s.post(base+'/api/shipping-forecast/delete',json={'id':saved.json()['id']},headers=headers,timeout=5).status_code==200
    assert s.get(base+'/api/shipping-forecast?version='+str(saved.json()['id']),timeout=5).status_code==400
    assert s.post(base+'/api/shipping-forecast/delete',json={'id':saved.json()['id']},timeout=5).status_code==403


def test_browser_desktop_mobile_controls_versions_and_parity(live,monkeypatch):
    base,db,state,web=live;seed(db,monkeypatch)
    from playwright.sync_api import sync_playwright,expect
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        try:
            page=browser.new_page(viewport=dict(width=1440,height=1000),timezone_id='America/Los_Angeles');errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(base+'/?code=601919&tab=trend')
            assert page.locator('#shipping-forecast-entry a').get_attribute('target')=='_blank'
            assert page.locator('#shipping-forecast-entry a').get_attribute('rel')=='noopener'
            assert page.locator('#shipping-forecast-entry a').inner_text()=='海控净利润测算/运价趋势'
            assert '→' not in page.locator('#shipping-forecast-entry a').inner_text()
            page.goto(base+'/?code=601919&tab=shipping-forecast')
            expect(page.locator('.scenario')).to_have_count(3)
            expect(page.locator('#save')).to_be_enabled()
            assert page.locator('.scenario h3').all_text_contents()==['悲观情景','中性情景','乐观情景']
            assert page.locator('.calc-explanation summary').all_text_contents()==['计算说明']*3
            page.locator('.calc-explanation summary').first.click()
            expect(page.locator('#explanation-bear')).to_contain_text('归母净利润')
            assert page.locator('#weekly-lead-weeks').input_value()=='2'
            assert page.locator('#industry tbody tr').count()==3
            page.locator('[data-industry-scope="沿海"]').uncheck()
            assert page.locator('#industry tbody tr').count()==2
            page.locator('[data-industry-scope="沿海"]').check()
            old_rate=page.locator('[data-case="base"][data-key="rate"]').input_value()
            page.select_option('#weekly-lead-weeks','3')
            expect(page.locator('#basis')).to_contain_text('提前3周')
            new_rate=page.locator('[data-case="base"][data-key="rate"]').input_value()
            assert new_rate!=old_rate
            assert page.locator('.scenario h3').all_text_contents()==['悲观情景','中性情景','乐观情景']
            page.locator('[data-case="base"][data-key="override"]').fill('0')
            page.locator('[data-case="base"][data-key="volume"]').fill('0')
            page.locator('#name').fill('隔离浏览器三情景')
            with page.expect_response('**/api/shipping-forecast/save') as response:page.click('#save')
            saved=response.value.json();assert response.value.ok
            snapshot=requests.get(base+'/api/shipping-forecast?version='+str(saved['id']),timeout=5).json()['snapshot']
            assert snapshot['scenarios']['base']['override']==0
            expect(page.locator('#status')).to_contain_text('已保存不可改写版本')
            assert f"{saved['results']['base']['profit']:,.2f}".rstrip('0').rstrip('.') in page.locator('#result-base').inner_text()
            stamp=page.locator('.version-item time').first.inner_text()
            assert len(stamp)==19 and stamp[4]=='-' and 'T' not in stamp
            page.reload();expect(page.locator('.version-item')).to_have_count(1)
            page.locator('#target').fill('2026Q4');page.click('#load')
            expect(page.locator('#basis')).to_contain_text('2026Q4')
            page.locator('.version-item input').first.click()
            assert page.locator('#target').input_value()=='2026Q4'
            assert page.locator('#version-detail').inner_text()==''
            page.click('#load-version')
            expect(page.locator('#status')).to_contain_text('已载入版本 #')
            page.wait_for_function("document.getElementById('scenarios').getBoundingClientRect().top < 320")
            assert page.locator('#target').input_value()=='2026Q3'
            assert page.locator('[data-case="base"][data-key="override"]').input_value()=='0'
            expect(page.locator('#version-detail')).to_contain_text('原预测净利润')
            assert 'input_hash' not in page.locator('#version-detail').inner_text()
            assert page.locator('#weekly-lead-weeks').input_value()=='3'
            page.locator('#tab-trend').click()
            expect(page.locator('#trend-view')).to_be_visible()
            expect(page.locator('#trend-chart .empty')).to_be_visible()
            page.select_option('#trend-quality','preferred')
            expect(page.locator('#trend-coverage')).to_contain_text('46')
            expect(page.locator('#trend-chart')).to_have_class(__import__('re').compile('js-plotly-plot'))
            page.select_option('#trend-profit','quarter')
            expect(page.locator('#trend-profit-chart')).to_have_class(__import__('re').compile('js-plotly-plot'))
            assert page.locator('#trend-years').evaluate('(e)=>e.multiple')
            page.select_option('#trend-years',['2023','2024'])
            expect(page.locator('#comparison-chart')).to_have_class(__import__('re').compile('js-plotly-plot'))
            assert page.locator('#comparison-chart').evaluate("e=>e.data.some(t=>t.mode.includes('text')&&t.text.includes('2023'))")
            assert page.locator('#comparison-chart').evaluate("e=>e.data.some(t=>t.text.some((label,i)=>label&&t.y[i]!==null))")
            assert page.locator('#comparison-chart').evaluate('e=>!e.layout.shapes?.length')
            marker=page.locator('#trend-chart .scatterlayer .trace .points path').first
            box=marker.bounding_box();assert box and box['width']>0
            page.mouse.move(box['x']+box['width']/2,box['y']+box['height']/2)
            expect(page.locator('#trend-hover-card')).to_be_visible()
            expect(page.locator('#trend-hover-card')).to_contain_text('CCFI 综合')
            expect(page.locator('#trend-hover-card')).to_contain_text('归母净利润')
            assert page.locator('#trend-chart').evaluate('e=>e.layout.shapes.length===1')
            assert page.locator('#trend-profit-chart').evaluate("e=>e.layout.shapes.some(s=>s.name==='neutral-forecast'&&s.type==='rect'&&s.x0==='2026-08-31'&&s.x1==='2026-10-30'&&s.y0===0&&s.y1>0)&&e.data.find(t=>t.name==='单季度归母净利润').width===60*86400000&&!e.layout.annotations?.some(a=>a.name==='neutral-forecast-label')&&e.layout.shapes.some(s=>s.type==='line')")
            latest=page.locator('#trend-chart .scatterlayer .trace .points path').last
            latest_box=latest.bounding_box();assert latest_box and latest_box['width']>0
            page.mouse.move(latest_box['x']+latest_box['width']/2,latest_box['y']+latest_box['height']/2)
            expect(page.locator('#trend-hover-card')).to_contain_text('中性预测 2026Q3')
            page.select_option('#trend-index','SCFI')
            expect(page.locator('#trend-chart')).to_contain_text('当前筛选没有可展示数据')
            page.select_option('#trend-index','CCFI')
            expect(page.locator('#trend-chart')).to_have_class(__import__('re').compile('js-plotly-plot'))
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.set_viewport_size(dict(width=390,height=844));page.wait_for_timeout(600)
            overflow=page.evaluate('''() => [...document.querySelectorAll('*')].map(e=>({tag:e.tagName,id:e.id,cls:String(e.className).slice(0,60),right:e.getBoundingClientRect().right,width:e.getBoundingClientRect().width})).filter(x=>x.right>innerWidth+1).slice(0,12)''')
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),overflow
            assert page.locator('.trend-filters').evaluate('(e)=>e.getBoundingClientRect().height>250')
            mobile_latest=page.locator('#trend-chart .scatterlayer .trace .points path').last
            mobile_box=mobile_latest.bounding_box();assert mobile_box and mobile_box['width']>0
            page.mouse.move(mobile_box['x']+mobile_box['width']/2,mobile_box['y']+mobile_box['height']/2)
            expect(page.locator('#trend-hover-card')).to_contain_text('中性预测 2026Q3')
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.locator('#tab-forecast').click()
            expect(page.locator('#forecast-view')).to_be_visible()
            assert page.locator('[data-case="base"][data-key="override"]').input_value()=='0'
            assert not errors
        finally:browser.close()
