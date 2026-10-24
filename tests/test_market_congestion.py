import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
import requests

from quarterly_dashboard import market_congestion as m
from quarterly_dashboard.collection import (RequestCollector, PacingPolicy, archive_source,
    begin_batch, finish_batch, parsed_source, read_source)
from quarterly_dashboard.maintenance import MaintenanceService
from quarterly_dashboard.storage import Database


@pytest.fixture
def db(tmp_path_factory):
    database=Database(tmp_path_factory.mktemp('cg')/'congestion.sqlite3');database.initialize()
    return database


def html(day='2026-10-16',percent='41.66',close='3716.28',top='6536.08',total='15690.66'):
    return f'''<html><head><meta name="_csrf" content="token"></head><body>
    <table id="tableID"><thead><tr><th>日期</th><th>收盘价</th><th>成交额排名前5%的个股的成交额(亿元)</th><th>全部A股成交额(亿元)</th><th>拥挤度(%)</th></tr></thead>
    <tbody><tr><td>{day}</td><td>{close}</td><td>{top}</td><td>{total}</td><td>{percent}</td></tr></tbody></table>
    <script type="application/ld+json">{{"variableMeasured":[{{"description":"{day}，大盘拥挤度：{percent}%"}}]}}</script></body></html>'''


def payload():
    return dict(items=[dict(date='2026-07-30',congestion=0.5,close=3800,top5AmountYi=10,totalAmountYi=20),dict(date='2026-07-31')])


def seed(db,channel='api_history',raw=None):
    dataset=m.DATASETS[channel]
    batch=begin_batch(db,'seed',dataset,'legu',domain='market')
    raw=raw if raw is not None else json.dumps(payload()).encode() if channel=='api_history' else html().encode()
    sid=archive_source(db,batch,'legu',dataset,dict(url=m.API if channel=='api_history' else m.PAGE),1,raw=raw,obtained_at='2026-10-17T07:36:09+00:00')
    parsed_source(db,sid,fields=['date','value'])
    return batch,sid


def ready(db):
    for channel,rows in [('api_history',m.parse_api(payload())),('html_recent_table',m.parse_html(html()))]:
        batch,sid=seed(db,channel)
        with db.connection(write=True) as conn:m.publish(conn,rows,channel,batch,sid)
        finish_batch(db,batch,'complete','unknown')


def test_parsers_units_missing_zero_columns_and_summary():
    rows=m.parse_api(payload())
    assert rows[-1]['value_status']=='source_missing'
    assert all(rows[-1][field] is None for field in m.VALUE_FIELDS)
    row=m.parse_html(html())[0]
    assert [row[k] for k in m.VALUE_FIELDS]==[.4166,3716.28,6536.08,15690.66]
    assert m.parse_html(html(percent='0',top='0'))[0]['value']==0
    # Header mapping rather than positional assumptions.
    page=html().replace('<th>收盘价</th>','<th>额外字段</th><th>收盘价</th>').replace('<td>3716.28</td>','<td>x</td><td>3716.28</td>')
    assert m.parse_html(page)[0]['close']==3716.28
    with pytest.raises(ValueError,match='矛盾'):m.parse_html(html().replace('41.66%','42.66%'))
    with pytest.raises(ValueError):m.parse_html(html().replace('亿元','元'))
    with pytest.raises(ValueError):m.parse_html(html(percent='NaN'))
    with pytest.raises(ValueError):m.parse_api({'items':payload()['items']*2})
    with pytest.raises(ValueError):m.parse_api({'items':[{'date':'2026-01-01'}]})


def test_revision_zero_missing_and_source_pair(db):
    batch,sid=seed(db,'html_recent_table');row=m.parse_html(html())[0]
    with db.connection(write=True) as conn:
        assert m.publish(conn,[row],'html_recent_table',batch,sid)['inserted']==1
        assert m.publish(conn,[row],'html_recent_table',batch,sid)['unchanged']==1
        assert m.publish(conn,[dict(row,close=3717)],'html_recent_table',batch,sid)['revised']==1
        assert m.publish(conn,[dict(row,close=None)],'html_recent_table',batch,sid)['withheld_dates']==['2026-10-16']
        assert conn.execute('SELECT count(*) FROM market_indicator_observations').fetchone()[0]==2
        assert conn.execute('SELECT close FROM market_indicator_observations WHERE is_latest=1').fetchone()[0]==3717
    other,wrong=seed(db)
    with pytest.raises(ValueError):
        with db.connection(write=True) as conn:m.publish(conn,[row],'html_recent_table',other,wrong)
    with pytest.raises(sqlite3.IntegrityError):
        with db.connection(write=True) as conn:
            conn.execute('INSERT INTO market_indicator_observations SELECT id+100,indicator_key,scope_key,provider,channel,frequency,definition_version,observation_date,value,close,top5_amount_yi,total_amount_yi,value_status,precision_digits,version,is_latest,content_hash,source_id,batch_id,obtained_at,updated_at,withdrawal_reason FROM market_indicator_observations WHERE is_latest=1')


class Clock:
    def __init__(self):self.now=datetime(2026,10,17,18,tzinfo=m.ZONE).timestamp()
    def time(self):return self.now
    def sleep(self,value):self.now+=value


class Session:
    def __init__(self,text,status=200):self.text=text;self.status=status;self.calls=[]
    def get(self,url,**kwargs):
        self.calls.append(url)
        r=requests.Response();r.status_code=self.status;r.encoding='utf-8'
        r.headers['Content-Type']='text/html';r._content=self.text.encode()
        return r
    def close(self):pass


def client(db,session):
    clock=Clock()
    return RequestCollector(db,session=session,policy=PacingPolicy(max_attempts=1),clock=clock.time,sleep=clock.sleep)


def test_archive_initialization_resume_and_metadata_only_updates(db):
    seed(db);seed(db,'html_recent_table')
    session=Session(html())
    result=m.run(db,m.start(db,'full'),http=client(db,session))
    assert result['status']=='complete' and session.calls==[]
    assert m.read(db)['initialized']
    paths={p for p in db.path.parent.rglob('*') if p.is_file() and p.suffix!='.lock'}
    result=m.run(db,m.start(db),http=client(db,session))
    assert result['status']=='complete' and session.calls==[m.PAGE]
    with db.connection() as conn:
        last=conn.execute('SELECT * FROM collection_sources ORDER BY id DESC LIMIT 1').fetchone()
        assert last['file_path'] is None and last['raw_sha256'] is None and last['stored_sha256'] is None
        assert last['raw_bytes'] is None and last['stored_bytes'] is None and last['parse_status']=='complete'
        assert conn.execute('SELECT count(*) FROM market_indicator_observations').fetchone()[0]==3
    with pytest.raises(ValueError,match='无响应文件'):read_source(db,last['id'])
    assert {p for p in db.path.parent.rglob('*') if p.is_file() and p.suffix!='.lock'}==paths
    assert m.run(db,m.start(db,'full'),http=client(db,session))['status']=='complete'
    assert len(session.calls)==1


def test_failure_keeps_facts_no_response_files_and_cools_provider(db):
    ready(db)
    session=Session('<html>blocked</html>',429)
    result=m.run(db,m.start(db),http=client(db,session))
    assert result['status']=='failed' and len(session.calls)==1
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM market_indicator_observations').fetchone()[0]==3
        assert conn.execute("SELECT cooldown_until FROM collection_provider_state WHERE source_system='legu'").fetchone()[0]
        last=conn.execute('SELECT * FROM collection_sources ORDER BY id DESC LIMIT 1').fetchone()
        assert last['file_path'] is None and last['parse_status']=='failed'


def test_parse_failure_is_atomic_and_distinct_from_refusal(db):
    ready(db);bad=Session(html().replace('41.66%','42.66%'))
    assert m.run(db,m.start(db),http=client(db,bad))['status']=='failed'
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM market_indicator_observations').fetchone()[0]==3
        assert conn.execute("SELECT cooldown_until FROM collection_provider_state WHERE source_system='legu'").fetchone()[0] is None


def test_read_missing_dates_not_confused_with_channel_nulls(db):
    ready(db)
    result=m.read(db)
    assert result['valid_dates']==2 and result['missing_dates']==['2026-07-31']
    assert result['latest']['value']==.4166 and result['latest']['close']==3716.28
    assert not result['overlay']['available']
    with pytest.raises(ValueError):m.read(db,index_code='sw:801010')


def test_daily_schedule_restart_busy_disabled_and_cooldown(db,monkeypatch):
    ready(db)
    monkeypatch.setattr(m,'utc_now',lambda:'2026-10-17T09:00:00+00:00')
    m.configure(db,{'enabled':True})
    now=datetime(2026,10,17,18,30,tzinfo=m.ZONE);calls=[]
    def execute(database,operation):
        calls.append(operation)
        with database.connection(write=True) as conn:conn.execute("UPDATE index_collection_operations SET status='complete' WHERE operation_id=?",(operation,))
    assert m.scheduled_tick(db,now=now-timedelta(hours=1),execute=execute) is None
    m.scheduled_tick(db,now=now,execute=execute)
    m.scheduled_tick(Database(db.path),now=now,execute=execute)
    assert len(calls)==1
    op=m.start(db)
    assert m.scheduled_tick(db,now=now+timedelta(days=1),execute=execute) is None
    with db.connection(write=True) as conn:
        conn.execute("UPDATE index_collection_operations SET status='cancelled' WHERE operation_id=?",(op,))
        conn.execute('INSERT INTO collection_provider_state(source_system,cooldown_until,updated_at) VALUES(?,?,?)',('legu',(now+timedelta(days=2)).isoformat(),m.utc_now()))
    assert m.scheduled_tick(db,now=now+timedelta(days=1),execute=execute)['skipped']
    assert len(calls)==1
    m.configure(db,{'enabled':False})
    assert m.scheduled_tick(db,now=now+timedelta(days=3),execute=execute) is None


def test_index_dependency_respects_pre_and_post_threshold_deduplication(db,monkeypatch):
    from quarterly_dashboard import collection
    from quarterly_dashboard.index_collection import start_operation
    ready(db)
    current={'now':datetime(2026,10,17,17,59,tzinfo=m.ZONE)}
    monkeypatch.setattr(m,'local_now',lambda:current['now'])
    monkeypatch.setattr(collection,'utc_now',lambda:current['now'].astimezone(timezone.utc).isoformat())
    operation=start_operation(db,'incremental')
    sessions=[]
    def collector_factory(database,**kwargs):
        session=Session(html(day='2026-10-16'))
        clock=Clock();clock.now=current['now'].timestamp()
        sessions.append(session)
        return RequestCollector(database,session=session,policy=PacingPolicy(max_attempts=1),
            clock=clock.time,sleep=clock.sleep,cancelled=kwargs.get('cancelled',lambda:False))
    monkeypatch.setattr(m,'RequestCollector',collector_factory)
    def check():
        result=m.prepare_index_dependency(db,operation,'2026-10-17',lambda:False)
        return result
    assert check()['status']=='html_not_updated'
    assert check()['status']=='already_checked'
    current['now']=datetime(2026,10,17,18,5,tzinfo=m.ZONE)
    assert check()['status']=='html_not_updated'
    assert check()['status']=='already_checked'
    assert [len(session.calls) for session in sessions]==[1,1]
    with db.connection() as conn:
        checks=conn.execute("SELECT count(*) FROM collection_batches WHERE dataset=? AND trigger_type='index_dependency' "
            "AND json_extract(scope_json,'$.purpose')='index_derivation_dependency'",(m.DATASETS['html_recent_table'],)).fetchone()[0]
    assert checks==2


def test_config_and_identity_definition_guards(db):
    with pytest.raises(ValueError):m.configure(db,{'enabled':True})
    with pytest.raises(ValueError):m.configure(db,{'local_time':'99:30'})
    with pytest.raises(ValueError):m.start(db,'audit')
    with pytest.raises(ValueError):m.validate_rows([{'observation_date':'2026-10-16'}],('unknown',)*6)


def test_maintenance_time_categories_and_foreign_keys(db):
    # Discovery also covers an empty table before initialization.
    from quarterly_dashboard.maintenance import DESCRIPTIONS,table_time
    with db.connection() as conn:
        for table in ('market_indicator_observations','collection_schedules'):
            columns=[r['name'] for r in conn.execute('PRAGMA table_info('+table+')')]
            stamp,basis=table_time(conn,table,columns)
            assert stamp is None
            assert table in DESCRIPTIONS
        assert conn.execute('PRAGMA foreign_key_check').fetchone() is None
    inventory=MaintenanceService(db).read()
    for table in ('market_indicator_observations','collection_schedules'):
        row=next(r for r in inventory['tables'] if r['name']==table)
        assert row['rows']==0 and row['updated_at'] is None and row['total_bytes']>0
        assert row['category']!='其他' and row['description']!='尚未登记用途'


@pytest.fixture
def live(db,monkeypatch):
    from http.server import ThreadingHTTPServer
    from threading import Thread
    from quarterly_dashboard import server
    from quarterly_dashboard.index_repository import catalog
    ready(db)
    with db.connection(write=True) as conn:
        catalog(conn,'cn:000001','上证指数','broad',provider='csindex',provider_code='000001')
        catalog(conn,'cn:000300','沪深300','broad',provider='csindex',provider_code='000300')
    monkeypatch.setattr(server,'DATABASE_PATH',db.path)
    calls=[];monkeypatch.setattr(server,'run_operation',lambda database,operation:calls.append(operation))
    for port in (8768,*range(18760,18780)):
        try:web=ThreadingHTTPServer(('127.0.0.1',port),server.Handler);break
        except OSError:continue
    else:raise RuntimeError('No free isolated test port')
    worker=Thread(target=web.serve_forever,daemon=True);worker.start()
    try:yield f'http://127.0.0.1:{web.server_port}',db,calls
    finally:
        web.shutdown();web.server_close();worker.join()
        if hasattr(web,'index_reader'):web.index_reader.close()


def test_http_contract_defaults_schedule_and_read_only(live):
    base,database,calls=live
    session=requests.Session();session.trust_env=False
    result=session.get(base+'/api/indices/congestion',timeout=5).json()
    assert result['latest']['value']==.4166 and len(result['broad_indices'])==2
    assert not calls
    assert session.post(base+'/api/indices/congestion/schedule',json={'enabled':True,'local_time':'18:30'},timeout=5).json()['local_time']=='18:30'
    assert session.post(base+'/api/indices/congestion/schedule',json={'enabled':False},headers={'Origin':'https://evil.test'},timeout=5).status_code==400
    assert session.post(base+'/api/indices/collect',json={'datasets':['wrong']},timeout=5).status_code==400
    assert session.post(base+'/api/indices/collect',json={'datasets':['market_congestion'],'mode':'incremental'},timeout=5).ok
    assert len(calls)==1
    assert session.post(base+'/api/indices/cancel',json={},timeout=5).ok


def test_browser_polling_preserves_zoom_and_filter_resets(live):
    playwright=pytest.importorskip('playwright.sync_api')
    from quarterly_dashboard.index_prices import publish_prices
    from test_index_prices import point,seed as price_seed
    base,database,calls=live
    batch,sid,code=price_seed(database,code='000001')
    with database.connection(write=True) as conn:
        publish_prices(conn,[point(batch,sid,code,observation_date='2026-10-15',close=3707.48),
                             point(batch,sid,code,observation_date='2026-10-16',close=3716.28)])
    operation=m.start(database,'incremental')  # Register a task, never run source collection.
    with playwright.sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        try:
            page=browser.new_page()
            page.clock.install()
            page.add_init_script("""window.statusReads=0;const originalFetch=window.fetch;
                window.fetch=async(...args)=>{const response=await originalFetch(...args);
                if(String(args[0]).startsWith('/api/indices/status')){await response.clone().json();window.statusReads++;}
                return response;};""")
            requests=[]
            page.on('request',lambda request:requests.append(request.url))
            page.goto(base+'/indices',wait_until='networkidle')
            page.wait_for_function("document.getElementById('congestion-current').textContent==='41.66%'")
            page.wait_for_function("document.getElementById('congestion-chart')._fullLayout?.yaxis2")
            drag=page.locator('#congestion-chart .nsewdrag').first
            drag.scroll_into_view_if_needed()
            box=drag.bounding_box()
            page.mouse.move(box['x']+box['width']*.25,box['y']+box['height']*.2)
            page.mouse.down()
            page.mouse.move(box['x']+box['width']*.75,box['y']+box['height']*.8,steps=8)
            page.mouse.up()
            page.wait_for_function("document.getElementById('congestion-chart')._fullLayout.xaxis.autorange===false")
            before=page.locator('#congestion-chart').evaluate("n=>[n._fullLayout.xaxis.range,n._fullLayout.yaxis.range,n._fullLayout.yaxis2.range]")
            page.locator('#congestion-chart').evaluate("n=>n.on('plotly_afterplot',()=>window.zoomRefreshes=(window.zoomRefreshes||0)+1)")
            revised=html(percent='42.00',top='6590.08')
            batch,sid=seed(database,'html_recent_table',raw=revised.encode())
            with database.connection(write=True) as conn:
                m.publish(conn,m.parse_html(revised),'html_recent_table',batch,sid)
                conn.execute("UPDATE index_read_revisions SET revision=revision+1 WHERE scope='indices'")
            finish_batch(database,batch,'complete')
            status_before=page.evaluate('window.statusReads')
            history_reads=lambda:len([url for url in requests if any(path in url for path in
                ('/api/indices/congestion','/api/indices/catalog','/api/indices/chart','/api/indices/price'))])
            history_before=history_reads()
            page.clock.fast_forward(11000)
            page.wait_for_function('minimum=>window.statusReads>=minimum',arg=status_before+2)
            # Even a changed data revision must not load history or redraw while running.
            assert history_reads()==history_before
            assert page.evaluate('window.zoomRefreshes||0')==0
            assert page.locator('#congestion-current').text_content()=='41.66%'
            with database.connection(write=True) as conn:
                conn.execute("UPDATE index_collection_operations SET status='complete',finished_at=? WHERE operation_id=?",(m.utc_now(),operation))
            page.clock.fast_forward(11000)
            page.wait_for_function("document.getElementById('congestion-current').textContent==='42.00%' && window.zoomRefreshes>0",timeout=15000)
            after=page.locator('#congestion-chart').evaluate("n=>[n._fullLayout.xaxis.range,n._fullLayout.yaxis.range,n._fullLayout.yaxis2.range]")
            assert after==before
            assert page.evaluate('window.zoomRefreshes')==1
            idle_requests=len(requests)
            page.clock.fast_forward(35000)
            assert len(requests)==idle_requests  # Both blocks stop their timers when idle.
            revision=page.locator('#congestion-chart').evaluate('n=>n.layout.uirevision')
            page.locator('#congestion-frequency').select_option('month')
            assert page.locator('#congestion-chart').evaluate('n=>n.layout.uirevision')!=revision
            assert page.locator('#congestion-chart').evaluate('n=>n._fullLayout.xaxis.range')!=before[0]
            assert page.locator('#congestion-chart').evaluate('n=>n._fullLayout.yaxis.autorange')
            assert not calls
            # A task started elsewhere is discovered on return without loading history.
            external_operation=m.start(database,'incremental')
            history_before=history_reads();status_before=page.evaluate('window.statusReads')
            page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            page.wait_for_function('minimum=>window.statusReads>=minimum',arg=status_before+2)
            assert history_reads()==history_before
            with database.connection(write=True) as conn:
                conn.execute("UPDATE index_collection_operations SET status='complete',finished_at=? WHERE operation_id=?",(m.utc_now(),external_operation))
            page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            page.wait_for_function("!document.getElementById('congestion-update').disabled && document.getElementById('congestion-cancel').disabled")
            # Starting from idle wakes both progress blocks, without reloading chart data.
            history_before=history_reads()
            page.locator('#congestion-update').click()
            page.wait_for_function("!document.getElementById('cancel').disabled && !document.getElementById('congestion-cancel').disabled")
            assert history_reads()==history_before
            assert len(calls)==1
        finally:browser.close()


def test_browser_dual_axes_all_broad_ranges_and_read_only(live):
    playwright=pytest.importorskip('playwright.sync_api')
    from quarterly_dashboard.index_prices import publish_prices
    from test_index_prices import point,seed as price_seed
    base,database,calls=live
    batch,sid,code=price_seed(database,code='000001')
    with database.connection(write=True) as conn:
        publish_prices(conn,[point(batch,sid,code,observation_date='2026-10-15',close=3707.48),
                             point(batch,sid,code,observation_date='2026-10-16',close=3716.28)])
    with playwright.sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        try:
            page=browser.new_page(timezone_id='America/Los_Angeles');errors=[];external=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.on('request',lambda req:external.append(req.url) if not req.url.startswith(base) else None)
            page.goto(base+'/indices',wait_until='networkidle')
            page.wait_for_function("document.getElementById('congestion-current').textContent==='41.66%'")
            assert page.locator('#congestion-index').input_value()=='cn:000001'
            assert page.locator('#congestion-range').input_value()=='5'
            assert page.locator('#congestion-frequency').input_value()=='day'
            assert page.locator('#congestion-index').locator('option:checked').inner_text()=='上证指数'
            assert page.locator('#congestion-date').inner_text()=='2026-10-16'
            assert not page.locator('#congestion-coverage').is_visible()
            assert page.locator('#congestion-chart').evaluate("n=>n.data.map(t=>t.name)")==['大盘拥挤度','上证指数']
            assert page.locator('#congestion-chart').evaluate("n=>n._fullLayout.xaxis.hoverformat")=='%Y-%m-%d'
            assert page.locator('#congestion-chart').evaluate("n=>n.data.some(t=>t.yaxis==='y2')")
            assert page.locator('#congestion-chart').evaluate("n=>n._fullLayout.yaxis2.side")=='right'
            assert page.locator('#congestion-chart').evaluate("n=>n._fullLayout.yaxis.title.text")=='拥挤度（%）'
            assert page.locator('#congestion-chart').evaluate("n=>n._fullLayout.yaxis2.title.text")=='指数点位'
            assert '6536.08' in page.locator('#congestion-recent').text_content().replace(',','')
            assert page.locator('#congestion-benchmark').input_value()=='percentile'
            assert '日样本（2 个）' in page.locator('#congestion-history-summary').inner_text()
            assert page.locator('#congestion-position').inner_text()!='待判断'
            assert page.locator('#congestion-chart').evaluate('n=>n.layout.shapes.length')==3
            summary=page.locator('#congestion-history-summary').inner_text()
            level=page.locator('#congestion-high').inner_text()
            page.locator('#congestion-frequency').select_option('month')
            assert page.locator('#congestion-history-summary').inner_text()==summary
            assert page.locator('#congestion-high').inner_text()==level
            page.locator('#congestion-benchmark').select_option('stddev')
            assert page.locator('#congestion-high-label').inner_text()=='上限（均值 +1σ）'
            assert page.locator('#congestion-history-summary').inner_text()==summary
            assert page.locator('#congestion-chart').evaluate("n=>n.layout.shapes.every(s=>s.yref==='y' && s.line.dash==='dash')")
            assert '总体标准差' in page.locator('#congestion-comparison').inner_text()
            page.locator('#congestion-frequency').select_option('day')
            page.locator('#congestion-index').select_option(label='上证指数-legu')
            page.wait_for_function("document.getElementById('congestion-chart').data.some(t=>t.name==='上证指数-legu')")
            assert page.locator('#congestion-chart').evaluate("n=>n.data.find(t=>t.yaxis==='y2').y.filter(v=>v!==null)")==[3800,3716.28]
            assert '拥挤度采集附带的收盘价' in page.locator('#congestion-points').text_content()
            assert page.locator('#congestion-history-summary').inner_text()==summary
            page.locator('#congestion-range').select_option('10')
            assert page.locator('#congestion-frequency').input_value()=='week'
            page.locator('#congestion-frequency').select_option('month')
            assert page.locator('#congestion-frequency').input_value()=='month'
            page.locator('#congestion-range').select_option('all')
            assert page.locator('#congestion-frequency').input_value()=='week'
            page.locator('#congestion-index').select_option('cn:000300')
            page.wait_for_function("document.getElementById('congestion-points').textContent.includes('暂无本地点位')")
            assert page.locator('#congestion-range').input_value()=='all'
            assert page.locator('#congestion-current').inner_text()=='41.66%'
            page.locator('#congestion-range').select_option('5')
            assert page.locator('#congestion-frequency').input_value()=='day'
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
            assert not calls and not external and not errors
        finally:browser.close()

def test_due_slot_reuses_successful_cross_midnight_html_check(db,monkeypatch):
    ready(db)
    monkeypatch.setattr(m,'utc_now',lambda:'2026-10-24T08:00:00+00:00')
    m.configure(db,{'enabled':True,'local_time':'18:00'})
    batch,sid=seed(db,'html_recent_table')
    finish_batch(db,batch,'complete')
    with db.connection(write=True) as conn:
        conn.execute('UPDATE collection_sources SET obtained_at=? WHERE id=?',('2026-10-25T00:30:00+00:00',sid))
    result=m.scheduled_tick(db,now=datetime(2026,10,25,9,tzinfo=m.ZONE),execute=lambda *args:pytest.fail('successful due-period check should be reused'))
    assert result['skipped']
    with db.connection() as conn:
        row=conn.execute('SELECT scope_json FROM index_collection_operations WHERE operation_id=?',(result['operation_id'],)).fetchone()
        assert json.loads(row[0])['scheduled_date']=='2026-10-24'


def test_new_daily_schedule_does_not_replay_slot_before_configuration(db,monkeypatch):
    ready(db)
    monkeypatch.setattr(m,'utc_now',lambda:'2026-10-25T14:00:00+00:00')
    m.configure(db,{'enabled':True,'local_time':'18:00'})
    assert m.scheduled_tick(db,now=datetime(2026,10,25,22,5,tzinfo=m.ZONE),execute=lambda *args:pytest.fail('save is not a run')) is None
