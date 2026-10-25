"""Same-date spread calculation and real browser filters on isolated data."""
import json

import pytest
import requests

from stock_analysis.domains.indices.index_read_service import IndexReadService
from stock_analysis.domains.indices.index_repository import publish
from test_index_prices import seed, point
from stock_analysis.domains.indices.index_prices import publish_prices
from test_index_ui import index_server, db


def populate(database):
    batch, sid, code = seed(database)
    days = ['2026-10-05','2026-10-06','2026-10-07','2026-10-08','2026-10-09']
    with database.connection(write=True) as conn:
        publish(conn,[dict(index_code=code,provider='csindex',series_key='history.rolling',
            observation_date=day,frequency='daily',pe_rolling=pe,batch_id=batch,source_id=sid)
            for day,pe in zip(days,[20,50,100,0,-10])])
        for day,yield_value in zip(days,['5',None,'2','0','-1']):
            conn.execute('INSERT INTO sovereign_yield_observations '
                '(observation_date,definition_version,china_10y,content_hash,version,is_latest,batch_id,source_id,updated_at) '
                "VALUES(?,'v1',?,?,1,1,?,?,?)",(day,yield_value,'a'*64,batch,sid,'2026-10-20T00:00:00Z'))
    return code


def test_same_day_missing_zero_negative_and_bond_revision(db):
    code=populate(db)
    reader=IndexReadService(db)
    try:
        result=json.loads(reader.equity_bond_spread(code))
        assert [r['spread'] for r in result['rows']]==[0,None,-1,None,None]
        assert result['observations']==2 and result['last_date']=='2026-10-07'
        with pytest.raises(ValueError):reader.equity_bond_spread('sw:missing')
        with db.connection(write=True) as conn:
            conn.execute("UPDATE sovereign_yield_observations SET china_10y='0',updated_at='2026-10-20T01:00:00Z' WHERE observation_date='2026-10-06'")
        updated=json.loads(reader.equity_bond_spread(code))
        assert updated['revision']!=result['revision']
        assert updated['rows'][1]['spread']==2
    finally:reader.close()


def test_spread_http_and_browser_filters_statistics(index_server):
    base,calls,database=index_server
    populate(database)
    batch,sid,other=seed(database,code='000001')
    with database.connection(write=True) as conn:
        publish(conn,[dict(index_code=other,provider='csindex',series_key='dashboard',frequency='daily',
            observation_date='2026-10-09',pe_rolling=10,batch_id=batch,source_id=sid)])
    with database.connection(write=True) as conn:
        publish_prices(conn,[point(batch,sid,'cn:000300',observation_date='2026-10-07',close=100),
                             point(batch,sid,'cn:000300',observation_date='2026-10-09',close=110)])
    response=requests.get(base+'/api/indices/equity-bond-spread?code=cn:000300',timeout=5)
    assert response.ok and response.json()['observations']==2
    assert requests.get(base+'/api/indices/equity-bond-spread?code='+other,timeout=5).status_code==400
    playwright=pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        try:
            page=browser.new_page();errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(base+'/indices',wait_until='networkidle')
            page.locator('[data-metric-family=equity_bond_spread]').click()
            page.wait_for_function("document.getElementById('current').textContent==='-1.00%'")
            assert page.locator('#code option').count()==1
            assert not page.locator('#series-control').is_visible()
            assert '0.0%' in page.locator('#basis').inner_text()
            basis=page.locator('#basis').inner_text()
            page.locator('#frequency').select_option('day')
            assert page.locator('#chart').evaluate('n=>n.data[0].y')==[0,None,-1,None,None]
            assert page.locator('#chart').evaluate('n=>n.layout.hovermode')=='x unified'
            page.locator('#point-overlay').click()
            page.wait_for_function("document.getElementById('chart').data?.length===2")
            assert page.locator('#chart').evaluate('n=>n.data[1].y')==[None,None,100,None,110]
            assert page.locator('#chart').evaluate('n=>n.data[1].yaxis')=='y2'
            page.locator('#frequency').select_option('month')
            assert page.locator('#chart').evaluate('n=>n.data[0].y')==[None]
            page.locator('#benchmark').select_option('stddev')
            assert page.locator('#basis').inner_text()==basis
            page.locator('[data-metric-family=crowding]').click()
            assert page.locator('#code option').count()==2
            page.locator('[data-metric-family=equity_bond_spread]').click()
            page.locator('#kind').select_option('industry')
            assert page.locator('[data-metric-family=equity_bond_spread]').count()==0
            page.locator('#kind').select_option('broad')
            assert page.locator('#code option').count()==2
            assert not errors and not calls
        finally:browser.close()
