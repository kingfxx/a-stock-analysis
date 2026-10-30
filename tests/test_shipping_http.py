"""Isolated HTTP and browser scenario parity; fixture owns and closes its temporary port."""
import json
from copy import deepcopy
from datetime import date,timedelta
import requests
from test_collection_management_ui import live
from stock_analysis.domains.research.sectors.shipping import shipping_forecast as forecast
from stock_analysis.collection.collection import begin_batch,archive_source


def test_trend_connects_quality_transitions_without_crossing_missing_weeks(live,monkeypatch):
    base,db,state,web=live;seed(db,monkeypatch)
    from playwright.sync_api import sync_playwright
    payload=requests.get(base+'/api/shipping-trend?index=CCFI&frequency=weekly&quality=preferred&profit=none',timeout=5).json()
    template=payload['points'][0]
    payload['points']=[dict(template,date=d,period_start=d,period_end=d,value=v,quality=q) for d,v,q in [
        ('2026-09-24',1918,'calendar_inferred'),('2026-09-30',1923,'calendar_inferred_flagged'),
        ('2026-10-09',1912,'calendar_inferred_flagged'),('2026-10-16',1922.64,'official'),
        ('2026-10-30',1881.82,'official')]]
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        try:
            page=browser.new_page();page.route('**/api/shipping-trend?**',lambda route:route.fulfill(json=payload))
            page.goto(base+'/?code=601919&tab=shipping-forecast&view=trend')
            page.wait_for_function("document.getElementById('trend-chart').data?.length===3")
            traces=page.locator('#trend-chart').evaluate('e=>e.data.map(t=>({name:t.name,x:t.x,y:t.y,size:t.marker.size}))')
            flagged=next(t for t in traces if 'flagged' in t['name']);official=next(t for t in traces if '官方' in t['name'])
            assert flagged['y'][:3]==[1918,1923,1912] and flagged['size'][0]==0
            assert official['y'][2:4]==[1912,1922.64] and official['size'][2]==0
            assert official['y'][4] is None and official['y'][5]==1881.82
        finally:browser.close()


def test_route_revenue_chart_basis_and_route_controls(live,monkeypatch):
    base,db,state,web=live;seed(db,monkeypatch)
    from playwright.sync_api import sync_playwright,expect
    fixture={'routes':[{'scope':'transpacific','name':'跨太平洋'},{'scope':'domestic','name':'中国大陆'}],
             'series':{'quarter':[{'label':'2025Q1','scope':'transpacific','value':10,'yoy':None},
                                  {'label':'2026Q1','scope':'transpacific','value':15,'yoy':50},
                                  {'label':'2026Q1','scope':'domestic','value':5,'yoy':-20}],
                       'ytd':[{'label':'2026Q2','scope':'transpacific','value':40,'yoy':25}],
                       'annual':[{'label':'2025','scope':'transpacific','value':80,'yoy':None}]}}
    fixture['metrics']={'revenue':{'series':fixture['series'],'unit':'亿元'},
        'volume':{'series':{'quarter':[{'label':'2026Q1','scope':'transpacific','value':25,'yoy':5}],'ytd':[],'annual':[]},'unit':'万TEU'},
        'unit_revenue':{'series':{'quarter':[{'label':'2026Q1','scope':'transpacific','value':1500,'yoy':10}],'ytd':[],'annual':[]},'unit':'美元/TEU'}}
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        try:
            for width in (1440,390):
                page=browser.new_page(viewport={'width':width,'height':900}); errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.route('**/api/shipping-route-revenues',lambda route:route.fulfill(json=fixture))
                page.goto(base+'/?code=601919&tab=shipping-forecast&view=trend')
                page.wait_for_function("document.getElementById('route-revenue-chart').data?.length===2")
                chart=page.locator('#route-revenue-chart')
                assert chart.evaluate("e=>e.data[0].text.includes('+50.0%') && e.layout.hovermode==='x unified'")
                expect(page.locator('#route-revenue-table')).to_contain_text('+50.00%')
                page.locator('[data-route-metric=volume]').click()
                page.wait_for_function("document.getElementById('route-revenue-chart').layout?.yaxis.title.text==='箱量（万TEU）'")
                expect(page.locator('[data-route-metric=volume]')).to_have_attribute('aria-pressed','true')
                page.locator('[data-route-metric=unit_revenue]').click()
                page.wait_for_function("document.getElementById('route-revenue-chart').layout?.yaxis.title.text==='单箱收入（美元/TEU）'")
                assert chart.evaluate("e=>e.data[0].y[0]===1500")
                page.locator('[data-route-metric=revenue]').click()
                page.select_option('#route-revenue-basis','annual')
                page.wait_for_function("document.getElementById('route-revenue-chart').data?.[0].x[0]==='2025'")
                page.locator('#route-revenue-filter input[value=domestic]').uncheck()
                page.wait_for_function("document.getElementById('route-revenue-chart').data?.length===1")
                page.select_option('#route-revenue-basis','ytd')
                page.wait_for_function("document.getElementById('route-revenue-chart').data?.[0].y[0]===40")
                expect(page.locator('#route-revenue-status')).to_contain_text('同比')
                assert not errors
                page.close()
        finally:browser.close()


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
    assert s.get(base+'/api/shipping-route-revenues',timeout=5).json()['series']['quarter']==[]
    assert trend.ok
    data=trend.json()
    assert data['statistic']=='monthly_mean' and len(data['points'])==46
    assert all(p['quality']=='transcribed_unverified' and p['channel']=='user_excel' for p in data['points'])
    assert len(data['profit'])>0 and all(r['value'] is not None for r in data['profit'])
    assert s.get(base+'/api/shipping-trend?index=SCFI&frequency=monthly',timeout=5).json()['points']==[]
    assert s.post(base+'/api/shipping-forecast/delete',json={'id':saved.json()['id']},headers=headers,timeout=5).status_code==200
    assert s.get(base+'/api/shipping-forecast?version='+str(saved.json()['id']),timeout=5).status_code==400
    assert s.post(base+'/api/shipping-forecast/delete',json={'id':saved.json()['id']},timeout=5).status_code==403



def assert_monthly_quarter_alignment(page):
    # Compare rendered SVG edges with the monthly markers in both charts.
    result=page.evaluate("""() => {
        const rate=document.getElementById('trend-chart'),profit=document.getElementById('trend-profit-chart'),
            t=profit.data[0],bars=[...profit.querySelectorAll('.barlayer .point path')],
            points=[...rate.querySelectorAll('.scatterlayer .trace .points path')],
            series=rate.data[0],axis=profit._fullLayout.xaxis,
            px=x=>profit.getBoundingClientRect().left+axis._offset+axis.l2p(x);
        const checks=t.customdata.map((date,i)=>{
            const last=Number(date.slice(0,4))*12+Number(date.slice(5,7))-1,
                ids=[last-2,last-1,last].map(x=>series.x.indexOf(x));
            if(ids.some(i=>i<0))return true;
            const box=bars[i].getBoundingClientRect(),xs=ids.map(i=>{const b=points[i].getBoundingClientRect();return b.x+b.width/2;});
            return Math.abs(box.left-xs[0])<1&&Math.abs((box.left+box.right)/2-xs[1])<1&&Math.abs(box.right-xs[2])<1;
        });
        const shape=profit.layout.shapes.find(s=>s.name==='neutral-forecast'),
            q3=2026*12+6,forecastOk=shape&&shape.x0===q3&&shape.x1===q3+2,
            gap=t.x.every((x,i)=>i===0||x-t.width[i]/2>t.x[i-1]+t.width[i-1]/2);
        return {checks,forecastOk,gap,axes:JSON.stringify(rate._fullLayout.xaxis.range)===JSON.stringify(axis.range),
            samePixels:series.x.every(x=>Math.abs(px(x)-(rate.getBoundingClientRect().left+rate._fullLayout.xaxis._offset+rate._fullLayout.xaxis.l2p(x)))<1)};
    }""")
    assert all(result['checks']) and result['forecastOk'] and result['gap'] and result['axes'] and result['samePixels'],result
    page.locator('#trend-chart').scroll_into_view_if_needed()
    for month in ('04','05','06'):
        marker=page.locator('#trend-chart').evaluate("""(e,month)=>{
            const i=e.data[0].customdata.indexOf('2026-'+month+'-'+(month==='05'?'31':'30')),
                p=e.querySelectorAll('.scatterlayer .trace .points path')[i].getBoundingClientRect();
            return {x:p.x+p.width/2,y:p.y+p.height/2};
        }""",month)
        page.mouse.move(marker['x'],marker['y'])
        from playwright.sync_api import expect
        expect(page.locator('#trend-hover-card')).to_contain_text('2026-'+month+'-'+('31' if month=='05' else '30'))
        expect(page.locator('#trend-hover-card')).to_contain_text('2026Q2')


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
            assert page.locator('#shipping-forecast-entry').evaluate("e=>getComputedStyle(e).marginLeft===getComputedStyle(document.querySelector('.chart-explanation')).marginLeft")
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
            assert page.locator('#trend-profit').input_value()=='quarter'
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
            assert page.locator('#trend-chart').evaluate("e=>JSON.stringify(e._fullLayout.xaxis.range)===JSON.stringify(document.querySelector('#trend-profit-chart')._fullLayout.xaxis.range)")
            assert_monthly_quarter_alignment(page)
            page.evaluate("Plotly.relayout('trend-chart',{'xaxis.range[0]':24312,'xaxis.range[1]':24322})")
            page.wait_for_function("document.getElementById('trend-profit-chart')._fullLayout.xaxis.range[0]===24312")
            assert_monthly_quarter_alignment(page)
            assert page.locator('#trend-chart').evaluate("e=>e.layout.xaxis.ticktext.includes('2026-04')&&e.layout.xaxis.ticktext.includes('2026-06')")
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
            assert_monthly_quarter_alignment(page)
            mobile_latest=page.locator('#trend-chart .scatterlayer .trace .points path').last
            mobile_box=mobile_latest.bounding_box();assert mobile_box and mobile_box['width']>0
            page.mouse.move(mobile_box['x']+mobile_box['width']/2,mobile_box['y']+mobile_box['height']/2)
            expect(page.locator('#trend-hover-card')).to_contain_text('中性预测 2026Q3')
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.locator('#tab-forecast').click()
            expect(page.locator('#forecast-view')).to_be_visible()
            assert page.locator('[data-case="base"][data-key="override"]').input_value()=='0'
            page.goto(base+'/?code=601919&tab=shipping-forecast&view=trend')
            expect(page.locator('#trend-view')).to_be_visible()
            page.wait_for_function("document.getElementById('trend-coverage').textContent.length>0")
            page.select_option('#trend-quality','preferred')
            expect(page.locator('#trend-chart')).to_have_class(__import__('re').compile('js-plotly-plot'))
            assert page.locator('#trend-profit').input_value()=='quarter'
            expect(page.locator('#trend-profit-chart')).to_have_class(__import__('re').compile('js-plotly-plot'))
            assert page.locator('#trend-chart').evaluate("e=>JSON.stringify(e._fullLayout.xaxis.range)===JSON.stringify(document.querySelector('#trend-profit-chart')._fullLayout.xaxis.range)")
            assert not errors
        finally:browser.close()


def test_browser_month_week_switch_preserves_quarter_alignment(live,monkeypatch):
    base,db,state,web=live;seed(db,monkeypatch)
    from playwright.sync_api import sync_playwright,expect
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        try:
            for width in (1440,390):
                page=browser.new_page(viewport=dict(width=width,height=1000))
                page.goto(base+'/?code=601919&tab=shipping-forecast&view=trend')
                page.wait_for_function("document.getElementById('trend-coverage').textContent.length>0")
                page.select_option('#trend-quality','preferred')
                page.wait_for_function("document.getElementById('trend-chart').data?.length")
                page.select_option('#trend-profit','quarter')
                page.wait_for_function("document.getElementById('trend-profit-chart').data?.length")
                page.select_option('#trend-frequency','weekly')
                page.wait_for_function("document.getElementById('trend-chart')._fullLayout?.xaxis.type==='date' && document.getElementById('trend-profit-chart')._fullLayout?.xaxis.type==='date'")
                page.evaluate("Plotly.relayout('trend-chart',{'xaxis.range[0]':'2026-04-01','xaxis.range[1]':'2026-10-01'})")
                page.wait_for_function("Date.parse(document.getElementById('trend-profit-chart')._fullLayout.xaxis.range[0])===Date.parse('2026-04-01')")
                result=page.evaluate("""()=>{
                    const a=document.getElementById('trend-chart'),b=document.getElementById('trend-profit-chart'),t=b.data[0],i=t.customdata.indexOf('2026-06-30'),
                        start=Date.parse(t.x[i])-t.width[i]/2,end=Date.parse(t.x[i])+t.width[i]/2,day=86400000,
                        dates=a.data[0].x.filter(d=>d>='2026-04-01'&&d<'2026-07-01'),
                        box=b.querySelectorAll('.barlayer .point path')[i].getBoundingClientRect(),axis=a._fullLayout.xaxis,
                        px=d=>a.getBoundingClientRect().left+axis._offset+axis.l2p(axis.d2l(d)),
                        shape=b.layout.shapes.find(s=>s.name==='neutral-forecast');
                    return {dateAxis:a._fullLayout.xaxis.type==='date'&&b._fullLayout.xaxis.type==='date',
                        sameRange:JSON.stringify(axis.range)===JSON.stringify(b._fullLayout.xaxis.range),
                        nearEdges:Math.abs(start-Date.parse('2026-04-01'))<=4*day&&Math.abs(end-Date.parse('2026-07-01'))<=4*day,
                        weeklyPixels:dates.every(d=>px(d)>=box.left-4*day/ (Date.parse('2026-10-01')-Date.parse('2026-04-01'))*axis._length-1&&px(d)<=box.right+4*day/(Date.parse('2026-10-01')-Date.parse('2026-04-01'))*axis._length+1),
                        gap:Date.parse(shape.x0)>end&&Date.parse(shape.x0)-end<=8*day,
                        forecast:Math.abs(Date.parse(shape.x0)-Date.parse('2026-07-01'))<=4*day&&Math.abs(Date.parse(shape.x1)-Date.parse('2026-10-01'))<=4*day};
                }""")
                assert all(result.values()),result
                page.locator('#trend-chart').scroll_into_view_if_needed()
                for day,quarter in [('2026-04-03','2026Q2'),('2026-06-26','2026Q2'),('2026-07-03','2026Q3')]:
                    xy=page.locator('#trend-chart').evaluate("""(e,day)=>{const i=e.data[0].x.indexOf(day),p=e.querySelectorAll('.scatterlayer .trace .points path')[i].getBoundingClientRect();return {x:p.x+p.width/2,y:p.y+p.height/2};}""",day)
                    page.mouse.move(xy['x'],xy['y'])
                    expect(page.locator('#trend-hover-card')).to_contain_text(day)
                    expect(page.locator('#trend-hover-card')).to_contain_text(quarter)
                page.select_option('#trend-frequency','monthly')
                page.wait_for_function("document.getElementById('trend-chart')._fullLayout?.xaxis.type==='linear' && document.getElementById('trend-profit-chart')._fullLayout?.xaxis.type==='linear'")
                assert_monthly_quarter_alignment(page)
                page.close()
        finally:browser.close()


def test_weekly_forecast_without_overlapping_actual_profits(live,monkeypatch):
    base,db,state,web=live;seed(db,monkeypatch)
    batch=begin_batch(db,'weekly-official-fixture','shipping.fixture','sse',domain='research_shipping')
    source=archive_source(db,batch,'sse','shipping.fixture',dict(transport='fixture'),1,raw=b'official weekly fixture')
    forecast.publish_rates(db,[dict(index_name='CCFI',frequency='weekly',route='综合',statistic='release',
        period_start=day,period_end=day,value=1500,unit='点',quality='official',channel='sse',published_on=day)
        for day in ('2026-10-16','2026-10-23')],batch,source)
    from playwright.sync_api import sync_playwright,expect
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        try:
            for width in (1440,390):
                page=browser.new_page(viewport=dict(width=width,height=1000))
                page.goto(base+'/?code=601919&tab=shipping-forecast&view=trend')
                page.wait_for_function("document.getElementById('trend-coverage').textContent.length>0")
                page.select_option('#trend-frequency','weekly')
                page.wait_for_function("document.getElementById('trend-chart')._fullLayout?.xaxis.type==='date'")
                with page.expect_response('**/api/shipping-trend?**') as response:
                    page.select_option('#trend-profit','quarter')
                assert response.value.json()['profit']==[]
                page.wait_for_function("document.getElementById('trend-profit-chart').data?.some(t=>t.meta?.forecast)")
                assert page.locator('#trend-profit-chart').evaluate("""e=>{const s=e.layout.shapes.find(s=>s.name==='neutral-forecast'),a=e._fullLayout;return s.line.dash==='dash'&&s.y1>0&&a.yaxis.range[1]>=s.y1&&Date.parse(a.xaxis.range[0])<=Date.parse(s.x0)&&Date.parse(a.xaxis.range[1])>=Date.parse(s.x1)&&e.data[0].x.length===0;}""")
                bar=page.locator('#trend-profit-chart .barlayer .trace').last.locator('.point path')
                bar.scroll_into_view_if_needed();box=bar.bounding_box();assert box and box['width']>0 and box['height']>0
                page.mouse.move(box['x']+box['width']/2,box['y']+box['height']/2)
                expect(page.locator('#trend-hover-card')).to_contain_text('中性预测 2026Q3')
                expect(page.locator('#trend-hover-card')).to_contain_text('亿元')
                page.close()
        finally:browser.close()


def test_scenario_profit_comparisons_and_edge_baselines(live,monkeypatch):
    base,db,state,web=live;seed(db,monkeypatch)
    from playwright.sync_api import sync_playwright,expect
    session=requests.Session();session.trust_env=False
    def fmt(value):return f'{value:,.2f}'.rstrip('0').rstrip('.')
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        try:
            for target,bases in [('2026Q3',(40,20)),('2026Q1',(40,20)),('2026Q3',(0,None)),('2026Q3',(-40,-20))]:
                payload=session.get(base+'/api/shipping-forecast',params={'target':target},timeout=5).json()
                year=int(target[:4]);quarter=int(target[-1]);labels=[f'{year-1}Q{quarter}',f'{year}Q{quarter-1}' if quarter>1 else f'{year-1}Q4']
                for label,value in zip(labels,bases):
                    row=next(r for r in payload['context']['history'] if r['label']==label)
                    row['profit']=value
                page=browser.new_page(viewport=dict(width=390,height=1000))
                page.route(base+'/api/shipping-forecast?**',lambda route:route.fulfill(json=payload))
                page.goto(base+'/?code=601919&tab=shipping-forecast')
                expect(page.locator('.scenario')).to_have_count(3)
                def check(key,extra=0):
                    params=deepcopy(payload['defaults'][key]);params['extra']+=extra
                    profit=forecast.calculate(payload['context'],params)['profit']
                    rows=page.locator('#comparison-'+key+' .profit-comparison-row')
                    assert rows.count()==2
                    for index,(name,baseline) in enumerate(zip(('同比','环比'),bases)):
                        row=rows.nth(index);text=row.inner_text();rate=row.locator('.profit-change')
                        assert name+'：' in text and '比较期' in text and 'Q' not in text
                        if baseline is None:
                            assert '无法计算' in text and '未记录' in text
                            assert row.get_attribute('title')=='比较期数据缺失'
                        elif baseline==0:
                            assert '无法计算' in text and '比较期 0 亿元' in text
                            assert row.get_attribute('title')=='比较期为零，无法计算'
                        else:
                            change=(profit-baseline)/abs(baseline)*100
                            assert rate.inner_text()==fmt(abs(change))+'%'
                            assert '比较期 '+fmt(baseline)+' 亿元' in text
                            direction='up' if change>0 else 'down' if change<0 else 'flat'
                            assert direction in rate.get_attribute('class').split()
                            expected_color={'up':'rgb(192, 57, 43)','down':'rgb(22, 128, 60)','flat':'rgb(82, 107, 120)'}[direction]
                            assert rate.evaluate('e=>getComputedStyle(e).color')==expected_color
                            if baseline<0:assert row.get_attribute('title')=='基期亏损，按绝对值计算'
                    assert rows.nth(1).bounding_box()['y']>rows.nth(0).bounding_box()['y']
                for key in ('bear','base','bull'):check(key)
                page.locator('[data-case="base"][data-key="extra"]').fill(str(payload['defaults']['base']['extra']+10))
                check('base',10)
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                page.close()
        finally:browser.close()
