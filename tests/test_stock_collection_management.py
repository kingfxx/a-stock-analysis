"""Stock trigger, persistence and shared scheduling contracts on isolated databases."""
import json
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

import pytest
import requests

from stock_analysis.infrastructure.storage import Database, SyncKey, SyncResult, utc_now
from stock_analysis.infrastructure.app_settings import AppSettings
from stock_analysis.domains.stocks.data_service import StockDataService
from stock_analysis.domains.stocks.collection_operations import StockCollections, validate_params
from stock_analysis.domains.stocks import collection_schedules
from stock_analysis.collection.collection import begin_batch, finish_batch, RequestCollector, PacingPolicy, parsed_source
from stock_analysis.collection.execution_context import execution_context, current_execution
from stock_analysis.collection.collection_management import CollectionManagement, Scheduler


@pytest.fixture
def service(tmp_path):
    db = Database(tmp_path/'stock.sqlite3'); db.initialize()
    data = StockDataService(AppSettings(DATABASE_PATH=db.path, CACHE=tmp_path/'fundamentals',
        CHIP_CACHE=tmp_path/'chips', VALUATION_CACHE=tmp_path/'valuation'))
    return StockCollections(db, data)


def reserve(service, **params):
    with service.db.connection(write=True) as conn:
        return service.reserve(conn, params)


def group(service, name, codes):
    for code in codes:
        service.db.ensure_instrument(code)
    with service.db.connection(write=True) as conn:
        identity = conn.execute('INSERT INTO stock_groups(name,position) VALUES(?,0)', (name,)).lastrowid
        conn.executemany('INSERT INTO stock_group_members(group_id,instrument_id,position) SELECT ?,id,0 FROM instruments WHERE code=?', [(identity,c) for c in codes])
    return identity


def test_existing_and_no_data_stocks_do_not_initialize(service, monkeypatch):
    identity = service.db.ensure_instrument('601919')
    run = service.db.start_sync(SyncKey(identity,'financial:lrb','test'), parser_version='test', methodology_version='test')
    service.db.complete_sync(run, SyncResult(0, no_data=True), lambda conn: None)
    monkeypatch.setattr(service, 'run', lambda *_: pytest.fail('unexpected collection'))
    assert service.start(dict(codes=['601919']), initial=True)['status'] == 'existing'
    assert not service.page_status('601919')['needs_initial']
    for full in (False, True):
        _, payload, _ = service.data.page_payload('601919', full)
        assert not any(payload['loading'].values()) and not payload['price_needs_update']
        assert not payload['refresh_requested']


def test_initial_once_and_failed_visit_never_retries(service, monkeypatch):
    monkeypatch.setattr('stock_analysis.domains.stocks.collection_operations.Thread', lambda **_: SimpleNamespace(start=lambda: None))
    first = service.start(dict(codes=['601919']), initial=True)
    second = service.start(dict(codes=['601919']), initial=True)
    assert first == second
    with service.db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM collection_operations').fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM collection_batches').fetchone()[0] == 15
        assert conn.execute('SELECT mode FROM collection_operations').fetchone()[0] == 'full'
    service.recover()
    assert service.start(dict(codes=['601919']), initial=True)['status'] == 'failed'
    assert not service.page_status('601919')['needs_initial']


def test_persistent_conflict_snapshot_partial_failure_and_resume(service, monkeypatch):
    identity = group(service, 'holdings', ['601919','605499'])
    ref = reserve(service, group_id=identity, datasets=['financial','dividends'])
    with pytest.raises(ValueError, match='已有运行'):
        reserve(service, codes=['601919'], datasets=['financial'])
    with service.db.connection(write=True) as conn:
        conn.execute('DELETE FROM stock_group_members WHERE group_id=?', (identity,))
    calls = []
    def collect(code, dataset, full, collector):
        calls.append((code,dataset,full))
        if code == '605499' and dataset == 'dividends':
            raise ValueError('simulated failure')
    monkeypatch.setattr(service, 'collect', collect)
    monkeypatch.setattr(service.data, '_add_sqlite_missing_name', lambda *a,**k: None)
    service.run(ref)
    result = service.detail(ref)
    assert result['status'] == 'failed' and len(calls) == 4
    assert sum(x['status'] == 'complete' for x in result['items']) == 3
    retry = reserve(service, codes=['601919','605499'], datasets=['financial','dividends'], mode='resume')
    assert sum(x['status'] == 'reused' for x in service.detail(retry)['items']) == 3


def test_metadata_only_parent_evidence_and_public_pacing(service):
    response = requests.Response(); response.status_code = 200; response._content = b'{"ok":true}'
    class Session:
        def get(self,*args,**kwargs): return response
        def close(self): pass
    collector = RequestCollector(service.db, session=Session(), policy=PacingPolicy(max_attempts=1))
    ref = reserve(service, codes=['601919'], datasets=['financing'])
    item = service.detail(ref)['items'][0]['id']
    with execution_context(scope=dict(parent_operation=ref,parent_item=item,batch_pacing=True), trigger='scheduled', cancelled=lambda:False,collector=collector,batch_pacing=True):
        batch = begin_batch(service.db, 'sync_runs:fake', 'stock.financing', 'eastmoney', domain='stock')
        _, source = collector.get(batch,'eastmoney','stock.financing','https://example.test/data',evidence_mode='metadata_only',stock_fast=True)
        parsed_source(service.db,source)
        finish_batch(service.db,batch,'complete')
    with service.db.connection() as conn:
        b = conn.execute('SELECT * FROM collection_batches WHERE id=?',(batch,)).fetchone()
        assert b['trigger_type'] == 'scheduled' and json.loads(b['scope_json'])['parent_item'] == item
        s = conn.execute('SELECT * FROM collection_sources WHERE id=?',(source,)).fetchone()
        assert s['file_path'] is None and s['raw_sha256'] is None and s['raw_bytes'] is None
    assert not list((service.db.path.parent/'collection_sources').rglob('*.raw.gz'))
    assert current_execution.get() is None


def test_group_schedules_reuse_same_slot_and_no_save_execution(service, monkeypatch):
    ids = [group(service, name, ['601919']) for name in ('holdings','watch')]
    calls = []
    monkeypatch.setattr(service,'collect',lambda *args: calls.append(args[:2]))
    for identity in ids:
        collection_schedules.configure(service.db,dict(enabled=True, local_time='18:00',rule=dict(frequency='daily'),
            params=dict(group_id=identity,datasets=['dividends'])))
    assert calls == []
    # Configure before a fixed slot; operation completion after that slot is reusable.
    with service.db.connection(write=True) as conn:
        conn.execute("UPDATE collection_schedules SET updated_at='2026-01-01T00:00:00+00:00' WHERE schedule_id LIKE 'stocks:%'")
    now = datetime(2026,1,2,19,tzinfo=timezone(timedelta(hours=8)))
    collection_schedules.tick(service, now, lambda:False)
    collection_schedules.tick(service, now, lambda:False)
    assert calls == [('601919','dividends')]
    with service.db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM collection_schedule_slots').fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM collection_batches WHERE status='reused'").fetchone()[0] == 1
    # A later manual request still performs a check.
    ref = reserve(service,codes=['601919'],datasets=['dividends'])
    service.run(ref)
    assert len(calls) == 2


def test_cancel_keeps_submitted_work_and_management_reads_local(service, monkeypatch):
    ref = reserve(service,codes=['601919'],datasets=['dividends','financing'])
    def collect(*args): service.cancel()
    monkeypatch.setattr(service,'collect',collect)
    service.run(ref)
    assert service.detail(ref)['status'] == 'cancelled'
    assert all(x['status'] == 'cancelled' for x in service.detail(ref)['items'])
    task = next(t for t in CollectionManagement(service.db,None,service).read()['tasks'] if t['task_key']=='stocks')
    assert not task['running'] and task['coverage']=='unknown' and not task['rules']


def test_incremental_never_becomes_periodic_audit_and_resume_keeps_full_mode(service,monkeypatch):
    from stock_analysis.domains.stocks.sync_support import audit_due
    assert audit_due({},'financial')
    with execution_context(scope={'requested_mode':'incremental'}):
        assert not audit_due({},'financial')
    ref=reserve(service,codes=['601919'],datasets=['financing'],mode='full')
    monkeypatch.setattr(service,'collect',lambda *args: (_ for _ in ()).throw(ValueError('offline')))
    service.run(ref)
    retry=reserve(service,codes=['601919'],datasets=['financing'],mode='resume')
    assert service.detail(retry)['items'][0]['mode']=='full'


def test_upgrade_v42_preserves_stocks_and_does_not_initialize_or_enable(tmp_path,monkeypatch):
    from stock_analysis.infrastructure import storage
    migrations=storage.MIGRATIONS
    db=Database(tmp_path/'old.sqlite3')
    with monkeypatch.context() as patch:
        patch.setattr(storage,'MIGRATIONS',migrations[:-1]);db.initialize()
        identity=db.ensure_instrument('601919')
        run=db.start_sync(SyncKey(identity,'financial:lrb','test'),parser_version='test',methodology_version='test')
        db.complete_sync(run,SyncResult(0,no_data=True),lambda conn:None)
    db.initialize()
    service=StockCollections(db,None)
    assert not service.page_status('601919')['needs_initial']
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM collection_operations').fetchone()[0]==0
        assert conn.execute("SELECT count(*) FROM collection_schedules WHERE schedule_id LIKE 'stocks:%'").fetchone()[0]==0


def test_interrupted_slot_recovers_original_members_after_group_edit(service,monkeypatch):
    identity=group(service,'holdings',['601919'])
    params=dict(group_id=identity,datasets=['financing'],mode='incremental')
    collection_schedules.configure(service.db,dict(enabled=True,local_time='18:00',rule=dict(frequency='daily'),params=params))
    with service.db.connection(write=True) as conn:
        conn.execute("UPDATE collection_schedules SET updated_at='2026-01-01T00:00:00+00:00' WHERE schedule_id=?", (f'stocks:{identity}',))
        ref=service.reserve(conn,params,trigger='scheduled')
        conn.execute("INSERT INTO collection_schedule_slots VALUES(?,?,'running',?,1,?,NULL,?,NULL)",
                     (f'stocks:{identity}','2026-01-02',ref,utc_now(),utc_now()))
    CollectionManagement(service.db,None,service).recover()
    service.db.ensure_instrument('605499')
    with service.db.connection(write=True) as conn:
        conn.execute('DELETE FROM stock_group_members WHERE group_id=?',(identity,))
        conn.execute('INSERT INTO stock_group_members(group_id,instrument_id,position) SELECT ?,id,0 FROM instruments WHERE code=?',(identity,'605499'))
    calls=[];monkeypatch.setattr(service,'collect',lambda code,*args:calls.append(code))
    collection_schedules.tick(service,datetime(2026,1,2,19,tzinfo=timezone(timedelta(hours=8))),lambda:False)
    assert calls==['601919']
    with service.db.connection() as conn:
        slot=conn.execute('SELECT status,attempts FROM collection_schedule_slots').fetchone()
        assert tuple(slot)==('complete',2)


def test_group_delete_closes_rule_even_if_sqlite_reuses_group_id(service):
    from stock_analysis.domains.stocks.stock_library import StockLibrary
    identity=group(service,'holdings',['601919'])
    collection_schedules.configure(service.db,dict(enabled=True,local_time='18:00',rule=dict(frequency='daily'),params=dict(group_id=identity)))
    StockLibrary(service.db).change(dict(action='delete',id=identity))
    group(service,'replacement',['601919'])
    with service.db.connection() as conn:
        assert collection_schedules.read_rules(conn)==[]
        assert tuple(conn.execute('SELECT deleted,enabled FROM collection_schedules WHERE schedule_id=?',(f'stocks:{identity}',)).fetchone())==(1,0)


def test_same_group_topic_rules_edit_delete_preserve_legacy_and_history(service):
    identity=group(service,'holdings',['601919'])
    legacy=dict(enabled=True,local_time='19:00',rule=dict(frequency='daily'),params=dict(group_id=identity,datasets=['prices']))
    collection_schedules.configure(service.db,legacy)
    monthly=dict(enabled=True,local_time='20:00',rule=dict(frequency='monthly',day=15),params=dict(group_id=identity,datasets=['financial']),schedule_key=None)
    saved=collection_schedules.configure(service.db,monthly);key=saved['schedule_key']
    assert key!=f'stocks:{identity}'
    with service.db.connection() as conn:
        rules=collection_schedules.read_rules(conn)
        assert len(rules)==2 and next(r for r in rules if r['task_key']==f'stocks:{identity}')['local_time']=='19:00'
    collection_schedules.configure(service.db,{**monthly,'schedule_key':key,'local_time':'21:00'})
    with service.db.connection(write=True) as conn:
        conn.execute("INSERT INTO collection_schedule_slots VALUES(?,?,'complete',NULL,1,?,?,?,NULL)",(key,'2026-10-15',utc_now(),utc_now(),utc_now()))
        conn.execute("INSERT INTO collection_schedule_slots VALUES(?,?,'pending',NULL,1,?,?,?,NULL)",(key,'2026-11-15',utc_now(),utc_now(),utc_now()))
    collection_schedules.configure(service.db,dict(action='delete',schedule_key=key))
    with service.db.connection() as conn:
        assert len(collection_schedules.read_rules(conn))==1
        assert conn.execute('SELECT enabled FROM collection_schedules WHERE schedule_id=?',(key,)).fetchone()[0]==0
        assert conn.execute('SELECT count(*) FROM collection_schedule_slots WHERE schedule_id=?',(key,)).fetchone()[0]==2
        assert conn.execute("SELECT status FROM collection_schedule_slots WHERE schedule_id=? AND slot_key='2026-11-15'",(key,)).fetchone()[0]=='failed'
    with pytest.raises(ValueError,match='已删除'):
        collection_schedules.configure(service.db,{**monthly,'schedule_key':key})
    assert collection_schedules.configure(service.db,monthly)['schedule_key']!=key


@pytest.mark.parametrize('params',[{'codes':[]},{'codes':['601919'],'datasets':['unknown']},{'group_id':True},{'codes':['601919'],'url':'x'}])
def test_parameters_reject_unregistered_work(params):
    with pytest.raises(ValueError): validate_params(params)


def test_financial_season_group_rule_preserves_incremental_work(service,monkeypatch):
    identity=group(service,'season',['601919'])
    command=dict(enabled=True,local_time='18:00',rule=dict(frequency='financial_season'),
                 params=dict(group_id=identity,datasets=['financing'],mode='incremental'),schedule_key=None)
    saved=collection_schedules.configure(service.db,command)
    with service.db.connection(write=True) as conn:
        assert conn.execute('SELECT count(*) FROM collection_operations').fetchone()[0]==0
        conn.execute("UPDATE collection_schedules SET updated_at='2026-01-01T00:00:00+00:00'")
        assert collection_schedules.read_rules(conn)[0]['rule']==dict(frequency='financial_season',days_before=30,days_after=7)
    calls=[]
    monkeypatch.setattr(service,'collect',lambda code,dataset,mode,*args:calls.append((code,dataset,mode)))
    zone=timezone(timedelta(hours=8))
    for day in (30,31,31):
        collection_schedules.tick(service,datetime(2026,3,day,19,tzinfo=zone),lambda:False)
    assert calls==[('601919','financing',False)]
    with service.db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM collection_schedule_slots').fetchone()[0]==1
    collection_schedules.configure(service.db,{**command,'schedule_key':saved['schedule_key'],
        'rule':dict(frequency='financial_season',days_before=10,days_after=0)})
    with service.db.connection() as conn:
        assert collection_schedules.read_rules(conn)[0]['rule']==dict(frequency='financial_season',days_before=10,days_after=0)
