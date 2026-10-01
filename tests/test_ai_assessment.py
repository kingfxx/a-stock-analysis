import copy
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from threading import Event, Thread
import time
from uuid import uuid4

import pytest
import requests

from quarterly_dashboard import server
from quarterly_dashboard.analysis_snapshot import capture, digest, encoded, changes, SnapshotError
from quarterly_dashboard.analysis_repository import AnalysisRepository
from quarterly_dashboard.analysis_validation import prompt, validate_output, OUTPUT_VERSION, DIMENSIONS
from quarterly_dashboard.analysis_service import AnalysisService
from quarterly_dashboard.ai_provider import ChatGPTProvider, WindowsCredentials, ProviderError, PLAN_SCOPE, STREAM_ERRORS
from quarterly_dashboard.storage import Database, SyncKey, SyncResult, utc_now, restore_backup


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / 'test.sqlite3')
    db.initialize()
    identity = db.ensure_instrument('600900', '测试企业')
    reports = []
    for year in range(2019, 2027):
        for quarter, end in enumerate(('03-31','06-30','09-30','12-31'), 1):
            period = f'{year}-{end}'
            if period > '2026-06-30':
                continue
            reports.append({'period':period, 'publish_date':(date.fromisoformat(period)+timedelta(days=30)).isoformat(),
                'revenue_ytd':quarter*100*(year-2018), 'profit_ytd':quarter*20, 'equity':200,
                'operating_cash_flow_ytd':quarter*25, 'capex_ytd':quarter*5,
                'operating_cost_ytd':quarter*50, 'net_profit_ytd':quarter*21,
                'monetary_funds':100, 'shares':20})
    key = SyncKey(identity,'financial:merged','legacy')
    run = db.start_sync(key,parser_version='test',methodology_version='test')
    db.complete_sync(run,SyncResult(len(reports),reports[0]['period'],reports[-1]['period'],reports[-1]['period']),
        lambda conn: db.upsert_financial_reports(conn,key,run,[{'period':r['period'],'raw_json':r} for r in reports]))
    for metric in ('pe','pb'):
        key = SyncKey(identity,'valuation:'+metric,'baidu:opendata')
        run = db.start_sync(key,parser_version='test',methodology_version='test')
        points = [{'observed_on':(date(2026,8,1)+timedelta(days=i)).isoformat(),'value':i+1,
                   'sampling_version':'test','source_windows':[],'raw_json':{'value':i+1}} for i in range(60)]
        db.complete_sync(run,SyncResult(len(points),points[0]['observed_on'],points[-1]['observed_on'],points[-1]['observed_on']),
            lambda conn: db.upsert_valuation_observations(conn,key,run,points))
    return db


def snapshot(db):
    return capture(db,'600900',as_of='2026-10-01')


def valid_result(input_data, **changes):
    evidence = input_data['evidence'][0]['id']
    result = {'schema_version':OUTPUT_VERSION,'code':'600900','verdict':'观察','summary':'经营稳定，仍需核实估值适用性。',
        'dimensions':{name:{'status':statuses[-1],'explanation':'相关维度需要核实。','evidence_ids':[evidence]}
                      for name,statuses in DIMENSIONS.items()},
        'supporting_factors':[{'explanation':'经营数据可供进一步核实。','evidence_ids':[evidence]}],
        'risks':[{'explanation':'行业适用性尚待核实。','evidence_ids':[evidence]}],
        'change_conditions':[{'direction':'核实','condition':'确认后续现金流与利润变化一致','indicator':'下一期经营现金流和净利润'}],
        'unknowns':['重大公告和竞争优势需要另行核实。']}
    return {**result, **changes}


class FakeProvider:
    catalog = [{'slug':'test-model','display_name':'测试模型'}]
    def __init__(self):
        self.calls=0
        self.entered=Event()
        self.release=Event()
        self.release.set()
        self.failure=False
    def status(self):
        return {'connected':True,'plan_authorized':True,'account_ref':'test-account','account':'测试'}
    def models(self):
        return self.catalog
    def infer(self, run_id, model, instructions, data, cancelled):
        self.calls+=1
        self.entered.set()
        assert self.release.wait(5)
        if self.failure:
            raise ProviderError('模型缺少成功终态',diagnostic={'http_status':200,
                'response_headers':{'x-request-id':'req_fake'}})
        return {'text':encoded(valid_result(data)),'model':'test-model','usage':{'input_tokens':1}}
    def cancel(self, run_id):
        self.release.set()
    def disconnect(self):
        return {'connected':False}


def service(db, provider=None):
    item = AnalysisService(db,provider or FakeProvider(),snapshotter=lambda database,code:capture(database,code,as_of='2026-10-01'))
    item.repository.set_model('test-model')
    return item


def wait_terminal(item, run_id):
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        run=item.repository.run(run_id)
        if run['status'] not in ('queued','running','validating'):
            return run
        time.sleep(.02)
    pytest.fail('任务未结束')


def test_consistent_snapshot_and_hash(database, monkeypatch):
    opened=[]
    original=database._open
    def track(mode='rw'):
        opened.append(mode)
        return original(mode)
    monkeypatch.setattr(database,'_open',track)
    first=snapshot(database)
    assert opened == ['ro']
    with database.connection(write=True) as conn:
        conn.execute("UPDATE financial_reports SET obtained_at='2099-01-01T00:00:00Z'")
        conn.execute("UPDATE sync_state SET checked_at=succeeded_at")
    second=snapshot(database)
    assert first['hash']==second['hash']
    assert first['manifest']!=second['manifest']
    with database.connection(write=True) as conn:
        conn.execute("INSERT INTO report_overrides(instrument_id,period,field_name,value_json,origin,updated_at) VALUES (?,?,'profit_ytd','999','manual',?)",
                     (first['instrument_id'],'2026-06-30',utc_now()))
    third=snapshot(database)
    assert first['hash']!=third['hash']
    assert '财报/手工覆盖' in changes(first['input'],third['input'])
    assert len([e for e in first['input']['evidence'] if e['id'].startswith('financial.quarter.')]) == 12
    assert len([e for e in first['input']['evidence'] if e['id'].startswith('financial.year.')]) == 5
    pe=next(e for e in first['input']['evidence'] if e['id']=='valuation.pe.5y')
    assert pe['sample_count']>2  # daily observations were not collapsed by dividend projection.
    ttm=next(e for e in first['input']['evidence'] if e['id']=='financial.ttm.2026-06-30')['value']
    assert ttm['profit']==80 and ttm['operating_cash_flow']==100
    assert ttm['net_cash'] is None  # unknown components are not zero.


def test_missing_data_prevents_inference(database):
    item=service(database)
    with pytest.raises(SnapshotError):
        item.create({'code':'000001','request_key':uuid4().hex})
    assert item.provider.calls==0


def test_global_queue_limit_idempotency_and_cross_stock_relation(database):
    repo=AnalysisRepository(database)
    first=snapshot(database)
    key=uuid4().hex
    one=repo.enqueue(first,key,'test-model','account',prompt())
    assert repo.enqueue(first,key,'test-model','account',prompt())['id']==one['id']
    repo.claim()
    second=copy.deepcopy(first)
    second['instrument_id']=database.ensure_instrument('000001')
    second['input']['instrument']['code']='000001'
    second['hash']=digest(second['input'])
    two=repo.enqueue(second,uuid4().hex,'test-model','account',prompt())
    assert two['status']=='queued'
    third=copy.deepcopy(first)
    third['instrument_id']=database.ensure_instrument('000002')
    third['input']['instrument']['code']='000002'
    third['hash']=digest(third['input'])
    with pytest.raises(ValueError,match='等待任务'):
        repo.enqueue(third,uuid4().hex,'test-model','account',prompt())
    with pytest.raises(ValueError,match='幂等键'):
        repo.enqueue(second,key,'test-model','account',prompt())


def test_migration_preserves_facts_and_groups_and_prebackup(database,tmp_path,monkeypatch):
    from quarterly_dashboard import storage
    old=storage.MIGRATIONS
    monkeypatch.setattr(storage,'MIGRATIONS',old[:-1])
    previous=Database(tmp_path/'version6.sqlite3'); previous.initialize()
    identity=previous.ensure_instrument('600900','原名称')
    from quarterly_dashboard.stock_library import StockLibrary
    # Create through actual repository contract rather than bypassing its constraints.
    StockLibrary(previous).change({'action':'create','name':'原分组'})
    monkeypatch.setattr(storage,'MIGRATIONS',old)
    previous.initialize()
    assert previous.check()['schema_version']==7
    with previous.connection() as conn:
        assert conn.execute('SELECT name FROM instruments WHERE id=?',(identity,)).fetchone()[0]=='原名称'
        assert conn.execute('SELECT name FROM stock_groups').fetchone()[0]=='原分组'
    backups=list((previous.path.parent/'backups').glob('pre-migration-*.sqlite3'))
    assert len(backups)==1 and Database(backups[0]).check(current=False)['schema_version']==6


def test_windows_backup_does_not_require_hardlinks(database,tmp_path,monkeypatch):
    import os
    if os.name!='nt': pytest.skip('Windows-specific no-overwrite rename')
    def unavailable(*args): raise OSError('Hard links unavailable on this volume')
    monkeypatch.setattr(os,'link',unavailable)
    backup=database.backup(tmp_path/'no-hardlink.sqlite3')
    assert Database(backup).check()['schema_version']==7
    from quarterly_dashboard.storage import StorageError
    with pytest.raises(StorageError): database.backup(backup)


def test_repository_atomic_dedup_state_foreign_key_and_backup(database,tmp_path):
    repo=AnalysisRepository(database)
    snap=snapshot(database)
    def create(i):
        return repo.enqueue(snap,uuid4().hex,'test-model','account',prompt())['id']
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids=list(pool.map(create,range(4)))
    assert len(set(ids))==1
    run=repo.claim()
    assert run['status']=='running'
    assert repo.transition(run['id'],'running','validating')
    assert repo.cancel(run['id'])
    assert not repo.transition(run['id'],'validating','succeeded',result_json='{}',verdict='观察',summary='a')
    other=database.ensure_instrument('000001')
    with pytest.raises(sqlite3.IntegrityError), database.connection(write=True) as conn:
        conn.execute('UPDATE ai_analysis_runs SET instrument_id=? WHERE id=?',(other,run['id']))
    with pytest.raises(sqlite3.IntegrityError), database.connection(write=True) as conn:
        conn.execute("UPDATE ai_analysis_snapshots SET input_json='{}'")
    backed=database.daily_backup()
    restored=restore_backup(backed,tmp_path/'restored.sqlite3')
    assert AnalysisRepository(Database(restored)).run(run['id'])['status']=='cancelled'
    with pytest.raises(sqlite3.IntegrityError), database.connection(write=True) as conn:
        conn.execute("UPDATE ai_analysis_runs SET status='running',completed_at=NULL WHERE id=?",(run['id'],))


@pytest.mark.parametrize('fault',['unknown_evidence','wrong_code','too_long','candidate','trade','negative_pe','missing_dimension','invalid_json','extra_field'])
def test_output_rejects_invalid_reports(database,fault):
    data=snapshot(database)['input']
    result=valid_result(data)
    if fault=='unknown_evidence': result['risks'][0]['evidence_ids']=['invented']
    if fault=='wrong_code': result['code']='000001'
    if fault=='too_long': result['summary']='长'*181
    if fault=='candidate': result['verdict']='买入候选'
    if fault=='trade': result['summary']='目标价必涨'
    if fault=='negative_pe': result['summary']='负PE意味着低估'
    if fault=='missing_dimension': result['dimensions'].pop('chips')
    if fault=='extra_field': result['fake']='source'
    with pytest.raises(ValueError):
        validate_output('not-json' if fault=='invalid_json' else encoded(result),data)


def test_success_reuse_failure_keeps_report_and_no_network_reads(database,monkeypatch):
    old_backup=database.daily_backup()
    backup_before=old_backup.stat()
    def unexpected_backup(*args,**kwargs):
        pytest.fail('AI analysis must not trigger a backup')
    monkeypatch.setattr(database,'daily_backup',unexpected_backup)
    item=service(database)
    run=item.create({'code':'600900','request_key':uuid4().hex})
    succeeded=wait_terminal(item,run['id'])
    assert succeeded['status']=='succeeded'
    if item.worker: item.worker.join(3)
    assert old_backup.stat().st_mtime_ns==backup_before.st_mtime_ns
    assert old_backup.stat().st_size==backup_before.st_size
    assert AnalysisRepository(Database(old_backup)).history('600900')['items']==[]
    for _ in range(2):
        item.overview('600900')
        item.repository.history('600900')
    reused=item.create({'code':'600900','request_key':uuid4().hex})
    assert reused['id']==run['id'] and item.provider.calls==1
    item.provider.failure=True
    failed=item.create({'code':'600900','request_key':uuid4().hex,'force':True})
    assert wait_terminal(item,failed['id'])['status']=='failed'
    diagnostic=json.loads(item.repository.run(failed['id'],internal=True)['diagnostic_json'])
    assert diagnostic['provider']['response_headers']['x-request-id']=='req_fake'
    assert item.overview('600900')['report']['id']==run['id']


def test_cancel_late_success_restart_and_queue_limit(database):
    provider=FakeProvider(); provider.release.clear()
    item=service(database,provider)
    run=item.create({'code':'600900','request_key':uuid4().hex})
    assert provider.entered.wait(3)
    # Writer remains usable while inference blocks: network has no open read transaction.
    with database.connection(write=True) as conn:
        conn.execute("UPDATE ai_analysis_preferences SET model='test-model'")
    item.cancel(run['id'])
    assert wait_terminal(item,run['id'])['status']=='cancelled'
    provider.release.set()
    if item.worker: item.worker.join(3)
    assert item.repository.run(run['id'])['status']=='cancelled'
    repo=item.repository
    snap=snapshot(database)
    queued=repo.enqueue(snap,uuid4().hex,'test-model','account',prompt(),force=True)
    assert repo.recover()==1
    assert repo.run(queued['id'])['status']=='interrupted'
    assert provider.calls==1


class MemoryCredentials:
    def __init__(self): self.value=None
    def load(self): return self.value
    def save(self,value): self.value=value
    def clear(self): self.value=None


def test_pkce_state_nonce_client_validation_and_windows_protection(tmp_path):
    credentials=MemoryCredentials()
    provider=ChatGPTProvider(tmp_path,credentials=credentials)
    first=provider.connect(8768)
    assert 'dynamic_agent_client' in first['authorization_url'] and 'S256' in first['authorization_url']
    host=provider.config['ext_agent_host_id']
    pending=provider.pending.copy()
    with pytest.raises(ProviderError): provider.callback({'state':'invalid','code':'bad'})
    assert provider.pending is not None
    with pytest.raises(ProviderError): provider.callback({'state':pending['state'],'client_id':'dynamic_agent_client','code':'bad'})
    assert provider.pending is None
    provider=ChatGPTProvider(tmp_path,credentials=credentials)
    provider.connect(8768)
    assert provider.config['ext_agent_host_id']==host
    secure=WindowsCredentials(tmp_path/'protected.dpapi')
    if __import__('os').name=='nt':
        secure.save({'access_token':'test-secret'})
        assert b'test-secret' not in secure.path.read_bytes()
        assert secure.load()['access_token']=='test-secret'
        secure.clear()


class StreamResponse:
    status_code=200
    headers={'x-request-id':'req_test','Retry-After':'60'}
    def __init__(self,events): self.events=events; self.closed=False
    def iter_lines(self,**kwargs):
        for event in self.events:
            yield b'data: '+json.dumps(event).encode()
            yield b''
    def close(self): self.closed=True


@pytest.mark.parametrize('http_failure',[False,True])
def test_provider_preserves_redacted_server_diagnostics(tmp_path,http_failure):
    raw={'code':'subscription_sharing_usage_limit_exceeded','message':'echo access-secret refresh-secret Bearer random-secret',
         'param':None,'type':'usage_limit','details':{'access_token':'access-secret','refresh_token':'refresh-secret',
         'Authorization':'Bearer random-secret','retry_after':60,'limit':'application'}}
    stream=StreamResponse([{'type':'response.failed','response':{'id':'resp_test','error':raw}}])
    if http_failure:
        stream.status_code=429
        stream.iter_content=lambda **kwargs: iter([json.dumps({'error':raw}).encode()])
    class Session:
        calls=0
        def post(self,*args,**kwargs):
            self.calls+=1
            return stream
    credentials=MemoryCredentials()
    credentials.value={'access_token':'access-secret','refresh_token':'refresh-secret',
        'expires_at':time.time()+3600,'scopes':[PLAN_SCOPE],'subject':'subject','client_id':'oaiapp_test'}
    session=Session()
    provider=ChatGPTProvider(tmp_path,session=session,credentials=credentials)
    provider.config={'subject':'subject'}
    with pytest.raises(ProviderError) as caught:
        provider.infer('test','gpt-5.6-sol','instructions',{},Event())
    diagnostic=caught.value.diagnostic
    assert diagnostic['http_status']==(429 if http_failure else 200)
    assert diagnostic['response_headers']=={'x-request-id':'req_test','Retry-After':'60'}
    error=diagnostic['raw_error_response']['error'] if http_failure else diagnostic['raw_error_response']
    assert error['details']['limit']=='application'
    assert error['code']==raw['code'] and error['param'] is None
    for secret in ('access-secret','refresh-secret','random-secret'):
        assert secret not in encoded(diagnostic)
    assert diagnostic['received_at_beijing'].endswith('+08:00')
    assert diagnostic['registered_subject_matches'] and not diagnostic['workspace_independently_verified']
    assert session.calls==1 and stream.closed


def test_signed_oauth_identity_scopes_refresh_restart_and_disconnect(tmp_path):
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    jwk=json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())); jwk['kid']='test-key'
    credentials=MemoryCredentials()
    class Response:
        status_code=200
        def __init__(self,data): self.data=data
        def json(self): return self.data
    class Session:
        def get(self,url,**kwargs):
            if 'openid-configuration' in url:
                return Response({'issuer':'https://auth.openai.com','jwks_uri':'https://auth.openai.com/.well-known/jwks.json',
                                 'revocation_endpoint':'https://auth.openai.com/api/accounts/oauth/revoke'})
            return Response({'keys':[jwk]})
        def post(self,url,**kwargs):
            if url.endswith('/revoke'):
                assert kwargs['data']['token_type_hint']=='refresh_token'
                return Response({})
            claims={'sub':'test-subject','aud':'oaiapp_test','iss':'https://auth.openai.com','iat':int(time.time()),
                    'exp':int(time.time())+3600,'nonce':provider.pending['nonce'] if provider.pending else nonce,'name':'测试账号'}
            return Response({'access_token':'test-access','refresh_token':'test-refresh','expires_in':3600,
                             'scope':'openid profile email offline_access resource.invoke '+PLAN_SCOPE,
                             'id_token':jwt.encode(claims,key,algorithm='RS256',headers={'kid':'test-key'})})
    session=Session()
    provider=ChatGPTProvider(tmp_path,session=session,credentials=credentials)
    provider.connect(8768)
    nonce=provider.pending['nonce']
    provider.callback({'state':provider.pending['state'],'client_id':'oaiapp_test','code':'test-code'})
    assert provider.status()['connected'] and provider.status()['plan_authorized']
    assert 'test-access' not in provider.config_path.read_text()
    record=provider.record.copy()
    with pytest.raises(ProviderError): provider._identity(record['id_token'],'oaiapp_test','wrong-nonce')
    with pytest.raises(jwt.InvalidAudienceError): provider._identity(record['id_token'],'oaiapp_other',nonce)
    provider=ChatGPTProvider(tmp_path,session=session,credentials=credentials)
    assert provider.status()['connected']
    provider.record['expires_at']=0
    assert provider._access()=='test-access'
    assert provider.record['expires_at']>time.time()
    assert provider.disconnect()['connected'] is False
    assert credentials.load() is None
    assert provider.config['client_id']=='oaiapp_test'


@pytest.mark.parametrize('terminal',['completed','failed','incomplete','missing','refusal','empty','oversized',*STREAM_ERRORS])
@pytest.mark.parametrize('delta_only',[False,True])
def test_provider_requires_completed_stream(tmp_path,terminal,delta_only):
    response={'status':'completed','output':[{'content':[{'type':'output_text','text':'{}'}]}]}
    event={'type':'response.'+terminal,'response':response}
    if terminal=='refusal': response['output'][0]['content']=[{'type':'refusal','refusal':'no'}]
    if delta_only or terminal=='empty': response['output']=[]
    events=[] if terminal=='empty' else [{'type':'response.output_text.delta','delta':'{'},
        {'type':'response.output_text.delta','delta':'}'}]
    if terminal=='oversized': events=[{'type':'response.output_text.delta','delta':'x'*65537}]
    if terminal in ('empty','oversized'): event['type']='response.completed'
    if terminal in STREAM_ERRORS:
        event['type']='response.failed'
        response['error']={'code':terminal,'message':'secret diagnostic must not cross boundary'}
    if terminal!='missing': events.append(event)
    stream=StreamResponse(events)
    class Session:
        def post(self,url,**kwargs):
            assert url=='https://api.openai.com/v1/responses'
            assert kwargs['json']['store'] is False and kwargs['json']['stream'] is True
            assert 'tools' not in kwargs['json'] and 'max_output_tokens' not in kwargs['json']
            return stream
    credentials=MemoryCredentials()
    credentials.value={'access_token':'test','expires_at':time.time()+3600,'scopes':[PLAN_SCOPE]}
    provider=ChatGPTProvider(tmp_path,session=Session(),credentials=credentials)
    if terminal=='completed':
        assert provider.infer('test','test-model','prompt',{},Event())['text']=='{}'
    else:
        with pytest.raises(ProviderError) as error: provider.infer('test','test-model','prompt',{},Event())
        if terminal in STREAM_ERRORS:
            assert terminal in str(error.value)
            assert 'secret diagnostic' not in str(error.value)
    assert stream.closed


def test_http_cross_site_token_host_history_and_no_model_reads(database,monkeypatch):
    item=service(database)
    monkeypatch.setattr(server,'ai_service',lambda:item)
    http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
    thread=Thread(target=http.serve_forever,daemon=True); thread.start()
    base=f'http://127.0.0.1:{http.server_port}'
    try:
        status=requests.get(base+'/api/ai/status').json()
        assert 'access_token' not in encoded(status)
        for path in ('/api/analysis?code=600900','/api/analysis/history?code=600900'):
            assert requests.get(base+path).status_code==200
        assert item.provider.calls==0
        command={'code':'600900','request_key':uuid4().hex}
        assert requests.post(base+'/api/analysis',json=command).status_code==403
        headers={'Origin':'https://malicious.example','X-Local-Session':status['session_token']}
        assert requests.post(base+'/api/analysis',json=command,headers=headers).status_code==403
        headers['Origin']=base
        assert requests.post(base+'/api/analysis',json=command,headers=headers).status_code==202
        assert requests.get(base+'/api/ai/status',headers={'Host':'malicious.example'}).status_code==400
    finally:
        if item.worker: item.worker.join(3)
        http.shutdown(); http.server_close(); thread.join(3)
