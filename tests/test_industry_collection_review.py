"""Independent checks of bulk industry pacing against existing stock routing."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests

from quarterly_dashboard.collection import ProviderBlocked, RequestCollector, begin_batch, encoded
from quarterly_dashboard.storage import Database
from quarterly_dashboard import market_financial as mf
from test_market_financial import row as financial_row, PERIOD


class Clock:
    def __init__(self):self.now=1_800_000_000.0;self.lock=threading.Lock()
    def time(self):
        with self.lock:return self.now
    def sleep(self,seconds):
        with self.lock:self.now+=seconds
        time.sleep(.001)


def response(status=200):
    result=requests.Response();result.status_code=status
    result._content=b'{"success":true,"data":[]}'
    result.headers['Content-Type']='application/json'
    return result


class Session:
    def __init__(self,clock,status=200):self.clock=clock;self.status=status;self.calls=[]
    def get(self,*args,**kwargs):self.calls.append(self.clock.time());return response(self.status)


@pytest.fixture
def setup(tmp_path):
    db=Database(tmp_path/'industry-review.sqlite3');db.initialize()
    batch=begin_batch(db,'industry-review','market.financial.test','eastmoney')
    clock=Clock();session=Session(clock)
    collector=RequestCollector(db,session=session,clock=clock.time,sleep=clock.sleep,uniform=lambda a,b:a)
    return db,batch,clock,session,collector


def bulk(collector,batch,profile='financial'):
    return collector.get(batch,'eastmoney','market.financial.test','https://example.test',industry_profile=profile)


@pytest.mark.parametrize('profile,concurrency',[('financial',2),('snapshot',1)])
def test_industry_policy_waits_two_to_five_seconds_and_fifteen_after_twenty(setup,profile,concurrency):
    _,batch,clock,session,collector=setup
    policy=collector.policy_for('eastmoney',industry_profile=profile)
    assert (policy.interval,policy.jitter,policy.window,policy.window_requests,
        policy.rest_every,policy.rest_min,policy.rest_max,policy.max_concurrency)==(2,3,60,30,20,15,15,concurrency)
    state=collector.state('eastmoney');state['budget_json']=encoded(dict(starts=[],count=19,completed=19))
    collector.save_state(state)
    bulk(collector,batch,profile);bulk(collector,batch,profile)
    assert session.calls[1]-session.calls[0]==17
    collector.uniform=lambda a,b:b
    bulk(collector,batch,profile);bulk(collector,batch,profile)
    assert session.calls[-1]-session.calls[-2]==5


def test_stock_fast_still_ignores_bulk_wait_and_full_rolling_window(setup):
    db,batch,clock,session,collector=setup
    state=collector.state('eastmoney')
    state['budget_json']=encoded(dict(starts=[clock.time()]*30,ready_at=clock.time()+3600,count=30,completed=30))
    collector.save_state(state)
    before=clock.time()
    collector.get(batch,'eastmoney','stock.financing','https://example.test/stock',
        stock_fast=True,evidence_mode='metadata_only')
    assert session.calls==[before]
    state=json.loads(collector.state('eastmoney')['budget_json'])
    assert len(state['starts'])==31 and state['count']==31 and state['completed']==31
    assert state['ready_at']>=before+3600


@pytest.mark.parametrize('first',['bulk','stock'])
def test_stock_and_bulk_refusals_share_cooldown_in_both_directions(setup,first):
    db,batch,clock,session,collector=setup
    session.status=403
    with pytest.raises(ProviderBlocked):
        if first=='bulk':bulk(collector,batch)
        else:collector.get(batch,'eastmoney','stock.financing','https://example.test/stock',stock_fast=True,evidence_mode='metadata_only')
    stock=Session(clock)
    other=RequestCollector(db,session=stock,clock=clock.time,sleep=clock.sleep,uniform=lambda a,b:a)
    with pytest.raises(ProviderBlocked):
        if first=='bulk':other.get(batch,'eastmoney','stock.financing','https://example.test/stock',stock_fast=True,evidence_mode='metadata_only')
        else:bulk(other,batch)
    assert not stock.calls


def test_bulk_counts_prior_stock_requests_without_resetting_shared_window(setup):
    db,batch,clock,session,collector=setup
    state=collector.state('eastmoney')
    state['budget_json']=encoded(dict(starts=[clock.time()-10]*30,count=30,completed=30,ready_at=0))
    collector.save_state(state)
    before=clock.time()
    bulk(collector,batch)
    assert session.calls[0]>=before+50
    state=json.loads(collector.state('eastmoney')['budget_json'])
    assert state['count']==31 and state['completed']==31


def test_stock_waits_for_bulk_slots_but_not_bulk_completion_interval(setup):
    db,batch,clock,_,_=setup
    entered=threading.Event();release=threading.Event();stock_entered=threading.Event()
    guard=threading.Lock();active=[0];maximum=[0];stock_previous=[];stock_starts=[]
    class BulkSession:
        def get(self,*args,**kwargs):
            with guard:
                active[0]+=1;maximum[0]=max(maximum[0],active[0])
                if active[0]==2:entered.set()
            assert release.wait(5)
            with guard:active[0]-=1
            return response()
    class StockSession:
        def get(self,*args,**kwargs):
            stock_starts.append(clock.time())
            with db.connection() as conn:
                state=json.loads(conn.execute("SELECT budget_json FROM collection_provider_state WHERE source_system='eastmoney'").fetchone()[0])
            stock_previous.append(state['completed_at'])
            stock_entered.set()
            return response()
    def client(session):return RequestCollector(db,session=session,clock=clock.time,sleep=clock.sleep,uniform=lambda a,b:a)
    clients=[client(BulkSession()) for _ in range(2)];stock=client(StockSession())
    with ThreadPoolExecutor(max_workers=3) as pool:
        pending=[pool.submit(bulk,item,batch) for item in clients]
        try:
            assert entered.wait(5)
            pending.append(pool.submit(stock.get,batch,'eastmoney','stock.financing','https://example.test/stock',stock_fast=True,evidence_mode='metadata_only'))
            assert not stock_entered.wait(.05)
        finally:release.set()
        for future in pending:future.result(timeout=5)
    assert maximum[0]==2 and len(stock_starts)==1
    assert 0<=stock_starts[0]-stock_previous[0]<2


def test_index_and_cninfo_policies_are_unchanged(setup):
    *_,collector=setup
    index=collector.policy_for('csindex',index_parallel=True)
    cninfo=collector.policy_for('cninfo')
    normal=collector.policy_for('eastmoney')
    assert (index.max_concurrency,index.rest_min,index.rest_max)==(2,10,10)
    assert (cninfo.max_concurrency,cninfo.interval,cninfo.rest_min)==(1,2,0)
    assert (normal.max_concurrency,normal.interval,normal.window_requests)==(1,10,5)


def test_real_four_interface_collector_overlaps_two_and_keeps_each_page_order(setup):
    db,_,clock,_,_=setup
    gate=threading.Event();guard=threading.Lock();active=[0];maximum=[0];calls={name:[] for name in mf.FIELDS};closed=[]
    class FinancialSession:
        def get(self,url,**kwargs):
            params=kwargs['params'];name=next(k for k,v in mf.FIELDS.items() if v['report']==params['reportName'])
            page=params['pageNumber']
            with guard:
                active[0]+=1;maximum[0]=max(maximum[0],active[0]);calls[name].append(page)
                if active[0]==2:gate.set()
            gate.wait(2);time.sleep(.01)
            rows=[]
            for i in range((page-1)*500,min(page*500,501)):
                item=financial_row(name);code=str(600000+i)
                item.update(SECURITY_CODE=code,SECUCODE=code+'.SH');rows.append(item)
            result=response();result._content=encoded(dict(success=True,code=0,result=dict(count=501,pages=2,data=rows))).encode()
            with guard:active[0]-=1
            return result
        def close(self):closed.append(id(self))
    client=RequestCollector(db,session_factory=FinancialSession,clock=clock.time,sleep=clock.sleep,uniform=lambda a,b:a)
    datasets=mf.collect_public(db,'industry-review-pages',PERIOD,client,parallel_interfaces=True)
    assert maximum[0]==2
    assert all(pages==[1,2] for pages in calls.values())
    assert len(set(closed))==4 and client.requests==8
    with db.connection() as conn:
        assert all(conn.execute(f'SELECT count(*) FROM market_financial_{name}').fetchone()[0]==0 for name in mf.FIELDS)
    with db.connection(write=True) as conn:summary=mf.publish(conn,None,PERIOD,datasets)
    assert all(item['rows']==501 for item in summary.values())


def test_parallel_collection_failure_leaves_no_partial_facts_or_abandoned_batches(setup):
    db,_,clock,_,_=setup
    class FinancialSession:
        def get(self,url,**kwargs):
            name=next(k for k,v in mf.FIELDS.items() if v['report']==kwargs['params']['reportName'])
            item=financial_row(name)
            if name=='income':item['SECURITY_CODE']='invalid'
            result=response();result._content=encoded(dict(success=True,code=0,result=dict(count=1,pages=1,data=[item]))).encode()
            return result
        def close(self):pass
    client=RequestCollector(db,session_factory=FinancialSession,clock=clock.time,sleep=clock.sleep,uniform=lambda a,b:a)
    with pytest.raises(ValueError):mf.collect_public(db,'industry-review-failure',PERIOD,client,parallel_interfaces=True)
    with db.connection() as conn:
        assert all(conn.execute(f'SELECT count(*) FROM market_financial_{name}').fetchone()[0]==0 for name in mf.FIELDS)
        actual=conn.execute("SELECT count(*) FROM collection_sources s JOIN collection_batches b ON b.id=s.batch_id WHERE b.operation_id='industry-review-failure'").fetchone()[0]
        assert actual==client.requests
        assert conn.execute("SELECT count(*) FROM collection_batches WHERE operation_id='industry-review-failure' AND status='running'").fetchone()[0]==0
