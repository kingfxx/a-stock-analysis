import json

import pytest

from quarterly_dashboard.collection import (PacingPolicy, ProviderBlocked, RequestCollector,
    begin_batch)
from quarterly_dashboard.storage import Database


class Clock:
    def __init__(self): self.value=1_800_000_000.0
    def time(self): return self.value
    def sleep(self,seconds): self.value+=seconds


class Response:
    def __init__(self,status=200,body=b'{"data":[]} ',headers=None):
        self.status_code=status;self.content=body;self.text=body.decode();self.headers=headers or {'Content-Type':'application/json'}
    def json(self): return json.loads(self.text)


class Session:
    def __init__(self,clock,responses=None): self.clock=clock;self.responses=iter(responses or []);self.calls=[]
    def get(self,*args,**kwargs):
        self.calls.append(self.clock.time())
        try: return next(self.responses)
        except StopIteration: return Response()


def setup(tmp_path,*,responses=None,policy=None):
    db=Database(tmp_path/'pacing.sqlite3');db.initialize()
    batch=begin_batch(db,'test-op','test.dataset','cninfo')
    clock=Clock();session=Session(clock,responses)
    collector=RequestCollector(db,session=session,policy=policy,clock=clock.time,sleep=clock.sleep,uniform=lambda *_:0)
    return db,batch,clock,session,collector


def test_provider_specific_policy_cannot_be_used_as_global_policy(tmp_path):
    db=Database(tmp_path/'policy.sqlite3');db.initialize()
    with pytest.raises(ValueError):
        RequestCollector(db,policy=RequestCollector(db).policy_for('cninfo'))


def call(collector,batch,provider='cninfo',*,stock_fast=False):
    return collector.get(batch,provider,'test.dataset','https://example.test/data',expect='json',stock_fast=stock_fast)


def test_cninfo_get_uses_two_second_interval_and_no_twentieth_request_rest(tmp_path):
    _,batch,clock,session,collector=setup(tmp_path)
    for _ in range(21): call(collector,batch)
    assert [b-a for a,b in zip(session.calls,session.calls[1:])] == [2]*20
    assert collector.report()['provider_policies']['cninfo']['interval']==2
    assert collector.report()['provider_policies']['cninfo']['window_requests']==30


@pytest.mark.parametrize('transport',['get','invoke'])
def test_cninfo_window_crosses_collectors_and_stock_fast_does_not_bypass(tmp_path,transport):
    db,batch,clock,session,collector=setup(tmp_path)
    initial=clock.value
    state=collector.state('cninfo')
    state['budget_json']=json.dumps({'starts':[clock.value-10]*30,'ready_at':clock.value,'count':30})
    collector.save_state(state)
    second=RequestCollector(db,session=session,clock=clock.time,sleep=clock.sleep,uniform=lambda *_:0)
    if transport=='get':
        call(second,batch,stock_fast=True)
    else:
        class SdkResponse:
            status_code=200;headers={'Content-Type':'application/json'};text=''
        response=SdkResponse()
        second.invoke(batch,'cninfo','test.dataset',{'method':'POST'},lambda:response,
            serialize=lambda _:b'{}',response_of=lambda _:response)
    sent_at=session.calls[0] if transport=='get' else clock.value
    assert sent_at>=initial+50


def test_cninfo_invoke_shares_pacing_and_policy_with_get(tmp_path):
    _,batch,clock,session,collector=setup(tmp_path)
    call(collector,batch)
    at=[]
    class SdkResponse:
        status_code=200
        headers={'Content-Type':'application/json'}
        text=''
    sdk=SdkResponse()
    collector.invoke(batch,'cninfo','test.dataset',{'method':'POST'},lambda:(at.append(clock.time()) or sdk),
        serialize=lambda _:b'{}',response_of=lambda _:sdk)
    assert at[0]-session.calls[0]==2


def test_cninfo_denial_cools_down_and_other_provider_keeps_default_policy(tmp_path):
    denied=Response(403,b'{"data":[]}',{'Content-Type':'application/json','Retry-After':'7200'})
    db,batch,clock,session,collector=setup(tmp_path,responses=[denied])
    with pytest.raises(ProviderBlocked): call(collector,batch)
    state=collector.state('cninfo')
    deadline=__import__('datetime').datetime.fromisoformat(state['cooldown_until']).timestamp()
    assert deadline-clock.value>=7200
    assert json.loads(state['budget_json'])['count']==1
    stock_batch=begin_batch(db,'test-stock','stock.dataset','sina')
    stock_session=Session(clock)
    stock=RequestCollector(db,session=stock_session,clock=clock.time,sleep=clock.sleep,uniform=lambda *_:0)
    call(stock,stock_batch,'sina',stock_fast=False)
    call(stock,stock_batch,'sina',stock_fast=False)
    assert stock_session.calls[1]-stock_session.calls[0]>=10
    assert stock.policy_for('sina')==PacingPolicy()


def test_repeated_cninfo_denial_extends_cooldown(tmp_path):
    denied=Response(403,b'{"data":[]}')
    db,batch,clock,session,collector=setup(tmp_path,responses=[denied])
    with pytest.raises(ProviderBlocked): call(collector,batch)
    first=collector.state('cninfo')
    first_deadline=__import__('datetime').datetime.fromisoformat(first['cooldown_until']).timestamp()
    clock.value=first_deadline+1
    again=RequestCollector(db,session=Session(clock,[denied]),clock=clock.time,sleep=clock.sleep,uniform=lambda *_:0)
    with pytest.raises(ProviderBlocked): call(again,batch)
    state=again.state('cninfo')
    deadline=__import__('datetime').datetime.fromisoformat(state['cooldown_until']).timestamp()
    assert state['denials']==2
    assert deadline-clock.value>=7200


def test_non_cninfo_stock_fast_still_skips_interval_and_window(tmp_path):
    _,batch,clock,session,collector=setup(tmp_path)
    # Create a source batch whose provider matches the stock request below.
    with collector.db.connection(write=True) as conn:
        conn.execute("UPDATE collection_batches SET source_system='sina' WHERE id=?",(batch,))
    call(collector,batch,'sina',stock_fast=True)
    call(collector,batch,'sina',stock_fast=True)
    assert session.calls[1]==session.calls[0]
