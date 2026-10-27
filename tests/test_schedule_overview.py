"""Schedule identities, scheduled-only history and shared progress, all offline."""
import json

import pytest

from test_collection_management import management
from test_collection_management_ui import live


def seed(db):
    with db.connection(write=True) as conn:
        for identity in ('indices:a', 'indices:b'):
            conn.execute("INSERT INTO collection_schedules(schedule_id,task_key,enabled,timezone,local_time,updated_at,rule_json,params_json) VALUES(?,'indices',1,'Asia/Shanghai','21:00','2026-10-01T00:00:00+00:00',?,?)",
                         (identity, json.dumps(dict(frequency='daily')), json.dumps(dict(mode='incremental', providers=['csindex']))))
        for index in range(7):
            ref='scheduled:'+str(index)
            status='running' if index==6 else 'complete'
            stamp=f'2026-10-{index+1:02d}T13:00:00+00:00'
            result=dict(work_progress=dict(total=2,success=1 if index==6 else 2,running=1 if index==6 else 0,providers={}))
            conn.execute("INSERT INTO collection_operations(operation_id,task_key,status,trigger_type,started_at,result_json) VALUES(?,'indices',?,'scheduled',?,?)", (ref,status,stamp,json.dumps(result)))
            conn.execute("INSERT INTO collection_schedule_slots VALUES(?,?,?, ?,1,?,NULL,?,NULL)", ('indices:a',stamp[:10],status,ref,stamp,stamp))
        for index in range(25):
            conn.execute("INSERT INTO collection_operations(operation_id,task_key,status,started_at) VALUES(?,'indices','complete','2026-10-26T00:00:00+00:00')", ('manual:'+str(index),))


def test_history_is_per_schedule_and_ignores_manual_twenty_limit(management):
    seed(management.db)
    result=management.read()
    task=next(task for task in result['tasks'] if task['task_key']=='indices')
    rules={rule['schedule_id']:rule for rule in task['rules']}
    assert rules['indices:b']['history']==[]
    history=rules['indices:a']['history']
    assert len(history)==5
    assert [run['operation_ref'] for run in history]==['scheduled:'+str(i) for i in range(6,1,-1)]
    assert history[0]['status']=='running'
    assert history[0]['result']['work_progress']['success']==1
    assert 'data_counts' in history[0]['result']
    assert all(run['operation_ref'].startswith('manual:') for run in task['history'])


def test_toggle_preserves_scope_running_task_and_deleted_identity(management):
    seed(management.db)
    with management.db.connection() as conn:
        before=dict(conn.execute("SELECT * FROM collection_schedules WHERE schedule_id='indices:a'").fetchone())
    management.configure('indices',dict(action='toggle',schedule_key='indices:a',enabled=False))
    with management.db.connection() as conn:
        after=dict(conn.execute("SELECT * FROM collection_schedules WHERE schedule_id='indices:a'").fetchone())
        assert all(after[key]==value for key,value in before.items() if key not in ('enabled','updated_at'))
        assert after['enabled']==0
        assert conn.execute("SELECT status,cancel_requested FROM collection_operations WHERE operation_id='scheduled:6'").fetchone()[:]==('running',0)
    management.configure('indices',dict(action='toggle',schedule_key='indices:a',enabled=True))
    with pytest.raises(ValueError):
        management.configure('indices',dict(action='toggle',schedule_key='indices:a',enabled=1))
    with pytest.raises(ValueError):
        management.configure('sovereign_yields',dict(action='toggle',schedule_key='indices:a',enabled=True))
    management.configure('indices',dict(action='delete',schedule_key='indices:a'))
    with pytest.raises(ValueError):
        management.configure('indices',dict(action='toggle',schedule_key='indices:a',enabled=True))


def test_browser_overview_history_progress_edit_pause_and_tabs(live):
    from playwright.sync_api import sync_playwright,expect
    base,db,state,web=live
    seed(db)
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        try:
            page=browser.new_page(viewport=dict(width=1200,height=900))
            errors=[];page.on('pageerror',lambda exc:errors.append(str(exc)))
            page.goto(base+'/harness')
            page.locator('#collection-overview-tab').click()
            row=page.locator('[data-schedule-id="indices:a"]')
            expect(row).to_be_visible()
            row.locator('[data-schedule-history]>summary').click()
            expect(row.locator('[data-schedule-run]')).to_have_count(5)
            row.locator('[data-schedule-run="0"]').click()
            expect(row.locator('[data-progress-summary]')).to_contain_text('1/2')
            row.locator('[data-schedule-run="1"]').click()
            expect(row.locator('[data-progress-summary]')).to_contain_text('2/2')
            row.locator('[data-schedule-edit]').click()
            editor=page.locator('[data-schedule-editor]')
            expect(editor.locator('[name=local_time]')).to_have_value('21:00')
            editor.locator('[name=local_time]').fill('22:15')
            with page.expect_response('**/api/maintenance/collections/schedule') as response:
                editor.locator('[type=submit]').click()
            assert response.value.ok
            expect(row).to_contain_text('22:15')
            row.locator('[data-schedule-toggle]').click()
            expect(row.locator('.schedule-state')).to_contain_text('已暂停')
            page.locator('[data-schedule-filter=paused]').click()
            expect(page.locator('[data-schedule-id]')).to_have_count(1)
            page.locator('#collection-overview-tab').press('ArrowLeft')
            expect(page.locator('#collection-settings-tab')).to_be_focused()
            expect(page.locator('#collection-settings-panel')).to_be_visible()
            page.locator('#collection-overview-tab').click()
            page.click('#collection-overview-refresh')
            expect(row.locator('[data-progress-summary]')).to_contain_text('2/2')
            assert errors==[] and state['storage']==0
            with db.connection() as conn:
                assert conn.execute('SELECT count(*) FROM collection_operations').fetchone()[0]==32
        finally:
            browser.close()


def test_browser_stock_plan_editor_reuses_group_dataset_and_rule(live):
    from playwright.sync_api import sync_playwright,expect
    from stock_analysis import application as app
    base,db,state,web=live
    with db.connection(write=True) as conn:
        group=conn.execute("INSERT INTO stock_groups(name,position) VALUES('核心持仓',0)").lastrowid
    manager=app.collection_management()
    saved=manager.configure('stocks',dict(enabled=False,local_time='19:30',
        rule=dict(frequency='financial_season',days_before=15,days_after=3),
        params=dict(group_id=group,datasets=['financial'],mode='incremental'),schedule_key=None))
    identity=saved['schedule_key']
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        try:
            page=browser.new_page();errors=[]
            page.on('pageerror',lambda exc:errors.append(str(exc)))
            page.goto(base+'/harness');page.click('#collection-overview-tab')
            row=page.locator(f'[data-schedule-id="{identity}"]')
            row.locator('[data-schedule-edit]').click()
            editor=page.locator('[data-schedule-editor]')
            expect(editor.locator('[name=dataset]:checked')).to_have_count(1)
            expect(editor.locator('[name=days_before]')).to_have_value('15')
            expect(editor.locator('[name=days_after]')).to_have_value('3')
            editor.locator('[name=local_time]').fill('20:10')
            with page.expect_response('**/api/maintenance/collections/schedule') as response:
                editor.locator('[type=submit]').click()
            assert response.value.ok
            expect(row).to_contain_text('20:10')
            for width in (1100,390):
                page.set_viewport_size(dict(width=width,height=900))
                assert page.evaluate("document.querySelector('#collection-overview-panel').scrollWidth <= document.querySelector('#collection-overview-panel').clientWidth + 1")
            with db.connection() as conn:
                assert conn.execute('SELECT count(*) FROM collection_schedules').fetchone()[0]==1
                assert conn.execute('SELECT count(*) FROM collection_operations').fetchone()[0]==0
            assert errors==[] and state['storage']==0
        finally:
            browser.close()
