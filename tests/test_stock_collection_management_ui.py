"""New stock controls use the public page and read-only data endpoints."""
import requests
from test_collection_management_ui import live
from stock_analysis.domains.stocks.collection_operations import StockCollections
from stock_analysis.domains.stocks.data_service import StockDataService
from stock_analysis.infrastructure.storage import SyncKey, SyncResult


def test_stock_reads_never_collect_and_commands_share_public_history(live, monkeypatch):
    base,db,state,web=live
    identity=db.ensure_instrument('601919')
    run=db.start_sync(SyncKey(identity,'financial:lrb','test'),parser_version='test',methodology_version='test')
    db.complete_sync(run,SyncResult(0,no_data=True),lambda conn:None)
    calls=[]
    monkeypatch.setattr(StockCollections,'collect',lambda self,code,dataset,full,collector:calls.append((code,dataset,full)))
    session=requests.Session();session.trust_env=False
    for path in ('/api/stock-collection','/api/financial','/api/valuation','/api/dividends','/api/shareholders','/api/financing','/api/prices'):
        result=session.get(base+path+'?code=601919',timeout=10)
        assert result.ok, result.text
    result=session.get(base+'/api/financial?code=601919&refresh=1',timeout=10)
    assert not result.ok and calls==[]
    assert session.post(base+'/api/stock-collection',json={'initial':True,'params':{'codes':['601919']}},timeout=10).json()['status']=='existing'
    assert calls==[]
    assert session.post(base+'/api/stock-collection',json={'params':{'codes':['601919'],'datasets':['financing']}},headers={'Origin':'https://evil.test'},timeout=10).status_code==400
    result=session.post(base+'/api/stock-collection',json={'params':{'codes':['601919'],'datasets':['financing']}},timeout=10)
    assert result.ok
    import time
    for _ in range(100):
        info=session.get(base+'/api/stock-collection?code=601919',timeout=5).json()
        if info['operation']['status']!='running':break
        time.sleep(.02)
    assert calls==[('601919','financing',False)]
    task=next(task for task in session.get(base+'/api/maintenance/collections',timeout=10).json()['tasks'] if task['task_key']=='stocks')
    assert task['history'][0]['operation_ref']==result.json()['operation_ref']
    assert task['history'][0]['result']['work_progress']['success']==1


def test_browser_stock_module_group_rules_and_selected_data(live,monkeypatch):
    from playwright.sync_api import sync_playwright,expect
    base,db,state,web=live
    identity=db.ensure_instrument('601919')
    with db.connection(write=True) as conn:
        group=conn.execute("INSERT INTO stock_groups(name,position) VALUES('持仓',0)").lastrowid
        conn.execute('INSERT INTO stock_group_members(group_id,instrument_id,position) VALUES(?,?,0)',(group,identity))
    calls=[]
    monkeypatch.setattr(StockCollections,'collect',lambda self,code,dataset,full,collector:calls.append((code,dataset)))
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        try:
            page=browser.new_page();errors=[];page.on('pageerror',lambda exc:errors.append(str(exc)))
            page.goto(base+'/harness')
            card=page.locator('[data-task="stocks"]');expect(card).to_have_count(1)
            schedule=card.locator('[data-task-schedule]')
            schedule.locator('..').locator('summary').first.click()
            schedule.locator('[name=group_id]').select_option(str(group))
            schedule.locator('[name=enabled]').check()
            for option in schedule.locator('[name=dataset]').all():option.uncheck()
            schedule.locator('[name=dataset][value=financing]').check()
            schedule.locator('[name=local_time]').fill('20:30')
            schedule.locator('[type=submit]').click()
            expect(card.locator('[data-stock-rules]')).to_contain_text('持仓')
            expect(card.locator('[data-stock-rules]')).to_contain_text('20:30')
            assert calls==[]
            page.reload();card=page.locator('[data-task="stocks"]');schedule=card.locator('[data-task-schedule]')
            schedule.locator('..').locator('summary').first.click()
            card.locator('[data-stock-edit-rule]').first.click()
            expect(schedule.locator('[name=local_time]')).to_have_value('20:30')
            expect(schedule.locator('[name=dataset]:checked')).to_have_count(1)
            manual=card.locator('[data-task-run]');manual.locator('[name=group_id]').select_option(str(group))
            for option in manual.locator('[name=dataset]').all():option.uncheck()
            manual.locator('[name=dataset][value=financing]').check()
            page.on('dialog',lambda dialog:dialog.accept())
            manual.locator('[type=submit]').click()
            expect(card.locator('[data-task-history]')).to_contain_text('stock:')
            assert calls==[('601919','financing')] and errors==[] and state['storage']==0
        finally:browser.close()


def test_browser_multiple_topics_edit_delete_and_alignment(live):
    from playwright.sync_api import sync_playwright,expect
    base,db,state,web=live
    with db.connection(write=True) as conn:
        group=conn.execute("INSERT INTO stock_groups(name,position) VALUES('持仓',0)").lastrowid
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        try:
            page=browser.new_page(viewport=dict(width=1920,height=1080));errors=[]
            page.on('pageerror',lambda exc:errors.append(str(exc)))
            page.goto(base+'/harness');card=page.locator('[data-task="stocks"]')
            manual=card.locator('[data-task-run]')
            box=manual.locator('fieldset').bounding_box();control=manual.locator('[name=group_id]').bounding_box()
            assert abs(box['y']+box['height']-control['y']-control['height'])<2
            card.locator('[data-stock-schedules]>summary').click();form=card.locator('[data-task-schedule]')
            def topics(datasets):
                for option in form.locator('[name=dataset]').all():option.uncheck()
                for dataset in datasets:form.locator(f'[name=dataset][value={dataset}]').check()
            form.locator('[name=group_id]').select_option(str(group));topics(['financial'])
            form.locator('[name=enabled]').check();form.locator('[name=frequency]').select_option('monthly')
            expect(form.locator('[data-stock-monthday]')).to_be_visible();expect(form.locator('[data-stock-weekday]')).not_to_be_visible()
            expect(form.locator('[name=window_start]')).to_have_count(0)
            form.locator('[name=day]').fill('15');form.locator('[name=local_time]').fill('20:00');form.locator('[type=submit]').click()
            expect(card.locator('[data-stock-edit-rule]')).to_have_count(1)
            form.locator('[data-stock-rule-select]').select_option('');topics(['prices','financing'])
            form.locator('[name=enabled]').check();form.locator('[name=local_time]').fill('19:00');form.locator('[type=submit]').click()
            expect(card.locator('[data-stock-edit-rule]')).to_have_count(2)
            expect(card.locator('[data-stock-rules]')).to_contain_text('每月 15日 20:00')
            expect(card.locator('[data-stock-rules]')).to_contain_text('每日 19:00')
            monthly=card.locator('.stock-schedule-row').filter(has_text='每月')
            monthly.locator('[data-stock-edit-rule]').click();form.locator('[name=local_time]').fill('21:00');form.locator('[type=submit]').click()
            expect(monthly).to_contain_text('21:00');expect(card.locator('[data-stock-edit-rule]')).to_have_count(2)
            daily=card.locator('.stock-schedule-row').filter(has_text='每日');expect(daily).to_contain_text('19:00')
            page.on('dialog',lambda dialog:dialog.accept());monthly.locator('[data-stock-delete-rule]').click()
            expect(card.locator('[data-stock-edit-rule]')).to_have_count(1);expect(daily).to_contain_text('19:00')
            size=form.locator('[name=group_id]').bounding_box()
            assert size['height']<=40 and size['width']<=240
            page.set_viewport_size(dict(width=390,height=844))
            assert not page.evaluate('document.documentElement.scrollWidth>innerWidth+1')
            assert errors==[] and state['storage']==0
            with db.connection() as conn:
                assert conn.execute('SELECT count(*) FROM stock_collection_operations').fetchone()[0]==0
        finally:browser.close()
