"""Control consolidation: actual legacy fixtures, no external requests or daily writes."""
import json
import sqlite3
from datetime import datetime
from types import SimpleNamespace

import pytest

from stock_analysis.infrastructure import storage
from stock_analysis.infrastructure.storage import Database, SyncKey, utc_now
from stock_analysis.collection.collection import begin_batch, finish_batch
from stock_analysis.collection.collection_management import CollectionManagement, ZONE
from stock_analysis.collection.execution_context import execution_context
from stock_analysis.domains.stocks.collection_operations import StockCollections
from stock_analysis.domains.stocks.collection_plan import selected,versions
from stock_analysis.domains.stocks.stock_collection import begin_stock_batch


def legacy(tmp_path, monkeypatch):
    db=Database(tmp_path/'old.sqlite3')
    with monkeypatch.context() as patch:
        patch.setattr(storage,'MIGRATIONS',storage.MIGRATIONS[:43])
        db.initialize()
    return db


def raw(db):
    conn=sqlite3.connect(db.path)
    conn.row_factory=sqlite3.Row
    return conn


def test_upgrade_preserves_ids_diagnostics_unknown_parents_and_provider_state(tmp_path,monkeypatch):
    db=legacy(tmp_path,monkeypatch)
    with raw(db) as c:
        c.execute("INSERT INTO index_collection_operations(operation_id,mode,scope_json,status,started_at,result_json) VALUES('op','full','{}','complete','old',?)",
            (json.dumps(dict(results=[dict(batch_id=41,inserted=2,coverage=dict(missing=['2020-01-01']))],
                work_progress=dict(tasks={'local-skip':dict(status='skipped',message='已是最新，无来源请求')}))),))
        c.execute("INSERT INTO collection_batches(id,operation_id,domain,dataset,source_system,trigger_type,status,started_at,result_json) VALUES(41,'op','index','index.cs.history','csindex','manual','complete','old',?)",
            (json.dumps(dict(inserted=2,coverage=dict(missing=['2020-01-01']))),))
        c.execute("INSERT INTO collection_batches(id,operation_id,domain,dataset,source_system,trigger_type,status,started_at) VALUES(42,'old-cli','index','calendar','csindex','manual','failed','old')")
        c.execute("INSERT INTO collection_sources(id,batch_id,source_system,dataset,request_key,attempt_no,request_json,obtained_at) VALUES(12,41,'csindex','index.cs.history','k',1,'{}','old')")
        c.execute("INSERT INTO collection_provider_state VALUES('csindex','2099-01-01','denied',2,'{\"starts\":[1,2]}','old')")
        c.execute("INSERT INTO sw_update_runs(id,action,target,recheck,started_at,status,result_json) VALUES(9,'financial_period','2026-09-30',0,'old','complete','{\"unique_business_result\":7}')")
    db.initialize()
    with db.connection() as c:
        assert c.execute('PRAGMA foreign_key_check').fetchall()==[]
        assert c.execute('SELECT batch_id FROM collection_sources WHERE id=12').fetchone()[0]==41
        assert c.execute('SELECT operation_id FROM collection_batches WHERE id=41').fetchone()[0]=='op'
        assert tuple(c.execute('SELECT operation_id,legacy_operation_ref FROM collection_batches WHERE id=42').fetchone())==(None,'old-cli')
        result=json.loads(c.execute("SELECT result_json FROM collection_operations WHERE operation_id='op'").fetchone()[0])
        assert 'results' not in result and 'legacy_unmapped_results' not in result
        assert result['legacy_task_states']=={'local-skip':dict(status='skipped',message='已是最新，无来源请求')}
        assert 'tasks' not in result['work_progress']
        assert json.loads(c.execute('SELECT result_json FROM collection_batches WHERE id=41').fetchone()[0])['coverage']==dict(missing=['2020-01-01'])
        assert tuple(c.execute("SELECT cooldown_until,denials,budget_json FROM collection_provider_state WHERE source_system='csindex'").fetchone())==('2099-01-01',2,'{"starts":[1,2]}')
        assert c.execute("SELECT id FROM collection_operations WHERE operation_id='sw_update_runs:9'").fetchone()[0]==9
        names={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert not names & {'collection_items','stock_collection_items','stock_collection_operations','index_collection_operations','collection_schedule_rules','sw_update_runs'}
    db.initialize()  # Re-initialization must not remigrate or duplicate history.
    with db.connection() as c:
        assert c.execute('SELECT count(*) FROM collection_operations').fetchone()[0]==2


def test_upgrade_schedule_deleted_and_disabled_are_distinct(tmp_path,monkeypatch):
    db=legacy(tmp_path,monkeypatch)
    with raw(db) as c:
        c.executemany('INSERT INTO collection_schedules VALUES(?,?,?,?,?)',[
            ('market_congestion',1,'Asia/Shanghai','18:00','old'),('stocks:1:deleted',0,'Asia/Shanghai','19:00','old'),
            ('stocks:1:disabled',0,'Asia/Shanghai','20:00','old')])
        c.execute("INSERT INTO collection_schedule_rules VALUES('stocks:1:disabled','{\"frequency\":\"weekly\",\"weekday\":2}','{\"group_id\":1,\"mode\":\"incremental\"}','rule-old')")
        c.execute("INSERT INTO collection_schedule_slots VALUES('stocks:1:deleted','day','complete',NULL,1,'old','old','old',NULL)")
    db.initialize()
    with db.connection() as c:
        assert tuple(c.execute("SELECT task_key,deleted,enabled FROM collection_schedules WHERE schedule_id='stocks:1:deleted'").fetchone())==('stocks',1,0)
        assert tuple(c.execute("SELECT deleted,enabled,updated_at,rule_updated_at FROM collection_schedules WHERE schedule_id='stocks:1:disabled'").fetchone())==(0,0,'old','rule-old')
        assert c.execute('PRAGMA foreign_key_check').fetchall()==[]


def test_conflicting_legacy_identity_rolls_back_entire_migration(tmp_path,monkeypatch):
    db=legacy(tmp_path,monkeypatch)
    with raw(db) as c:
        c.execute("INSERT INTO index_collection_operations(operation_id,mode,scope_json,status,started_at) VALUES('collision','full','{}','complete','old')")
        c.execute("INSERT INTO stock_collection_operations(id,mode,trigger_type,scope_json,status,started_at) VALUES('collision','full','initial','{}','complete','old')")
    with pytest.raises(sqlite3.IntegrityError):
        db.initialize()
    with raw(db) as c:
        assert c.execute('PRAGMA user_version').fetchone()[0]==43
        assert c.execute('SELECT count(*) FROM stock_collection_operations').fetchone()[0]==1
        assert c.execute("SELECT count(*) FROM sqlite_master WHERE name='collection_operations'").fetchone()[0]==0


def test_legacy_unstarted_work_retains_full_mode_without_inventing_old_batches(tmp_path,monkeypatch):
    db=legacy(tmp_path,monkeypatch)
    with raw(db) as c:
        c.execute("INSERT INTO stock_collection_operations(id,mode,trigger_type,scope_json,status,started_at) VALUES('old-stock','full','manual','{}','failed','old')")
        c.execute("INSERT INTO stock_collection_items(operation_id,code,dataset,mode,status) VALUES('old-stock','601919','prices','full','pending')")
    db.initialize()
    with db.connection(write=True) as c:
        assert c.execute('SELECT count(*) FROM collection_batches').fetchone()[0]==0
        ref=StockCollections(db,None).reserve(c,dict(codes=['601919'],datasets=['prices'],mode='resume'))
        assert {r[0] for r in c.execute('SELECT mode FROM collection_batches WHERE operation_id=?',(ref,))}=={'full'}


def test_multiple_nonstock_schedules_independent_slots_and_deletion(tmp_path,monkeypatch):
    db=Database(tmp_path/'new.sqlite3');db.initialize()
    m=CollectionManagement(db,None)
    ids=[]
    for hour,provider in [('18:00','csindex'),('19:00','sws')]:
        ids.append(m.configure('indices',dict(schedule_key=None,enabled=True,local_time=hour,rule=dict(frequency='daily'),params=dict(mode='incremental',providers=[provider])))['schedule_key'])
    with db.connection(write=True) as c:
        c.execute("UPDATE collection_schedules SET updated_at='2020-01-01T00:00:00+00:00'")
        assert c.execute('SELECT count(*) FROM collection_operations').fetchone()[0]==0
    executed=[]
    def execute(key,params,ref):
        executed.append((key,params,ref))
        with db.connection(write=True) as c:
            c.execute("UPDATE collection_operations SET status='complete',finished_at=? WHERE operation_id=?",(utc_now(),ref))
    monkeypatch.setattr(m,'execute',execute)
    now=datetime(2026,10,27,20,tzinfo=ZONE)
    m.tick(now=now);m.tick(now=now)
    assert len(executed)==2
    assert {tuple(x[1]['providers']) for x in executed}=={('csindex',),('sws',)}
    with db.connection() as c:
        assert {r[0] for r in c.execute('SELECT schedule_id FROM collection_schedule_slots')}==set(ids)
        assert {r[0] for r in c.execute('SELECT trigger_type FROM collection_operations')}=={'scheduled'}
    m.configure('indices',dict(action='delete',schedule_key=ids[0]))
    m.tick(now=datetime(2026,10,28,20,tzinfo=ZONE))
    assert len(executed)==3 and executed[-1][1]['providers']==['sws']


def test_pending_segments_have_no_fake_start_and_resume_only_failed_source(tmp_path,monkeypatch):
    db=Database(tmp_path/'stock.sqlite3');db.initialize()
    stock=StockCollections(db,SimpleNamespace())
    with db.connection(write=True) as c:
        ref=stock.reserve(c,dict(codes=['601919'],datasets=['prices'],mode='full'))
        assert c.execute('SELECT count(*) FROM collection_batches WHERE started_at IS NULL AND finished_at IS NULL').fetchone()[0]==2
    calls=[]
    failed=[True]
    def collect(code,dataset,full,collector):
        for detail in ('prices_raw','prices_adjusted'):
            if not selected(detail):
                continue
            calls.append(detail)
            run=db.start_sync(SyncKey(db.ensure_instrument(code),detail,'tencent'),parser_version='test',methodology_version='test')
            parser,definition=versions(detail)
            batch=begin_stock_batch(db,run,'tencent',detail,code,scope=dict(source_interface='fqkline/get'),parser_version=parser,definition_version=definition)
            finish_batch(db,batch,'failed' if detail=='prices_adjusted' and failed[0] else 'complete')
    monkeypatch.setattr(stock,'collect',collect)
    stock.run(ref);stock.run(ref)
    assert calls==['prices_raw','prices_adjusted']
    with db.connection(write=True) as c:
        retry=stock.reserve(c,dict(codes=['601919'],datasets=['prices'],mode='resume'))
        assert [r[0] for r in c.execute('SELECT status FROM collection_batches WHERE coalesce(operation_id,legacy_operation_ref)=? ORDER BY sequence',(retry,))]==['reused','pending']
    failed[0]=False
    stock.run(retry)
    assert calls==['prices_raw','prices_adjusted','prices_adjusted']
    assert stock.detail(retry)['status']=='complete'


def test_new_progress_json_does_not_store_task_array(tmp_path):
    from stock_analysis.domains.indices.index_collection import start_operation, OperationProgress
    db=Database(tmp_path/'new.sqlite3');db.initialize()
    ref=start_operation(db,providers=['csindex'])
    tracker=OperationProgress(db,ref)
    for i in range(100):
        tracker.register('csindex',str(i),'任务'+str(i))
    with db.connection() as c:
        value=c.execute('SELECT result_json FROM collection_operations WHERE operation_id=?',(ref,)).fetchone()[0]
    assert len(value.encode())<4096
    assert 'tasks' not in json.loads(value)['work_progress']
    assert json.loads(value)['work_progress']['pending']==100


def test_real_financial_service_claims_four_planned_segments_without_child_batches(tmp_path):
    from stock_analysis.domains.financials.fundamental_service import FundamentalService
    db=Database(tmp_path/'financial.sqlite3');db.initialize()
    requested=[]
    def fetch(code,kind,num,page):
        requested.append(kind)
        rows=[{'item_title':'营业收入','item_value':'10'},{'item_title':'归属于母公司所有者的净利润','item_value':'2'}] if kind=='lrb' else []
        return {'records':{'20260930':{'publish_date':'20261001','data':rows}},'total':1}
    fundamentals=FundamentalService(db,tmp_path/'legacy',fetch_page=fetch)
    data=SimpleNamespace(p4_services=lambda:(fundamentals,None,None),services=lambda:(None,None),_add_sqlite_missing_name=lambda *a,**k:None)
    stock=StockCollections(db,data)
    with db.connection(write=True) as c:
        ref=stock.reserve(c,dict(codes=['601919'],datasets=['financial']))
        planned=[r[0] for r in c.execute('SELECT id FROM collection_batches ORDER BY id')]
    stock.run(ref)
    with db.connection() as c:
        batches=c.execute('SELECT id,operation_id,status,sync_run_id FROM collection_batches ORDER BY id').fetchall()
        assert [r['id'] for r in batches]==planned
        assert all(r['operation_id']==ref and r['status']=='complete' and r['sync_run_id'] for r in batches)
        assert c.execute('SELECT count(*) FROM collection_sources').fetchone()[0]==0
    assert set(requested)=={'lrb','fzb','llb','gjzb'}
    assert stock.detail(ref)['status']=='complete'


def test_congestion_daily_reuse_uses_shared_schedule_slot(tmp_path,monkeypatch):
    from stock_analysis.collection.collection import archive_source,parsed_source
    from stock_analysis.domains.market import market_congestion
    db=Database(tmp_path/'congestion.sqlite3');db.initialize()
    monkeypatch.setattr(market_congestion,'initialized',lambda conn:True)
    m=CollectionManagement(db,None)
    m.configure('market_congestion',dict(enabled=True,local_time='18:00',rule=dict(frequency='daily'),params=dict(mode='incremental')))
    with db.connection(write=True) as c:
        c.execute("UPDATE collection_schedules SET updated_at='2020-01-01T00:00:00+00:00'")
    batch=begin_batch(db,'legacy-manual',market_congestion.DATASETS['html_recent_table'],'legu')
    source=archive_source(db,batch,'legu',market_congestion.DATASETS['html_recent_table'],{},1,obtained_at='2026-10-27T10:30:00+00:00')
    parsed_source(db,source);finish_batch(db,batch,'complete')
    monkeypatch.setattr(m,'execute',lambda *a:pytest.fail('Already checked in this slot'))
    m.tick(now=datetime(2026,10,27,19,tzinfo=ZONE));m.tick(now=datetime(2026,10,27,19,tzinfo=ZONE))
    with db.connection() as c:
        assert c.execute('SELECT count(*) FROM collection_schedule_slots').fetchone()[0]==1
        row=c.execute('SELECT status,trigger_type,result_json FROM collection_operations').fetchone()
        assert tuple(row)[:2]==('complete','scheduled')
        assert json.loads(row['result_json'])['skipped'] is True
        assert c.execute('SELECT count(*) FROM collection_sources').fetchone()[0]==1


def test_resume_rejects_segments_from_an_old_definition(tmp_path,monkeypatch):
    from stock_analysis.domains.stocks import price_service
    db=Database(tmp_path/'definitions.sqlite3');db.initialize();stock=StockCollections(db,None)
    with db.connection(write=True) as c:
        ref=stock.reserve(c,dict(codes=['601919'],datasets=['prices']))
        c.execute("UPDATE collection_batches SET status='skipped',finished_at=? WHERE operation_id=?",(utc_now(),ref))
        c.execute("UPDATE collection_operations SET status='complete' WHERE operation_id=?",(ref,))
    monkeypatch.setattr(price_service,'COLLECTION_DEFINITION_VERSION','changed-definition')
    with db.connection(write=True) as c:
        retry=stock.reserve(c,dict(codes=['601919'],datasets=['prices'],mode='resume'))
        values=c.execute('SELECT status,definition_version FROM collection_batches WHERE operation_id=?',(retry,)).fetchall()
        assert all(tuple(row)==('pending','changed-definition') for row in values)


def test_resume_does_not_expand_an_incremental_segment_to_full(tmp_path,monkeypatch):
    db=Database(tmp_path/'modes.sqlite3');db.initialize();stock=StockCollections(db,None)
    with db.connection(write=True) as c:
        ref=stock.reserve(c,dict(codes=['601919'],datasets=['prices'],mode='full'))
        c.execute("UPDATE collection_batches SET mode='incremental' WHERE dataset='stock.prices_raw'")
    calls=[]
    monkeypatch.setattr(stock,'collect',lambda code,dataset,full,collector:calls.append((full,selected('prices_raw'),selected('prices_adjusted'))))
    stock.run(ref)
    assert calls==[(False,True,False),(True,False,True)]
    assert stock.detail(ref)['status']=='complete'


def test_coverage_aggregation_is_not_cut_off_by_the_hundred_batch_display_limit(tmp_path):
    from stock_analysis.domains.indices.index_collection import start_operation
    db=Database(tmp_path/'coverage.sqlite3');db.initialize();m=CollectionManagement(db,None)
    ref=start_operation(db,providers=['csindex'])
    for index in range(101):
        batch=begin_batch(db,ref,'index.cs.history','csindex')
        finish_batch(db,batch,'complete','partial' if index==0 else 'complete')
    task=next(t for t in m.read()['tasks'] if t['task_key']=='indices')
    assert len(task['batches'])==100
    assert task['coverage']=='partial'
    page=m.detail('indices',ref)['batch_page']
    assert page['total']==101 and len(page['items'])==100
    assert page['next_url'] and page['next_before_batch_id']
    tail=m.detail('indices',ref,page['next_before_batch_id'])['batch_page']
    assert len(tail['items'])==1 and tail['items'][0]['coverage_status']=='partial'
    from stock_analysis.collection.evidence import batch_evidence
    with db.connection(write=True) as c:
        c.execute('UPDATE collection_batches SET result_json=? WHERE id=?',(json.dumps({'coverage_diagnostic':['missing']}),tail['items'][0]['id']))
    evidence=batch_evidence(db,tail['items'][0]['id'])
    assert evidence['batch']['result']=={'coverage_diagnostic':['missing']}
    assert evidence['sources']==[]


def test_caught_reservation_conflict_cannot_commit_an_orphan_operation(tmp_path):
    db=Database(tmp_path/'reservation.sqlite3');db.initialize();stock=StockCollections(db,None)
    with db.connection(write=True) as c:
        stock.reserve(c,dict(codes=['601919'],datasets=['financial']))
    # Scheduler catches a busy range inside its transaction and retries later.
    # An earlier non-conflicting stock must not leave half of the new run behind.
    with db.connection(write=True) as c:
        with pytest.raises(ValueError,match='已有运行'):
            stock.reserve(c,dict(codes=['605499','601919'],datasets=['financial']))
    with db.connection() as c:
        assert c.execute('SELECT count(*) FROM collection_operations').fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM collection_batches').fetchone()[0]==4
        assert {r[0] for r in c.execute('SELECT code FROM collection_batches')}=={'601919'}
