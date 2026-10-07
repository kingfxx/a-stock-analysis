import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from quarterly_dashboard import stock_cleanup
from quarterly_dashboard.company_report_service import CompanyReportService
from quarterly_dashboard.database_restore import RequestGate
from quarterly_dashboard.industry_service import IndustryService
from quarterly_dashboard.maintenance import MaintenanceService
from quarterly_dashboard.stock_cleanup import StockCleanupService
from quarterly_dashboard.stock_library import StockLibrary
from quarterly_dashboard.storage import Database, SyncKey, SyncResult
from test_ai_assessment import database, snapshot
from test_industries import fixture_bundle


@pytest.fixture
def cleanup(database):
    StockLibrary(database).change({'action':'unfollow','codes':['600900']})
    cache = database.path.parent/'fundamentals'/'600900.json'
    cache.parent.mkdir();cache.write_text('{"old":"cache"}',encoding='utf-8')
    return StockCleanupService(database)


def execute(service, categories, codes=None):
    backup_before={p.name:p.read_bytes() for p in (service.root/'backups').glob('*') if p.is_file()}
    preview = service.preview({'codes':codes or ['600900'],'categories':categories})
    gate = RequestGate();gate.enter()
    try:
        result=service.execute({'token':preview['token'],'confirm':'清理'},gate)
        assert {p.name:p.read_bytes() for p in (service.root/'backups').glob('*') if p.is_file()}==backup_before
        return result
    finally:
        gate.leave()


def tables(db, names):
    with db.connection() as conn:
        return {name:[tuple(r) for r in conn.execute('SELECT * FROM "'+name+'" ORDER BY rowid')] for name in names}


def document(db, tmp_path, code='600900', body=b'%PDF-test'):
    original = tmp_path / (uuid4().hex+'.pdf');original.write_bytes(body)
    return CompanyReportService(db).import_report(code,'2025-12-31',original)


def test_cache_default_preserves_business_facts_and_industry(cleanup):
    db=cleanup.db
    industry=IndustryService(db);industry.import_bundle(fixture_bundle(),capture_local=False)
    with db.connection() as conn:
        names=[r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'sw_%'")]
    before=tables(db,names+['financial_reports','valuation_observations','instruments'])
    comparison=industry.read(code='300750')
    candidates=cleanup.candidates()
    assert candidates['defaults']==['cache'] and candidates['stocks'][0]['unfollowed_days']>=0
    assert candidates['stocks'][0]['summary']['cache']['file_bytes']>0
    preview=cleanup.preview({'codes':['600900'],'categories':['cache']})
    assert preview['delete_files']==1 and not preview['delete_rows']
    assert (db.path.parent/'fundamentals'/'600900.json').exists()
    result=execute(cleanup,['cache'])
    assert result['backups']==[] and result['delete_files']==1
    assert not (db.path.parent/'fundamentals'/'600900.json').exists()
    assert tables(db,names+['financial_reports','valuation_observations','instruments'])==before
    assert IndustryService(db).read(code='300750')==comparison
    assert StockLibrary(db).is_unfollowed('600900')
    row=next(r for r in MaintenanceService(db).read()['tables'] if r['name']=='stock_cleanup_runs')
    assert row['rows']==1 and row['category']=='运行与配置' and row['updated_at']
    assert db.check()['integrity']=='ok'


def test_financial_selection_removes_facts_and_watermarks_without_backup(cleanup):
    result=execute(cleanup,['cache','financial'])
    assert result['delete_rows']>0 and result['backups']==[]
    with cleanup.db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM financial_reports').fetchone()[0]==0
        assert conn.execute('SELECT count(*) FROM valuation_observations').fetchone()[0]==0
        assert conn.execute('SELECT count(*) FROM sync_state').fetchone()[0]==0
        assert conn.execute('SELECT count(*) FROM instruments').fetchone()[0]==1
    assert not list((cleanup.root/'backups').glob('before-stock-cleanup-*'))
    assert cleanup.db.check()['integrity']=='ok'


def test_kept_snapshot_blocks_source_and_report_cache_cleanup(cleanup,tmp_path):
    from quarterly_dashboard.analysis_repository import AnalysisRepository
    from quarterly_dashboard.analysis_validation import prompt
    doc=document(cleanup.db,tmp_path)
    service=CompanyReportService(cleanup.db)
    parse_path=service.resolve(doc['relative_path']).parent/'parsed'/'test'/'pages.jsonl'
    parse_path.parent.mkdir(parents=True);parse_path.write_text('cached pages')
    with cleanup.db.connection(write=True) as conn:
        conn.execute("INSERT INTO company_report_parses(document_id,parser_version,status,page_count,relative_path,quality_json,created_at) "
                     "VALUES (?, 'test','succeeded',1,?,'{}','2026-10-07T00:00:00+00:00')",(doc['id'],service._relative(parse_path)))
    snap=snapshot(cleanup.db);snap['manifest']['company_documents']=[doc]
    run=AnalysisRepository(cleanup.db).enqueue(snap,uuid4().hex,'model','account',prompt())
    AnalysisRepository(cleanup.db).cancel(run['id'])
    preview=cleanup.preview({'codes':['600900'],'categories':['cache','financial','reports']})
    assert preview['tables']['financial_reports']>0
    assert 'company_report_documents' not in preview['tables']
    assert 'company_report_parses' not in preview['tables']
    assert any('引用' in r for r in preview['summary']['600900']['reports']['reasons'])
    execute(cleanup,['cache','financial','reports'])
    assert parse_path.exists() and service.resolve(doc['relative_path']).is_file()


def test_explicit_research_and_reports_cleanup_without_backup_and_version_numbers(cleanup,tmp_path):
    doc=document(cleanup.db,tmp_path)
    research=cleanup.root/'research_reports'/'sh600900'/'2026-10-07'/'report.html'
    research.parent.mkdir(parents=True);research.write_text('saved research')
    result=execute(cleanup,['cache','research','reports'])
    assert not research.exists()
    assert not CompanyReportService(cleanup.db).resolve(doc['relative_path']).exists()
    assert (cleanup.root/'company_reports'/Path(doc['relative_path']).parent/'manifest.json').exists()
    assert result['backups']==[] and not list((cleanup.root/'backups').glob('before-stock-cleanup-*'))
    new=document(cleanup.db,tmp_path,body=b'%PDF-new')
    assert Path(new['relative_path']).parent.name=='v2'
    assert cleanup.db.check()['integrity']=='ok'


@pytest.mark.parametrize('change',['restore','file','reference'])
def test_stale_preview_rejected_before_deletion(cleanup,change):
    preview=cleanup.preview({'codes':['600900'],'categories':['cache']})
    cache=cleanup.root/'fundamentals'/'600900.json'
    if change=='restore':
        StockLibrary(cleanup.db).change({'action':'restore','codes':['600900']})
    elif change=='file':
        cache.write_text('newer cache')
    else:
        from quarterly_dashboard.analysis_repository import AnalysisRepository
        from quarterly_dashboard.analysis_validation import prompt
        run=AnalysisRepository(cleanup.db).enqueue(snapshot(cleanup.db),uuid4().hex,'model','account',prompt())
        AnalysisRepository(cleanup.db).cancel(run['id'])
    gate=RequestGate();gate.enter()
    with pytest.raises(ValueError):cleanup.execute({'token':preview['token'],'confirm':'清理'},gate)
    assert cache.exists() and not gate.blocked


def test_busy_request_and_task_reject_cleanup(cleanup):
    preview=cleanup.preview({'codes':['600900'],'categories':['cache']})
    gate=RequestGate();gate.enter();gate.enter()
    with pytest.raises(ValueError,match='请求'):cleanup.execute({'token':preview['token'],'confirm':'清理'},gate)
    gate.leave()
    run=cleanup.db.start_sync(SyncKey(cleanup.db.ensure_instrument('600900'),'financial:lrb','test'),parser_version='test',methodology_version='test')
    with pytest.raises(ValueError,match='任务'):cleanup.execute({'token':preview['token'],'confirm':'清理'},gate)
    assert (cleanup.root/'fundamentals'/'600900.json').exists() and not gate.blocked
    cleanup.db.fail_sync(run,'test finished')


def test_db_failure_restores_staged_files_and_records(cleanup,monkeypatch):
    with cleanup.db.connection(write=True) as conn:
        conn.execute("CREATE TRIGGER reject_cleanup BEFORE INSERT ON stock_cleanup_runs BEGIN SELECT RAISE(ABORT,'test failure'); END")
    before=tables(cleanup.db,['financial_reports','valuation_observations'])
    with pytest.raises(Exception,match='test failure'):execute(cleanup,['cache','financial'])
    assert (cleanup.root/'fundamentals'/'600900.json').is_file()
    assert tables(cleanup.db,['financial_reports','valuation_observations'])==before
    assert not list(cleanup.staging.iterdir())


def test_recover_uncommitted_staged_files(cleanup):
    operation=uuid4().hex
    relative='fundamentals/600900.json'
    folder=cleanup.staging/operation
    staged=folder/'files'/relative;staged.parent.mkdir(parents=True)
    cleanup.path(relative).replace(staged)
    cleanup._save(folder/'journal.json',{'id':operation,'files':[relative]})
    cleanup.recover()
    assert cleanup.path(relative).exists() and not folder.exists()


@pytest.mark.parametrize('categories',[[],['financial'],['sw_financial_facts'],[{}]])
def test_invalid_scope_is_rejected(cleanup,categories):
    with pytest.raises(ValueError):cleanup.preview({'codes':['600900'],'categories':categories})


def test_cross_stock_export_keeps_referenced_pdf(cleanup,tmp_path):
    doc=document(cleanup.db,tmp_path)
    other=cleanup.root/'research_reports'/'sz300750'/'2026-10-07'/'report.md'
    other.parent.mkdir(parents=True);other.write_text(json.dumps({'source':doc['content_hash']}))
    preview=cleanup.preview({'codes':['600900'],'categories':['cache','reports']})
    assert not preview['tables'].get('company_report_documents')


def test_industry_latest_research_retained_while_financial_sources_removed(cleanup):
    from quarterly_dashboard.analysis_repository import AnalysisRepository
    from quarterly_dashboard.analysis_validation import prompt
    db=cleanup.db
    industry=IndustryService(db)
    bundle=fixture_bundle();bundle['members'].append({**bundle['members'][0],'stock_code':'600900','name':'测试企业'})
    industry.import_bundle(bundle,capture_local=False)
    repo=AnalysisRepository(db)
    run=repo.enqueue(snapshot(db),uuid4().hex,'model','account',prompt())
    repo.transition(run['id'],'queued','running');repo.transition(run['id'],'running','validating')
    repo.transition(run['id'],'validating','succeeded',result_json='{}',verdict='观察',summary='industry summary')
    before=IndustryService(db).read(code='600900')
    research_before=tables(db,['ai_analysis_runs','ai_analysis_snapshots'])
    preview=cleanup.preview({'codes':['600900'],'categories':['cache','financial','research']})
    assert not preview['tables'].get('ai_analysis_runs')
    assert preview['tables']['financial_reports']>0
    execute(cleanup,['cache','financial','research'])
    assert IndustryService(db).read(code='600900')==before
    assert tables(db,['ai_analysis_runs','ai_analysis_snapshots'])==research_before
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM financial_reports').fetchone()[0]==0
        assert not conn.execute("SELECT 1 FROM sync_state WHERE dataset LIKE 'financial:%'").fetchone()


def test_financial_cleanup_preserves_every_industry_row_and_fresh_comparison(cleanup):
    db=cleanup.db;industry=IndustryService(db)
    bundle=fixture_bundle();bundle['members'].append({**bundle['members'][0],'stock_code':'600900','name':'测试企业'})
    industry.import_bundle(bundle,capture_local=False)
    identity=db.ensure_instrument('600900')
    other=db.ensure_instrument('300750')
    for stock_id in (identity,other):
        for kind in ('gjzb','lrb','fzb','llb'):
            key=SyncKey(stock_id,'financial:'+kind,'sina:'+kind)
            run=db.start_sync(key,parser_version='test',methodology_version='test')
            db.complete_sync(run,SyncResult(1,'2025-12-31','2025-12-31','2025-12-31'),lambda conn:db.upsert_financial_reports(
                conn,key,run,[{'period':'2025-12-31','raw_json':{'rCurrency':'CNY','rType':'合并期末','data':[]}}]))
    with db.connection(write=True) as conn:
        source_hash=conn.execute('SELECT content_hash FROM financial_reports WHERE instrument_id=?',(identity,)).fetchone()[0]
        conn.execute('INSERT INTO sw_financial_provenance(content_hash,provenance_json) VALUES (?,?)',
                     ('cleanup-reference-test',json.dumps({'local_content_hash':source_hash})))
        names=[r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'sw_%'")]
        other_before=[tuple(r) for r in conn.execute('SELECT * FROM financial_reports WHERE instrument_id=?',(other,))]
    before=tables(db,names);comparison=IndustryService(db).read(code='600900')
    result=execute(cleanup,['cache','financial'])
    assert result['tables']['financial_reports']>0
    assert tables(db,names)==before
    assert IndustryService(db).read(code='600900')==comparison
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM financial_reports WHERE instrument_id=?',(identity,)).fetchone()[0]==0
        assert not conn.execute("SELECT 1 FROM sync_state WHERE instrument_id=? AND dataset LIKE 'financial:%'",(identity,)).fetchone()
        assert [tuple(r) for r in conn.execute('SELECT * FROM financial_reports WHERE instrument_id=?',(other,))]==other_before
    assert IndustryService(db).local_period('2025-12-31',['600900'])=={}
    assert db.check()['integrity']=='ok'


def test_committed_cleanup_recovery_never_resurrects_files(cleanup,monkeypatch):
    original=stock_cleanup.shutil.rmtree
    def fail_finalization(path):
        raise OSError('simulated interruption after commit')
    with monkeypatch.context() as patch:
        patch.setattr(stock_cleanup.shutil,'rmtree',fail_finalization)
        with pytest.raises(OSError,match='after commit'):execute(cleanup,['cache'])
    assert not cleanup.path('fundamentals/600900.json').exists()
    cleanup.recover()
    assert not cleanup.path('fundamentals/600900.json').exists() and not list(cleanup.staging.iterdir())
    with cleanup.db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM stock_cleanup_runs').fetchone()[0]==1


def test_corrupt_pdf_keeps_only_available_parse_cache(cleanup,tmp_path):
    doc=document(cleanup.db,tmp_path)
    pdf=CompanyReportService(cleanup.db).resolve(doc['relative_path'])
    parsed=pdf.parent/'parsed'/'test'/'pages.jsonl';parsed.parent.mkdir(parents=True);parsed.write_text('only remaining text')
    pdf.write_bytes(b'corrupted PDF')
    preview=cleanup.preview({'codes':['600900'],'categories':['cache']})
    assert all('pages.jsonl' not in f['path'] for f in preview['files'])
    assert any('哈希异常' in r for r in preview['summary']['600900']['cache']['reasons'])
    execute(cleanup,['cache']);assert parsed.exists()


def test_cleanup_never_calls_database_or_report_backup(cleanup,tmp_path,monkeypatch):
    import quarterly_dashboard.report_backup as report_backup_module
    doc=document(cleanup.db,tmp_path)
    def fail(*args,**kwargs):raise AssertionError('cleanup must not create backups')
    monkeypatch.setattr(Database,'backup',fail)
    monkeypatch.setattr(report_backup_module,'backup',fail)
    result=execute(cleanup,['cache','financial','research','reports'])
    assert result['backups']==[]
    assert not list((cleanup.root/'backups').glob('before-stock-cleanup-*'))
    assert not CompanyReportService(cleanup.db).resolve(doc['relative_path']).exists()
    assert not cleanup.path('fundamentals/600900.json').exists()


@pytest.mark.parametrize('relative',['../industry_sources/file','industry_sources/file','company_reports/../../stock_analysis.sqlite3'])
def test_path_scope_never_accepts_industry_or_database(cleanup,relative):
    with pytest.raises(ValueError):cleanup.path(relative)


def test_cleanup_http_requires_origin_session_preview_and_confirmation(cleanup,monkeypatch):
    from threading import Thread
    from urllib.request import Request,urlopen
    from urllib.error import HTTPError
    from quarterly_dashboard import server

    monkeypatch.setattr(server,'DATABASE_PATH',cleanup.db.path)
    httpd=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
    httpd.request_gate=RequestGate();httpd.stock_cleanup=cleanup
    thread=Thread(target=httpd.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{httpd.server_port}'
    def post(action,body,origin=base,token=server.LOCAL_SESSION_TOKEN):
        with urlopen(Request(base+('/api/maintenance/compact' if action=='compact' else '/api/maintenance/stock-cleanup/'+action),data=json.dumps(body).encode(),
                headers={'Origin':origin,'X-Local-Session':token,'Content-Type':'application/json'}),timeout=10) as response:
            return json.load(response)
    try:
        with urlopen(base+'/api/maintenance/stock-cleanup',timeout=10) as response:
            assert json.load(response)['defaults']==['cache']
        for origin,token in [('https://example.org',server.LOCAL_SESSION_TOKEN),(base,'bad')]:
            with pytest.raises(HTTPError):post('preview',{'codes':['600900'],'categories':['cache']},origin,token)
        for origin,token in [('https://example.org',server.LOCAL_SESSION_TOKEN),(base,'bad')]:
            with pytest.raises(HTTPError):post('compact',{'confirm':'整理'},origin,token)
        with pytest.raises(HTTPError):post('compact',{'confirm':'yes'})
        assert post('compact',{'confirm':'整理'})['after_bytes']>0
        preview=post('preview',{'codes':['600900'],'categories':['cache']})
        with pytest.raises(HTTPError):post('execute',{'token':preview['token'],'confirm':'yes'})
        result=post('execute',{'token':preview['token'],'confirm':'清理'})
        assert result['delete_files']==1
        with pytest.raises(HTTPError):post('execute',{'token':preview['token'],'confirm':'清理'})
    finally:
        httpd.shutdown();httpd.server_close();thread.join(timeout=2)


@pytest.mark.parametrize('expiry',['2099-01-01T00:00:00+00:00','2000-01-01T00:00:00+00:00'])
def test_price_leases_protect_live_versions_and_expired_versions_delete_atomically(cleanup,expiry):
    from quarterly_dashboard.storage import utc_now
    from test_storage import price_candidate
    db=cleanup.db
    key=SyncKey(db.ensure_instrument('600900'),'prices_adjusted','qq','qfq')
    run=db.start_sync(key,parser_version='test',methodology_version='test')
    with db.connection(write=True) as conn:
        version=price_candidate(conn,key,run,[('2026-09-29',10)])
        conn.execute("UPDATE adjusted_price_versions SET status='complete',validated_at=? WHERE id=?",(utc_now(),version))
    db.complete_sync(run,SyncResult(1,'2026-09-29','2026-09-29','2026-09-29',active_price_version_id=version),lambda conn:None)
    with db.connection(write=True) as conn:
        conn.execute('INSERT INTO price_version_leases VALUES (?,?)',(version,expiry))
    execute(cleanup,['cache','financial'])
    with db.connection() as conn:
        kept=conn.execute('SELECT count(*) FROM adjusted_price_versions').fetchone()[0]
        assert kept==(1 if expiry.startswith('2099') else 0)
        assert conn.execute('SELECT count(*) FROM adjusted_daily_prices').fetchone()[0]==kept
    assert db.check()['integrity']=='ok'


def test_compaction_shrinks_database_without_changing_records(cleanup):
    db=cleanup.db
    IndustryService(db).import_bundle(fixture_bundle(),capture_local=False)
    with db.connection(write=True) as conn:
        conn.execute("UPDATE financial_reports SET raw_json=?",(json.dumps({'padding':'x'*2_000_000}),))
    with db.connection(write=True) as conn:
        conn.execute("UPDATE financial_reports SET raw_json='{}'")
    with db.connection() as conn:
        names=[r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    before=tables(db,names)
    cleanup.preview({'codes':['600900'],'categories':['cache']})
    gate=RequestGate();gate.enter()
    result=cleanup.compact({'confirm':'整理'},gate)
    gate.leave()
    assert result['after_bytes']<result['before_bytes']
    assert result['released_bytes']==result['before_bytes']-result['after_bytes']
    assert result['after_bytes']==db.path.stat().st_size
    assert tables(db,names)==before
    assert not cleanup.prepared and not gate.blocked
    assert db.check()['integrity']=='ok'


@pytest.mark.parametrize('blocked',['confirmation','requests','task','disk','lock'])
def test_compaction_preconditions_preserve_database(cleanup,monkeypatch,blocked):
    from collections import namedtuple
    import sqlite3
    from quarterly_dashboard import maintenance
    gate=RequestGate();gate.enter()
    command={'confirm':'整理'}
    external=None
    if blocked=='confirmation':command={'confirm':'yes'}
    elif blocked=='requests':gate.enter()
    elif blocked=='task':cleanup.db.start_sync(SyncKey(cleanup.db.ensure_instrument('600900'),'financial:lrb','test'),parser_version='test',methodology_version='test')
    elif blocked=='disk':monkeypatch.setattr(maintenance.shutil,'disk_usage',lambda folder:namedtuple('Space','total used free')(100,100,0))
    elif blocked=='lock':
        external=sqlite3.connect(cleanup.db.path)
        external.execute('BEGIN IMMEDIATE')
    before=cleanup.db.path.read_bytes()
    try:
        with pytest.raises((ValueError,sqlite3.OperationalError)):
            cleanup.compact(command,gate)
        assert cleanup.db.path.read_bytes()==before
        assert not gate.blocked
    finally:
        if external:external.close()
        gate.leave()
        if blocked=='requests':gate.leave()
