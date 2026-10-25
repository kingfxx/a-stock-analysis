"""HTTP/browser acceptance on an isolated database and explicit available port."""
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
import requests

from stock_analysis.collection.collection_management import CollectionManagement, TASKS
from stock_analysis.maintenance.database_restore import RequestGate, RestoreManager
from stock_analysis.infrastructure.storage import Database


@pytest.fixture
def live(tmp_path,monkeypatch):
    from stock_analysis.http import server
    db=Database(tmp_path/'ui.sqlite3');db.initialize()
    monkeypatch.setattr(server,'DATABASE_PATH',db.path)
    root=Path(__file__).resolve().parents[1]
    state={'storage':0,'collections':0,'running':False}
    original_read=CollectionManagement.read
    def read(self):
        result=original_read(self)
        if state['running']:next(t for t in result['tasks'] if t['task_key']=='market_financial')['running']=True
        return result
    monkeypatch.setattr(CollectionManagement,'read',read)
    def execute(self,key,params,ref):
        with self.db.connection(write=True) as conn:
            if ref.startswith('sw_update_runs:'):
                conn.execute("UPDATE sw_update_runs SET status='complete',finished_at='2026-10-25T00:00:00+00:00' WHERE id=?",(int(ref.split(':')[1]),))
            else:
                conn.execute("UPDATE index_collection_operations SET status='complete',finished_at='2026-10-25T00:00:00+00:00' WHERE operation_id=?",(ref,))
    monkeypatch.setattr(CollectionManagement,'execute',execute)
    class Handler(server.Handler):
        def log_message(self,*args):pass
        def _do_GET(self):
            if self.path=='/harness':
                body=b'''<!doctype html><html><meta charset="utf-8"><style>.panel{overflow:hidden}</style><link rel="stylesheet" href="/maintenance.css"><link rel="stylesheet" href="/collection-progress.css"><button id="maintenance-tab">maintenance</button><button id="leave">leave</button><section id="maintenance-panel"></section><script src="/datetime.js"></script><script>window.initStockCleanup=()=>{};</script><script src="/collection-progress.js"></script><script src="/maintenance.js"></script><script>initMaintenance();document.getElementById('leave').onclick=()=>document.getElementById('maintenance-panel').hidden=true;document.getElementById('maintenance-tab').addEventListener('click',()=>document.getElementById('maintenance-panel').hidden=false);</script></html>'''
                self.send_response(200);self.send_header('Content-Type','text/html;charset=utf-8');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
            if self.path.startswith('/api/maintenance/storage'):state['storage']+=1
            if self.path=='/api/maintenance/collections':state['collections']+=1
            if self.path in ('/maintenance.js','/datetime.js','/maintenance.css','/collection-progress.js','/collection-progress.css'):
                path=root/'web'/self.path[1:];body=path.read_bytes()
                self.send_response(200);self.send_header('Content-Type','text/css' if path.suffix=='.css' else 'text/javascript');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
            super()._do_GET()
    for port in (*range(18760,18780),8768,8766):
        try:web=ThreadingHTTPServer(('127.0.0.1',port),Handler);break
        except OSError:continue
    else:raise RuntimeError('No free isolated port')
    web.request_gate=RequestGate();web.restore_manager=RestoreManager(db)
    worker=Thread(target=web.serve_forever,daemon=True);worker.start()
    try:yield f'http://127.0.0.1:{web.server_port}',db,state,web
    finally:web.shutdown();web.server_close();worker.join()


def test_http_whitelist_shared_state_and_maintenance_gate(live):
    base,db,state,web=live
    session=requests.Session();session.trust_env=False
    result=session.get(base+'/api/maintenance/collections',timeout=5)
    assert result.ok and len(result.json()['tasks'])==len(TASKS) and state['storage']==0
    for command in ({'task_key':'bad'},{'task_key':'indices','params':{'url':'bad'}}):
        assert session.post(base+'/api/maintenance/collections/run',json=command,timeout=5).status_code==400
    assert session.post(base+'/api/maintenance/collections/run',json={'task_key':'indices'},headers={'Origin':'https://evil.test'},timeout=5).status_code==400
    result=session.post(base+'/api/maintenance/collections/schedule',json={'task_key':'indices','schedule':{'enabled':True}},timeout=5)
    assert result.ok
    with db.connection() as conn:assert conn.execute('SELECT count(*) FROM index_collection_operations').fetchone()[0]==0
    web.request_gate.blocked=True
    result=session.post(base+'/api/maintenance/collections/run',json={'task_key':'indices'},timeout=5)
    assert result.status_code==503
    web.request_gate.blocked=False
    result=session.post(base+'/api/maintenance/collections/run',json={'task_key':'indices'},timeout=5)
    assert result.ok


def test_browser_rate_templates_custom_save_polling_and_guards(live):
    from playwright.sync_api import sync_playwright, expect
    base, db, state, web = live
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        try:
            page=browser.new_page(); errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(base+'/harness')
            card=page.locator('[data-task="indices"]')
            expect(card.locator('[data-task-rates]')).not_to_have_attribute('open','')
            card.locator('[data-task-rates] > summary').click()
            forms=card.locator('[data-task-rate-form]'); expect(forms).to_have_count(3)
            form=card.locator('[data-rate-provider="legu"]')
            expect(form.locator('input[name=interval]')).to_have_value('3')
            form.locator('[data-rate-preset]').select_option('default')
            expect(form.locator('input[name=interval]')).to_have_value('10')
            with db.connection() as conn:
                assert conn.execute('SELECT count(*) FROM collection_rate_limits').fetchone()[0]==0
            form.locator('[data-rate-preset]').select_option('faster')
            expect(form.locator('input[name=interval]')).to_have_value('3')
            form.locator('input[name=interval]').fill('7.5')
            page.click('#maintenance-refresh');page.wait_for_timeout(200)
            expect(form.locator('input[name=interval]')).to_have_value('7.5')
            expect(form.locator('[data-rate-preset]')).to_have_value('custom')
            form.locator('button[type=submit]').click()
            expect(form.locator('[data-rate-status]')).to_contain_text('已保存')
            page.reload();card=page.locator('[data-task="indices"]')
            card.locator('[data-task-rates] > summary').click()
            form=card.locator('[data-rate-provider="legu"]')
            expect(form.locator('input[name=interval]')).to_have_value('7.5')
            expect(form.locator('input[name=max_concurrency]')).to_have_attribute('max','1')
            styles=(Path(__file__).resolve().parents[1]/'web/index.html').read_text(encoding='utf-8').split('<style>',1)[1].split('</style>',1)[0]
            page.add_style_tag(content=styles)
            card.screenshot(path=str(db.path.parent/'rate-preview.png'))
            state['running']=True;page.click('#maintenance-refresh');page.wait_for_timeout(150)
            expect(form.locator('button[type=submit]')).to_be_disabled()
            assert state['storage']==0 and not errors
        finally:browser.close()


def test_rate_http_validation_no_execution_and_maintenance_gate(live):
    from stock_analysis.collection.collection_rate_limits import fields
    from stock_analysis.collection.collection import PacingPolicy
    base,db,state,web=live
    session=requests.Session();session.trust_env=False
    command=dict(task_key='indices',provider='csindex',settings=fields(PacingPolicy()))
    url=base+'/api/maintenance/collections/rate-limit'
    result=session.post(url,json=command,timeout=5)
    assert result.ok
    for patch in ({'provider':'bad'},{'params':{}},{'settings':{'interval':0}}):
        assert session.post(url,json=dict(command,**patch),timeout=5).status_code==400
    assert session.post(url,json=command,headers={'Origin':'https://evil.test'},timeout=5).status_code==400
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM collection_rate_limits').fetchone()[0]==1
        assert conn.execute('SELECT count(*) FROM index_collection_operations').fetchone()[0]==0
    web.request_gate.blocked=True
    assert session.post(url,json=command,timeout=5).status_code==503
    web.request_gate.blocked=False



def test_browser_default_lazy_scans_events_polling_and_keyboard(live):
    base,db,state,web=live
    from playwright.sync_api import sync_playwright,expect
    with db.connection(write=True) as conn:
        conn.execute("INSERT INTO index_collection_operations(operation_id,mode,scope_json,status,started_at,finished_at,result_json) VALUES('detail-test','incremental','{}','complete','2026-10-25','2026-10-25',?)",(json.dumps({'files':['payload only in detail']}),))
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        try:
            page=browser.new_page(timezone_id='America/Los_Angeles');errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(base+'/harness');expect(page.locator('.collection-task')).to_have_count(len(TASKS)-3)
            assert page.locator('[role=tab]').all_text_contents()==['采集管理','数据库预览','未关注股票管理']
            expect(page.locator('#maintenance-collection-tab')).to_have_attribute('aria-selected','true')
            assert state['storage']==0
            page.locator('[data-task="indices"] > details').nth(1).locator('summary').first.click()
            expect(page.locator('[data-task="indices"] [data-run]').first).to_have_attribute('open','')
            page.locator('[data-task="indices"] [data-operation-ref]').click()
            expect(page.locator('[data-task="indices"] [data-task-history]')).to_contain_text('payload only in detail')
            page.click('#maintenance-refresh');page.wait_for_timeout(150)
            assert state['storage']==0
            state['running']=True
            page.click('#maintenance-refresh');page.wait_for_timeout(150)
            page.locator('[data-task="market_financial"] [data-task-run] [data-target-preset]').select_option('custom')
            page.locator('[data-task="market_financial"] [data-task-run] input[name=target]').fill('2025-12-31')
            before=state['collections'];page.wait_for_timeout(3200)
            assert state['collections']>before and state['storage']==0
            assert page.locator('[data-task="market_financial"] [data-task-run] input[name=target]').input_value()=='2025-12-31'
            page.click('#leave');page.wait_for_timeout(100);before=state['collections']
            page.evaluate("window.dispatchEvent(new CustomEvent('stock-cleanup-complete'))")
            page.wait_for_timeout(3200)
            assert state['collections']==before and state['storage']==0
            state['running']=False;page.click('#maintenance-tab')
            page.click('#maintenance-database-tab');expect(page.locator('#maintenance-overview .maintenance-database')).to_be_visible()
            assert state['storage']==1
            page.click('#maintenance-collection-tab');page.click('#maintenance-database-tab');page.wait_for_timeout(100)
            assert state['storage']==1
            page.click('#maintenance-refresh');page.wait_for_timeout(150);assert state['storage']==2
            page.locator('#maintenance-database-tab').focus();page.keyboard.press('ArrowLeft')
            expect(page.locator('#maintenance-collection-tab')).to_be_focused()
            page.keyboard.press('End');expect(page.locator('#maintenance-stocks-tab')).to_be_focused()
            assert not errors
        finally:browser.close()

def test_browser_reuses_index_progress_and_task_specific_confirmation(live):
    base,db,state,web=live
    from playwright.sync_api import sync_playwright,expect
    result={'work_progress':{'total':42,'success':6,'skipped':36,'failed':0,'cancelled':0,'running':0,'pending':0,'providers':{
        'csindex':{'total':18,'success':1,'skipped':17},'legu':{'total':24,'success':5,'skipped':19}}}}
    with db.connection(write=True) as conn:
        conn.execute("INSERT INTO index_collection_operations(operation_id,mode,scope_json,status,started_at,finished_at,result_json) VALUES('progress-test','incremental',?,'complete','2026-10-25','2026-10-25',?)",(json.dumps({'providers':['csindex','legu'],'datasets':[]}),json.dumps(result)))
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        try:
            page=browser.new_page();page.goto(base+'/harness');expect(page.locator('.collection-task')).to_have_count(len(TASKS)-3)
            card=page.locator('[data-task="indices"]')
            card.locator(':scope > details').nth(1).locator('summary').first.click()
            expect(card.locator('[data-run]').first).to_contain_text('已处理 42/42 项')
            expect(card.locator('[data-run]').first).to_contain_text('中证指数：已处理 18/18 项')
            expect(card.locator('[data-run]').first).to_contain_text('乐咕乐股：已处理 24/24 项')
            messages=[]
            page.on('dialog',lambda dialog:(messages.append(dialog.message),dialog.dismiss()))
            card.locator('[data-task-run] button[type=submit]').click()
            assert '全市场财务' not in messages[-1] and '快照' not in messages[-1]
            assert '中证指数' in messages[-1] and '本入口不附带国债采集' in messages[-1]
            assert '历史初始化／回填或核查可能耗时' not in messages[-1]
            card.locator('[data-task-run] select[name=mode]').select_option('full')
            card.locator('[data-task-run] button[type=submit]').click()
            assert '历史初始化／回填或核查可能耗时' in messages[-1]
            bond=page.locator('[data-task="sovereign_yields"]')
            expect(bond.locator('h3')).to_contain_text('东方财富')
            expect(bond).to_contain_text('本入口独立采集东方财富中美国债收益率')
            bond.locator('[data-task-run] button[type=submit]').click()
            assert '东方财富' in messages[-1] and '独立运行' in messages[-1] and '全市场财务' not in messages[-1]
            page.locator('[data-task="market_financial"] [data-task-run] button[type=submit]').click()
            assert '全A股四表' in messages[-1] and '季度交易快照' not in messages[-1]
            with db.connection() as conn:
                assert conn.execute('SELECT count(*) FROM index_collection_operations').fetchone()[0]==1
        finally:browser.close()

def test_browser_latest_once_collapsed_batches_and_dropdown_multi_select(live):
    base,db,state,web=live
    from playwright.sync_api import sync_playwright,expect
    with db.connection(write=True) as conn:
        for ref,stamp in [('older','2026-10-24'),('latest','2026-10-25')]:
            conn.execute("INSERT INTO index_collection_operations(operation_id,mode,scope_json,status,started_at,finished_at,result_json) VALUES(?,'incremental','{}','complete',?,?,?)",(ref,stamp,stamp,json.dumps({'work_progress':{'total':1,'success':1}})))
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        try:
            page=browser.new_page();page.goto(base+'/harness');expect(page.locator('.collection-task')).to_have_count(len(TASKS)-3)
            card=page.locator('[data-task="indices"]')
            expect(card.locator('[data-latest-progress]')).to_have_count(0)
            expect(card.locator('[data-run=latest]')).to_have_attribute('open','')
            expect(card.locator('[data-run=older]')).not_to_have_attribute('open','')
            expect(card.locator('[data-task-batches]')).not_to_have_attribute('open','')
            card.locator(':scope > details').nth(1).locator('summary').first.click()
            expect(card.locator('[data-run=latest] > summary')).to_contain_text('2026-10-25')
            expect(card.locator('[data-run=latest] .index-progress-details')).to_have_count(1)
            card.locator('[data-run=latest] > summary').click()
            card.locator('[data-task-batches] > summary').click()
            page.click('#maintenance-refresh');page.wait_for_timeout(150)
            expect(card.locator('[data-run=latest]')).not_to_have_attribute('open','')
            expect(card.locator('[data-task-batches]')).to_have_attribute('open','')
            menu=card.locator('[data-task-run] [data-provider-menu]')
            expect(menu).not_to_have_attribute('open','')
            menu.locator('summary').focus();page.keyboard.press('Enter')
            expect(menu).to_have_attribute('open','')
            menu.locator('input[value=legu]').uncheck()
            expect(menu.locator('[data-provider-selection]')).to_have_text('已选 2 / 3 项')
            assert card.locator('[data-task-run]').evaluate("form=>new FormData(form).getAll('provider')")==['csindex','sws']
            page.keyboard.press('Escape');expect(menu).not_to_have_attribute('open','')
            page.click('#maintenance-refresh');page.wait_for_timeout(150)
            expect(menu.locator('input[value=legu]')).not_to_be_checked()
            # Scheduled provider selection is an independent dropdown with the same semantics.
            card.locator(':scope > details').first.locator('summary').first.click()
            scheduled=card.locator('[data-task-schedule] [data-provider-menu]')
            scheduled.locator('summary').click();scheduled.locator('input[value=sws]').uncheck()
            expect(scheduled.locator('[data-provider-selection]')).to_have_text('已选 2 / 3 项')
            page.click('#maintenance-refresh');page.wait_for_timeout(150)
            expect(scheduled).not_to_have_attribute('open','')
            expect(scheduled.locator('input[value=sws]')).not_to_be_checked()
            # A newly arriving running operation is the newest timestamped row and opens once.
            with db.connection(write=True) as conn:
                conn.execute("INSERT INTO index_collection_operations(operation_id,mode,scope_json,status,started_at) VALUES('new-running','incremental','{}','running','2026-10-26')")
            page.click('#maintenance-refresh');page.wait_for_timeout(150)
            expect(card.locator('[data-run]').first).to_have_attribute('data-run','new-running')
            expect(card.locator('[data-run]').first).to_have_attribute('open','')
            expect(card.locator('[data-run]').first.locator('summary')).to_contain_text('running')
        finally:browser.close()

def test_browser_recent_four_targets_default_latest_and_custom_preserved(live):
    from datetime import datetime,timezone
    from stock_analysis.domains.industries.industry_service import IndustryService
    from playwright.sync_api import sync_playwright,expect
    base,db,state,web=live
    mg=CollectionManagement(db,IndustryService(db))
    mg.configure('market_financial',{'enabled':False,'params':{'target':'2026-06-30'}})
    with db.connection(write=True) as conn:
        conn.execute("INSERT INTO sw_update_runs(action,target,recheck,started_at,status) VALUES('financial_period','2026-06-30',0,'2026-10-15','complete')")
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        try:
            page=browser.new_page(timezone_id='America/Los_Angeles');page.clock.install(time=datetime(2026,10,25,tzinfo=timezone.utc))
            page.goto(base+'/harness');expect(page.locator('.collection-task')).to_have_count(len(TASKS)-3)
            financial=page.locator('[data-task=market_financial] [data-task-run]')
            snapshot=page.locator('[data-task=trade_snapshot] [data-task-run]')
            assert financial.locator('[data-target-preset] option').evaluate_all('(nodes)=>nodes.map(n=>n.value)')==['2026-09-30','2026-06-30','2026-03-31','2025-12-31','custom']
            assert snapshot.locator('[data-target-preset] option').evaluate_all('(nodes)=>nodes.map(n=>n.value)')==['2026Q3','2026Q2','2026Q1','2025Q4','custom']
            expect(financial.locator('[data-target-preset]')).to_have_value('2026-09-30')
            expect(snapshot.locator('[data-target-preset]')).to_have_value('2026Q3')
            expect(page.locator('[data-task=market_financial] [data-task-schedule] [data-target-preset]')).to_have_value('2026-06-30')
            financial.locator('[data-target-preset]').select_option('custom')
            financial.locator('input[name=target]').fill('2024-12-31')
            page.click('#maintenance-refresh');page.wait_for_timeout(150)
            expect(financial.locator('input[name=target]')).to_have_value('2024-12-31')
            assert financial.evaluate("form=>new FormData(form).get('target')")=='2024-12-31'
            financial.locator('[data-target-preset]').select_option('2026-06-30')
            assert financial.evaluate("form=>new FormData(form).get('target')")=='2026-06-30'
            expect(financial.locator('input[name=target]')).to_be_hidden()
        finally:browser.close()


def test_browser_provider_options_not_clipped_by_card_or_viewport(live):
    from playwright.sync_api import sync_playwright,expect
    base,db,state,web=live
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        try:
            page=browser.new_page(viewport={'width':1100,'height':600});page.goto(base+'/harness');expect(page.locator('.collection-task')).to_have_count(len(TASKS)-3)
            card=page.locator('[data-task=indices]')
            assert card.evaluate('node=>getComputedStyle(node).overflow')=='visible'
            for form in ('[data-task-run]','[data-task-schedule]'):
                if form=='[data-task-schedule]':card.locator(':scope > details').first.locator('summary').first.click()
                menu=card.locator(form+' [data-provider-menu]')
                menu.locator('summary').scroll_into_view_if_needed()
                page.evaluate('(y)=>window.scrollTo(0,y)',menu.locator('summary').evaluate('node=>node.getBoundingClientRect().top+scrollY-innerHeight+80'))
                menu.locator('summary').click()
                expect(menu.locator('.collection-provider-options label')).to_have_count(3)
                for label in menu.locator('.collection-provider-options label').all():expect(label).to_be_in_viewport(ratio=1)
                menu.locator('input[value=sws]').uncheck();menu.locator('input[value=sws]').check()
                expect(menu.locator('[data-provider-selection]')).to_have_text('已选 3 / 3 项')
                page.keyboard.press('Escape')
        finally:browser.close()


def test_business_pages_have_navigation_only_no_batch_collection_controls(live):
    from playwright.sync_api import sync_playwright,expect
    base,db,state,web=live
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        try:
            page=browser.new_page();errors=[];writes=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            def readonly(route):
                path=route.request.url.split('?')[0]
                if route.request.method!='GET' or any(path.endswith('/api/'+key) for key in ('financial','dividends','valuation','prices','financing','shareholders')):
                    writes.append(path) if route.request.method!='GET' else None
                    route.fulfill(status=503,content_type='application/json',body='{}')
                else:route.continue_()
            page.route('**/api/**',readonly)
            for url in ('/indices','/?tab=industry'):
                page.goto(base+url,wait_until='networkidle')
                expect(page.locator('#incremental,#full,#review,#audit,#cancel,#congestion-full,#congestion-update,#congestion-cancel,#congestion-save,#sw-refresh-financial,#sw-refresh-cap')).to_have_count(0)
                assert page.locator('a[href="/?tab=maintenance"]').count()>0
            assert not errors,errors
            assert not any('/api/indices/collect' in url or '/api/industry/refresh' in url for url in writes)
        finally:browser.close()


@pytest.mark.parametrize('width',[1440,390])
def test_shipping_one_module_independent_settings_and_monthly_image_fallback(live,width):
    from playwright.sync_api import sync_playwright,expect
    base,db,state,web=live
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        try:
            page=browser.new_page(viewport={'width':width,'height':900});errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(base+'/harness')
            module=page.locator('[data-shipping-module]')
            expect(module).to_have_count(1)
            expect(page.locator('.collection-task[data-task^=shipping_]')).to_have_count(0)
            picker=module.locator('[data-shipping-select]')
            assert picker.locator('option').evaluate_all('(nodes)=>nodes.map(n=>n.value)')==['shipping_monthly','shipping_weekly','shipping_research_import']
            expect(picker).to_have_value('shipping_monthly')
            monthly=module.locator('[data-task=shipping_monthly]')
            weekly=module.locator('[data-task=shipping_weekly]')
            correction=module.locator('[data-task=shipping_image_review]')
            expect(monthly).to_be_visible();expect(weekly).to_be_hidden();expect(correction).to_be_hidden()
            expect(monthly).to_contain_text('同一次采集自动处理 HTML 表格和图片')
            monthly.locator('[data-task-run] select[name=mode]').select_option('backfill')
            monthly.locator('[data-task-run] input[name=start]').fill('2014-01-01')
            monthly.locator('[data-task-run] input[name=end]').fill('2026-09-30')
            monthly.locator(':scope > details').first.locator('summary').first.click()
            monthly_schedule=monthly.locator('[data-task-schedule]')
            monthly_schedule.locator('[name=local_time]').fill('04:03')
            picker.select_option('shipping_weekly')
            expect(monthly).to_be_hidden();expect(weekly).to_be_visible()
            weekly.locator('[data-task-run] select[name=mode]').select_option('review')
            weekly.locator(':scope > details').first.locator('summary').first.click()
            weekly_schedule=weekly.locator('[data-task-schedule]')
            weekly_schedule.locator('[name=local_time]').fill('03:02')
            with page.expect_response('**/api/maintenance/collections/schedule') as response:
                weekly_schedule.locator('button[type=submit]').click()
            assert response.value.ok
            picker.select_option('shipping_monthly')
            expect(monthly.locator('[data-task-run] input[name=start]')).to_have_value('2014-01-01')
            expect(monthly.locator('[data-task-run] select[name=mode]')).to_have_value('backfill')
            expect(monthly_schedule.locator('[name=local_time]')).to_have_value('04:03')
            with page.expect_response('**/api/maintenance/collections/schedule') as response:
                monthly_schedule.locator('button[type=submit]').click()
            assert response.value.ok
            session=requests.Session();session.trust_env=False
            tasks={t['task_key']:t for t in session.get(base+'/api/maintenance/collections').json()['tasks']}
            assert tasks['shipping_weekly']['schedule']['local_time']=='03:02'
            assert tasks['shipping_monthly']['schedule']['local_time']=='04:03'
            assert all(not task['schedule']['enabled'] for key,task in tasks.items() if key.startswith('shipping_'))
            commands=[]
            def capture(route):
                commands.append(route.request.post_data_json)
                route.fulfill(status=200,content_type='application/json',body='{}')
            page.route('**/api/maintenance/collections/run',capture)
            page.on('dialog',lambda dialog:dialog.accept())
            monthly.locator('[data-task-run] button[type=submit]').click()
            page.wait_for_timeout(150)
            assert commands[-1]=={'task_key':'shipping_monthly','params':{'mode':'backfill','start':'2014-01-01','end':'2026-09-30'}}
            module.locator('[data-shipping-correction] > summary').click();expect(correction).to_be_visible()
            expect(correction.locator('h3')).to_contain_text('人工核验补正')
            picker.select_option('shipping_research_import')
            expect(module.locator('[data-task=shipping_research_import]')).to_be_visible()
            expect(correction).to_be_hidden()
            # A running monthly job remains reachable and does not change the selected type on refresh.
            with db.connection(write=True) as conn:
                conn.execute("INSERT INTO index_collection_operations(operation_id,mode,scope_json,status,started_at,result_json) VALUES('shipping-active','backfill',?,'running','2026-10-25',?)",(json.dumps({'dataset':'shipping_monthly'}),json.dumps({'work_progress':{'total':306,'success':8,'pending':298}})))
            page.click('#maintenance-refresh')
            expect(module.locator('[data-shipping-status]')).to_contain_text('运行中')
            expect(picker).to_have_value('shipping_research_import')
            picker.select_option('shipping_monthly')
            expect(monthly.locator('[data-task-cancel]')).to_be_enabled()
            page.route('**/api/maintenance/collections/cancel',capture)
            monthly.locator('[data-task-cancel]').click();page.wait_for_timeout(150)
            assert commands[-1]=={'task_key':'shipping_monthly'}
            page.reload();expect(page.locator('[data-shipping-select]')).to_have_value('shipping_monthly')
            assert not errors,errors
        finally:browser.close()
