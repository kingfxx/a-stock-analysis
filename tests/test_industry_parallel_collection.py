import json
import threading

import pytest

from stock_analysis.domains.market import market_financial as mf
from stock_analysis.collection.collection import ProviderBlocked, RequestCollector
from stock_analysis.infrastructure.storage import Database
from test_market_financial import PERIOD, row


class Clock:
    def __init__(self):
        self.value=1_800_000_000.0
        self.lock=threading.Lock()

    def time(self):
        with self.lock:
            return self.value

    def sleep(self,seconds):
        with self.lock:
            self.value+=seconds


class Response:
    def __init__(self,payload,status=200):
        self.status_code=status
        self.content=json.dumps(payload,separators=(',',':')).encode()
        self.text=self.content.decode()
        self.headers={'Content-Type':'application/json'}

    def json(self):
        return json.loads(self.text)


def payload(dataset,page):
    rows=[row(dataset,code=f'{600000+index:06d}') for index in range((page-1)*500,min(page*500,501))]
    return {'success':True,'code':0,'result':{'count':501,'pages':2,'data':rows}}


def test_daily_financial_collection_overlaps_two_interfaces_and_keeps_each_page_order(tmp_path):
    db=Database(tmp_path/'industry-parallel.sqlite3');db.initialize()
    clock=Clock()
    first_pair=threading.Event()
    tracker={'active':0,'maximum':0,'calls':0,'first_pages':0,'orders':{}}
    lock=threading.Lock()

    class Session:
        def get(self,url,*,params,**kwargs):
            dataset=next(name for name,spec in mf.FIELDS.items() if spec['report']==params['reportName'])
            page=params['pageNumber']
            with lock:
                tracker['calls']+=1
                tracker['active']+=1
                tracker['maximum']=max(tracker['maximum'],tracker['active'])
                tracker['orders'].setdefault(dataset,[]).append(page)
                if page==1:
                    tracker['first_pages']+=1
                    if tracker['first_pages']>=2:first_pair.set()
            if page==1:
                assert first_pair.wait(3), 'two financial interfaces did not reach HTTP together'
            with lock:
                tracker['active']-=1
            return Response(payload(dataset,page))

        def close(self):
            pass

    collector=RequestCollector(db,session_factory=Session,clock=clock.time,sleep=clock.sleep,uniform=lambda *_:0)
    progress=[]
    datasets=mf.collect_public(db,'industry-run:parallel',PERIOD,collector,lambda _:None,
        parallel_interfaces=True,progress_state=lambda state:progress.append(state))
    assert tracker['maximum']==2
    assert tracker['calls']==8==collector.requests
    assert all(tracker['orders'][name]==[1,2] for name in mf.FIELDS)
    assert all(len(item['rows'])==501 for item in datasets)
    assert progress[-1]['phase']=='validated'
    for state in progress:
        if state.get('interfaces'):
            for name,item in state['interfaces'].items():
                assert item['page']<=item['pages'] if item['pages'] is not None else item['page']==0
    with db.connection(write=True) as conn:
        summary=mf.publish(conn,None,PERIOD,datasets)
    assert set(summary)==set(mf.FIELDS)


def test_parallel_financial_failure_counts_sent_requests_and_returns_no_publishable_result(tmp_path):
    db=Database(tmp_path/'industry-parallel-failure.sqlite3');db.initialize()
    clock=Clock()
    tracker={'calls':0}
    lock=threading.Lock()

    class Session:
        def get(self,url,*,params,**kwargs):
            with lock:tracker['calls']+=1
            if params['reportName']==mf.FIELDS['income']['report']:
                return Response({'data':[]},status=403)
            dataset=next(name for name,spec in mf.FIELDS.items() if spec['report']==params['reportName'])
            return Response({'success':True,'code':0,'result':{'count':1,'pages':1,'data':[row(dataset)]}})

        def close(self):
            pass

    collector=RequestCollector(db,session_factory=Session,clock=clock.time,sleep=clock.sleep,uniform=lambda *_:0)
    with pytest.raises((ProviderBlocked,RuntimeError)):
        mf.collect_public(db,'industry-run:failed',PERIOD,collector,lambda _:None,parallel_interfaces=True)
    assert collector.requests==tracker['calls']
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM market_financial_income').fetchone()[0]==0
        assert conn.execute('SELECT count(*) FROM market_financial_balance').fetchone()[0]==0
        batches=conn.execute("SELECT status FROM collection_batches WHERE coalesce(operation_id,legacy_operation_ref)='industry-run:failed'").fetchall()
    assert len(batches)==4
    assert all(item['status']=='failed' for item in batches)
