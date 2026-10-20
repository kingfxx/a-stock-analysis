"""Crowding reads existing daily amount ratios and uses the actual page scripts."""
import json
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import pytest

from quarterly_dashboard import server, market_congestion
from quarterly_dashboard.index_prices import publish_prices, read_overlay
from quarterly_dashboard.index_read_service import IndexReadService
from quarterly_dashboard.index_repository import publish
from test_index_prices import db, seed, point


def populate(db):
    batch, sid, code = seed(db)
    other_batch, other_sid, industry = seed(db, provider='sws', code='801010', kind='industry')
    with db.connection(write=True) as conn:
        publish_prices(conn, [point(batch,sid,code,observation_date=day,close=100+i)
            for i,(day,value) in enumerate([('2026-09-28',0),('2026-09-29',10),('2026-09-30',None),('2026-10-09',20)])])
        conn.execute("UPDATE index_price_observations SET amount_ratio=CASE close WHEN 100 THEN 0 WHEN 101 THEN 10 WHEN 103 THEN 20 END")
        publish_prices(conn,[point(other_batch,other_sid,industry,provider='sws',key='analysis.points',amount_ratio=4.82)])
        publish(conn,[dict(index_code=code,provider='csindex',series_key='history.rolling',observation_date='2026-10-09',frequency='daily',pe_rolling=13.1,batch_id=batch,source_id=sid)])
    missing_batch,missing_sid,missing_code=seed(db,code='000001')
    with db.connection(write=True) as conn:
        publish_prices(conn,[point(missing_batch,missing_sid,missing_code)])
    return code,industry


def test_crowding_daily_series_nulls_zero_and_cache_revision(db):
    code,industry=populate(db)
    reader=IndexReadService(db)
    try:
        result=json.loads(reader.price_overlay(code,field='amount_ratio'))
        assert result['observations']==3
        assert [r['amount_ratio'] for r in result['rows']]==[0,10,None,20]
        assert json.loads(reader.price_overlay(industry,field='amount_ratio'))['rows'][0]['amount_ratio']==4.82
        assert not read_overlay(db,'missing',field='amount_ratio')['available']
        assert 'amount_ratio' not in json.loads(reader.price_overlay(code))['rows'][0]
        batch,sid,_=seed(db)
        with db.connection(write=True) as conn:
            conn.execute("UPDATE index_price_observations SET amount_ratio=30 WHERE close=103")
        updated=json.loads(reader.price_overlay(code,field='amount_ratio'))
        assert updated['revision']!=result['revision'] and updated['rows'][-1]['amount_ratio']==30
    finally:
        reader.close()


def test_browser_crowding_switch_statistics_gaps_and_valuation(db):
    playwright=pytest.importorskip('playwright.sync_api')
    import plotly
    code,industry=populate(db)
    reader=IndexReadService(db)
    root=Path(__file__).resolve().parents[1]
    calls=[]
    def route(request):
        url=urlparse(request.request.url);path=url.path;query=parse_qs(url.query)
        calls.append(path)
        if path=='/indices':
            body=(root/'web/indices.html').read_text(encoding='utf-8').replace('__RESEARCH_SIDEBAR__',server.research_sidebar('indices'))
            request.fulfill(body=body,content_type='text/html');return
        if path=='/api/indices/catalog':body=reader.catalog()
        elif path=='/api/indices/status':body=reader.status()
        elif path=='/api/indices/crowding':body=reader.price_overlay(query['code'][0],field='amount_ratio')
        elif path=='/api/indices/price':body=reader.price_overlay(query['code'][0])
        elif path=='/api/indices/chart':body=reader.chart(code=query['code'][0],provider=query['provider'][0],series=query['series'][0],definition=query['definition'][0],metric=query['metric'][0])
        elif path=='/api/indices/congestion':body=json.dumps(market_congestion.read(db))
        elif path.startswith('/api/'):body='{}'
        else:
            file=Path(plotly.__file__).parent/'package_data/plotly.min.js' if path=='/plotly.min.js' else root/'web'/path.lstrip('/')
            request.fulfill(path=str(file),content_type='text/javascript' if path.endswith('.js') else 'text/css');return
        request.fulfill(body=body,content_type='application/json')
    try:
        with playwright.sync_playwright() as p:
            browser=p.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page();errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.route('https://indices.test/**',route)
                page.goto('https://indices.test/indices',wait_until='networkidle')
                page.wait_for_function("document.getElementById('current').textContent==='13.10 倍'")
                page.locator('[data-metric-family=crowding]').click()
                page.wait_for_function("document.getElementById('current').textContent==='20.00%'")
                assert '66.7%' in page.locator('#basis').inner_text()
                basis=page.locator('#basis').inner_text()
                page.locator('#frequency').select_option('day')
                assert page.locator('#chart').evaluate('n=>n.data[0].y')==[0,10,None,20]
                page.locator('#frequency').select_option('month')
                assert page.locator('#basis').inner_text()==basis
                assert page.locator('#chart').evaluate('n=>n.data[0].y')==[None,20]
                page.locator('#benchmark').select_option('stddev')
                assert page.locator('#basis').inner_text()==basis
                page.locator('#point-overlay').click()
                assert page.locator('#chart').evaluate("n=>n.data[1].yaxis")=='y2'
                page.locator('#kind').select_option('industry')
                page.wait_for_function("document.getElementById('current').textContent==='4.82%'")
                assert not page.locator('#series-control').is_visible()
                page.locator('#kind').select_option('broad')
                page.wait_for_function("document.getElementById('current').textContent==='20.00%'")
                page.locator('#code').select_option('cn:000001')
                page.wait_for_function("document.getElementById('coverage').textContent==='该指数暂无本地成交额占比数据。'")
                assert page.locator('#current').inner_text()=='—'
                assert page.locator('#chart').evaluate('n=>n.data[0].y')==[]
                page.locator('#code').select_option(code)
                page.wait_for_function("document.getElementById('current').textContent==='20.00%'")
                page.locator('[data-metric-family=pe]').click()
                page.wait_for_function("document.getElementById('current').textContent==='13.10 倍'")
                assert not errors
                assert not any('/collect' in path for path in calls)
            finally:browser.close()
    finally:reader.close()
