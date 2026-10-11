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
            assert page.locator('#metric-tabs button').all_text_contents()==['PE(TTM)','PB']
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
            assert page.locator('#metric-tabs button').all_text_contents()==['PE(TTM)','PB','股息率']
            assert not page.locator('#series-control').is_visible()
            assert page.locator('#industry-source').is_visible()
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
