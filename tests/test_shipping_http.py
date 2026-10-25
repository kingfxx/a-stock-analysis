"""Isolated HTTP and browser scenario parity; fixture owns and closes its temporary port."""
import json
from copy import deepcopy
import requests
from test_collection_management_ui import live
from quarterly_dashboard import shipping_forecast as forecast
from quarterly_dashboard.collection import begin_batch,archive_source


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
    history=[];business={}
    for n in range(14):
        y,q=2023+n//4,n%4+1;period=f'{y}-'+('03-31','06-30','09-30','12-31')[q-1]
        label=f'{y}Q{q}'
        history.append(dict(period=period,label=label,profit=20+n,revenue=160+n,cost=100,pretax=30,sources={}))
        business[period]=dict(international=70+n,domestic=20,teu=1000000,unit_rmb=7000+n*100)
    monkeypatch.setattr(forecast,'financials',lambda c:deepcopy(history))
    monkeypatch.setattr(forecast,'business_quarters',lambda c:(deepcopy(business),[]))


def test_http_read_only_auth_and_saved_snapshot(live,monkeypatch):
    base,db,state,web=live;seed(db,monkeypatch)
    s=requests.Session();s.trust_env=False
    payload=s.get(base+'/api/shipping-forecast',timeout=5).json()
    assert payload['context']['model']['n']==14
    with db.connection() as c:assert c.execute('SELECT count(*) FROM research_shipping_forecast_versions').fetchone()[0]==0
    command=dict(target='2026Q3',name='隔离验收',input_hash=payload['context']['input_hash'],scenarios=payload['defaults'])
    assert s.post(base+'/api/shipping-forecast/save',json=command,timeout=5).status_code==403
    headers=dict(Origin=base,**{'X-Local-Session':payload['session_token']})
    saved=s.post(base+'/api/shipping-forecast/save',json=command,headers=headers,timeout=5)
    assert saved.ok
    version=s.get(base+'/api/shipping-forecast?version='+str(saved.json()['id']),timeout=5).json()
    assert version['snapshot']['results']==saved.json()['results'] and version['actual_profit'] is None
    assert s.get(base+'/api/shipping-forecast?code=600900',timeout=5).status_code==400
    trend=s.get(base+'/api/shipping-trend?index=CCFI&frequency=monthly&quality=preferred&profit=quarter',timeout=5)
    assert trend.ok
    data=trend.json()
    assert data['statistic']=='monthly_mean' and len(data['points'])==46
    assert all(p['quality']=='transcribed_unverified' and p['channel']=='user_excel' for p in data['points'])
    assert s.get(base+'/api/shipping-trend?index=SCFI&frequency=monthly',timeout=5).json()['points']==[]


def test_browser_desktop_mobile_controls_versions_and_parity(live,monkeypatch):
    base,db,state,web=live;seed(db,monkeypatch)
    from playwright.sync_api import sync_playwright,expect
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        try:
            page=browser.new_page(viewport=dict(width=1440,height=1000),timezone_id='America/Los_Angeles');errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(base+'/?code=601919&tab=shipping-forecast')
            expect(page.locator('.scenario')).to_have_count(3)
            expect(page.locator('#save')).to_be_enabled()
            page.locator('[data-case="base"][data-key="override"]').fill('0')
            page.locator('[data-case="base"][data-key="volume"]').fill('0')
            page.locator('#name').fill('隔离浏览器三情景')
            with page.expect_response('**/api/shipping-forecast/save') as response:page.click('#save')
            saved=response.value.json();assert response.value.ok
            expect(page.locator('#status')).to_contain_text('已保存不可改写版本')
            assert f"{saved['results']['base']['profit']:,.2f}".rstrip('0').rstrip('.') in page.locator('#result-base').inner_text()
            page.locator('[data-version]').first.click()
            expect(page.locator('#version-detail')).to_contain_text('predictions')
            page.locator('#tab-trend').click()
            expect(page.locator('#trend-view')).to_be_visible()
            page.select_option('#trend-quality','preferred')
            expect(page.locator('#trend-coverage')).to_contain_text('46')
            expect(page.locator('#trend-chart')).to_have_class(__import__('re').compile('js-plotly-plot'))
            assert page.locator('#trend-years').evaluate('(e)=>e.multiple')
            page.select_option('#trend-years',['2023','2024'])
            expect(page.locator('#comparison-chart')).to_have_class(__import__('re').compile('js-plotly-plot'))
            page.select_option('#trend-index','SCFI')
            expect(page.locator('#trend-chart')).to_contain_text('当前筛选没有可展示数据')
            page.select_option('#trend-index','CCFI')
            expect(page.locator('#trend-chart')).to_have_class(__import__('re').compile('js-plotly-plot'))
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.set_viewport_size(dict(width=390,height=844));page.wait_for_timeout(600)
            overflow=page.evaluate('''() => [...document.querySelectorAll('*')].map(e=>({tag:e.tagName,id:e.id,cls:String(e.className).slice(0,60),right:e.getBoundingClientRect().right,width:e.getBoundingClientRect().width})).filter(x=>x.right>innerWidth+1).slice(0,12)''')
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),overflow
            assert page.locator('.trend-filters').evaluate('(e)=>e.getBoundingClientRect().height>250')
            page.locator('#tab-forecast').click()
            expect(page.locator('#forecast-view')).to_be_visible()
            assert page.locator('[data-case="base"][data-key="override"]').input_value()=='0'
            page.locator('#target').fill('2026Q4');page.click('#load')
            expect(page.locator('#basis')).to_contain_text('2026Q4')
            assert page.locator('#result-base').inner_text().startswith('窗口缺值')
            assert not errors
        finally:browser.close()
