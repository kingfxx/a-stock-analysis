"""Offline real service integration for grouped source requests and publication."""
import json
import re
import time
from itertools import count
from threading import Barrier, Lock, Event

import pytest
import requests

from test_stock_collection_management import service, reserve
from stock_analysis.collection.collection import RequestCollector, PacingPolicy
from stock_analysis.domains.stocks import collection_operations


def test_dividends_start_while_financials_run_but_report_consumers_wait(service,monkeypatch):
    codes=['600519','601919']
    dividend_started={code:Event() for code in codes}
    financial_done={code:Event() for code in codes}
    observed=[]
    def collect(code,dataset,full,collector):
        if dataset=='financial':
            assert dividend_started[code].wait(timeout=3), 'dividend unnecessarily waits for financials'
            financial_done[code].set()
        elif dataset=='dividends':dividend_started[code].set()
        else:
            assert financial_done[code].is_set(), 'report consumer started too early'
            observed.append((code,dataset))
    monkeypatch.setattr(service,'collect',collect)
    ref=reserve(service,codes=codes,datasets=['financial','dividends','announcements','valuation'])
    service.run(ref)
    assert service.detail(ref)['status']=='complete'
    assert len(observed)==4


def test_group_claims_roll_back_if_second_claim_fails(service,monkeypatch):
    from stock_analysis.domains.stocks.group_batches import GroupBatches
    batches=GroupBatches(None)
    original=batches.create
    count=0
    def create(call,conn=None):
        nonlocal count
        count+=1
        if count==2:raise RuntimeError('injected claim failure')
        return original(call,conn)
    monkeypatch.setattr(batches,'create',create)
    calls=[(service.db,'test','stock.business_segments','eastmoney',None,None,
            dict(scope=dict(code=code)),{}) for code in ('600519','601919')]
    with pytest.raises(RuntimeError,match='injected'):
        batches.merge(calls)
    with service.db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM collection_batches').fetchone()[0]==0


@pytest.mark.parametrize('recover',[False,True])
def test_orphan_claim_uses_failed_sync_outcome(service,recover):
    from stock_analysis.infrastructure.storage import SyncKey
    identity=service.db.ensure_instrument('600519')
    run=service.db.start_sync(SyncKey(identity,'business_segments','eastmoney:RPT_F10_FN_MAINOP'),
        parser_version='test',methodology_version='test')
    service.db.fail_sync(run,RuntimeError('database is locked'))
    ref=reserve(service,codes=['600519'],datasets=['business_segments'])
    with service.db.connection(write=True) as conn:
        conn.execute("UPDATE collection_batches SET status='running',sync_run_id=? WHERE operation_id=?",(run,ref))
        if recover:
            conn.execute("UPDATE collection_operations SET status='cancelled',cancel_requested=1 WHERE operation_id=?",(ref,))
    if recover:
        service.db.recover_interrupted_runs()
        service.recover()
    else:service._finish(ref)
    with service.db.connection() as conn:
        row=conn.execute('SELECT status,error FROM collection_batches WHERE operation_id=?',(ref,)).fetchone()
        assert row['status']=='failed' and 'database is locked' in row['error']


@pytest.mark.parametrize('phase',['cache','error_recording'])
def test_channel_failure_completes_waiters_and_closes(service,monkeypatch,phase):
    from stock_analysis.domains.stocks.group_requests import GroupRequests
    broker=GroupRequests(service.db,lambda:False)
    broker.open(['sina'])
    channel=broker.channels['sina']
    if phase=='cache':channel.cache[(123,)]=object()
    else:
        def request_failure(jobs):raise ValueError('injected request failure')
        monkeypatch.setattr(channel,'requests',request_failure)
    def fail(*args,**kwargs):raise RuntimeError('injected cache failure')
    monkeypatch.setattr(service.db,'connection',fail)
    try:
        with pytest.raises(RuntimeError,match='injected cache failure'):
            payload=None if phase=='cache' else (123,'sina','stock.test','GET','https://example.test',{}, {}, {})
            broker.submit('sina','begin' if phase=='cache' else 'request',payload)
        with pytest.raises(RuntimeError,match='injected cache failure'):
            broker.submit('sina','begin',None)
    finally:
        broker.close()
    assert not channel.thread.is_alive()


def response(body):
    r=requests.Response();r.status_code=200;r._content=json.dumps(body).encode();return r


class EastSession:
    def __init__(self,calls,lock,corrupt=False):self.calls,self.lock,self.corrupt=calls,lock,corrupt
    def close(self):pass
    def get(self,url,params=None,**kwargs):
        with self.lock:self.calls.append(dict(params))
        codes=re.findall(r'"(\d{6})(?:\.SH|\.SZ)?"',params['filter'])
        rows=[dict(SECURITY_CODE=code,SECUCODE=code+'.SH',END_DATE='2026-06-30',NOTICE_DATE='2026-08-27',HOLDER_TOTAL_NUM=1000+i) for i,code in enumerate(codes)]
        if self.corrupt and ' in ' in params['filter']:rows[0]['SECURITY_CODE']='999999'
        size=int(params.get('pageSize',500));page=int(params.get('pageNumber',1))
        return response(dict(success=True,code=0,result=dict(data=rows[(page-1)*size:page*size],count=len(rows),pages=1)))


def factory(session):
    ticks=count(time.time(),200)
    return lambda db,**kwargs: RequestCollector(db,session_factory=session,clock=lambda:next(ticks),sleep=lambda _:None,policy=PacingPolicy(),**kwargs)


@pytest.mark.parametrize('size',[2,6,20,21])
def test_f10_shared_request_and_independent_sync(service,monkeypatch,size):
    calls=[];lock=Lock()
    monkeypatch.setattr(collection_operations,'RequestCollector',factory(lambda:EastSession(calls,lock)))
    codes=[str(600000+i) for i in range(size)]
    if size>=20:
        from stock_analysis.domains.stocks.group_batches import GroupBatches
        original=GroupBatches.begin;ready=Barrier(size)
        def begin(self,*args,**kwargs):
            ready.wait(timeout=15);return original(self,*args,**kwargs)
        monkeypatch.setattr(GroupBatches,'begin',begin)
    ref=reserve(service,codes=codes,datasets=['shareholders'])
    service.run(ref)
    result=service.detail(ref)
    assert result['status']=='complete',result
    assert len(result['items'])==size and all(r['status']=='complete' for r in result['items'])
    with service.db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM shareholder_observations').fetchone()[0]==size
        assert conn.execute("SELECT count(*) FROM sync_runs WHERE status='success'").fetchone()[0]==size
        batches=conn.execute('SELECT * FROM collection_batches WHERE operation_id=?',(ref,)).fetchall()
        assert len(batches)==(2 if size==21 else 1)
        sources=conn.execute('SELECT * FROM collection_sources').fetchall()
        assert len(sources)==len(batches)==len(calls)
        assert all(s['file_path'] is None for s in sources)
        assert all(len(json.loads(b['scope_json']).get('stock_members',[]))<=20 for b in batches)
        if size<=20:
            values=json.loads(batches[0]['result_json']);assert values['inserted']==size
    retry=reserve(service,codes=codes,datasets=['shareholders'],mode='resume')
    assert all(item['status']=='reused' for item in service.detail(retry)['items'])
    assert not any(call['reportName']=='RPT_HOLDERNUM_DET' for call in calls)


def test_outside_symbol_fails_all_members_without_facts(service,monkeypatch):
    calls=[];lock=Lock()
    monkeypatch.setattr(collection_operations,'RequestCollector',factory(lambda:EastSession(calls,lock,True)))
    ref=reserve(service,codes=['601919','600519'],datasets=['shareholders']);service.run(ref)
    assert service.detail(ref)['status']=='failed'
    with service.db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM shareholder_observations').fetchone()[0]==0
        assert conn.execute("SELECT count(*) FROM sync_state WHERE data_status='data'").fetchone()[0]==0


def test_different_providers_really_overlap(service,monkeypatch):
    from stock_analysis.collection.collection import begin_batch,finish_batch
    from stock_analysis.collection.execution_context import current_execution
    barrier=Barrier(2);calls=[]
    class Session:
        def close(self):pass
        def get(self,url,**kwargs):
            barrier.wait(timeout=5);calls.append(url);return response(dict(code=0,data={}))
    monkeypatch.setattr(collection_operations,'RequestCollector',factory(Session))
    def collect(code,dataset,full,collector):
        provider='tencent' if dataset=='prices' else 'baidu'
        batch=begin_batch(service.db,'unused','test.'+dataset,provider,domain='stock',scope=dict(code=code))
        collector.get(batch,provider,'test.'+dataset,'https://example.test/'+provider,evidence_mode='metadata_only')
        finish_batch(service.db,batch,'complete')
    monkeypatch.setattr(service,'collect',collect)
    ref=reserve(service,codes=['601919','600519'],datasets=['prices','valuation'])
    service.run(ref)
    assert service.detail(ref)['status']=='complete'
    assert len(calls)==4


@pytest.mark.parametrize('dataset,table',[('business_segments','business_segment_items'),('financing','financing_daily'),('dividends','dividend_events'),('valuation','industry_snapshots')])
def test_other_eastmoney_interfaces_share_one_request(service,monkeypatch,dataset,table):
    calls=[];lock=Lock()
    class Session(EastSession):
        def get(self,url,params=None,**kwargs):
            calls.append(params)
            codes=re.findall(r'"(\d{6})(?:\.SH|\.SZ)?"',params['filter'])
            rows=[]
            for code in codes:
                if dataset=='business_segments':
                    from test_business_segments import item
                    rows.append({**item(period='2026-06-30',value='123456789012345.123456'),'SECUCODE':code+'.SH','SECURITY_CODE':code})
                elif dataset=='financing':
                    rows.append(dict(SCODE=code,DATE='2026-10-26',RZYE=100,RZRQYE=101,RZJME=-1,SPJ=10))
                elif dataset=='dividends':
                    rows.append(dict(SECURITY_CODE=code,REPORT_DATE='2026-06-30',PLAN_NOTICE_DATE='2026-08-27',NOTICE_DATE='2026-08-27',EX_DIVIDEND_DATE='2026-10-01',PRETAX_BONUS_RMB=1,ASSIGN_PROGRESS='实施分配'))
                else:
                    rows.append(dict(SECUCODE=code+'.SH',SECURITY_CODE='行业平均',CORRE_SECURITY_CODE='行业平均',INDUSTRY_NAME=code,PE_TTM=int(code),PB_MRQ=1,PS_TTM=2,REPORT_DATE='2026-06-30',TOTAL_COUNT=5))
            return response(dict(success=True,code=0,result=dict(data=rows,count=len(rows),pages=1)))
    monkeypatch.setattr(collection_operations,'RequestCollector',factory(lambda:Session(calls,lock)))
    if dataset=='valuation':
        original=service.collect
        def collect(code,kind,full,collector):
            identity=service.db.ensure_instrument(code)
            service.data.p4_services()[2]._update_industry(code,identity,refresh=True,full=full)
        monkeypatch.setattr(service,'collect',collect)
    ref=reserve(service,codes=['601919','600519'],datasets=[dataset]);service.run(ref)
    result=service.detail(ref)
    assert result['status']=='complete',result
    with service.db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM '+table).fetchone()[0]==2
        if dataset=='business_segments':
            assert conn.execute('SELECT revenue FROM business_segment_items LIMIT 1').fetchone()[0]=='123456789012345.123456'
            assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    assert len(calls)==1,calls


def test_cninfo_identity_and_announcement_shared_sources_have_valid_foreign_keys(service,monkeypatch):
    from stock_analysis.domains.stocks.announcement_service import AnnouncementService
    from test_announcement_service import item
    calls=[]
    class Session:
        def close(self):pass
        def get(self,url,**kwargs):
            calls.append(('identity',None))
            return response(dict(stockList=[dict(code=c,orgId='org-'+c) for c in ('601919','600519')]))
        def post(self,url,data=None,**kwargs):
            codes=[part.split(',')[0] for part in data['stock'].split(';')]
            calls.append(('announcements',codes))
            rows=[item('2025年年度报告','2026-04-10',code=c,ident=c) for c in codes]
            return response(dict(announcements=rows,totalAnnouncement=len(rows),totalpages=0,hasMore=False))
    monkeypatch.setattr(collection_operations,'RequestCollector',factory(Session))
    def collect(code,dataset,full,collector):
        service.db.ensure_instrument(code)
        AnnouncementService(service.db,collector).update(code,[dict(period='2025-12-31')],refresh=True,allow_initial=True)
    monkeypatch.setattr(service,'collect',collect)
    ref=reserve(service,codes=['601919','600519'],datasets=['announcements']);service.run(ref)
    assert service.detail(ref)['status']=='complete',service.detail(ref)
    with service.db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM stock_announcement_metadata').fetchone()[0]==2
        assert conn.execute('SELECT count(*) FROM stock_announcement_identities').fetchone()[0]==2
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        assert conn.execute('SELECT count(*) FROM collection_sources').fetchone()[0]==2
        counters=json.loads(conn.execute('SELECT result_json FROM collection_batches WHERE operation_id=?',(ref,)).fetchone()[0])
        assert counters['disclosure_counts']['inserted']==2
    assert len(calls)==2,calls


def test_partial_stock_commit_is_not_republished_on_resume(service,monkeypatch):
    calls=[];lock=Lock()
    class Session(EastSession):
        def get(self,*args,**kwargs):
            r=super().get(*args,**kwargs);data=r.json();
            for row in data['result']['data']:
                if row['SECURITY_CODE']=='600519':row['HOLDER_TOTAL_NUM']='invalid'
            return response(data)
    monkeypatch.setattr(collection_operations,'RequestCollector',factory(lambda:Session(calls,lock)))
    ref=reserve(service,codes=['601919','600519'],datasets=['shareholders']);service.run(ref)
    result=service.detail(ref)
    assert result['status']=='failed'
    assert {r['code']:r['status'] for r in result['items']}=={'601919':'complete','600519':'failed'}
    assert next(r for r in result['items'] if r['code']=='600519')['data_counts'][0]['published'] is None
    with service.db.connection() as conn:assert conn.execute('SELECT count(*) FROM shareholder_observations').fetchone()[0]==1
    retry=reserve(service,codes=['601919','600519'],datasets=['shareholders'],mode='resume')
    assert {r['code']:r['status'] for r in service.detail(retry)['items']}=={'601919':'reused','600519':'pending'}


def test_shared_pages_validate_before_publication_and_do_not_duplicate_counts(service,monkeypatch):
    from datetime import date,timedelta
    calls=[];lock=Lock()
    class Session(EastSession):
        def get(self,url,params=None,**kwargs):
            calls.append(params)
            codes=re.findall(r'"(\d{6})"',params['filter']);size=params['pageSize'];page=params['pageNumber']
            rows=[dict(SECURITY_CODE=c,END_DATE=(date(2026,1,1)+timedelta(days=i)).isoformat(),NOTICE_DATE='2026-08-27',HOLDER_TOTAL_NUM=1000+i) for c in codes for i in range(30)]
            return response(dict(success=True,code=0,result=dict(count=len(rows),pages=(len(rows)+size-1)//size,data=rows[(page-1)*size:page*size])))
    monkeypatch.setattr(collection_operations,'RequestCollector',factory(lambda:Session(calls,lock)))
    ref=reserve(service,codes=[str(600000+i) for i in range(20)],datasets=['shareholders']);service.run(ref)
    assert service.detail(ref)['status']=='complete'
    with service.db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM shareholder_observations').fetchone()[0]==600
        batch=conn.execute('SELECT result_json FROM collection_batches WHERE operation_id=?',(ref,)).fetchone()
        assert json.loads(batch[0])['inserted']==600
    assert [c['pageNumber'] for c in calls]==[1,2,1]


def test_refused_provider_stops_while_other_provider_finishes(service,monkeypatch):
    from stock_analysis.collection.collection import begin_batch,finish_batch
    calls=[];lock=Lock()
    class Session:
        def close(self):pass
        def get(self,url,**kwargs):
            with lock:calls.append(url)
            r=response(dict(code=0,data={}))
            if url.endswith('eastmoney'):r.status_code=429
            return r
    monkeypatch.setattr(collection_operations,'RequestCollector',factory(Session))
    def collect(code,dataset,full,collector):
        provider='eastmoney' if dataset=='shareholders' else 'tencent'
        batch=begin_batch(service.db,'unused','test.'+dataset,provider,domain='stock',scope=dict(code=code))
        try:collector.get(batch,provider,'test.'+dataset,'https://example.test/'+provider,evidence_mode='metadata_only')
        except Exception:
            finish_batch(service.db,batch,'failed');raise
        else:finish_batch(service.db,batch,'complete')
    monkeypatch.setattr(service,'collect',collect)
    ref=reserve(service,codes=['601919','600519'],datasets=['shareholders','prices']);service.run(ref)
    assert service.detail(ref)['status']=='failed'
    assert sum(url.endswith('eastmoney') for url in calls)==1
    assert sum(url.endswith('tencent') for url in calls)==2
    with service.db.connection() as conn:
        assert conn.execute("SELECT cooldown_until FROM collection_provider_state WHERE source_system='eastmoney'").fetchone()[0]


def test_cancel_before_group_execution_sends_nothing(service,monkeypatch):
    calls=[];lock=Lock()
    monkeypatch.setattr(collection_operations,'RequestCollector',factory(lambda:EastSession(calls,lock)))
    ref=reserve(service,codes=['601919','600519'],datasets=['shareholders']);service.cancel();service.run(ref)
    assert not calls and service.detail(ref)['status']=='cancelled'


def test_database_check_rejects_lost_single_stock_run_identity(tmp_path,monkeypatch):
    from test_business_segments import setup
    from stock_analysis.infrastructure.storage import StorageError
    db,svc,session=setup(tmp_path,monkeypatch);svc.update('605499')
    assert db.check()['integrity']=='ok'
    with db.connection(write=True) as conn:conn.execute('UPDATE collection_batches SET sync_run_id=NULL')
    with pytest.raises(StorageError,match='relationships'):db.check()
