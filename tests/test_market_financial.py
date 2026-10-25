import copy
import gzip
import json
from pathlib import Path
import sqlite3

import pytest
import requests

from stock_analysis.domains.market import market_financial as mf
from stock_analysis.domains.industries.industry_service import IndustryService
from stock_analysis.maintenance.maintenance import MaintenanceService
from stock_analysis.collection.collection import RequestCollector,archive_source,begin_batch as begin_collection_batch,parsed_source
from stock_analysis.infrastructure.storage import Database
from test_industries import fixture_bundle

PERIOD = '2025-06-30'


@pytest.fixture
def db(tmp_path):
    result = Database(tmp_path/'sample.sqlite3')
    result.initialize()
    return result


def row(dataset, value=0, code='600000', exchange='SH'):
    values = {key: None for key in mf.FIELDS[dataset]['fields']}
    values.update(SECUCODE=code+'.'+exchange, SECURITY_CODE=code,
                  SECURITY_TYPE_CODE='058001001', NOTICE_DATE='2025-08-01 00:00:00')
    values[mf.FIELDS[dataset].get('date_field','REPORT_DATE')]=PERIOD+' 00:00:00'
    metric = next(k for k, v in mf.FIELDS[dataset]['fields'].items() if v['type'] == 'REAL')
    values[metric] = value
    return values


def source(page=1):
    return {'page_number': page, 'url': 'https://example.test', 'params': {'pageNumber': page},
            'file_path': 'sample.json.gz', 'sha256': 'a'*64, 'raw_bytes': 1000, 'stored_bytes': 200,
            'obtained_at': '2026-10-08T00:00:00+00:00'}


def datasets():
    return [{'dataset': name, 'rows': [row(name)], 'sources': [source()], 'checkpoint': 'unused'}
            for name in mf.FIELDS]


def prepared_public_datasets(db,operation_id,period,result=None):
    result=result or datasets()
    for item in result:
        name=item['dataset'];dataset='market.financial.'+name
        batch=begin_collection_batch(db,operation_id,dataset,'eastmoney',period,period,
            scope={'period':period,'report_name':mf.FIELDS[name]['report'],'columns':'ALL'},
            frequency='quarterly',domain='market',
            parser_version='market-financial-v1',definition_version=mf.FIELDS[name]['definition_version'],
            universe_version='eastmoney-a-share-v1')
        params={'reportName':mf.FIELDS[name]['report'],'columns':'ALL','pageNumber':1}
        sid=archive_source(db,batch,'eastmoney',dataset,{'transport':'http','method':'GET',
            'url':'https://example.test','params':params},1,raw=json.dumps({'data':item['rows']}).encode(),
            obtained_at='2026-10-14T00:00:00+00:00')
        parsed_source(db,sid,fields=sorted(item['rows'][0]))
        item['collection_batch_id']=batch
        item['sources']=[{'page_number':1,'collection_source_id':sid,'obtained_at':'2026-10-14T00:00:00+00:00',
            'params':params,'url':'https://example.test','raw_bytes':100,'stored_bytes':90}]
    return result


def save(db, data):
    batch = mf.begin_batch(db, PERIOD)
    with db.connection(write=True) as conn:
        summary = mf.publish(conn, batch, PERIOD, data)
    return batch, summary


def test_migration_inventory_empty_and_populated(db):
    inventory = MaintenanceService(db).read()
    tables = [t for t in inventory['tables'] if t['name'].startswith('market_financial_')]
    assert len(tables) == 6
    assert all(t['rows'] == 0 and t['updated_at'] is None and t['total_bytes'] > 0 for t in tables)
    assert all(t['category'] != '其他' and '尚未登记' not in t['description'] for t in tables)
    save(db, datasets())
    populated = MaintenanceService(db).read()['tables']
    tables = [t for t in populated if t['name'].startswith('market_financial_')]
    assert all(t['rows'] > 0 and t['updated_at'] and 'at' in t['time_basis'] for t in tables)


def test_v32_rebuild_preserves_legacy_rows_and_adds_disjoint_public_references(tmp_path, monkeypatch):
    from stock_analysis.infrastructure import storage
    path=tmp_path/'pre-v32.sqlite3'
    db=Database(path)
    latest=storage.MIGRATIONS
    # Build a v31 database, then apply only the v32 migration under test.
    monkeypatch.setattr(storage,'MIGRATIONS',tuple(m for m in latest if m[0]<=31))
    db.initialize()
    with db.connection(write=True) as conn:
        batch=conn.execute("INSERT INTO market_financial_batches(report_date,started_at,status,obtained_at) VALUES('2025-06-30','2026-01-01','complete','2026-01-01')").lastrowid
        ids={}
        for dataset,table in zip(mf.FIELDS,('market_financial_income','market_financial_balance','market_financial_cashflow','market_financial_performance')):
            source_id=conn.execute("INSERT INTO market_financial_sources(batch_id,dataset,page_number,url,params_json,file_path,content_hash,raw_bytes,stored_bytes,obtained_at,fields_json) VALUES(?,?,1,'https://old.test','{}','old.gz','abc',1,1,'2026-01-01','[]')",(batch,dataset)).lastrowid
            conn.execute(f"INSERT INTO {table}(exchange,report_date,version,is_latest,batch_id,source_id,content_hash,definition_version,obtained_at,updated_at,security_code) VALUES('sh','2025-06-30',1,1,?,?,'legacy-hash','v1','2026-01-01','2026-01-02','600000')",(batch,source_id))
            ids[table]=(batch,source_id,tuple(conn.execute(f'SELECT * FROM {table}').fetchone()))
    monkeypatch.setattr(storage,'MIGRATIONS',tuple(m for m in latest if m[0]<=32))
    db.initialize()
    with db.connection() as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0]==32
        for table,(old_batch,old_source,old_row) in ids.items():
            row=conn.execute(f'SELECT * FROM {table}').fetchone()
            assert tuple(row)[:len(old_row)]==old_row
            assert (row['batch_id'],row['source_id'],row['content_hash'],row['security_code'])==(old_batch,old_source,'legacy-hash','600000')
            assert (row['collection_batch_id'],row['collection_source_id'])==(None,None)
            assert conn.execute(f"SELECT count(*) FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",(table,)).fetchone()[0]==2
        assert conn.execute('SELECT count(*) FROM market_financial_industry').fetchone()[0]==1
        assert conn.execute('PRAGMA foreign_key_check').fetchall()==[]
    from stock_analysis.collection.collection import archive_source
    from historical_collection import begin_batch
    public_batch=begin_batch(db,'op','market.financial.income','eastmoney',start='2025-06-30',end='2025-06-30')
    public_source=archive_source(db,public_batch,'eastmoney','market.financial.income',{'url':'https://new.test'},1,raw=b'{}')
    with db.connection(write=True) as conn:
        conn.execute("UPDATE market_financial_income SET batch_id=NULL,source_id=NULL,collection_batch_id=?,collection_source_id=?",(public_batch,public_source))
    mismatched_batch=begin_batch(db,'op2','market.financial.income','eastmoney',start='2025-06-30',end='2025-06-30')
    with pytest.raises(sqlite3.IntegrityError):
        with db.connection(write=True) as conn:
            conn.execute("UPDATE market_financial_income SET collection_batch_id=? WHERE collection_source_id=?",(mismatched_batch,public_source))


def test_public_financial_collect_and_atomic_publish_uses_shared_provenance(db):
    class Clock:
        value=10000
        def time(self):return self.value
        def sleep(self,seconds):self.value+=seconds
    clock=Clock()
    class Session:
        def __init__(self):self.calls=0
        def get(self,*args,**kwargs):
            dataset=list(mf.FIELDS)[self.calls];self.calls+=1
            payload={'success':True,'code':0,'result':{'count':1,'pages':1,'data':[row(dataset)]}}
            result=requests.Response();result.status_code=200;result._content=json.dumps(payload).encode()
            result.headers['Content-Type']='application/json';result.encoding='utf-8'
            return result
    collector=RequestCollector(db,session=Session(),clock=clock.time,sleep=clock.sleep,uniform=lambda a,b:0)
    datasets=mf.collect_public(db,'sw_update_runs:sample',PERIOD,collector,lambda _message:None)
    with db.connection(write=True) as conn:
        summary=mf.publish(conn,None,PERIOD,datasets)
    assert summary['income']['new']==1 and collector.requests==4
    with db.connection() as conn:
        rows=conn.execute('SELECT collection_batch_id,collection_source_id,batch_id,source_id FROM market_financial_income').fetchall()
        assert len(rows)==1 and rows[0]['collection_batch_id'] and rows[0]['collection_source_id']
        assert rows[0]['batch_id'] is None and rows[0]['source_id'] is None
        assert conn.execute("SELECT count(*) FROM collection_batches WHERE operation_id='sw_update_runs:sample' AND status='complete'").fetchone()[0]==4
        assert conn.execute('SELECT count(*) FROM market_financial_sources').fetchone()[0]==0
        assert conn.execute('PRAGMA foreign_key_check').fetchall()==[]


def test_public_financial_reuses_hash_verified_same_day_pages_across_operations(db):
    from datetime import datetime,timezone
    class Clock:
        value=datetime(2026,10,14,tzinfo=timezone.utc).timestamp()
        def time(self):return self.value
        def sleep(self,seconds):self.value+=seconds
    clock=Clock()
    class Session:
        calls=0
        def get(self,*args,**kwargs):
            dataset=next(name for name,spec in mf.FIELDS.items() if spec['report']==kwargs['params']['reportName']);self.calls+=1
            payload={'success':True,'code':0,'result':{'count':1,'pages':1,'data':[row(dataset,value=0 if self.calls<=4 else 2)]}}
            result=requests.Response();result.status_code=200;result._content=json.dumps(payload).encode()
            result.headers['Content-Type']='application/json';result.encoding='utf-8'
            return result
    session=Session()
    collector=RequestCollector(db,session=session,clock=clock.time,sleep=clock.sleep,uniform=lambda a,b:0)
    first=mf.collect_public(db,'operation:first',PERIOD,collector,lambda _message:None)
    with db.connection(write=True) as conn:
        mf.publish(conn,None,PERIOD,first)
    original_obtained={}
    with db.connection() as conn:
        original_obtained={r['dataset']:r['obtained_at'] for r in conn.execute(
            "SELECT dataset,obtained_at FROM collection_sources WHERE batch_id IN "
            "(SELECT id FROM collection_batches WHERE operation_id='operation:first')")}
    second=mf.collect_public(db,'operation:restart',PERIOD,collector,lambda _message:None)
    with db.connection(write=True) as conn:
        mf.publish(conn,None,PERIOD,second)
    assert session.calls==4 and collector.requests==4
    checked=mf.collect_public(db,'operation:explicit-recheck',PERIOD,collector,lambda _message:None,recheck=True)
    with db.connection(write=True) as conn:
        checked_summary=mf.publish(conn,None,PERIOD,checked)
    assert session.calls==8 and collector.requests==8
    assert all(checked_summary[name]['revised']==1 for name in mf.FIELDS)
    with db.connection() as conn:
        reused=conn.execute("SELECT s.dataset,s.obtained_at,s.reused_source_id,o.obtained_at AS original_time "
            "FROM collection_sources s JOIN collection_batches b ON b.id=s.batch_id "
            "JOIN collection_sources o ON o.id=s.reused_source_id WHERE b.operation_id='operation:restart'").fetchall()
        assert len(reused)==4
        assert all(r['reused_source_id'] and r['obtained_at']==r['original_time'] for r in reused)
        assert {r['dataset']:r['obtained_at'] for r in reused}==original_obtained
        income_source=conn.execute("SELECT s.id,s.file_path FROM collection_sources s JOIN collection_batches b ON b.id=s.batch_id "
            "WHERE b.operation_id='operation:explicit-recheck' AND s.dataset='market.financial.income'").fetchone()
    # Simulate a verified completed archive whose page-one total no longer matches its archived page set.
    from stock_analysis.collection.collection import read_source
    raw,_=read_source(db,income_source['id'])
    payload=json.loads(raw);payload['result'].update(count=501,pages=2)
    changed=json.dumps(payload).encode();stored=gzip.compress(changed,mtime=0)
    archive_path=db.path.parent/income_source['file_path'];archive_path.write_bytes(stored)
    with db.connection(write=True) as conn:
        conn.execute('UPDATE collection_sources SET raw_sha256=?,stored_sha256=?,raw_bytes=?,stored_bytes=? WHERE id=?',
            (mf.digest(changed),mf.digest(stored),len(changed),len(stored),income_source['id']))
    refreshed=mf.collect_public(db,'operation:page-count-change',PERIOD,collector,lambda _message:None)
    with db.connection(write=True) as conn:
        mf.publish(conn,None,PERIOD,refreshed)
    assert session.calls==9 and collector.requests==9
    assert all(item['sources'][0]['collection_source_id'] for item in refreshed)


def test_public_financial_never_mixes_pages_from_different_batches(db):
    from datetime import datetime,timezone
    from stock_analysis.collection.collection import finish_batch
    stamp='2026-10-14T02:00:00+00:00'
    original_batch_ids={}
    for dataset,spec in mf.FIELDS.items():
        public_dataset='market.financial.'+dataset
        scope={'period':PERIOD,'report_name':spec['report'],'columns':'ALL','page_size':500,
               'security_type_code':'058001001','date_field':spec.get('date_field','REPORT_DATE'),
               'definition_version':spec['definition_version']}
        batch=begin_collection_batch(db,'operation:cache-complete',public_dataset,'eastmoney',PERIOD,PERIOD,
            scope=scope,frequency='quarterly',domain='market',parser_version='market-financial-v1',
            definition_version=spec['definition_version'],universe_version='eastmoney-a-share-v1')
        original_batch_ids[dataset]=batch
        rows=[row(dataset,code=str(100000+i)) for i in range(501)]
        for page,page_rows in ((1,rows[:500]),(2,rows[500:])):
            request=mf._financial_page_request(spec,PERIOD,page)
            payload={'success':True,'code':0,'result':{'count':501,'pages':2,'data':page_rows}}
            source_id=archive_source(db,batch,'eastmoney',public_dataset,request,page,
                raw=json.dumps(payload).encode(),obtained_at=stamp)
            parsed_source(db,source_id,fields=sorted(page_rows[0]))
        finish_batch(db,batch,'complete')
    # New interrupted page one has a different 500-security set but no page two.
    spec=mf.FIELDS['income'];public_dataset='market.financial.income'
    scope={'period':PERIOD,'report_name':spec['report'],'columns':'ALL','page_size':500,
           'security_type_code':'058001001','date_field':spec.get('date_field','REPORT_DATE'),
           'definition_version':spec['definition_version']}
    interrupted=begin_collection_batch(db,'operation:interrupted',public_dataset,'eastmoney',PERIOD,PERIOD,
        scope=scope,frequency='quarterly',domain='market',parser_version='market-financial-v1',
        definition_version=spec['definition_version'],universe_version='eastmoney-a-share-v1')
    changed_rows=[row('income',code=str(200000+i)) for i in range(500)]
    changed={'success':True,'code':0,'result':{'count':501,'pages':2,'data':changed_rows}}
    source_id=archive_source(db,interrupted,'eastmoney',public_dataset,mf._financial_page_request(spec,PERIOD,1),1,
        raw=json.dumps(changed).encode(),obtained_at=stamp)
    parsed_source(db,source_id,fields=sorted(changed_rows[0]))
    finish_batch(db,interrupted,'failed',error='模拟第二页中断')
    class Clock:
        value=datetime(2026,10,14,tzinfo=timezone.utc).timestamp()
        def time(self):return self.value
        def sleep(self,seconds):self.value+=seconds
    class FreshNetwork:
        def get(self,url,params,**kwargs):
            dataset=next(name for name,item in mf.FIELDS.items() if item['report']==params['reportName'])
            fresh=[row(dataset,code=str(300000+i)) for i in range(501)]
            page=params['pageNumber']
            payload={'success':True,'code':0,'result':{'count':501,'pages':2,'data':fresh[(page-1)*500:page*500]}}
            response=requests.Response();response.status_code=200;response._content=json.dumps(payload).encode()
            response.headers['Content-Type']='application/json';response.encoding='utf-8'
            return response
    clock=Clock()
    collector=RequestCollector(db,session=FreshNetwork(),clock=clock.time,sleep=clock.sleep,uniform=lambda a,b:0)
    datasets=mf.collect_public(db,'operation:resume',PERIOD,collector,lambda _message:None)
    with db.connection(write=True) as conn:
        mf.publish(conn,None,PERIOD,datasets)
    assert collector.requests==2
    with db.connection() as conn:
        current_income=conn.execute("SELECT s.request_status,s.reused_source_id,json_extract(s.request_json,'$.params.pageNumber') AS page "
            "FROM collection_sources s JOIN collection_batches b ON b.id=s.batch_id WHERE b.operation_id='operation:resume' "
            "AND s.dataset='market.financial.income' ORDER BY page").fetchall()
        assert [r['page'] for r in current_income]==[1,2]
        assert all(r['request_status']=='success' and r['reused_source_id'] is None for r in current_income)
        published_codes={r[0] for r in conn.execute("SELECT security_code FROM market_financial_income "
            "WHERE report_date=? AND is_latest=1",(PERIOD,))}
        assert published_codes=={str(300000+i) for i in range(501)}


def test_repeat_zero_negative_enrichment_revision_and_reversion(db):
    data = datasets()
    metric = next(k for k, v in mf.FIELDS['income']['fields'].items() if v['type'] == 'REAL')
    _, result = save(db, data)
    assert result['income']['new'] == 1
    _, result = save(db, data)
    assert result['income']['unchanged'] == 1
    # A valid zero is a real observation, not a missing field.
    data[0]['rows'][0][metric] = -2
    _, result = save(db, data)
    assert result['income']['revised'] == 1
    data[0]['rows'][0][metric] = 0
    save(db, data)
    # Previously null numeric fields may be completed without a stale predecessor.
    data[0]['rows'][0]['OPERATE_COST'] = 0
    _, result = save(db, data)
    assert result['income']['enriched'] == 1
    data[0]['rows'][0]['SECURITY_NAME_ABBR'] = '新名称'
    _, result = save(db, data)
    assert result['income']['unchanged'] == 1
    with db.connection() as conn:
        rows = conn.execute('SELECT * FROM market_financial_income ORDER BY version').fetchall()
        assert len(rows) == 4 and [r[metric.lower()] for r in rows] == [0, -2, 0, 0]
        assert rows[-1]['operate_cost'] == 0 and rows[-1]['is_latest'] == 1
        assert sum(r['is_latest'] for r in rows) == 1
        assert rows[-1]['obtained_at'] == source()['obtained_at']
    data[0]['rows'][0]['OPERATE_COST'] = None
    with pytest.raises(ValueError,match='疑似撤回'):
        save(db, data)


def test_announcement_change_and_definition_change_keep_versions(db, monkeypatch):
    data = datasets()
    save(db, data)
    data[0]['rows'][0]['NOTICE_DATE'] = '2025-08-02 00:00:00'
    _, result = save(db, data)
    assert result['income']['revised'] == 1
    monkeypatch.setitem(mf.FIELDS['income'], 'definition_version', 'summary-v2')
    _, result = save(db, data)
    assert result['income']['revised'] == 1


def test_unknown_nonfinite_or_wrong_scope_fail(db):
    for change in [{'UNKNOWN_FIELD': 1}, {'PARENT_NETPROFIT': float('inf')},
                   {'SECURITY_TYPE_CODE': 'B'}, {'REPORT_DATE': '2024-06-30'},
                   {'SECUCODE': '000001.SZ'}, {'PARENT_NETPROFIT': 'bad'}]:
        bad = row('income') | change
        with pytest.raises(ValueError):
            mf.normalized('income', bad, PERIOD)


def test_atomic_publication_rolls_back_legacy_and_all_new_tables(db):
    service = IndustryService(db)
    original = fixture_bundle()
    service.import_bundle(copy.deepcopy(original), capture_local=False)
    with db.connection() as conn:
        before = conn.execute('SELECT count(*) FROM sw_imports').fetchone()[0]
    changed = copy.deepcopy(original)
    changed['financials'][0]['revenue'] = 456
    batch = mf.begin_batch(db, PERIOD)
    data = datasets()
    data[1]['rows'][0]['UNKNOWN_FIELD'] = 1
    with pytest.raises(ValueError, match='未登记'):
        service.import_bundle(changed, capture_local=False,
                              publish=lambda conn: mf.publish(conn, batch, PERIOD, data))
    mf.fail_batch(db, batch, 'failure')
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM sw_imports').fetchone()[0] == before
        assert conn.execute('SELECT count(*) FROM market_financial_sources').fetchone()[0] == 0
        assert all(conn.execute(f'SELECT count(*) FROM market_financial_{name}').fetchone()[0] == 0
                   for name in mf.FIELDS)
        assert conn.execute('SELECT status FROM market_financial_batches').fetchone()[0] == 'failed'
    # Even an unchanged legacy import must invoke the shared publication hook.
    batch = mf.begin_batch(db, PERIOD)
    service.import_bundle(copy.deepcopy(original), capture_local=False,
                          publish=lambda conn: mf.publish(conn, batch, PERIOD, datasets()))
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM market_financial_income').fetchone()[0] == 1


def test_abrupt_coverage_drop_cannot_replace_previous_data(db):
    save(db, datasets())
    bad = datasets()
    bad[1]['rows'] = []
    with pytest.raises(ValueError, match='覆盖骤减'):
        save(db, bad)
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM market_financial_sources').fetchone()[0] == 4


class Pacer:
    def before_request(self):
        pass


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(kwargs['params'])
        payload = next(self.responses)
        class Response:
            content = json.dumps(payload, ensure_ascii=False).encode()
            def raise_for_status(self):
                pass
        return Response()


def payload(records, count=None, pages=1):
    return {'success': True, 'result': {'count': len(records) if count is None else count,
                                       'pages': pages, 'data': records}}


def test_legacy_dataset_collector_is_disabled():
    with pytest.raises(ValueError, match='collect_public'):
        mf.collect_dataset(None, None, 'income', PERIOD, None, None)


def test_recovery_fails_only_unfinished_batches(db):
    batch, _ = save(db, datasets())
    interrupted = mf.begin_batch(db, PERIOD)
    from stock_analysis.collection.collection import finish_batch
    with db.connection(write=True) as conn:
        run_id=conn.execute("INSERT INTO sw_update_runs(action,target,recheck,started_at,status) "
            "VALUES('financial_period',? ,0,'2026-10-14','running')",(PERIOD,)).lastrowid
    operation='sw_update_runs:'+str(run_id)
    children=[]
    for domain,dataset,provider in (
            ('market','market.financial.income','eastmoney'),
            ('market','market.listings.sse','sse'),
            ('market','market.listings.szse','szse'),
            ('market','industry.classification.taxonomy','sws')):
        children.append(begin_collection_batch(db,operation,dataset,provider,PERIOD,PERIOD,domain=domain))
    completed_child=begin_collection_batch(db,operation,'industry.classification.members','sws',PERIOD,PERIOD,domain='market')
    finish_batch(db,completed_child,'complete')
    unrelated=begin_collection_batch(db,'other-operation','industry.classification.members','sws',PERIOD,PERIOD,domain='market')
    IndustryService(db).recover_interrupted_runs()
    with db.connection() as conn:
        states = dict(conn.execute('SELECT id,status FROM market_financial_batches'))
        child_states=dict(conn.execute('SELECT id,status FROM collection_batches'))
    assert states == {batch: 'complete', interrupted: 'failed'}
    assert all(child_states[item]=='failed' for item in children)
    assert child_states[completed_child]=='complete' and child_states[unrelated]=='running'


def test_daily_button_collects_once_without_legacy_reads_or_writes(db, tmp_path, monkeypatch):
    from stock_analysis.domains.industries import industry_updates; from stock_analysis.domains.industries import industry_memberships
    service = IndustryService(db, tmp_path/'industry-sources')
    bundle = fixture_bundle()
    existing = {r['stock_code'] for r in bundle['members']}
    for i in range(600000, 610000):
        code = str(i)
        if code not in existing:
            bundle['members'].append({**bundle['members'][0], 'stock_code': code})
        if len(bundle['members']) == 4000:
            break
    service.import_bundle(bundle, capture_local=False)
    monkeypatch.setattr(industry_memberships, 'ensure_daily_memberships', lambda *a, **k: {'reused': True})
    monkeypatch.setattr(service, 'local_period', lambda *a: pytest.fail('daily financial read individual Sina'))
    monkeypatch.setattr(service, 'import_bundle', lambda *a,**k: pytest.fail('daily financial used legacy publication'))
    with db.connection() as conn:
        legacy={table:[tuple(r) for r in conn.execute('SELECT * FROM '+table)] for table in
                ('sw_financial_facts','sw_financial_provenance','sw_imports','sw_cap_facts')}
    calls = []
    def collect(database,operation_id,period,request_collector,progress,*,recheck=False,**kwargs):
        calls.append((operation_id,period,recheck))
        data = datasets()
        data[0]['rows'][0]['TOTAL_OPERATE_INCOME'] = 50
        return prepared_public_datasets(database,operation_id,period,data)
    monkeypatch.setattr(mf, 'collect_public', collect)
    monkeypatch.setattr(industry_updates, 'fetch_cap_quarter', lambda *a, **k: pytest.fail('finance fetched cap'))
    result = industry_updates.perform(service, 'financial_period', PERIOD, True, lambda _: None)
    assert len(calls) == 1 and calls[0][1:] == (PERIOD,True)
    assert set(result['statements']) == {*mf.FIELDS, 'performance'}
    assert result['returned_count']==1 and result['legacy_updated'] is False
    with db.connection() as conn:
        assert conn.execute('SELECT total_operate_income FROM market_financial_income').fetchone()[0] == 50
        for table,rows in legacy.items():
            assert [tuple(r) for r in conn.execute('SELECT * FROM '+table)]==rows
        imports = conn.execute('SELECT count(*) FROM sw_imports').fetchone()[0]
    def failed(database,operation_id,period,request_collector,progress,**kwargs):
        data=prepared_public_datasets(database,operation_id,period)
        from stock_analysis.collection.collection import finish_batch
        cashflow=next(item for item in data if item['dataset']=='cashflow')
        finish_batch(database,cashflow['collection_batch_id'],'failed',error='现金流量表失败')
        raise ValueError('现金流量表失败')
    monkeypatch.setattr(mf, 'collect_public', failed)
    with pytest.raises(ValueError, match='现金流量表失败'):
        industry_updates.perform(service, 'financial_period', PERIOD, False, lambda _: None)
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM sw_imports').fetchone()[0] == imports
        assert conn.execute('SELECT count(*) FROM market_financial_income').fetchone()[0] == 1
        assert conn.execute("SELECT status FROM collection_batches WHERE dataset='market.financial.cashflow' ORDER BY id DESC").fetchone()[0] == 'failed'


def test_explicit_history_range():
    from stock_analysis.domains.market.market_financial_backfill import periods
    assert periods('2025-09-30', '2026-06-30') == ['2025-09-30', '2025-12-31', '2026-03-31', '2026-06-30']
    assert len(periods('2014-09-30','2016-06-30'))==8
    with pytest.raises(ValueError):
        periods('2025-09-30', '2025-06-30')


def test_performance_all_fields_zero_negative_and_announcement_revision(db):
    data=datasets()
    performance=next(d for d in data if d['dataset']=='performance')
    performance['rows'][0].update(BASIC_EPS=0,WEIGHTAVG_ROE=-2.68,ASSIGNDSCRPT='不分配不转增',
                                  UPDATE_DATE='2026-10-08',EITIME='2026-10-08 01:00:00')
    save(db,data)
    performance['rows'][0]['EITIME']='2026-10-08 02:00:00'
    _,summary=save(db,data)
    assert summary['performance']['unchanged']==1
    with db.connection() as conn:
        record=conn.execute('SELECT * FROM market_financial_performance').fetchone()
        assert record['report_date']==PERIOD and record['source_report_date']==PERIOD+' 00:00:00'
        assert record['basic_eps']==0 and record['weightavg_roe']==-2.68
        assert record['assigndscrpt']=='不分配不转增'
    performance['rows'][0]['NOTICE_DATE']='2025-08-02 00:00:00'
    _,summary=save(db,data)
    assert summary['performance']['revised']==1


def test_history_updates_only_new_four_tables_and_skip_requires_all_columns(db,tmp_path,monkeypatch):
    from stock_analysis.domains.market import market_financial_backfill as history
    IndustryService(db).import_bundle(fixture_bundle(),capture_local=False)
    before=history.legacy_fingerprint(db)
    data=datasets()
    # A previous partial CPD batch must not cause the full-field backfill to skip.
    for d in data:
        for source in d['sources']:
            source['params']['columns']='ALL' if d['dataset']!='performance' else 'BASIC_EPS'
        d['checkpoint']=tmp_path/'unused'
    save(db,data)
    with db.connection() as conn:
        assert history.completed_batch(conn,PERIOD) is None
    for d in data:
        d['sources'][0]['params']['columns']='ALL'
    calls=[]
    def collect(database,operation_id,period,request_collector,progress,*,recheck=False):
        calls.append((period,recheck));return prepared_public_datasets(database,operation_id,period,copy.deepcopy(data))
    monkeypatch.setattr(mf,'collect_public',collect)
    result=history.run(db,PERIOD,PERIOD,tmp_path/'report.json')
    assert result['status']=='complete' and calls==[(PERIOD,False)]
    assert history.legacy_fingerprint(db)==before
    assert result['legacy_updated'] is False
    with db.connection() as conn:
        batches=conn.execute("SELECT status,coverage_status,result_json FROM collection_batches "
            "WHERE scope_start=? AND scope_end=? AND dataset LIKE 'market.financial.%'",(PERIOD,PERIOD)).fetchall()
        assert len(batches)==4
        assert all(row['status']=='complete' and row['coverage_status']=='unknown' for row in batches)
        for batch in batches:
            detail=json.loads(batch['result_json'])
            assert detail['request_count']==detail['source_count']==1
            assert detail['verified_count']==detail['published_count']==1
            assert detail['field_value_counts']
    repeat=history.run(db,PERIOD,PERIOD,tmp_path/'repeat.json')
    assert repeat['periods'][0]['skipped'] is True and calls==[(PERIOD,False)]
    audited=history.run(db,PERIOD,PERIOD,tmp_path/'recheck.json',recheck=True)
    assert audited['status']=='complete' and not audited['periods'][0].get('skipped')
    assert calls==[(PERIOD,False),(PERIOD,True)]
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM market_financial_performance').fetchone()[0]==1
    with db.connection(write=True) as conn:
        conn.execute("UPDATE collection_batches SET coverage_status='empty' "
                     "WHERE dataset='market.financial.income' AND scope_start=? AND scope_end=?",(PERIOD,PERIOD))
    with db.connection() as conn:
        assert history.completed_public_batch(conn,PERIOD) is False
