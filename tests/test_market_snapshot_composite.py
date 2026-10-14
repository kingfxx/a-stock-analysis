import json

import pytest
import requests

from quarterly_dashboard.collection import RequestCollector, ProviderBlocked, begin_batch, archive_source, read_source
from quarterly_dashboard.market_trade_snapshot import FIELDS, normalized, publish, publish_metric_patch
from quarterly_dashboard.storage import Database

COMPOSITE='historical-composite-v1'

@pytest.fixture
def db(tmp_path):
    result=Database(tmp_path/'test.sqlite3');result.initialize();return result

def source(db):
    batch=begin_batch(db,'test','market.trade_snapshot','composite')
    sid=archive_source(db,batch,'composite','market.trade_snapshot',{'operation':'compute'},1,raw=b'{}')
    return batch,sid

def values(cap=1000,close=10,pe=5):
    row={field:None for field in FIELDS}
    row.update(SECURITY_CODE='000001',SECUCODE='000001.SZ',TRADE_DATE='2015-03-31',
        TOTAL_SHARES=100 if cap is not None else None,TOTAL_MARKET_CAP=cap,CLOSE_PRICE=close,PE_TTM=pe,CHANGE_RATE=0)
    return normalized(row,'2015-03-31')

def test_later_metric_supplement_retains_full_row_and_versions(db):
    b,s=source(db)
    with db.connection(write=True) as c:publish(c,b,[(values(),s,'2026-10-14')],definition=COMPOSITE)
    b2,s2=source(db)
    with db.connection(write=True) as c:
        assert publish_metric_patch(c,b2,('000001','sz','2015-03-31'),{'ps_ttm':1.8},s2,'2026-10-15',definition=COMPOSITE)['revised']==1
        records=c.execute('SELECT * FROM market_trade_snapshot ORDER BY version').fetchall()
        assert len(records)==2 and records[0]['is_latest']==0 and records[0]['ps_ttm'] is None
        assert records[1]['version']==2 and records[1]['is_latest']==1 and records[1]['ps_ttm']==1.8
        assert records[1]['close_price']==10 and records[1]['total_market_cap']==1000 and records[1]['pe_ttm']==5
        assert publish_metric_patch(c,b2,('000001','sz','2015-03-31'),{'ps_ttm':1.8},s2,'2026-10-15',definition=COMPOSITE)['unchanged']==1
    with pytest.raises(ValueError,match='有值变空'):
        with db.connection(write=True) as c:publish_metric_patch(c,b2,('000001','sz','2015-03-31'),{'pe_ttm':None},s2,'2026-10-15',definition=COMPOSITE)
    with db.connection() as c:assert c.execute('SELECT count(*) FROM market_trade_snapshot').fetchone()[0]==2

def test_independent_definitions_and_field_priority(db):
    b,s=source(db)
    with db.connection(write=True) as c:
        publish(c,b,[(values(),s,'now')],definition=COMPOSITE)
        publish(c,b,[(values(cap=None,close=11),s,'now')])
        caps=c.execute('SELECT * FROM market_cap_latest').fetchall()
        assert len(caps)==1 and caps[0]['source']=='composite:historical-composite-v1'
        publish(c,b,[(values(cap=1200,close=12),s,'now')])
        caps=c.execute('SELECT * FROM market_cap_latest').fetchall()
        assert len(caps)==1 and caps[0]['total_cap']==1200 and caps[0]['source']=='eastmoney:market_trade_snapshot'
        assert c.execute('SELECT count(*) FROM market_trade_snapshot WHERE is_latest=1').fetchone()[0]==2
        assert c.execute('PRAGMA foreign_key_check').fetchall()==[]

def test_multi_row_atomic_failure(db):
    b,s=source(db)
    second=values();second.update(security_code='000002',secucode='000002.SZ')
    with pytest.raises(Exception):
        with db.connection(write=True) as c:publish(c,b,[(values(),s,'now'),(second,s+100,'now')],definition=COMPOSITE)
    with db.connection() as c:assert c.execute('SELECT count(*) FROM market_trade_snapshot').fetchone()[0]==0

class Clock:
    now=10000
    def time(self):return self.now
    def sleep(self,n):self.now+=n

def test_sdk_transport_shared_budget_and_binary_archive(db):
    b,s=source(db);clock=Clock();starts=[]
    def invoke(collector):
        def callback():starts.append(clock.now);clock.sleep(2);return b'arrow-evidence'
        return collector.invoke(b,'bigquant','test.sdk',{'transport':'flight'},callback,serialize=lambda v:v)
    for _ in range(6):
        c=RequestCollector(db,clock=clock.time,sleep=clock.sleep,uniform=lambda a,b:a)
        value,sid=invoke(c);assert value==read_source(db,sid)[0]==b'arrow-evidence'
    assert all(b-a>=12 for a,b in zip(starts,starts[1:]))
    assert starts[5]-starts[0]>=60

def test_sdk_denial_stops_and_persists_cooldown(db):
    b,s=source(db);clock=Clock();client=RequestCollector(db,clock=clock.time,sleep=clock.sleep,uniform=lambda a,b:a)
    r=requests.Response();r.status_code=429;r._content=b'{"error":"too many requests"}'
    with pytest.raises(ProviderBlocked):client.invoke(b,'bigquant','test.sdk',{'transport':'http'},lambda:r,serialize=lambda r:r.content,response_of=lambda r:r)
    with db.connection() as c:
        state=c.execute("SELECT * FROM collection_provider_state WHERE source_system='bigquant'").fetchone()
        assert state['cooldown_until'] and state['denials']==1
    with pytest.raises(ProviderBlocked):client.invoke(b,'bigquant','test.sdk',{},lambda:pytest.fail('request after denial'),serialize=lambda v:v)

def test_sdk_retry_preserves_attempt_and_redacts_error(db):
    b,s=source(db);clock=Clock();client=RequestCollector(db,clock=clock.time,sleep=clock.sleep,uniform=lambda a,b:a)
    calls=[]
    def callback():
        calls.append(clock.now)
        if len(calls)==1:raise requests.Timeout('private credential')
        return b'ok'
    _,sid=client.invoke(b,'bigquant','test.sdk',{},callback,serialize=lambda v:v)
    assert calls[1]-calls[0]>=60
    with db.connection() as c:
        rows=c.execute("SELECT * FROM collection_sources WHERE dataset='test.sdk' ORDER BY id").fetchall()
        assert len(rows)==2 and rows[0]['error']=='Timeout' and rows[1]['attempt_no']==2
