"""Isolated scheduling and registry contracts: no source HTTP calls."""
from datetime import datetime, timezone
from threading import Event, Thread

import pytest

from stock_analysis.collection.collection_management import CollectionManagement, ZONE, due_slot, next_check, validate_params, validate_rule, TASKS
from stock_analysis.domains.industries.industry_service import IndustryService
from stock_analysis.infrastructure.storage import Database
from stock_analysis.domains.indices import index_collection; from stock_analysis.domains.market import market_congestion


@pytest.fixture
def management(tmp_path):
    db=Database(tmp_path/'management.sqlite3');db.initialize()
    return CollectionManagement(db,IndustryService(db))


def now(value='2026-10-25T19:00:00'):
    return datetime.fromisoformat(value).replace(tzinfo=ZONE)


def complete(management, key, params, ref):
    with management.db.connection(write=True) as conn:
        if ref.startswith('sw_update_runs:'):
            conn.execute("UPDATE collection_operations SET status='complete',finished_at='2026-10-25T11:00:00+00:00' WHERE id=?",(int(ref.split(':')[1]),))
        else:
            conn.execute("UPDATE collection_operations SET status='complete',finished_at='2026-10-25T11:00:00+00:00' WHERE operation_id=?",(ref,))


def enable(management,key='indices',rule=None,params=None):
    result=management.configure(key,dict(enabled=True,local_time='18:00',rule=rule or {'frequency':'daily'},params=params or {'mode':'incremental'}))
    with management.db.connection(write=True) as conn:
        conn.execute('UPDATE collection_schedules SET updated_at=? WHERE task_key=?',('2026-01-01T00:00:00+00:00',key))
    return result


def test_reads_are_local_and_new_schedules_are_disabled(management,monkeypatch):
    monkeypatch.setattr(index_collection,'run_operation',lambda *a:pytest.fail('read started network'))
    result=management.read()
    assert {t['task_key'] for t in result['tasks']} == set(TASKS)
    assert all(not item['schedule']['enabled'] for item in result['tasks'])
    with management.db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM collection_schedules').fetchone()[0]==0
        assert conn.execute('SELECT count(*) FROM collection_batches').fetchone()[0]==0


def test_rule_calendar_month_end_year_and_coalescing():
    schedule=dict(local_time='18:00',enabled=True,rule=validate_rule({'frequency':'monthly','day':31}))
    assert due_slot(schedule,now('2027-03-01T12:00:00'))=='2027-02-28'
    assert due_slot(schedule,now('2027-01-01T12:00:00'))=='2026-12-31'
    assert next_check(schedule,now('2027-02-27T19:00:00')).startswith('2027-02-28T18:00')
    schedule['rule']=validate_rule({'frequency':'weekly','weekday':0})
    assert due_slot(schedule,now())=='2026-10-19'
    schedule['rule']=validate_rule({'frequency':'monthly','window':[5,15]})
    assert due_slot(schedule,now())=='2026-10-15'
    assert due_slot(schedule,now('2026-11-03T19:00:00'))=='2026-10-15'
    assert due_slot(schedule,now('2026-11-06T19:00:00'))=='2026-11-06'


@pytest.mark.parametrize('key,params',[
    ('arbitrary',{}),('indices',{'url':'https://bad'}),('indices',{'mode':'bogus'}),
    ('indices',{'providers':['bad']}),('indices',{'start':'2026-10-26','end':'2026-10-25'}),
    ('market_financial',{'target':'2026-01-01'}),('trade_snapshot',{'target':'2020Q5'}),
    ('market_congestion',{'start':'2020-01-01'}),
])
def test_whitelist_rejects_invalid_inputs(key,params):
    with pytest.raises(ValueError):validate_params(key,params)


def test_schedule_save_does_not_execute_and_legacy_congestion_preserved(management,monkeypatch):
    with management.db.connection(write=True) as conn:
        conn.execute('INSERT INTO collection_schedules(schedule_id,enabled,timezone,local_time,updated_at,task_key) VALUES(?,?,?,?,?,\'market_congestion\')',('market_congestion',1,'Asia/Shanghai','19:33','2026-10-17T00:00:00+00:00'))
    enable(management)
    with management.db.connection() as conn:
        row=dict(conn.execute("SELECT * FROM collection_schedules WHERE task_key='market_congestion'").fetchone())
        assert row['enabled']==1 and row['local_time']=='19:33'
        assert conn.execute('SELECT count(*) FROM collection_operations').fetchone()[0]==0
    called=[]
    monkeypatch.setattr(market_congestion,'initialized',lambda conn:True)
    def execute(key,params,ref):
        called.append(key);complete(management,key,params,ref)
    management.execute=execute
    management.tick(now=now())
    assert called.count('market_congestion')==1


def test_idempotent_ticks_and_restart_merge_missed_slots(management):
    enable(management)
    management.execute=lambda *args:complete(management,*args)
    management.tick(now=now('2026-10-20T19:00:00'))
    management.tick(now=now());management.tick(now=now())
    other=CollectionManagement(management.db,IndustryService(management.db))
    other.execute=lambda *args:complete(other,*args)
    other.tick(now=now())
    with management.db.connection() as conn:
        assert [r[0] for r in conn.execute('SELECT slot_key FROM collection_schedule_slots ORDER BY slot_key')]==['2026-10-20','2026-10-25']
        assert conn.execute('SELECT count(*) FROM collection_operations').fetchone()[0]==2


def test_cross_process_style_claim_and_disable_running(management):
    enable(management)
    started,finish=Event(),Event()
    def execute(*args):
        started.set();assert finish.wait(8);complete(management,*args)
    management.execute=execute
    worker=Thread(target=lambda:management.tick(now=now()));worker.start();assert started.wait(5)
    other=CollectionManagement(management.db,IndustryService(management.db))
    other.tick(now=now())
    with pytest.raises(ValueError):index_collection.start_operation(management.db,'incremental')
    management.configure('indices',{'enabled':False})
    with management.db.connection() as conn:
        assert conn.execute('SELECT status,cancel_requested FROM collection_operations').fetchone()[:]==('running',0)
    finish.set();worker.join(8);assert not worker.is_alive()
    management.tick(now=now('2026-10-26T19:00:00'))
    with management.db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM collection_schedule_slots').fetchone()[0]==1


def test_old_industry_and_new_entry_share_reservation(management,monkeypatch):
    with management.db.connection(write=True) as conn:
        ref=management.reserve(conn,'market_financial',{'mode':'incremental','target':'2026-06-30'})
    assert management.industry.start_refresh('cap_quarter','2026Q2')['running']
    with management.db.connection(write=True) as conn:
        with pytest.raises(ValueError):management.reserve(conn,'trade_snapshot',{'mode':'incremental','target':'2026Q2'})
    assert ref.startswith('sw_update_runs:')


def test_cooldown_utc_and_recovery_bounded(management):
    enable(management)
    management.execute=lambda *args:complete(management,*args)
    with management.db.connection(write=True) as conn:
        conn.execute('INSERT INTO collection_provider_state VALUES(?,?,?,?,?,?)',('legu','2026-10-25T12:00:00+00:00','denied',1,'{}','2026-10-25T10:00:00+00:00'))
    management.tick(now=now())
    with management.db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM collection_schedule_slots').fetchone()[0]==0
    management.tick(now=now('2026-10-25T21:00:00'))
    with management.db.connection(write=True) as conn:
        conn.execute("UPDATE collection_schedule_slots SET status='running'")
    management.recover();management.tick(now=now('2026-10-25T22:00:00'))
    with management.db.connection(write=True) as conn:
        assert conn.execute('SELECT attempts FROM collection_schedule_slots').fetchone()[0]==2
        conn.execute("UPDATE collection_schedule_slots SET status='running'")
    management.recover();management.tick(now=now('2026-10-25T23:00:00'))
    with management.db.connection() as conn:
        assert conn.execute('SELECT status,attempts FROM collection_schedule_slots').fetchone()[:]==('failed',2)


def test_new_schedule_incremental_only_and_publication_window_target(management):
    with pytest.raises(ValueError):enable(management,params={'mode':'full'})
    with pytest.raises(ValueError):enable(management,rule={'frequency':'monthly','window':[1,10]})
    enable(management,'market_financial',rule={'frequency':'monthly','window':[1,31]},params={'target':'2026-06-30'})
    management.execute=lambda *args:complete(management,*args)
    management.tick(now=now())
    # Merely executing successfully is not proof of complete coverage: next day checks again.
    management.tick(now=now('2026-10-26T19:00:00'))
    with management.db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM collection_schedule_slots').fetchone()[0]==2


def test_maintenance_registration_empty_and_actual_times(management):
    from stock_analysis.maintenance.maintenance import DESCRIPTIONS,table_time
    with management.db.connection() as conn:
        for name in ('collection_schedules','collection_schedule_slots'):
            assert DESCRIPTIONS[name][0]=='运行与配置'
            columns=[r['name'] for r in conn.execute('PRAGMA table_info('+name+')')]
            assert table_time(conn,name,columns)[0] is None
    enable(management)
    with management.db.connection() as conn:
        assert table_time(conn,'collection_schedules',['updated_at'])[0]

def _process_tick(path, barrier):
    db=Database(path)
    management=CollectionManagement(db,IndustryService(db))
    management.execute=lambda *args:complete(management,*args)
    barrier.wait(timeout=10)
    management.tick(now=now())


def test_real_cross_process_slot_claim(management):
    import multiprocessing
    enable(management)
    context=multiprocessing.get_context('spawn');barrier=context.Barrier(2)
    workers=[context.Process(target=_process_tick,args=(str(management.db.path),barrier)) for _ in range(2)]
    for worker in workers:worker.start()
    for worker in workers:worker.join(15)
    assert all(not worker.is_alive() and worker.exitcode==0 for worker in workers)
    with management.db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM collection_schedule_slots').fetchone()[0]==1
        assert conn.execute('SELECT count(*) FROM collection_operations').fetchone()[0]==1


def test_saved_configuration_waits_for_future_slot(management):
    management.configure('indices',{'enabled':True})
    with management.db.connection(write=True) as conn:
        conn.execute("UPDATE collection_schedules SET updated_at='2026-10-25T11:30:00+00:00'")
    management.execute=lambda *args:complete(management,*args)
    management.tick(now=now())
    with management.db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM collection_schedule_slots').fetchone()[0]==0
    management.tick(now=now('2026-10-26T19:00:00'))
    with management.db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM collection_schedule_slots').fetchone()[0]==1


def test_public_batches_keep_real_scheduled_trigger(management):
    from stock_analysis.collection.collection import begin_batch
    enable(management)
    def execute(key,params,ref):
        batch=begin_batch(management.db,ref,'index.test','legu',scope={'test':True})
        with management.db.connection() as conn:
            row=conn.execute('SELECT trigger_type,scope_json FROM collection_batches WHERE id=?',(batch,)).fetchone()
            assert row['trigger_type']=='scheduled' and 'schedule' in row['scope_json']
        complete(management,key,params,ref)
    management.execute=execute
    management.tick(now=now())


def test_scheduler_gate_and_shutdown_cancel_only_scheduled(management):
    from stock_analysis.collection.collection_management import Scheduler
    from stock_analysis.maintenance.database_restore import RequestGate
    gate=RequestGate();gate.blocked=True;entered=Event()
    def tick(**kwargs):entered.set()
    management.tick=tick
    scheduler=Scheduler(management,gate=gate);scheduler.start();scheduler.close()
    assert not entered.is_set() and gate.active==0
    # Shutdown cancellation applies to scheduler references, not other manual tasks.
    enable(management)
    with management.db.connection(write=True) as conn:
        ref=management.reserve(conn,'market_financial',{'mode':'incremental','target':'2026-06-30'})
        conn.execute("INSERT INTO collection_schedule_slots VALUES('indices','test','running',?,1,'2026-10-25',NULL,'2026-10-25',NULL)",(ref,))
        manual=index_collection.start_operation(management.db,'incremental',connection=conn)
    scheduler=Scheduler(management,gate=gate);scheduler.thread.start();scheduler.close()
    with management.db.connection() as conn:
        assert conn.execute('SELECT cancel_requested FROM collection_operations').fetchone()[0]==1
        assert conn.execute('SELECT cancel_requested FROM collection_operations WHERE operation_id=?',(manual,)).fetchone()[0]==0

def test_list_projects_small_summary_and_detail_keeps_full_evidence(management):
    import json
    with management.db.connection(write=True) as conn:
        ref=management.reserve(conn,'indices',{'mode':'incremental','providers':['legu']})
        result={'message':'ready','network_requests':7,'files':['x'*100000],'work_progress':{'total':10,'success':9,'tasks':{'large':'x'*100000}}}
        conn.execute("UPDATE collection_operations SET status='complete',result_json=? WHERE operation_id=?",(json.dumps(result),ref))
    payload=management.read()
    summary=next(row for row in payload['tasks'] if row['task_key']=='indices')['history'][0]['result']
    assert 'files' not in summary and 'tasks' not in summary['work_progress']
    assert summary['network_requests']==7 and summary['work_progress']['total']==10
    assert len(json.dumps(payload))<20000
    assert management.detail('indices',ref)['result']==result
    with pytest.raises(ValueError):management.detail('market_congestion',ref)


def test_index_history_counts_publications_and_empty_returns_without_rewriting_history(management):
    import json
    from stock_analysis.collection.collection import begin_batch, finish_batch
    with management.db.connection(write=True) as conn:
        older=index_collection.start_operation(management.db,'incremental',['sws'],connection=conn)
        conn.execute("UPDATE collection_operations SET status='complete',started_at='2026-10-25' WHERE operation_id=?",(older,))
        latest=index_collection.start_operation(management.db,'incremental',['sws','legu'],connection=conn)
        conn.execute("UPDATE collection_operations SET status='complete',started_at='2026-10-26' WHERE operation_id=?",(latest,))
    def publish(ref,provider,result,coverage='unknown'):
        batch=begin_batch(management.db,ref,'index.sw.analysis' if provider=='sws' else 'index.legu.pe',provider,
                          '2026-10-26','2026-10-26')
        finish_batch(management.db,batch,'complete',coverage,result=result)
    publish(older,'sws',dict(inserted=31,revised=1,unchanged=0,returned=32,
                           price_counts=dict(inserted=31,revised=0,unchanged=1)))
    for _ in range(2):
        publish(latest,'sws',dict(inserted=0,revised=0,unchanged=0,returned=0,
                                price_counts=dict(inserted=0,revised=0,unchanged=0)),'empty')
    publish(latest,'legu',dict(inserted=0,revised=2,unchanged=10,
                             price_counts=dict(inserted=0,revised=0,unchanged=4)))
    # A legacy batch without counters must not be interpreted as a zero response.
    publish(latest,'legu',dict(message='legacy'),'empty')
    payload=next(t for t in management.read()['tasks'] if t['task_key']=='indices')
    runs={r['operation_ref']:r['result'] for r in payload['history']}
    counts={(r['provider'],r['label']):r for r in runs[latest]['data_counts']}
    sw=counts[('sws','估值')]
    assert sw['written']==0 and sw['inserted']==0 and sw['revised']==0
    assert sw['empty_returns']==2 and sw['empty_start']==sw['empty_end']=='2026-10-26'
    assert counts[('sws','行情')]['written']==0
    assert counts[('legu','估值')]['revised']==2 and counts[('legu','估值')]['unchanged']==10
    assert counts[('legu','估值')]['empty_returns']==0
    assert counts[('legu','估值')]['unrecorded_segments']==1
    assert runs[older]['data_counts'][0]['inserted']==31
    assert 'data_counts' not in management.detail('indices',latest)['result']
    with management.db.connection() as conn:
        assert all('data_counts' not in json.loads(r[0]) for r in conn.execute('SELECT result_json FROM collection_operations'))


@pytest.mark.parametrize('key,dataset,provider,values,written', [
    ('market_financial','market.financial.income','eastmoney',dict(rows=12,new=2,revised=3,enriched=1,unchanged=6,published_count=12),6),
    ('trade_snapshot','market.trade_snapshot','eastmoney',dict(rows=12,inserted=2,revised=3,unchanged=7),5),
    ('market_congestion','market.legu.congestion.page','legu',dict(rows=30,inserted=1,revised=2,unchanged=27),3),
    ('sovereign_yields','bond_zh_us_rate','eastmoney',dict(total=11,inserted=1,revised=0,unchanged=10),1),
    ('shipping_weekly','shipping.ccfi','sse',dict(visible_points=26,published=0),0),
    ('shipping_monthly','shipping.mot_rates','mot',dict(published=8),8),
    ('shipping_research_import','shipping.excel','user_excel',dict(published=233),233),
])
def test_shared_publication_counts_for_registered_collectors(management,key,dataset,provider,values,written):
    from stock_analysis.collection.collection import begin_batch,finish_batch
    params={'mode':'full' if key=='market_congestion' else 'incremental','target':'2026-06-30' if key=='market_financial' else '2026Q2'}
    with management.db.connection(write=True) as conn:
        ref=management.reserve(conn,key,params)
        if ref.startswith('sw_update_runs:'):
            conn.execute("UPDATE collection_operations SET status='complete' WHERE id=?",(int(ref.split(':')[1]),))
        else:
            conn.execute("UPDATE collection_operations SET status='complete' WHERE operation_id=?",(ref,))
    batch=begin_batch(management.db,ref,dataset,provider,'2026-10-26','2026-10-26')
    finish_batch(management.db,batch,'complete','unknown',result=values)
    task=next(t for t in management.read()['tasks'] if t['task_key']==key)
    counts=task['history'][0]['result']['data_counts'][0]
    assert counts['provider']==provider and counts['written']==written
    assert counts['empty_returns']==0  # published=0 can be an unchanged response.
    if key.startswith('shipping_'):
        assert counts['inserted'] is None and counts['revised'] is None
    if key=='market_financial':
        assert counts['enriched']==1 and counts['unchanged']==6


def test_shared_counts_review_import_skip_empty_and_unknown(management):
    import json
    from stock_analysis.collection.collection import begin_batch,finish_batch
    # Review publishes against an old image batch, so only this run's channels count.
    with management.db.connection(write=True) as conn:
        ref=management.reserve(conn,'shipping_image_review',{'mode':'incremental','file':'review.json'})
        conn.execute("UPDATE collection_operations SET status='complete',result_json=? WHERE operation_id=?",
                     (json.dumps({'channels':[{'published':4}]}),ref))
    task=next(t for t in management.read()['tasks'] if t['task_key']=='shipping_image_review')
    assert task['history'][0]['result']['data_counts'][0]['written']==4
    with management.db.connection(write=True) as conn:
        skipped=management.reserve(conn,'trade_snapshot',{'mode':'incremental','target':'2026Q2'})
        conn.execute("UPDATE collection_operations SET status='complete',result_json=? WHERE id=?",
                     (json.dumps({'snapshot':{'skipped':True,'inserted':999,'revised':99}}),int(skipped.split(':')[1])))
    task=next(t for t in management.read()['tasks'] if t['task_key']=='trade_snapshot')
    counts=task['history'][0]['result']['data_counts'][0]
    assert counts['written']==0 and counts['inserted'] is None and counts['reused_segments']==1
    with management.db.connection(write=True) as conn:
        ref=management.reserve(conn,'sovereign_yields',{'mode':'incremental'})
        conn.execute("UPDATE collection_operations SET status='failed' WHERE operation_id=?",(ref,))
    batch=begin_batch(management.db,ref,'bond_zh_us_rate','eastmoney','2026-10-26','2026-10-26')
    finish_batch(management.db,batch,'failed','empty',result={'total':0,'empty':True})
    task=next(t for t in management.read()['tasks'] if t['task_key']=='sovereign_yields')
    counts=task['history'][0]['result']['data_counts'][0]
    assert counts['empty_returns']==1 and counts['written'] is None
    with management.db.connection(write=True) as conn:
        ref=management.reserve(conn,'shipping_weekly',{'mode':'incremental'})
        conn.execute("UPDATE collection_operations SET status='complete' WHERE operation_id=?",(ref,))
    task=next(t for t in management.read()['tasks'] if t['task_key']=='shipping_weekly')
    assert all(c['written'] is None and c['empty_returns']==0 for c in task['history'][0]['result']['data_counts'])


def test_shared_counts_all_segments_and_future_standard_counters(management):
    from stock_analysis.collection.collection import begin_batch,finish_batch
    with management.db.connection(write=True) as conn:
        ref=management.reserve(conn,'sovereign_yields',{'mode':'incremental'})
        conn.execute("UPDATE collection_operations SET status='complete' WHERE operation_id=?",(ref,))
    for _ in range(101):
        batch=begin_batch(management.db,ref,'future.standard_facts','eastmoney')
        finish_batch(management.db,batch,'complete','unknown',result=dict(inserted=1,revised=0,unchanged=0,returned=1))
    # An unknown discovery result must not fabricate a publication count.
    batch=begin_batch(management.db,ref,'bond_zh_us_rate','eastmoney')
    finish_batch(management.db,batch,'complete','unknown',result=dict(kind='target_discovery',rows=50,total=1000))
    batch=begin_batch(management.db,ref,'future.standard_facts','eastmoney')
    finish_batch(management.db,batch,'failed','unknown',result=dict(inserted=True,revised=-2,returned='0'))
    task=next(t for t in management.read()['tasks'] if t['task_key']=='sovereign_yields')
    assert len(task['batches'])==100
    count=task['history'][0]['result']['data_counts'][0]
    assert count['label']=='future.standard_facts' and count['segments']==102
    assert count['written']==101 and count['inserted']==101 and count['returned']==101 and count['empty_returns']==0
    assert count['unrecorded_segments']==1


def test_explicit_complete_target_stops_publication_window(management):
    from stock_analysis.collection.collection import begin_batch,finish_batch
    enable(management,'market_financial',rule={'frequency':'monthly','window':[1,31]},params={'target':'2026-06-30'})
    def execute(key,params,ref):
        for dataset in ('income','balance','cashflow','performance'):
            batch=begin_batch(management.db,ref,'market.financial.'+dataset,'eastmoney')
            finish_batch(management.db,batch,'complete','complete')
        complete(management,key,params,ref)
    management.execute=execute
    management.tick(now=now());management.tick(now=now('2026-10-26T19:00:00'))
    with management.db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM collection_schedule_slots').fetchone()[0]==1

def test_bond_provider_and_independent_history_do_not_inherit_combined_failure(management):
    import json
    with management.db.connection(write=True) as conn:
        combined=index_collection.start_operation(management.db,'incremental',['legu'],datasets=['bond_zh_us_rate'],connection=conn)
        conn.execute("UPDATE collection_operations SET status='failed',error=?,finished_at=? WHERE operation_id=?",(json.dumps({'legu':'HTTP 429'}),'2026-10-25',combined))
        standalone=management.reserve(conn,'sovereign_yields',{'mode':'incremental'})
        conn.execute("UPDATE collection_operations SET status='complete',finished_at='2026-10-25' WHERE operation_id=?",(standalone,))
    rows={t['task_key']:t for t in management.read()['tasks']}
    assert rows['sovereign_yields']['providers']==['eastmoney']
    assert [r['operation_ref'] for r in rows['sovereign_yields']['history']]==[standalone]
    assert [r['operation_ref'] for r in rows['indices']['history']]==[combined]
    with management.db.connection() as conn:
        scope=json.loads(conn.execute('SELECT scope_json FROM collection_operations WHERE operation_id=?',(standalone,)).fetchone()[0])
        assert scope['providers']==[] and scope['datasets']==['bond_zh_us_rate']
    with pytest.raises(ValueError):management.detail('sovereign_yields',combined)


def test_bond_schedule_checks_eastmoney_cooldown_not_legu(management):
    enable(management,'sovereign_yields')
    management.execute=lambda *args:complete(management,*args)
    with management.db.connection(write=True) as conn:
        conn.execute('INSERT INTO collection_provider_state VALUES(?,?,?,?,?,?)',('legu','2026-10-27T12:00:00+00:00','denied',1,'{}','2026-10-25'))
    management.tick(now=now())
    with management.db.connection(write=True) as conn:
        assert conn.execute('SELECT count(*) FROM collection_schedule_slots').fetchone()[0]==1
        conn.execute('INSERT INTO collection_provider_state VALUES(?,?,?,?,?,?)',('eastmoney','2026-10-27T12:00:00+00:00','denied',1,'{}','2026-10-25'))
    management.tick(now=now('2026-10-26T19:00:00'))
    with management.db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM collection_schedule_slots').fetchone()[0]==1

@pytest.mark.parametrize('clock',['2026-10-25T22:00:00','2026-10-26T09:00:00'])
def test_previously_enabled_daily_schedule_catches_latest_due_slot(management,clock):
    enable(management)
    management.execute=lambda *args:complete(management,*args)
    management.tick(now=now(clock));management.tick(now=now(clock))
    with management.db.connection() as conn:
        assert [row[0] for row in conn.execute('SELECT slot_key FROM collection_schedule_slots')]==['2026-10-25']
        assert conn.execute('SELECT count(*) FROM collection_operations').fetchone()[0]==1


def test_congestion_next_morning_catches_yesterday_and_deduplicates(management,monkeypatch):
    with management.db.connection(write=True) as conn:
        conn.execute('INSERT INTO collection_schedules(schedule_id,enabled,timezone,local_time,updated_at,task_key) VALUES(?,?,?,?,?,\'market_congestion\')',('market_congestion',1,'Asia/Shanghai','18:00','2026-01-01T00:00:00+00:00'))
    monkeypatch.setattr(market_congestion,'initialized',lambda conn:True)
    calls=[]
    def execute(db,operation):
        calls.append(operation)
        with db.connection(write=True) as conn:
            conn.execute("UPDATE collection_operations SET status='complete' WHERE operation_id=?",(operation,))
    market_congestion.scheduled_tick(management.db,now=now('2026-10-26T09:00:00'),execute=execute)
    assert len(calls)==1
    market_congestion.scheduled_tick(management.db,now=now('2026-10-26T09:30:00'),execute=execute)
    assert len(calls)==1
    market_congestion.scheduled_tick(management.db,now=now('2026-10-26T22:00:00'),execute=execute)
    assert len(calls)==2
    with management.db.connection() as conn:
        assert [r[0] for r in conn.execute("SELECT json_extract(scope_json,'$.scheduled_date') FROM collection_operations ORDER BY started_at")]==['2026-10-25','2026-10-26']


@pytest.mark.parametrize('stamp,expected', [
    ('2026-03-30T19:00:00', '2025-11-07'),
    ('2026-03-31T17:59:00', '2025-11-07'),
    ('2026-03-31T18:00:00', '2026-03-31'),
    ('2026-04-30T19:00:00', '2026-04-30'),
    ('2026-05-07T19:00:00', '2026-05-07'),
    ('2026-05-08T19:00:00', '2026-05-07'),
    ('2026-08-01T19:00:00', '2026-08-01'),
    ('2026-09-07T19:00:00', '2026-09-07'),
    ('2026-10-01T19:00:00', '2026-10-01'),
    ('2026-11-07T19:00:00', '2026-11-07'),
    ('2027-01-01T19:00:00', '2026-11-07'),
])
def test_financial_season_daily_boundaries_and_coalescing(stamp, expected):
    schedule=dict(enabled=True,local_time='18:00',rule=validate_rule({'frequency':'financial_season'}))
    assert due_slot(schedule,now(stamp)) == expected
    assert due_slot(schedule,now(stamp).astimezone(timezone.utc)) == expected


def test_financial_season_next_check_zero_offsets_overlap_and_cross_year():
    schedule=dict(enabled=True,local_time='18:00',rule=validate_rule({'frequency':'financial_season'}))
    assert next_check(schedule,now('2026-05-07T19:00:00')).startswith('2026-08-01T18:00')
    assert next_check(schedule,now('2026-11-07T19:00:00')).startswith('2027-03-31T18:00')
    schedule['rule']=validate_rule(dict(frequency='financial_season',days_before=0,days_after=0))
    assert due_slot(schedule,now('2026-04-29T19:00:00'))=='2025-10-31'
    assert next_check(schedule,now('2026-04-30T19:00:00')).startswith('2026-08-31T18:00')
    schedule['rule']=validate_rule(dict(frequency='financial_season',days_before=90,days_after=90))
    assert due_slot(schedule,now('2027-01-15T19:00:00'))=='2027-01-15'
    assert due_slot(schedule,now('2026-09-01T19:00:00'))=='2026-09-01'
    schedule['enabled']=False
    assert next_check(schedule,now()) is None


@pytest.mark.parametrize('rule', [
    dict(frequency='daily',days_before=30), dict(frequency='monthly',days_after=7),
    dict(frequency='financial_season',day=1), dict(frequency='financial_season',window=[1,5]),
    dict(frequency='financial_season',weekday=1), dict(frequency='financial_season',days_before=True),
    dict(frequency='financial_season',days_before=-1), dict(frequency='financial_season',days_after=91),
    dict(frequency='financial_season',days_after=1.5), dict(frequency='financial_season',days_before='30'),
])
def test_financial_season_rejects_invalid_or_mixed_fields(rule):
    with pytest.raises(ValueError): validate_rule(rule)


@pytest.mark.parametrize('key,params', [
    ('indices', dict(mode='incremental',providers=['csindex'])),
    ('market_financial',dict(mode='incremental',target='2026-06-30')),
    ('trade_snapshot',dict(mode='incremental',target='2026Q2')),
    ('shipping_weekly',dict(mode='incremental')),
    ('shipping_monthly',dict(mode='incremental')),
    ('sovereign_yields',dict(mode='incremental')),
    ('market_congestion',dict(mode='incremental')),
])
def test_financial_season_shared_save_and_unchanged_execution(management,monkeypatch,key,params):
    monkeypatch.setattr(market_congestion,'initialized',lambda conn:True)
    saved=enable(management,key,rule={'frequency':'financial_season'},params=params)
    assert saved['rule']==dict(frequency='financial_season',days_before=30,days_after=7)
    with management.db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM collection_operations').fetchone()[0]==0
    calls=[]
    def execute(task, actual, ref):
        calls.append((task,actual));complete(management,task,actual,ref)
    management.execute=execute
    management.tick(now=now('2026-03-30T19:00:00'))
    assert calls==[]  # Last year's slot precedes save: no backfill.
    management.tick(now=now('2026-04-01T19:00:00'))
    management.tick(now=now('2026-04-01T20:00:00'))
    management.tick(now=now('2026-04-02T19:00:00'))
    assert calls==[(key,params),(key,params)]
    management.tick(now=now('2026-06-15T19:00:00'))
    management.tick(now=now('2026-07-15T19:00:00'))
    assert calls==[(key,params)]*3  # One final missed May 7 slot, no daily slots outside windows.
    with management.db.connection() as conn:
        assert [r[0] for r in conn.execute('SELECT slot_key FROM collection_schedule_slots ORDER BY slot_key')]==['2026-04-01','2026-04-02','2026-05-07']
