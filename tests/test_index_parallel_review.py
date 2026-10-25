"""Independent review checks for the user-authorized index pacing policy."""
import json
import multiprocessing
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import requests

from stock_analysis.collection.collection import (Cancelled, PacingPolicy, ProviderBlocked,
    RequestCollector, begin_batch, encoded)
from stock_analysis.infrastructure.storage import Database
from stock_analysis.domains.indices import index_collection as ic


class Clock:
    def __init__(self):self.now=1_800_000_000.0
    def time(self):return self.now
    def sleep(self,seconds):self.now+=seconds


def reply(status=200):
    result=requests.Response()
    result.status_code=status
    result._content=b'{"data":[]}'
    result.headers['Content-Type']='application/json'
    return result


class Session:
    def __init__(self,clock):self.clock=clock;self.starts=[]
    def get(self,*args,**kwargs):
        self.starts.append(self.clock.time())
        return reply()


@pytest.fixture
def setup(tmp_path):
    db=Database(tmp_path/'review.sqlite3');db.initialize()
    batch=begin_batch(db,'review','index.review','csindex')
    clock=Clock();session=Session(clock)
    collector=RequestCollector(db,session=session,clock=clock.time,sleep=clock.sleep,
        uniform=lambda a,b:a)
    return db,batch,clock,session,collector


def send(collector,batch):
    return collector.get(batch,'csindex','index.review','https://example.test',index_parallel=True)


def test_index_wait_uses_two_seconds_and_three_seconds_jitter(setup):
    _,batch,clock,session,collector=setup
    send(collector,batch);send(collector,batch)
    assert session.starts[1]-session.starts[0]==2
    collector.uniform=lambda a,b:b
    send(collector,batch);send(collector,batch)
    assert session.starts[-1]-session.starts[-2]==5


def test_index_window_waits_for_thirtieth_shared_start_to_expire(setup):
    db,batch,clock,session,collector=setup
    state=collector.state('csindex')
    state['budget_json']=encoded(dict(starts=[clock.now-10]*30,count=30,completed=30))
    collector.save_state(state)
    before=clock.now
    send(collector,batch)
    assert session.starts==[pytest.approx(before+50.001)]


def test_twentieth_completed_request_adds_ten_seconds(setup):
    _,batch,clock,session,collector=setup
    state=collector.state('csindex')
    state['budget_json']=encoded(dict(starts=[],count=19,completed=19))
    collector.save_state(state)
    send(collector,batch);send(collector,batch)
    assert session.starts[1]-session.starts[0]==12


def test_existing_budget_keeps_completion_count_when_new_field_is_absent(setup):
    _,batch,clock,session,collector=setup
    state=collector.state('csindex')
    state['budget_json']=encoded(dict(starts=[],count=19))
    collector.save_state(state)
    send(collector,batch);send(collector,batch)
    assert session.starts[1]-session.starts[0]==12


def test_default_request_honors_ready_time_despite_stale_parallel_metadata(setup):
    _,batch,clock,session,collector=setup
    state=collector.state('csindex')
    state['budget_json']=encoded(dict(starts=[],count=10,completed=9,
        completed_at=clock.now-10,ready_at=clock.now+60,
        inflight=[dict(token='crashed',started_at=clock.now-1,until=clock.now+100,limit=2)]))
    collector.save_state(state)
    before=clock.now
    collector.get(batch,'csindex','index.review','https://example.test/default')
    assert session.starts[0]>=before+60


@pytest.mark.parametrize('provider',[None,'cninfo'])
def test_only_authorized_index_policy_can_have_two_slots(provider):
    kwargs={} if provider is None else dict(provider='cninfo',interval=2,jitter=0,
        window_requests=30,rest_min=0,rest_max=0)
    with pytest.raises(ValueError):PacingPolicy(max_concurrency=2,**kwargs)


def test_cancel_during_network_still_archives_and_releases_shared_slot(setup):
    db,batch,clock,session,collector=setup
    cancelled=threading.Event()
    class CancelSession:
        def get(self,*args,**kwargs):
            cancelled.set()
            return reply()
    collector.session=CancelSession();collector.cancelled=cancelled.is_set
    try:send(collector,batch)
    except Cancelled:pass
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM collection_sources').fetchone()[0]==1
    state=collector.state('csindex')
    assert not json.loads(state['budget_json']).get('inflight')
    # A fresh request must be able to take both slots after finalization.
    other=RequestCollector(db,session=session,clock=clock.time,sleep=clock.sleep,
        uniform=lambda a,b:a)
    other.get(batch,'csindex','index.review','https://example.test/default')


def test_expired_cooldown_has_one_actual_probe_and_one_budget_reservation(setup):
    db,batch,clock,session,_=setup
    barrier=threading.Barrier(2)
    class PausedCollector(RequestCollector):
        def reserve_slot(self,*args,**kwargs):
            value=super().reserve_slot(*args,**kwargs)
            try:barrier.wait(.2)
            except threading.BrokenBarrierError:pass
            return value
    clients=[PausedCollector(db,session=session,clock=clock.time,sleep=clock.sleep,
        uniform=lambda a,b:a) for _ in range(2)]
    state=clients[0].state('csindex')
    state.update(cooldown_until=datetime.fromtimestamp(clock.now-1,timezone.utc).isoformat(),
        reason='HTTP 429',denials=1)
    clients[0].save_state(state)
    def run(client):
        try:return send(client,batch)
        except ProviderBlocked:return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run,clients))
    assert len(session.starts)==1
    assert sum(client.requests for client in clients)==1


def _process_request(path,batch,active,maximum,gate):
    class SlowSession:
        def get(self,*args,**kwargs):
            with active.get_lock():
                active.value+=1
                maximum.value=max(maximum.value,active.value)
                if active.value==2:gate.set()
            gate.wait(5)
            time.sleep(.15)
            with active.get_lock():active.value-=1
            return reply()
    collector=RequestCollector(Database(path),session=SlowSession())
    send(collector,batch)


def test_index_concurrency_cap_is_shared_across_processes(setup):
    db,batch,*_=setup
    context=multiprocessing.get_context('spawn')
    active=context.Value('i',0);maximum=context.Value('i',0);gate=context.Event()
    children=[context.Process(target=_process_request,args=(str(db.path),batch,active,maximum,gate))
              for _ in range(3)]
    try:
        for child in children:child.start()
        for child in children:
            child.join(20)
            assert child.exitcode==0
        assert maximum.value==2
    finally:
        for child in children:
            if child.is_alive():child.terminate();child.join(5)


@pytest.mark.parametrize('outcome',['complete','failed','cancelled'])
def test_operation_runs_sources_together_and_counts_failed_or_cancelled_workers(setup,monkeypatch,outcome):
    db,*_=setup
    barrier=threading.Barrier(3)
    sessions=[]
    class FakeCollector:
        def __init__(self,db,operation,progress):
            self.db=db;self.operation=operation;self.progress=progress
            self.http=SimpleNamespace(requests=0,retries=0,halted={},session=object())
            self.http.report=lambda:dict(network_requests=self.http.requests,
                retries=self.http.retries,halted=self.http.halted)
            sessions.append(self.http.session)
            self.trading_days=set()
        def load_calendar(self,**kwargs):
            self.trading_days={f'2026-10-{day:02}' for day in range(1,15)}
        def run(self,provider,count):
            self.http.requests=count
            self.progress(provider+' running')
            barrier.wait(3)
            if outcome=='cancelled':
                ic.cancel_operation(db)
                raise Cancelled('review cancellation')
            if provider=='legu' and outcome=='failed':
                self.http.retries=1
                raise ValueError('review source failure')
        def collect_cs(self,*args):self.run('csindex',2)
        def collect_legu(self,*args):self.run('legu',3)
        def collect_sw(self,*args):self.run('sws',4)
    monkeypatch.setattr(ic,'IndexCollector',FakeCollector)
    operation=ic.start_operation(db,'incremental',start='2026-10-01',end='2026-10-14')
    result=ic.run_operation(db,operation)
    summary=json.loads(result['result_json'])
    assert result['status']==outcome
    assert summary['network_requests']==9
    assert set(summary['providers'])=={'csindex','legu','sws'}
    assert len({id(session) for session in sessions})==4
    if outcome=='failed':
        assert summary['retries']==1
        assert 'legu' in summary['errors']
