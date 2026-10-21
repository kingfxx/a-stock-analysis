"""Read-only browser and HTTP acceptance on an isolated ephemeral loopback port."""
import json
from http.server import ThreadingHTTPServer
from threading import Thread

import pytest
import requests

from quarterly_dashboard import server
from quarterly_dashboard.collection import encoded
from quarterly_dashboard.index_repository import publish
from test_index_collection import db, source


@pytest.fixture
def index_server(db,monkeypatch):
    batch,sid,row=source(db)
    with db.connection(write=True) as conn:publish(conn,[row])
    monkeypatch.setattr(server,'DATABASE_PATH',db.path)
    calls=[]
    def runner(database,operation):
        calls.append(operation)
    monkeypatch.setattr(server,'run_operation',runner)
    # Windows ephemeral allocation can choose Chromium-blocked ports such as 6667.
    for port in (8766,8768,*range(18760,18780)):
        try:
            http=ThreadingHTTPServer(('127.0.0.1',port),server.Handler)
            break
        except OSError:
            continue
    else:
        raise RuntimeError('No isolated test port available')
    worker=Thread(target=http.serve_forever,daemon=True)
    worker.start()
    try:yield f'http://127.0.0.1:{http.server_port}',calls,db
    finally:
        http.shutdown();http.server_close();worker.join()
        if hasattr(http,'index_reader'):http.index_reader.close()


def test_index_http_read_collect_cancel_and_cross_origin(index_server):
    base,calls,db=index_server
    session=requests.Session();session.trust_env=False
    assert session.get(base+'/indices',timeout=5).status_code==200
    overview=session.get(base+'/api/indices',timeout=5).json()
    assert overview['catalog'][0]['index_code']=='cn:000300'
    assert len(overview['coverage'])==1 and not calls
    navigation=session.get(base+'/api/indices/catalog',timeout=5).json()
    assert navigation['catalog'][0]['name']=='沪深300' and 'batches' not in navigation
    status=session.get(base+'/api/indices/status',timeout=5).json()
    assert status['revision']==navigation['revision']
    compact=session.get(base+'/api/indices/chart?code=cn:000300&provider=csindex&series=history.rolling&metric=pe_rolling',timeout=5).json()
    assert compact['views']['pe_rolling']['10']['current']==13.1
    assert 'rows' not in compact and compact['revision']==navigation['revision']
    assert session.get(base+'/api/indices/chart?metric=wrong',timeout=5).status_code==400
    response=session.get(base+'/api/indices?code=cn:000300',timeout=5)
    assert response.status_code==400
    history=session.get(base+'/api/indices?code=cn:000300&provider=csindex&series=history.rolling',timeout=5).json()
    assert history['rows'][0]['pe_rolling']==13.1
    raw=session.get(base+'/api/indices/source?id='+str(history['rows'][0]['source_id']),timeout=5).json()
    assert raw['metadata']['raw_sha256'] and raw['body']=='{"data":[]}'
    denied=session.post(base+'/api/indices/collect',json={'mode':'full'},headers={'Origin':'https://example.test'},timeout=5)
    assert denied.status_code==400 and not calls
    created=session.post(base+'/api/indices/collect',json={'mode':'incremental','providers':['csindex']},timeout=5)
    assert created.status_code==200
    assert session.post(base+'/api/indices/collect',json={'mode':'full'},timeout=5).status_code==400
    assert session.post(base+'/api/indices/cancel',json={},timeout=5).json()['cancel_requested']
    with db.connection() as conn:
        operation=conn.execute('SELECT * FROM index_collection_operations').fetchone()
        assert operation['cancel_requested']==1
        assert json.loads(operation['scope_json'])['providers']==['csindex']


def test_index_point_overlay_http_reports_selected_real_series(index_server):
    from quarterly_dashboard.index_prices import publish_prices
    from test_index_prices import point as index_point,seed as point_seed
    base,_,db=index_server
    batch,sid,code=point_seed(db)
    with db.connection(write=True) as conn:
        publish_prices(conn,[index_point(batch,sid,code,observation_date='2026-10-08',close=4550),
                             index_point(batch,sid,code,observation_date='2026-10-09',close=4600)])
    response=requests.get(base+'/api/indices/price?code=cn:000300',timeout=5)
    assert response.ok
    body=response.json()
    assert body['available'] and body['provider']=='csindex' and body['frequency']=='daily'
    assert [(row['observation_date'],row['close']) for row in body['rows']]==[
        ('2026-10-08',4550),('2026-10-09',4600)]
    missing=requests.get(base+'/api/indices/price?code=sw:missing',timeout=5).json()
    assert not missing['available'] and missing['rows']==[]


def test_browser_cooldown_time_is_beijing_to_seconds(index_server):
    playwright=pytest.importorskip('playwright.sync_api')
    base,calls,db=index_server
    with db.connection(write=True) as conn:
        conn.execute('INSERT INTO collection_provider_state VALUES(?,?,?,?,?,?)',
            ('legu','2026-10-17T07:29:57.867636+00:00','HTTP 429',1,'{}','2026-10-17T06:29:57+00:00'))
    with playwright.sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        try:
            page=browser.new_page(timezone_id='America/Los_Angeles')
            errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(base+'/indices',wait_until='networkidle')
            assert page.locator('#cooldowns').inner_text()=='乐咕乐股：HTTP 429，冷却截止（北京时间）2026-10-17 15:29:57'
            assert not errors and not calls
        finally:
            browser.close()


def test_browser_toggles_real_index_points_on_right_axis(index_server):
    playwright=pytest.importorskip('playwright.sync_api')
    from quarterly_dashboard.index_prices import publish_prices
    from test_index_prices import point as index_point,seed as point_seed
    base,_,db=index_server
    batch,sid,code=point_seed(db)
    with db.connection(write=True) as conn:
        publish_prices(conn,[index_point(batch,sid,code,observation_date='2026-10-09',close=4600)])
    with playwright.sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        try:
            page=browser.new_page()
            errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(base+'/indices',wait_until='networkidle')
            page.wait_for_function("document.getElementById('current').textContent==='13.10 倍'")
            button=page.locator('#point-overlay')
            assert button.is_enabled() and button.get_attribute('aria-pressed')=='false'
            button.click()
            page.wait_for_function("document.getElementById('chart').data?.some(t=>t.name==='指数点位')")
            price=page.locator('#chart').evaluate("node=>node.data.find(t=>t.name==='指数点位')")
            assert price['y']==[4600] and price['customdata']==['2026-10-09']
            assert page.locator('#chart').evaluate("node=>node._fullLayout.yaxis2.side")=='right'
            assert '中证指数' in page.locator('#point-overlay-note').inner_text()
            button.click()
            page.wait_for_function("!document.getElementById('chart').data.some(t=>t.name==='指数点位')")
            assert button.get_attribute('aria-pressed')=='false' and not errors
        finally:browser.close()


def test_browser_overlay_follows_index_selection_and_legend_is_named_below_chart(index_server):
    playwright=pytest.importorskip('playwright.sync_api')
    from quarterly_dashboard.collection import finish_batch
    from quarterly_dashboard.index_prices import publish_prices
    from quarterly_dashboard.index_repository import publish
    from test_index_prices import point as index_point,seed as point_seed
    base,_,db=index_server
    batch,sid,code=point_seed(db)
    other_batch,other_sid,other_code=point_seed(db,code='000001')
    with db.connection(write=True) as conn:
        publish_prices(conn,[index_point(batch,sid,code,close=4600),
                             index_point(other_batch,other_sid,other_code,close=3900)])
        publish(conn,[dict(index_code=other_code,provider='csindex',series_key='history.rolling',
            observation_date='2026-10-09',frequency='daily',pe_rolling=21.2,
            batch_id=other_batch,source_id=other_sid)])
    finish_batch(db,other_batch,'complete')
    with playwright.sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        try:
            page=browser.new_page()
            errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(base+'/indices',wait_until='networkidle')
            page.wait_for_function("document.getElementById('current').textContent==='13.10 倍'")
            page.locator('#point-overlay').click()
            page.wait_for_function("document.getElementById('chart').data?.some(t=>t.name==='指数点位')")
            page.locator('#code').select_option(other_code)
            page.wait_for_function("document.getElementById('current').textContent==='21.20 倍' && document.getElementById('chart').data?.some(t=>t.name==='指数点位' && t.y.includes(3900))")
            chart=page.locator('#chart')
            assert page.locator('#point-overlay').get_attribute('aria-pressed')=='true'
            valuation=chart.evaluate("n=>n.data[0].name")
            assert valuation=='市盈率 PE（滚动）'
            legend=chart.evaluate("n=>n._fullLayout.legend")
            assert legend['orientation']=='h' and legend['y']<0 and legend['xanchor']=='center'
            assert all(not name.lower().startswith('trace ') for name in chart.evaluate("n=>n.data.map(t=>t.name)"))
            assert not errors
        finally:browser.close()


def test_browser_explicit_series_filters_sources_and_no_auto_collection(index_server):
    playwright=pytest.importorskip('playwright.sync_api')
    base,calls,db=index_server
    with playwright.sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        try:
            page=browser.new_page(viewport={'width':1280,'height':900})
            errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(base+'/indices?stock=sh601919',wait_until='networkidle')
            page.wait_for_function("document.getElementById('chart').data?.[0]?.y?.length===1")
            assert page.locator('#series').input_value()=='csindex|history.rolling|v1'
            assert page.locator('#metric').input_value()=='pe_rolling'
            assert page.locator('.stock-sidebar').is_visible()
            assert page.locator('[data-view=statements]').get_attribute('href') == '/?tab=statements&code=sh601919'
            assert page.locator('.research-tabs [aria-current="page"]').inner_text() == '指数分析'
            assert page.locator('.observations').count() == 0
            assert page.locator('#batches').count() == 0
            assert page.locator('#current-index-name').inner_text() == '沪深300'
            chart = page.locator('#chart')
            chart.scroll_into_view_if_needed()
            coordinates = chart.evaluate('''node => {
                const point=node.calcdata[0][0], layout=node._fullLayout, box=node.getBoundingClientRect();
                return {x:box.left+layout.xaxis._offset+layout.xaxis.l2p(point.x),
                        y:box.top+layout.yaxis._offset+layout.yaxis.l2p(point.y)+25};
            }''')
            page.mouse.move(coordinates['x'], coordinates['y'])
            page.wait_for_selector('#chart .hoverlayer .legend')
            hover = page.locator('#chart .hoverlayer').text_content()
            assert '13.10' in hover and '实际观察日' in hover and '2026-10-09' in hover
            assert page.locator('#current').inner_text() == '13.10 倍'
            basis = page.locator('#basis').inner_text()
            page.locator('#benchmark').select_option('stddev')
            assert page.locator('#high-label').inner_text() == '上限（均值 +1σ）'
            page.locator('#frequency').select_option('month')
            assert page.locator('#basis').inner_text() == basis
            assert not page.locator('#parent-control').is_visible()
            assert not calls and not errors
            page.locator('#kind').select_option('industry')
            assert page.locator('#code option').count()==0
            page.locator('#kind').select_option('broad')
            page.wait_for_function("document.getElementById('chart').data?.[0]?.y?.[0]===13.1")
            page.locator('#frequency').select_option('week')
            page.wait_for_function("document.getElementById('chart').data?.[0]?.y?.length===1")
            page.set_viewport_size({'width':390,'height':844})
            page.wait_for_function('document.documentElement.scrollWidth<=window.innerWidth')
            assert not errors
        finally:browser.close()


def test_browser_index_sidebar_restores_shared_stock_picker(index_server):
    playwright=pytest.importorskip('playwright.sync_api')
    base,_,database=index_server
    database.ensure_instrument('600000','浦发银行')
    with playwright.sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        try:
            page=browser.new_page(viewport={'width':390,'height':844})
            errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(base+'/indices',wait_until='networkidle')
            picker=page.locator('#cached-picker')
            assert picker.count()==1
            page.wait_for_function("document.getElementById('stock-picker-summary').textContent.includes('最近查看')")
            picker.locator('summary').click()
            assert page.locator('#cached-search').is_visible()
            assert page.locator('#stock-groups').inner_text().find('全部股票')>=0
            page.locator('#stock-groups .stock-group-row button').nth(1).click()
            assert '浦发银行' in page.locator('#cached-list').inner_text()
            assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
            assert not errors
        finally:browser.close()


def test_index_views_match_stock_rules_and_preserve_raw_values():
    from quarterly_dashboard.index_repository import index_valuation_views
    from quarterly_dashboard.valuation import valuation_summary_observations
    rows = [dict(observation_date=f"2026-0{month}-09", frequency='daily',
                 pe_rolling=value, dividend_yield=0) for month, value in [(1, -2), (2, 10), (3, 20), (4, 30)]]
    views = index_valuation_views(rows)
    expected = valuation_summary_observations([
        dict(date=r['observation_date'], pe=r['pe_rolling'] if r['pe_rolling']>0 else None,
             pe_raw=r['pe_rolling']) for r in rows], 'pe', 10, {})
    assert views['pe_rolling']['10'] == expected
    assert expected['count'] == 3 and expected['current'] == 30
    assert expected['negative_pe_ranges'] == [{'start':'2026-01-09','end':'2026-01-09'}]
    assert views['dividend_yield']['10']['count'] == 4
    assert views['dividend_yield']['10']['current'] == 0
    monthly = index_valuation_views([{**r, 'frequency':'monthly'} for r in rows])
    assert monthly['pe_rolling']['10']['rows_by_frequency']['day'] == []
    assert monthly['pe_rolling']['10']['rows_by_frequency']['week'] == []


def test_browser_industry_primary_and_secondary_selection(index_server):
    from quarterly_dashboard.index_repository import catalog
    base, calls, database = index_server
    with database.connection(write=True) as conn:
        for code, name, level, parent in [('sw:1','一级行业',1,None), ('sw:2','对应二级',2,'sw:1'), ('sw:3','待核对二级',2,None), ('sw:4','另一一级',1,None), ('sw:5','另一二级',2,'sw:4')]:
            catalog(conn,code,name,'industry',provider='sws',provider_code=code,level=level,parent=parent,system='sw2021')
    playwright=pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        try:
            page=browser.new_page()
            page.goto(base+'/indices',wait_until='networkidle')
            page.locator('#kind').select_option('industry')
            assert page.locator('#parent-control').is_visible()
            assert page.locator('#second-control').is_visible()
            assert not page.locator('#code-control').is_visible()
            page.locator('#parent').select_option('sw:1')
            assert page.locator('#industry-second').input_value()==''
            assert page.locator('#code').input_value()=='sw:1'
            assert page.locator('#current-index-name').inner_text()=='一级行业'
            assert page.locator('#industry-second option').count()==2
            page.locator('#industry-second').select_option('sw:2')
            assert page.locator('#code').input_value()=='sw:2'
            assert page.locator('#current-index-name').inner_text()=='对应二级'
            page.locator('#parent').select_option('sw:4')
            assert page.locator('#industry-second').input_value()==''
            assert page.locator('#code').input_value()=='sw:4'
            assert page.locator('#industry-second option').count()==2
            assert page.locator('#industry-second option').last.get_attribute('value')=='sw:5'
            page.locator('#kind').select_option('broad')
            assert not page.locator('#parent-control').is_visible()
            assert page.locator('#code-control').is_visible()
            assert not calls
        finally:browser.close()


def test_browser_metric_first_defaults_and_filtered_choices(index_server):
    from quarterly_dashboard.index_repository import catalog
    base, calls, database = index_server
    for provider, series, values in [
        ('legu','pb.ordinary',{'pb':1.2}), ('legu','pb.equal',{'pb':1.6}),
        ('legu','pe.ordinary',{'pe_ttm':15,'pe_static':18}),
        ('csindex','dashboard',{'pe_rolling':14,'pe_static':17,'pb':1.4,'dividend_yield':2}),
        ('sws','analysis.daily',{'pe_source':12,'pb':1.3,'dividend_yield':0}),
    ]:
        _, _, row = source(database, provider, series)
        row.pop('pe_rolling')
        row.update(values)
        if provider=='legu': row['frequency']='monthly'
        if provider=='sws':
            with database.connection(write=True) as conn:
                catalog(conn,'sw:1','一级行业','industry',provider='sws',provider_code='1',level=1,system='sw2021')
            row['index_code']='sw:1'
        with database.connection(write=True) as conn: publish(conn,[row])
    playwright=pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        try:
            page=browser.new_page()
            chart_requests=[]
            page.on('request',lambda request:chart_requests.append(request.url) if '/api/indices/chart?' in request.url else None)
            page.goto(base+'/indices',wait_until='networkidle')
            assert page.locator('#metric-tabs button').all_text_contents()==['PE(TTM)','PB','拥挤度','股债利差']
            assert page.locator('#series').input_value()=='csindex|history.rolling|v1'
            assert all('pb.' not in v for v in page.locator('#series option').evaluate_all('(nodes)=>nodes.map(n=>n.value)'))
            page.locator('[data-metric-family="pb"]').click()
            page.wait_for_function("document.getElementById('current').textContent==='1.20 倍'")
            assert page.locator('#series').input_value()=='legu|pb.ordinary|v1'
            choices=page.locator('#series option').evaluate_all('(nodes)=>nodes.map(n=>n.value)')
            assert len(choices)==3 and all('pe.' not in v and 'history.rolling' not in v for v in choices)
            page.locator('#series').select_option('legu|pb.equal|v1')
            page.wait_for_function("document.getElementById('current').textContent==='1.60 倍'")
            before=len(chart_requests)
            page.locator('[data-metric-family="pe"]').click()
            page.wait_for_function("document.getElementById('current').textContent==='13.10 倍'")
            assert len(chart_requests)==before  # PE is served by bounded page cache.
            assert page.locator('#series').input_value()=='csindex|history.rolling|v1'
            page.locator('#kind').select_option('industry')
            page.wait_for_function("document.getElementById('current').textContent==='12.00 倍'")
            assert page.locator('#metric-tabs button').all_text_contents()==['PE(TTM)','PB','股息率','拥挤度']
            assert not page.locator('#series-control').is_visible()
            assert page.locator('#industry-source').count()==0
            assert '尚待核实' in page.locator('#comparison').inner_text()
            assert page.locator('#metric').input_value()=='pe_source'
            page.locator('[data-metric-family="pb"]').click()
            page.wait_for_function("document.getElementById('current').textContent==='1.30 倍'")
            page.locator('[data-metric-family="dividend_yield"]').click()
            page.wait_for_function("document.getElementById('current').textContent==='0.00%'")
            assert page.locator('#series option').count()==1
            assert not calls
        finally:browser.close()


def test_current_taxonomy_mapping_is_unique_and_does_not_change_history(db):
    from quarterly_dashboard.index_repository import catalog, read_indices
    with db.connection(write=True) as conn:
        conn.executemany('INSERT INTO sw_industries(code,name,level,parent_code) VALUES(?,?,?,?)',[
            ('110000','First',1,None), ('110100','Child',2,'110000'),
            ('110200','Ambiguous',2,'110000'), ('110300','Ambiguous',2,'110000')])
        for code,name,level in [('sw:801010','First',1),('sw:801011','Child',2),('sw:801012','Ambiguous',2),('sw:801013','Missing',2)]:
            catalog(conn,code,name,'industry',provider='sws',provider_code=code,level=level,system='sw')
    _,_,row=source(db,'sws','analysis.daily')
    row.pop('pe_rolling');row.update(index_code='sw:801011',pe_source=10)
    with db.connection(write=True) as conn:publish(conn,[row])
    result=read_indices(db,code='sw:801011',provider='sws',series='analysis.daily',level=2)
    child=next(r for r in result['catalog'] if r['index_code']=='sw:801011')
    assert child['navigation_parent_index_code']=='sw:801010'
    assert child['navigation_taxonomy_code']=='110100'
    assert child['navigation_mapping_source']=='sw_industries:SW2021:name+level'
    assert child['parent_index_code'] is None
    assert all('navigation_parent_index_code' not in r for r in result['catalog'] if r['name'] in ('Ambiguous','Missing'))
    assert result['rows'][0]['catalog_as_of'] is None
    with db.connection() as conn:
        assert conn.execute("SELECT parent_index_code FROM index_catalog WHERE index_code='sw:801011'").fetchone()[0] is None


def test_browser_progress_panel_starts_closed_and_polling_preserves_manual_state(index_server):
    playwright=pytest.importorskip('playwright.sync_api')
    from quarterly_dashboard.collection import encoded
    from quarterly_dashboard.index_collection import start_operation
    base,_,db=index_server
    operation=start_operation(db,mode='incremental',providers=['csindex'])
    with db.connection(write=True) as conn:
        conn.execute('UPDATE index_collection_operations SET error=?,result_json=? WHERE operation_id=?',
            (encoded({'csindex':'ValueError: fixture error'}),encoded({'errors':{'csindex':'ValueError: fixture error'}}),operation))
    with playwright.sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        try:
            page=browser.new_page();errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(base+'/indices',wait_until='networkidle')
            page.wait_for_function("document.getElementById('current').textContent==='13.10 倍'")
            drag=page.locator('#chart .nsewdrag').first
            drag.scroll_into_view_if_needed();box=drag.bounding_box()
            page.mouse.move(box['x']+box['width']*.2,box['y']+box['height']*.2);page.mouse.down()
            page.mouse.move(box['x']+box['width']*.7,box['y']+box['height']*.8,steps=8);page.mouse.up()
            zoom=page.locator('#chart').evaluate('n=>[n._fullLayout.xaxis.range,n._fullLayout.yaxis.range]')
            page.locator('#chart').evaluate("n=>n.on('plotly_afterplot',()=>window.valuationRedraws=(window.valuationRedraws||0)+1)")
            toggle=page.locator('#progress-toggle');details=page.locator('#progress-details')
            assert toggle.inner_text()=='查看进度'
            assert toggle.get_attribute('aria-expanded')=='false' and details.is_hidden()
            toggle.click()
            assert toggle.inner_text()=='收起进度'
            assert toggle.get_attribute('aria-expanded')=='true' and not details.is_hidden()
            assert '该历史任务未记录总进度' in page.locator('#progress-summary').inner_text()
            assert 'ValueError' not in page.locator('#progress-details').inner_text()
            with db.connection(write=True) as conn:
                conn.execute("UPDATE index_read_revisions SET revision=revision+1 WHERE scope='indices'")
                conn.execute('UPDATE index_collection_operations SET status=?,error=NULL,result_json=? WHERE operation_id=?',
                    ('running',encoded({'work_progress':{'phase':'running','total':4,'success':1,'skipped':1,'failed':0,
                        'cancelled':0,'pending':1,'running':1,'providers':{'csindex':{'total':4,'success':1,'skipped':1,
                            'failed':0,'cancelled':0,'pending':1,'running':1,'current':'中证指数历史 000300'}}}}),operation))
            page.evaluate("window.dispatchEvent(new Event('focus'))")
            page.wait_for_function("document.getElementById('progress-summary').textContent.includes('已采集成功 1 项')")
            assert not details.is_hidden() and toggle.get_attribute('aria-expanded')=='true'
            assert '中证指数' in page.locator('#progress-sources').inner_text()
            assert page.evaluate('window.valuationRedraws||0')==0
            toggle.click()
            page.evaluate("window.dispatchEvent(new Event('focus'))")
            page.wait_for_timeout(200)
            assert details.is_hidden() and toggle.get_attribute('aria-expanded')=='false'
            assert 'cancelled' not in page.locator('#progress').inner_text().lower()
            with db.connection(write=True) as conn:
                conn.execute("UPDATE index_collection_operations SET status='complete' WHERE operation_id=?",(operation,))
            page.evaluate("window.dispatchEvent(new Event('focus'))")
            page.wait_for_function("window.valuationRedraws>0 && document.getElementById('current').textContent==='13.10 倍'")
            assert page.locator('#chart').evaluate('n=>[n._fullLayout.xaxis.range,n._fullLayout.yaxis.range]')==zoom
            assert not errors
        finally:browser.close()
