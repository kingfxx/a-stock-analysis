import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests

from stock_analysis.collection.collection import RequestCollector, begin_batch, encoded
from stock_analysis.collection.collection import PacingPolicy
from stock_analysis.domains.indices.index_collection import IndexCollector,start_operation
from stock_analysis.infrastructure.storage import Database


class ConcurrentSession:
    def __init__(self, gate, lock, active, maximum):
        self.gate=gate;self.lock=lock;self.active=active;self.maximum=maximum

    def get(self,url,**kwargs):
        with self.lock:
            self.active[0]+=1
            self.maximum[0]=max(self.maximum[0],self.active[0])
            if self.active[0]>=2:self.gate.set()
        self.gate.wait(5)
        reply=requests.Response()
        reply.status_code=200
        reply._content=encoded({'code':'200','data':[]}).encode()
        reply.headers['Content-Type']='application/json'
        reply.encoding='utf-8'
        with self.lock:self.active[0]-=1
        return reply


@pytest.fixture
def db(tmp_path):
    database=Database(tmp_path/'parallel.sqlite3')
    database.initialize()
    return database


def test_same_index_provider_reaches_exactly_two_parallel_http_requests(db):
    gate=threading.Event();lock=threading.Lock();active=[0];maximum=[0]
    batch=begin_batch(db,'operation','index.parallel.test','csindex')
    clients=[RequestCollector(db,session=ConcurrentSession(gate,lock,active,maximum)) for _ in range(3)]
    def run(i):
        return clients[i].get(batch,'csindex','index.parallel.test',f'https://example.test/{i}',index_parallel=True)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures=[pool.submit(run,i) for i in range(3)]
        results=[future.result(timeout=15) for future in futures]
    assert maximum[0]==2
    assert len(results)==3
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM collection_sources').fetchone()[0]==3


def test_different_index_providers_overlap_and_default_policy_remains_conservative(db):
    gate=threading.Event();lock=threading.Lock();active=[0];maximum=[0]
    clients=[RequestCollector(db,session=ConcurrentSession(gate,lock,active,maximum)) for _ in range(2)]
    batches=[begin_batch(db,'operation','index.parallel.test',provider) for provider in ('csindex','legu')]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda item:item[0].get(item[1],item[2],'index.parallel.test',
            f'https://{item[2]}.example.test',index_parallel=True),zip(clients,batches,('csindex','legu'))))
    assert maximum[0]==2 and len(results)==2
    assert clients[0].policy.interval==10 and clients[0].policy.window_requests==5
    assert clients[0].policy_for('csindex',index_parallel=True).max_concurrency==2


def test_legu_shared_scheduler_serializes_independent_collectors(db):
    lock=threading.Lock();active=[0];maximum=[0];spans=[]
    class SerialSession:
        def get(self,url,**kwargs):
            with lock:
                active[0]+=1
                maximum[0]=max(maximum[0],active[0])
                started=time.monotonic()
            time.sleep(.1)
            with lock:
                spans.append((started,time.monotonic()))
                active[0]-=1
            reply=requests.Response();reply.status_code=200
            reply._content=b'{"data":[]}'
            reply.headers['Content-Type']='application/json'
            return reply
    batch=begin_batch(db,'serial-legu','index.legu.test','legu')
    clients=[RequestCollector(db,session=SerialSession(),uniform=lambda low,high:low) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(client.get,batch,'legu','index.legu.test',
                             f'https://example.test/{i}',index_parallel=True)
                 for i,client in enumerate(clients)]
        results=[future.result(timeout=15) for future in futures]
    spans.sort()
    assert maximum[0]==1 and len(results)==2
    assert spans[1][0]-spans[0][1]>=2.95
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM collection_sources').fetchone()[0]==2


class FakeClock:
    def __init__(self):self.now=1000
    def time(self):return self.now
    def sleep(self,seconds):self.now+=seconds


class ImmediateSession:
    def __init__(self,clock,cancel=None):self.clock=clock;self.cancel=cancel;self.calls=[]
    def get(self,url,**kwargs):
        self.calls.append(self.clock.time())
        if self.cancel:self.cancel[0]=True
        reply=requests.Response();reply.status_code=200
        reply._content=encoded({'code':'200','data':[]}).encode()
        reply.headers['Content-Type']='application/json';reply.encoding='utf-8'
        return reply


def test_legu_completion_spacing_uses_three_to_seven_seconds(db):
    clock=FakeClock();session=ImmediateSession(clock)
    client=RequestCollector(db,session=session,clock=clock.time,sleep=clock.sleep,uniform=lambda low,high:low)
    batch=begin_batch(db,'legu-spacing','index.legu.test','legu')
    def send():client.get(batch,'legu','index.legu.test','https://example.test',index_parallel=True)
    send();send()
    assert session.calls[1]-session.calls[0]==3
    client.uniform=lambda low,high:high
    send();send()
    assert session.calls[-1]-session.calls[-2]==7
    state=client.state('legu')
    budget=json.loads(state['budget_json']);budget['completed']=19
    state['budget_json']=encoded(budget);client.save_state(state)
    send();send()
    assert session.calls[-1]-session.calls[-2]==17


def test_index_spacing_completed_rest_and_shared_window(db):
    clock=FakeClock();session=ImmediateSession(clock)
    client=RequestCollector(db,session=session,clock=clock.time,sleep=clock.sleep,uniform=lambda low,high:low)
    batch=begin_batch(db,'spacing','index.parallel.test','csindex')
    for i in range(21):client.get(batch,'csindex','index.parallel.test',f'https://example.test/{i}',index_parallel=True)
    assert session.calls[20]-session.calls[19]>=12

    state=client.state('legu')
    state['budget_json']=encoded({'starts':[clock.time()-59]*30,'ready_at':clock.time(),'count':30,'completed':0})
    client.save_state(state)
    before=clock.time()
    session2=ImmediateSession(clock)
    limited=RequestCollector(db,session=session2,clock=clock.time,sleep=clock.sleep,uniform=lambda low,high:low)
    batch2=begin_batch(db,'window','index.parallel.test','legu')
    limited.get(batch2,'legu','index.parallel.test','https://example.test/window',index_parallel=True)
    assert session2.calls[0]>=before+1


def test_legacy_budget_keeps_twentieth_completion_rest_and_default_honors_ready(db):
    clock=FakeClock();session=ImmediateSession(clock)
    client=RequestCollector(db,session=session,clock=clock.time,sleep=clock.sleep,uniform=lambda low,high:low)
    state=client.state('csindex')
    state['budget_json']=encoded({'starts':[],'ready_at':clock.time(),'count':19})
    client.save_state(state)
    batch=begin_batch(db,'legacy','index.parallel.test','csindex')
    client.get(batch,'csindex','index.parallel.test','https://example.test/20',index_parallel=True)
    first=session.calls[-1]
    client.get(batch,'csindex','index.parallel.test','https://example.test/21',index_parallel=True)
    assert session.calls[-1]-first>=12

    # The overlap exception is only for the index policy. A default request must
    # respect a completed request's ready_at even if an unrelated inflight exists.
    state=client.state('legu')
    now=clock.time()
    state['budget_json']=encoded({'starts':[now],'ready_at':now+15,'count':1,
                                  'inflight':[{'token':'other','until':now+180,'started_at':now,'limit':2}]})
    client.save_state(state)
    default=RequestCollector(db,session=ImmediateSession(clock),clock=clock.time,sleep=clock.sleep,uniform=lambda low,high:low)
    batch2=begin_batch(db,'default-ready','index.parallel.test','legu')
    default.get(batch2,'legu','index.parallel.test','https://example.test/default')
    assert default.session.calls[0]>=now+15


def test_cancel_after_response_still_releases_os_slots(db):
    clock=FakeClock();cancel=[False];session=ImmediateSession(clock,cancel)
    client=RequestCollector(db,session=session,clock=clock.time,sleep=clock.sleep,uniform=lambda low,high:low,
                            cancelled=lambda:cancel[0])
    batch=begin_batch(db,'cancel','index.parallel.test','csindex')
    client.get(batch,'csindex','index.parallel.test','https://example.test',index_parallel=True)
    cancel[0]=False
    # A fresh default request can obtain the same provider's exclusive OS slots.
    second=RequestCollector(db,session=ImmediateSession(clock),clock=clock.time,sleep=clock.sleep,uniform=lambda low,high:low)
    policy=second.policy_for('csindex')
    token,_,_,_,slots,_=second.reserve_slot('csindex',policy)
    second.release_slot('csindex',token,policy,1,slots)


def test_policy_rejects_unapproved_concurrency_and_reports_index_exception(db):
    with pytest.raises(ValueError):PacingPolicy(max_concurrency=2)
    with pytest.raises(ValueError):PacingPolicy(provider='cninfo',max_concurrency=2)
    report=RequestCollector(db).report()
    assert report['provider_policies']['index_parallel']['window_requests']==30
    assert report['provider_policies']['index_parallel']['rest_min']==10
    assert report['provider_policies']['index_parallel']['max_concurrency']==2
    assert report['provider_policies']['index_legu']['max_concurrency']==1
    assert report['provider_policies']['index_legu']['interval']==3
    assert report['provider_policies']['index_legu']['jitter']==4
    assert report['provider_policies']['index_legu']['window_requests']==30


def test_legu_work_units_run_serially(db,monkeypatch):
    from stock_analysis.domains.indices import index_collection as ic
    operation=start_operation(db,providers=['legu'],start='2026-10-01',end='2026-10-15')
    collector=IndexCollector(db,operation)
    monkeypatch.setattr(ic,'LG_CODES',{'000016.SH':'上证50','000300.SH':'沪深300'})
    def setup(worker,batch):
        worker.lg_headers={'X-CSRF-Token':'fixture'};worker.lg_token='fixture'
    monkeypatch.setattr(IndexCollector,'setup_legu',setup)
    lock=threading.Lock();active=[0];maximum=[0];calls=[]
    def collect(worker,code,name,dataset,start,end,batch):
        with lock:
            active[0]+=1;maximum[0]=max(maximum[0],active[0]);calls.append((code,dataset))
        time.sleep(.02)
        with lock:active[0]-=1
    monkeypatch.setattr(IndexCollector,'_collect_legu_item',collect)
    try:collector.collect_legu('2026-10-01','2026-10-15','incremental')
    finally:collector.http.session.close()
    assert maximum[0]==1
    assert calls==[(code,metric) for code in ic.LG_CODES for metric in ('pe','pb')]


def test_legu_worker_copies_csrf_headers_token_and_cookie_jar(db):
    from requests.cookies import RequestsCookieJar
    parent=IndexCollector(db,'operation')
    parent.http.session.cookies=RequestsCookieJar()
    parent.http.session.cookies.set('JSESSIONID','fixture-session',domain='legulegu.com')
    parent.lg_headers={'X-CSRF-Token':'csrf-fixture'}
    parent.lg_token='token-fixture'
    worker=parent.new_worker()
    worker.lg_headers=parent.lg_headers.copy()
    worker.lg_token=parent.lg_token
    assert worker.http.session.cookies.get('JSESSIONID',domain='legulegu.com')=='fixture-session'
    assert worker.http.session.cookies is not parent.http.session.cookies
    assert worker.lg_headers==parent.lg_headers and worker.lg_token==parent.lg_token


def test_sws_levels_keep_latest_month_first_while_running_in_parallel(db,monkeypatch):
    operation=start_operation(db,providers=['sws'],start='2026-09-01',end='2026-10-31')
    collector=IndexCollector(db,operation)
    collector.trading_days={'2026-09-10','2026-10-10'}
    lock=threading.Lock();calls={1:[],2:[]}
    def record(worker,begin,finish,level,label,mode):
        if level==1 and begin=='2026-10-01':time.sleep(.05)
        with lock:calls[level].append(begin)
    monkeypatch.setattr(IndexCollector,'_collect_sw_task',record)
    collector.collect_sw('2026-09-01','2026-10-31','full')
    assert calls[1]==['2026-10-01','2026-09-01']
    assert calls[2]==['2026-10-01','2026-09-01']
