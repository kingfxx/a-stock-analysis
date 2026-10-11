import gzip
import json
from pathlib import Path
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

import pytest
import requests

from quarterly_dashboard import index_collection as ic
from quarterly_dashboard.collection import (Cancelled, ProviderBlocked, RequestCollector, archive_source, begin_batch,
    digest, encoded, finish_batch, parsed_source, read_source, retry_after)
from quarterly_dashboard.index_repository import catalog, install_definitions, publish, read_indices
from quarterly_dashboard.maintenance import MaintenanceService
from quarterly_dashboard.storage import Database


@pytest.fixture
def db(tmp_path):
    result=Database(tmp_path/'indices.sqlite3')
    result.initialize()
    return result


class Clock:
    def __init__(self):self.now=10000
    def time(self):return self.now
    def sleep(self,seconds):self.now+=seconds


def response(payload=None,status=200,headers=None,body=None):
    result=requests.Response()
    result.status_code=status
    result._content=body if body is not None else encoded(payload or {'data':[]}).encode()
    result.headers.update(headers or {'Content-Type':'application/json'})
    result.encoding='utf-8'
    return result


class Session:
    def __init__(self,clock,results):self.clock=clock;self.results=list(results);self.calls=[]
    def mount(self,*args):pass
    def get(self,url,**kwargs):
        self.calls.append((self.clock.time(),url,kwargs))
        item=self.results.pop(0)
        if isinstance(item,Exception):raise item
        self.clock.sleep(2)
        return item


def http(db,results,clock=None):
    clock=clock or Clock()
    session=Session(clock,results)
    return RequestCollector(db,session=session,clock=clock.time,sleep=clock.sleep,uniform=lambda a,b:a),session,clock


def source(db,provider='csindex',series='history.rolling'):
    batch=begin_batch(db,'test','index.test',provider)
    result=archive_source(db,batch,provider,'index.test',{'method':'GET','url':'https://example.test','params':{}},1,
                          raw=b'{"data":[]}',response=response(),obtained_at='2026-10-11T01:00:00+00:00')
    parsed_source(db,result,fields=['peg'])
    with db.connection(write=True) as conn:
        catalog(conn,'cn:000300','沪深300','broad',provider=provider,provider_code='000300',source_id=result)
    row=dict(index_code='cn:000300',provider=provider,series_key=series,observation_date='2026-10-09',frequency='daily',
             pe_rolling=13.1,batch_id=batch,source_id=result)
    return batch,result,row


def test_dual_hash_archive_and_failed_attempts_survive(db):
    batch=begin_batch(db,'operation','index.test','csindex')
    client,session,clock=http(db,[requests.Timeout(),response(status=503),response()])
    _,sid=client.get(batch,'csindex','index.test','https://example.test',{'token':'secret','code':'1'})
    raw,item=read_source(db,sid)
    assert digest(raw)==item['raw_sha256']
    assert item['stored_sha256']==digest((db.path.parent/item['file_path']).read_bytes())
    assert client.requests==3 and client.retries==2
    assert session.calls[1][0]-session.calls[0][0]>=60
    assert session.calls[2][0]-session.calls[1][0]>=120
    finish_batch(db,batch,'failed',error='publication failed')
    with db.connection() as conn:
        attempts=conn.execute('SELECT * FROM collection_sources ORDER BY id').fetchall()
        assert [r['attempt_no'] for r in attempts]==[1,2,3]
        assert attempts[0]['file_path'] is None
        assert 'secret' not in ''.join(r['request_json'] for r in attempts)
        assert attempts[1]['http_status']==503


def test_pacing_shared_across_endpoints_instances_and_rest(db):
    clock=Clock()
    one,s1,_=http(db,[response() for _ in range(11)],clock)
    two,s2,_=http(db,[response() for _ in range(11)],clock)
    batch=begin_batch(db,'o','index.test','csindex')
    for i in range(22):
        client=one if i%2==0 else two
        client.get(batch,'csindex','index.test','https://example.test/'+str(i))
    times=sorted(call[0] for call in s1.calls+s2.calls)
    assert all(b-a>=12 for a,b in zip(times,times[1:]))  # completion plus ten seconds
    assert all(times[i+5]-times[i]>=60 for i in range(len(times)-5))
    assert times[20]-times[19]>=72


@pytest.mark.parametrize('reply',[response(status=403),response(status=429,headers={'Retry-After':'7200'}),
    response(body='验证码'.encode(),headers={'Content-Type':'text/html'}),response(body=b'<html>blocked</html>')])
def test_block_stops_source_persists_cooldown_and_one_probe(db,reply):
    clock=Clock()
    batch=begin_batch(db,'o','index.test','csindex')
    client,session,_=http(db,[reply,response()],clock)
    with pytest.raises(ProviderBlocked):client.get(batch,'csindex','index.test','https://example.test')
    with pytest.raises(ProviderBlocked):client.get(batch,'csindex','index.test','https://otherhost.test')
    assert len(session.calls)==1
    second,s2,_=http(db,[response(status=403),response()],clock)
    with pytest.raises(ProviderBlocked):second.get(batch,'csindex','index.test','https://example.test')
    assert len(s2.calls)==0
    state=second.state('csindex')
    deadline=datetime.fromisoformat(state['cooldown_until']).timestamp()
    assert deadline-clock.time()>=3600
    clock.now=deadline+1
    # This run remains halted; only a new task after cooldown may make one probe.
    second,s2,_=http(db,[response(status=403),response()],clock)
    with pytest.raises(ProviderBlocked):second.get(batch,'csindex','index.test','https://example.test')
    assert len(s2.calls)==1
    assert second.state('csindex')['denials']==2
    assert datetime.fromisoformat(second.state('csindex')['cooldown_until']).timestamp()-clock.time()>=7200


def test_healthy_probe_clears_cooldown_cancel_and_retry_date(db):
    clock=Clock()
    client,session,_=http(db,[response(status=403)],clock)
    batch=begin_batch(db,'o','index.test','csindex')
    with pytest.raises(ProviderBlocked):client.get(batch,'csindex','index.test','https://a.test')
    clock.now+=3601
    healthy,s2,_=http(db,[response()],clock)
    healthy.get(batch,'csindex','index.test','https://a.test')
    assert healthy.state('csindex')['cooldown_until'] is None
    healthy.cancelled=lambda:True
    with pytest.raises(Cancelled):healthy.wait(100)
    assert retry_after('Wed, 21 Oct 2026 07:28:00 GMT',0)==datetime(2026,10,21,7,28,tzinfo=timezone.utc).timestamp()


def test_versions_unchanged_corrections_withdrawal_and_source_mismatch(db):
    batch,sid,row=source(db)
    with db.connection(write=True) as conn:assert publish(conn,[row])['inserted']==1
    second,sid2,row2=source(db)
    with db.connection(write=True) as conn:assert publish(conn,[row2])['unchanged']==1
    with db.connection() as conn:assert conn.execute('SELECT source_id FROM index_valuation_observations').fetchone()[0]==sid
    with db.connection(write=True) as conn:assert publish(conn,[{**row2,'pe_rolling':0}])['revised']==1
    with db.connection(write=True) as conn:assert publish(conn,[{**row2,'pe_rolling':-2}])['revised']==1
    with pytest.raises(ValueError,match='撤回'):
        with db.connection(write=True) as conn:publish(conn,[{**row2,'pe_rolling':None,'explicit_nulls':True}])
    with db.connection(write=True) as conn:assert publish(conn,[{**row2,'pe_rolling':None,'withdrawal_reason':'provider explicitly withdrew'}])['revised']==1
    with db.connection() as conn:
        records=conn.execute('SELECT version,is_latest,pe_rolling FROM index_valuation_observations ORDER BY version').fetchall()
        assert [r['version'] for r in records]==[1,2,3,4]
        assert sum(r['is_latest'] for r in records)==1
    with pytest.raises(ValueError,match='批次'):
        with db.connection(write=True) as conn:publish(conn,[{**row2,'batch_id':batch}])
    with pytest.raises(sqlite3.IntegrityError):
        with db.connection(write=True) as conn:
            conn.execute('UPDATE index_valuation_observations SET batch_id=? WHERE source_id=?',(batch,sid2))


def test_catalog_history_types_overlap_parent_cycle_and_asof(db):
    with db.connection(write=True) as conn:
        catalog(conn,'sw:1','一级','industry',provider='sws',provider_code='1',system='sw',level=1,valid_from='2016-01-01')
        catalog(conn,'sw:2','旧名称','industry',provider='sws',provider_code='2',system='sw',level=2,parent='sw:1',valid_from='2016-01-01',valid_to='2020-12-31')
        catalog(conn,'sw:2','新名称','industry',provider='sws',provider_code='2',system='sw',level=2,parent='sw:1',valid_from='2021-01-01')
        catalog(conn,'cn:theme','半导体主题','theme',provider='sample',provider_code='theme')
        catalog(conn,'cn:strategy','红利策略','strategy',provider='sample',provider_code='strategy')
    with pytest.raises((ValueError,sqlite3.IntegrityError)):
        with db.connection(write=True) as conn:catalog(conn,'sw:1','环','industry',provider='sws',provider_code='1-new',system='sw',level=3,parent='sw:2')
    with pytest.raises(sqlite3.IntegrityError,match='overlap'):
        with db.connection(write=True) as conn:catalog(conn,'sw:2','重叠','industry',provider='sws',provider_code='2',system='sw',level=2,valid_from='2020-01-01')
    with pytest.raises(sqlite3.IntegrityError):
        with db.connection(write=True) as conn:catalog(conn,'cn:invalid','主题','theme',provider='sample',provider_code='invalid',level=0)
    batch=begin_batch(db,'o','index.sw.analysis','sws')
    sid=archive_source(db,batch,'sws','index.sw.analysis',{},1,raw=b'{}',response=response())
    parsed_source(db,sid,fields=['pe','pb','dp'])
    rows=[dict(index_code='sw:2',provider='sws',series_key='analysis.daily',observation_date=d,frequency='daily',batch_id=batch,source_id=sid,pe_source=10,pb=2,dividend_yield=2.5) for d in ('2020-01-02','2026-10-09')]
    with db.connection(write=True) as conn:publish(conn,rows)
    result=read_indices(db,code='sw:2',provider='sws',series='analysis.daily')
    assert [r['catalog_as_of']['name'] for r in result['rows']]==['旧名称','新名称']
    assert len(read_indices(db,kind='industry',level=2)['catalog'])==1
    assert all(r['industry_level'] is None for r in read_indices(db,kind='theme')['catalog'])


def test_parser_holidays_dates_schema_zero_negative_and_unknown_coverage():
    raw=[dict(indexCode='000300',indexNameCn='沪深300',tradeDate=d,peg=p) for d,p in [('20180618',13),('20180619',0),('20180620',-2),('19900101',13)]]
    kwargs=dict(provider='csindex',series_keys=['history.rolling'],code_key='indexCode',name_key='indexNameCn',date_key='tradeDate',start='2018-06-01',end='2018-06-30',trading_days={'2018-06-19','2018-06-20'},batch_id=1,source_id=1,expected_code='000300')
    rows,cats,anomalies=ic.parse_rows(raw,**kwargs)
    assert [r['pe_rolling'] for r in rows]==[0,-2]
    assert {a['reason'] for a in anomalies}=={'non_trading_day','out_of_window'}
    state,report=ic.coverage_summary(rows,kwargs['trading_days'],kwargs['start'],kwargs['end'],expected_codes=['cn:000300'])
    assert state=='complete' and report[0]['metrics']['pe_rolling']['coverage']==1
    assert ic.coverage_summary(rows,kwargs['trading_days'],kwargs['start'],kwargs['end'])[0]=='unknown'
    with pytest.raises(ValueError,match='字段缺失'):ic.parse_rows([{k:v for k,v in raw[1].items() if k!='peg'}],**kwargs)
    with pytest.raises(ValueError,match='重复'):ic.parse_rows([raw[1],raw[1]],**kwargs)
    assert ic.rolling_start(date(2024,2,29))==date(2014,2,28)
    assert ic.day(1791504000000)=='2026-10-09'


def test_truncated_sw_pages_keep_sources_but_publish_no_facts(db):
    operation=ic.start_operation(db,start='2026-10-08',end='2026-10-09')
    rows=[dict(swindexcode=str(800000+i),swindexname='行业'+str(i),bargaindate='2026-10-09',pe=10,pb=2,dp=2.5) for i in range(50)]
    client,session,_=http(db,[response({'data':{'count':51,'results':rows}}),response({'data':{'count':51,'results':[]}})])
    collector=ic.IndexCollector(db,operation,request_collector=client,sw_page_size=50)
    collector.trading_days={'2026-10-08','2026-10-09'}
    with pytest.raises(ValueError,match='截断'):collector.collect_sw('2026-10-08','2026-10-09','full')
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM index_valuation_observations').fetchone()[0]==0
        sources=conn.execute('SELECT parse_status FROM collection_sources ORDER BY id').fetchall()
        assert [r[0] for r in sources]==['complete','failed']


def test_maintenance_and_legacy_metadata_fit_without_migration(db):
    data=MaintenanceService(db).read()
    new_tables=['collection_batches','collection_sources','collection_provider_state','index_identities','index_catalog','index_provider_codes','index_metric_definitions','index_valuation_observations','index_collection_operations']
    for name in new_tables:
        row=next(r for r in data['tables'] if r['name']==name)
        assert row['description']!='尚未登记用途'
        assert row['rows']==0
    # Four financial datasets retain a shared operation and distinct old-ID namespaces.
    ids=[begin_batch(db,'legacy-financial:7',dataset,'eastmoney',scope={'universe_ref':{'type':'sw_imports','id':123}},trigger='manual') for dataset in ('income','balance','cashflow','performance')]
    price=begin_batch(db,'legacy-price:7','quarterly_price','tencent',scope={'universe_ref':{'type':'sw_imports','id':123}},frequency='quarterly',idempotency_key='stored-hash')
    for batch,provider,basis in [(ids[0],'eastmoney','raw_decompressed_json'),(price,'tencent','stored_gzip_file')]:
        with db.connection(write=True) as conn:
            conn.execute('INSERT INTO collection_sources(batch_id,source_system,dataset,request_key,attempt_no,request_json,hash_basis,file_path,obtained_at) VALUES(?,?,?,?,1,?,?,?,NULL)',
                (batch,provider,'legacy',provider+':7',encoded({'legacy_table':provider+':sources','legacy_id':7}),basis,'../legacy/sample.gz'))
    finish_batch(db,price,'complete')
    second=begin_batch(db,'legacy-price:8','quarterly_price','tencent',idempotency_key='stored-hash')
    with pytest.raises(sqlite3.IntegrityError):finish_batch(db,second,'complete')
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM market_financial_batches').fetchone()[0]==0
        assert conn.execute('SELECT count(*) FROM market_price_batches').fetchone()[0]==0
        assert conn.execute("SELECT count(*) FROM collection_batches WHERE operation_id='legacy-financial:7'").fetchone()[0]==4


def test_monthly_moving_point_keeps_snapshots_but_shows_latest_per_month(db):
    batch,sid,row=source(db,provider='legu',series='pb.ordinary')
    rows=[{**row,'frequency':'monthly','pe_rolling':None,'pb':value,'observation_date':d} for d,value in [('2026-09-30',1.1),('2026-10-08',1.2),('2026-10-09',1.3)]]
    with db.connection(write=True) as conn:publish(conn,rows)
    query=read_indices(db,code='cn:000300',provider='legu',series='pb.ordinary')
    assert [r['observation_date'] for r in query['rows']]==['2026-09-30','2026-10-09']
    assert query['coverage'][0]['observations']==2
    assert query['coverage'][0]['pb_first_date']=='2026-09-30'
    assert query['coverage'][0]['pb_last_date']=='2026-10-09'
    with db.connection() as conn:assert conn.execute('SELECT count(*) FROM index_valuation_observations').fetchone()[0]==3
    with pytest.raises(ValueError,match='月频'):read_indices(db,code='cn:000300',provider='legu',series='pb.ordinary',weekly=True)


@pytest.mark.parametrize('status',[200,403])
def test_concurrent_clients_share_lock_and_a_denial_cancels_queued_request(db,status):
    clock=Clock()
    lock=threading.Lock()
    active=0
    maximum=0
    calls=0
    class ConcurrentSession:
        def get(self,*args,**kwargs):
            nonlocal active,maximum,calls
            with lock:
                active+=1;calls+=1;maximum=max(maximum,active)
            time.sleep(.05)
            with lock:active-=1
            return response(status=status)
    clients=[RequestCollector(db,session=ConcurrentSession(),clock=clock.time,sleep=clock.sleep,uniform=lambda a,b:a) for _ in range(2)]
    batch=begin_batch(db,'o','index.test','csindex')
    barrier=threading.Barrier(2)
    def run(client):
        barrier.wait()
        try:return client.get(batch,'csindex','index.test','https://a.test')
        except ProviderBlocked:return None
    with ThreadPoolExecutor(max_workers=2) as executor:list(executor.map(run,clients))
    assert maximum==1 and calls==(2 if status==200 else 1)
    with db.connection() as conn:
        assert [r[0] for r in conn.execute('SELECT attempt_no FROM collection_sources ORDER BY id')]==([1,2] if status==200 else [1])


def test_provider_pe_pb_and_statistics_coexist_in_one_table(db):
    batch,sid,row=source(db,provider='legu',series='pe.ordinary')
    rows=[{**row,'pe_rolling':None,'pe_static':10+i,'pe_ttm':8+i,'series_key':'pe.'+method,'frequency':'monthly'} for i,method in enumerate(('ordinary','equal','median'))]
    rows.append({**row,'pe_rolling':None,'pb':1.2,'series_key':'pb.ordinary','frequency':'monthly'})
    with db.connection(write=True) as conn:
        assert publish(conn,rows)['inserted']==4
        catalog(conn,'cn:theme','合成半导体主题','theme',provider='csindex',provider_code='fixture-theme')
        catalog(conn,'cn:strategy','合成红利策略','strategy',provider='csindex',provider_code='fixture-strategy')
    cs_batch,cs_sid,cs_row=source(db)
    with db.connection(write=True) as conn:
        assert publish(conn,[{**cs_row,'index_code':code} for code in ('cn:theme','cn:strategy')])['inserted']==2
        assert conn.execute('SELECT count(*) FROM index_valuation_observations').fetchone()[0]==6


def test_verified_candidate_joins_incremental_but_coverage_remains_unknown(db):
    operation=ic.start_operation(db,providers=['csindex'],start='2026-10-09',end='2026-10-09')
    client,session,_=http(db,[response({'code':'200','data':[dict(indexCode='000510',indexNameCn='中证A500',tradeDate='20261009',peg=12.3)]})])
    collector=ic.IndexCollector(db,operation,request_collector=client)
    collector.trading_days={'2026-10-09'}
    collector.collect_cs_codes('2026-10-09','2026-10-09','full',{'000510':'中证A500'})
    with db.connection() as conn:
        batch=conn.execute("SELECT * FROM collection_batches WHERE dataset='index.cs.history'").fetchone()
        assert batch['coverage_status']=='unknown'
        assert json.loads(batch['result_json'])['coverage'][0]['expected'] is None
    assert collector.accepted_cs_candidates()=={'000510':'中证A500'}
    assert 'theme' not in collector.accepted_cs_candidates()


def test_incremental_window_extends_individual_series_and_keeps_overlap(db):
    _,_,row=source(db)
    with db.connection(write=True) as conn:
        catalog(conn,'cn:000016','上证50','broad',provider='csindex',provider_code='000016')
        publish(conn,[{**row,'observation_date':'2026-08-14'},
                      {**row,'index_code':'cn:000016','observation_date':'2026-10-09'}])
    collector=ic.IndexCollector(db,'test')
    baseline,end='2026-09-22','2026-10-09'
    assert collector.incremental_start(baseline,end,'csindex',['history.rolling'],'cn:000300')=='2026-08-14'
    assert collector.incremental_start(baseline,end,'csindex',['history.rolling'],'cn:000016')==baseline
    assert collector.incremental_start(baseline,end,'csindex',['history.rolling'],'cn:missing')==baseline
    # Future observations must not prevent the backfill to an earlier cutoff.
    assert collector.incremental_start(baseline,'2026-09-30','csindex',['history.rolling'],'cn:000016')==baseline


def test_legu_incremental_uses_month_start_and_slower_statistical_series(db):
    _,_,ordinary=source(db,'legu','pb.ordinary')
    _,_,equal=source(db,'legu','pb.equal')
    with db.connection(write=True) as conn:
        publish(conn,[{**ordinary,'frequency':'monthly','observation_date':'2026-06-30','pb':1.5,'pe_rolling':None},
                      {**equal,'frequency':'monthly','observation_date':'2026-05-29','pb':1.5,'pe_rolling':None}])
    collector=ic.IndexCollector(db,'test')
    assert collector.incremental_start('2026-07-01','2026-10-09','legu',['pb.ordinary','pb.equal'],'cn:000300')=='2026-05-01'


def test_sw_incremental_uses_level_watermark_not_retired_index(db):
    batch,sid,row=source(db,'sws','analysis.daily')
    with db.connection(write=True) as conn:
        for code,level in [('sw:1',1),('sw:2',2),('sw:old',2)]:
            catalog(conn,code,code,'industry',system='sw',level=level,provider='sws',provider_code=code)
        publish(conn,[{**row,'index_code':code,'observation_date':observed,'pe_source':12,'pe_rolling':None} for code,observed in
                      [('sw:1','2026-08-14'),('sw:2','2026-07-31'),('sw:old','2021-01-04')]])
    finish_batch(db,batch,'complete')
    collector=ic.IndexCollector(db,'test')
    collector.trading_days={'2026-08-03','2026-08-17','2026-09-22','2026-10-09'}
    assert collector.sw_incremental_starts('2026-09-22','2026-10-09')=={1:'2026-08-17',2:'2026-08-03'}


def test_new_only_daily_skips_latest_and_requests_only_after_watermark(db):
    batch,_,row=source(db)
    with db.connection(write=True) as conn:
        publish(conn,[row])
    finish_batch(db,batch,'complete')
    client,session,_=http(db,[])
    collector=ic.IndexCollector(db,'test',request_collector=client)
    collector.auto_incremental=True
    collector.trading_days={'2026-10-08','2026-10-09','2026-10-12'}
    assert collector.incremental_start('2026-09-22','2026-10-12','csindex',['history.rolling'],'cn:000300',new_only=True)=='2026-10-12'
    collector.collect_cs_codes('2026-09-22','2026-10-09','incremental',{'000300':'沪深300'})
    assert not session.calls
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM collection_sources').fetchone()[0]==1


def test_sw_latest_job_is_no_network_and_incomplete_level_is_not_skipped(db):
    batch,sid,row=source(db,'sws','analysis.daily')
    with db.connection(write=True) as conn:
        for code,level in [('sw:1',1),('sw:2',2)]:
            catalog(conn,code,code,'industry',system='sw',level=level,provider='sws',provider_code=code)
        publish(conn,[{**row,'index_code':code,'pe_source':12,'pe_rolling':None} for code in ('sw:1','sw:2')])
    finish_batch(db,batch,'complete')
    operation=ic.start_operation(db,'incremental',['sws'],end='2026-10-09')
    client,session,_=http(db,[])
    collector=ic.IndexCollector(db,operation,request_collector=client)
    collector.trading_days={f'2026-09-{d:02d}' for d in range(1,31)}|{'2026-10-08','2026-10-09'}
    collector.load_calendar=lambda **kwargs:None
    result=ic.run_operation(db,operation,collector=collector)
    assert result['status']=='complete' and not session.calls
    assert json.loads(result['result_json'])['network_requests']==0
    with db.connection(write=True) as conn:
        # An interrupted batch's facts cannot certify a level's completion.
        conn.execute("UPDATE collection_batches SET status='failed' WHERE id=?",(batch,))
    assert collector.sw_incremental_starts('2026-09-22','2026-10-09')=={1:'2026-09-22',2:'2026-09-22'}


def test_legu_new_only_skips_matching_cutoff_but_refreshes_moving_month(db):
    batches=[]
    for method in ('ordinary','equal','median'):
        batch,_,row=source(db,'legu','pb.'+method)
        with db.connection(write=True) as conn:
            publish(conn,[{**row,'frequency':'monthly','pe_rolling':None,'pb':1.5}])
        finish_batch(db,batch,'complete');batches.append(batch)
    collector=ic.IndexCollector(db,'test')
    keys=['pb.'+m for m in ('ordinary','equal','median')]
    assert collector.incremental_start('2026-07-01','2026-10-09','legu',keys,'cn:000300',new_only=True)>'2026-10-09'
    assert collector.incremental_start('2026-07-01','2026-10-12','legu',keys,'cn:000300',new_only=True)=='2026-10-01'


def test_review_keeps_explicit_overlap_and_does_not_skip_latest(db):
    batch,_,row=source(db)
    with db.connection(write=True) as conn:publish(conn,[row])
    finish_batch(db,batch,'complete')
    payload={'data':[dict(indexCode='000300',indexNameCn='沪深300',tradeDate='20261009',peg=13.1)]}
    client,session,_=http(db,[response(payload)])
    collector=ic.IndexCollector(db,'test',request_collector=client)
    collector.trading_days={'2026-10-09'}
    collector.collect_cs_codes('2026-09-22','2026-10-09','review',{'000300':'沪深300'})
    assert len(session.calls)==1


def test_calendar_cache_reuses_verified_file_without_network(db):
    batch=begin_batch(db,'old','index.calendar','sina')
    sid=archive_source(db,batch,'sina','index.calendar',{'url':'https://example.test/calendar'},1,
        raw=b'calendar fixture',response=response(),obtained_at='2026-10-11T01:00:00+00:00')
    parsed_source(db,sid,fields=['trading_date']);finish_batch(db,batch,'complete')
    client,session,_=http(db,[])
    collector=ic.IndexCollector(db,'test',request_collector=client)
    dates={(date(2020,1,1)+ic.timedelta(days=d)).isoformat() for d in range(5000)}
    collector.decode_calendar=lambda text:dates
    collector.load_calendar(cached=True)
    assert collector.trading_days==dates and not session.calls


def test_sw_only_latest_level_skips_while_other_level_remains_pending(db):
    batch,_,row=source(db,'sws','analysis.daily')
    with db.connection(write=True) as conn:
        catalog(conn,'sw:1','一级','industry',system='sw',level=1,provider='sws',provider_code='1')
        publish(conn,[{**row,'index_code':'sw:1','pe_source':12,'pe_rolling':None}])
    finish_batch(db,batch,'complete')
    collector=ic.IndexCollector(db,'test')
    collector.trading_days={'2026-09-22','2026-10-08','2026-10-09'}
    assert collector.sw_incremental_starts('2026-09-22','2026-10-09')=={1:'2026-10-10',2:'2026-09-22'}


def test_cs_boundary_copy_is_excluded_even_if_requested_start_is_a_trading_day():
    base=dict(indexCode='000688',indexNameCn='科创50',peg=None,close=1000.0,tradingVol=102090116.0,tradingValue=43.2)
    raw=[{**base,'tradeDate':'20161011'},{**base,'tradeDate':'20191231'},
         {**base,'tradeDate':'20200102','peg':20.1,'close':1010.0}]
    rows,cats,anomalies=ic.parse_rows(raw,provider='csindex',series_keys=['history.rolling'],code_key='indexCode',name_key='indexNameCn',date_key='tradeDate',
        start='2016-10-11',end='2020-01-02',trading_days={'2016-10-11','2019-12-31','2020-01-02'},batch_id=1,source_id=1,expected_code='000688')
    assert [r['observation_date'] for r in rows]==['2019-12-31','2020-01-02']
    assert anomalies==[dict(code='000688',date='2016-10-11',reason='boundary_placeholder_duplicate',copied_from='2019-12-31')]
    assert rows[0]['pe_rolling'] is None  # legitimate missing PE at a real source date remains missing


def test_coverage_separates_source_dates_from_valid_metric_dates(db):
    batch,sid,row=source(db)
    rows=[{**row,'observation_date':'2019-12-31','pe_rolling':None,'explicit_nulls':True},row]
    with db.connection(write=True) as conn:publish(conn,rows)
    summary=read_indices(db)['coverage'][0]
    assert summary['first_date']=='2019-12-31' and summary['observations']==2
    assert summary['pe_rolling_first_date']=='2026-10-09' and summary['pe_rolling_count']==1
def test_wait_deadline_crossed_between_clock_reads():
    times=iter([0,0.99,1.01])
    sleeps=[]
    client=RequestCollector(None,session=object(),clock=lambda:next(times),sleep=sleeps.append)
    client.wait(1)
    assert len(sleeps)==1 and 0<sleeps[0]<=1
