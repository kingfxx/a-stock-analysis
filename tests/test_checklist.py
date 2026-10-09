import json
from uuid import uuid4
import pytest
from quarterly_dashboard.analysis_repository import AnalysisRepository
from quarterly_dashboard.analysis_service import AnalysisService
from quarterly_dashboard.checklist_snapshot import capture
from quarterly_dashboard.checklist_validation import ITEMS,OUTPUT_VERSION,prompt,validate_output
from quarterly_dashboard.company_report_service import CompanyReportService
from quarterly_dashboard.report_backup import backup,restore
from quarterly_dashboard.storage import Database,SyncKey,SyncResult
from test_ai_assessment import database,FakeProvider,service,wait_terminal


def result(data):
    return {'schema_version':OUTPUT_VERSION,'code':data['instrument']['code'],'items':[
        {'id':key,'conclusion':'资料不足，待补充。','status':'limited','evidence_ids':['context.data_quality']} for key,_ in ITEMS]}

class ChecklistProvider(FakeProvider):
    def infer(self,run_id,model,instructions,data,cancelled):
        self.calls+=1
        return {'text':json.dumps(result(data)),'model':model}


def checklist_service(db):
    return AnalysisService(db,ChecklistProvider(),snapshotter=lambda db,code:capture(db,code,as_of='2026-10-02'),prompt_factory=prompt,validator=validate_output,output_version=OUTPUT_VERSION)


def test_run_exposes_corrected_product_display_without_mutating_history(database):
    from test_report_business_metrics import JOINED_AMOUNTS
    snap=capture(database,'600900',as_of='2026-10-02')
    snap['input']['evidence'].append({'id':'bus.page','metric':'report_excerpt','value':[{
        'text':JOINED_AMOUNTS,'business_metrics':{'revenue_mix':{'period':'2025-12-31',
        'rows':[{'name':'客车产品3,622,876.432,712,654.55','revenue':25.12}]}}}]})
    repo=AnalysisRepository(database)
    run=repo.enqueue(snap,uuid4().hex,'test-model','test-account',prompt())
    displayed=repo.run(run['id'])
    assert displayed['product_display_corrections'][0]['metrics']['revenue_mix']['denominator']==3622876.43
    assert displayed['input']==snap['input']
    assert json.loads(repo.run(run['id'],internal=True)['input_json'])==snap['input']


def test_local_snapshot_zero_gjzb_and_fallback_no_old_roe(database):
    key=SyncKey(database.ensure_instrument('600900'),'financial:gjzb','sina:gjzb')
    run=database.start_sync(key,parser_version='test',methodology_version='test')
    records=[{'period':'2026-06-30','raw_json':{'data':[{'item_field':'BIZTOTINCO','item_value':'0'},{'item_field':'PARENETP','item_value':None}]}}]
    database.complete_sync(run,SyncResult(1,'2026-06-30','2026-06-30','2026-06-30'),lambda conn:database.upsert_financial_reports(conn,key,run,records))
    snap=capture(database,'600900',as_of='2026-10-02')
    financial=next(e for e in snap['input']['evidence'] if e['metric']=='financial_period')
    assert financial['value']['revenue']==0
    assert financial['basis']['revenue']['report_type']=='gjzb'
    assert financial['value']['profit']==40
    assert 'roe' in snap['quality']['missing_items']
    assert snap['hash']==capture(database,'600900',as_of='2026-10-02')['hash']


def test_validator_unknown_duplicate_and_missing_ready(database):
    data=capture(database,'600900',as_of='2026-10-02')['input'];out=result(data)
    assert len(validate_output(json.dumps(out),data)['items'])==18
    out['items'][0]['evidence_ids']=['invented']
    with pytest.raises(ValueError):validate_output(json.dumps(out),data)
    out=result(data);out['items'][1]['status']='ready'
    with pytest.raises(ValueError):validate_output(json.dumps(out),data)
    out=result(data);out['items'][-1]['id']=out['items'][0]['id']
    with pytest.raises(ValueError):validate_output(json.dumps(out),data)


def test_new_history_isolated_reuse_delete_and_legacy_retained(database):
    old=service(database);old_run=old.create({'code':'600900','request_key':uuid4().hex});assert wait_terminal(old,old_run['id'])['status']=='succeeded'
    if old.worker:old.worker.join(3)
    new=checklist_service(database)
    assert not new.overview('600900')['report'];assert new.provider.calls==0
    run=new.create({'code':'600900','model':'test-model','request_key':uuid4().hex})
    assert wait_terminal(new,run['id'])['status']=='succeeded'
    if new.worker:new.worker.join(3)
    assert len(new.repository.history('600900')['items'])==1
    repeat=new.create({'code':'600900','model':'test-model','request_key':uuid4().hex})
    assert repeat['id']==run['id'] and new.provider.calls==1
    with pytest.raises(ValueError):new.repository.delete_history('600900',[old_run['id']])
    new.repository.delete_history('600900',[run['id']]);assert not new.overview('600900')['report']
    assert old.repository.run(old_run['id'])['status']=='succeeded'


def test_pdf_reuse_identity_failure_and_backup(database,tmp_path,monkeypatch):
    import pdfplumber
    class Page:
        def extract_text(self):return '600900 2025 年度报告 公司名称 测试公司 主要业务 经营模式 '+('供应链 市场布局 核心竞争力 公司无实际控制人 '*40)
    class PDF:
        pages=[Page()]
        def __enter__(self):return self
        def __exit__(self,*args):pass
    calls=[]
    monkeypatch.setattr(pdfplumber,'open',lambda path:(calls.append(path) or PDF()))
    source=tmp_path/'source.pdf';source.write_bytes(b'%PDF-test')
    svc=CompanyReportService(database);doc=svc.import_report('600900','2025-12-31',source,published_on='2026-04-01')
    first=svc.parse(doc);assert not first['cache_hit']
    assert svc.parse(doc)['cache_hit'] and len(calls)==1
    assert svc.import_report('600900','2025-12-31',source)['id']==doc['id']
    assert '/v1/' in doc['relative_path']
    archive=tmp_path/'complete.zip';backup(database,archive);restored=tmp_path/'restored';restore(archive,restored)
    restored_doc=Database(restored/'stock_analysis.sqlite3')
    assert CompanyReportService(restored_doc).cached_documents('600900')[0]['state']=='ready'
    with pytest.raises(ValueError):restore(archive,restored)
    wrong=svc.import_report('600900','2026-06-30',source)
    with pytest.raises(ValueError):svc.parse(wrong)
    with database.connection() as conn:
        assert conn.execute('SELECT status FROM company_report_parses WHERE document_id=?',(wrong['id'],)).fetchone()[0]=='failed'
    assert svc.parse(doc)['cache_hit']
    original=svc.resolve(doc['relative_path']);original.write_bytes(b'corrupt')
    with pytest.raises(ValueError):svc.parse(doc)
    original.write_bytes(source.read_bytes())
    assert svc.parse(doc)['id']==first['id']
    assert svc.parse(doc)['cache_hit']


@pytest.mark.parametrize('bare_period',[False,True,'range'])
def test_checklist_page_explicit_generation_history_and_mobile(database,monkeypatch,bare_period):
    import re
    from http.server import ThreadingHTTPServer
    from threading import Thread
    from quarterly_dashboard import server
    playwright=pytest.importorskip('playwright.sync_api')
    item=checklist_service(database);item.repository.set_model('test-model')
    monkeypatch.setattr(server,'DATABASE_PATH',database.path)
    monkeypatch.setattr(server,'ai_service',lambda:item)
    original_result=result
    def table_result(data):
        out=original_result(data)
        products=next(i for i in out['items'] if i['id']=='products')
        products['conclusion']='核心产品是乳制品。\n[2026年上半年] 主营业务收入口径；液体乳 — 365.90亿元 — 未披露利润贡献；奶粉及奶制品 — 168.45亿元 — 未披露利润贡献。\n[2025年度] 主营业务毛利合计395.08亿元；液体乳 — 221.34亿元 — 毛利贡献56.03%；奶粉及奶制品 — 136.24亿元 — 34.48%。'
        if bare_period:products['conclusion']=products['conclusion'].replace('[2026年上半年]', '2026年上半年｜').replace('[2025年度]', '2025年度 |')
        if bare_period=='range':
            products['conclusion']=products['conclusion'].replace('2026年上半年','2026年1-6月').replace('221.34亿元 — 毛利贡献56.03%','221.34亿元（56.03%）').replace('136.24亿元 — 34.48%','136.24亿元（34.48%）')
        return out
    monkeypatch.setattr(__import__(__name__), 'result', table_result)
    original=server.render_page
    def page_html(code,refresh):
        html=original(code,refresh)
        def replace(match):
            payload=json.loads(match[2]);payload['loading']={key:False for key in payload['loading']};payload['price_needs_update']=False
            return match[1]+json.dumps(payload,ensure_ascii=False)+match[3]
        return re.sub(r'(<script id="payload" type="application/json">)(.*?)(</script>)',replace,html)
    monkeypatch.setattr(server,'render_page',page_html)
    http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);thread=Thread(target=http.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{http.server_port}'
    try:
        with playwright.sync_playwright() as p:
            try:browser=p.chromium.launch(channel='msedge',headless=True)
            except playwright.Error:pytest.skip('Headless Edge unavailable')
            with browser:
                page=browser.new_page(viewport={'width':1280,'height':900});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                def route(request):
                    if request.request.url.split(base)[-1].startswith(('/api/analysis','/api/ai/','/api/stock-groups')):request.continue_()
                    else:request.fulfill(status=503,content_type='application/json',body='{"error":"isolated"}')
                page.route('**/api/*',route);page.goto(base+'/?code=600900',wait_until='networkidle')
                assert page.get_by_role('button',name='AI 研判',exact=True).count()==0
                page.get_by_role('button',name='Checklist',exact=True).click()
                drawer=page.get_by_role('dialog',name='投资 checklist',exact=True);drawer.wait_for(state='visible')
                assert item.provider.calls==0
                assert '本地财报资料' in drawer.inner_text() and '尚未下载' in drawer.inner_text()
                drawer.get_by_role('button',name='生成 checklist',exact=True).click()
                page.wait_for_function("document.querySelectorAll('.checklist-table > tr').length===18")
                assert drawer.locator('.checklist-product-table').count()==2
                assert '2025年度' in drawer.locator('.checklist-product-table').nth(1).inner_text()
                assert '221.34' in drawer.locator('.checklist-product-table').nth(1).inner_text()
                assert '56.03%' in drawer.locator('.checklist-product-table').nth(1).inner_text()
                assert drawer.locator('.checklist-item-evidence:visible').count()==0
                assert item.provider.calls==1
                assert '生成耗时'  in drawer.inner_text()
                for width in (1280,390):
                    page.set_viewport_size({'width':width,'height':900})
                    assert drawer.evaluate('(node)=>node.scrollWidth<=node.clientWidth+1')
                drawer.get_by_role('button',name='历史记录',exact=True).click()
                page.wait_for_function("document.querySelectorAll('.ai-history-select').length===1")
                assert '生成耗时' in drawer.locator('.ai-history-item').inner_text()
                drawer.locator('.ai-history-select').check();page.once('dialog',lambda confirm:confirm.accept())
                drawer.get_by_role('button',name='删除所选',exact=True).click()
                page.wait_for_function("document.querySelector('dialog[open]').textContent.includes('暂无checklist记录')")
                assert item.provider.calls==1 and item.repository.history('600900')['items']==[]
                assert not errors,errors
    finally:
        if item.worker:item.worker.join(3)
        http.shutdown();http.server_close();thread.join(3)


def test_hash_directory_migration_preserves_cache_history_and_versioning(database,tmp_path,monkeypatch):
    import hashlib
    import pdfplumber
    class Page:
        def extract_text(self):return '600900 2025 年度报告 公司名称 测试公司 '+('核心竞争力 经营模式 供应链 市场布局 '*60)
    class PDF:
        pages=[Page()]
        def __enter__(self):return self
        def __exit__(self,*args):pass
    monkeypatch.setattr(pdfplumber,'open',lambda file:PDF())
    source=tmp_path/'original.pdf';source.write_bytes(b'%PDF-original')
    svc=CompanyReportService(database);doc=svc.import_report('600900','2025-12-31',source);parsed=svc.parse(doc)
    current=svc.resolve(doc['relative_path']);old_dir=current.parent.with_name(doc['content_hash']);current.parent.rename(old_dir)
    old_prefix=svc._relative(old_dir)+'/'
    with database.connection(write=True) as conn:
        parse_path=conn.execute('SELECT relative_path FROM company_report_parses WHERE id=?',(parsed['id'],)).fetchone()[0]
        conn.execute('UPDATE company_report_documents SET relative_path=? WHERE id=?',(old_prefix+'report.pdf',doc['id']))
        conn.execute('UPDATE company_report_parses SET relative_path=? WHERE id=?',(old_prefix+parse_path.split('/v1/')[1],parsed['id']))
    item=checklist_service(database);run=item.create({'code':'600900','model':'test-model','request_key':uuid4().hex})
    assert wait_terminal(item,run['id'])['status']=='succeeded'
    if item.worker:item.worker.join(3)
    before=item.repository.run(run['id'])['input']
    moved=svc.migrate_directories();assert len(moved)==1 and '/v1/' in moved[0]['new_path']
    assert not old_dir.exists()
    assert svc.resolve(old_prefix+'report.pdf').is_file()
    assert svc.resolve(old_prefix+parse_path.split('/v1/')[1]).is_file()
    assert svc.cached_documents('600900')[0]['state']=='ready'
    with database.connection() as conn:updated=dict(conn.execute('SELECT * FROM company_report_documents WHERE id=?',(doc['id'],)).fetchone())
    assert svc.parse(updated)['cache_hit']
    assert item.repository.run(run['id'])['input']==before
    assert svc.migrate_directories()==[]
    assert svc.import_report('600900','2025-12-31',source)['id']==doc['id']
    source.write_bytes(b'%PDF-revised')
    revised=svc.import_report('600900','2025-12-31',source)
    assert '/v2/' in revised['relative_path'] and revised['supersedes_id']==doc['id']
    assert svc.resolve(moved[0]['new_path']).read_bytes()==b'%PDF-original'
    archive=tmp_path/'migrated.zip';backup(database,archive);restore(archive,tmp_path/'restored')


def test_discover_fulltext_titles_and_cached_report_state(database,monkeypatch,tmp_path):
    from quarterly_dashboard import company_report_service as module
    from datetime import datetime,timezone
    class Response:
        def __init__(self,data):self.data=data
        def raise_for_status(self):pass
        def json(self):return self.data
    monkeypatch.setattr(module.requests,'get',lambda *args,**kwargs:Response({'stockList':[{'code':'600900','orgId':'org'}]}))
    def announcements(*args,**kwargs):
        def item(title,url):return {'secCode':'600900','announcementTitle':title,'announcementTime':datetime(2026,8,28,tzinfo=timezone.utc).timestamp()*1000,'adjunctUrl':url}
        return Response({'announcements':[
            item('2019年年度报告','old.pdf'),item('2025年年度报告（全文）','annual.pdf'),
            item('2024年度报告全文','annual2024.pdf'),item('2025年年度报告摘要','summary.pdf'),
            item('2026年半年度报告全文','interim.pdf'),item('2025年年度报告的说明','notes.pdf')]})
    monkeypatch.setattr(module.requests,'post',announcements)
    svc=CompanyReportService(database);docs=svc.discover('600900')
    assert [(d['period'],d['kind']) for d in docs]==[('2025-12-31','annual'),('2026-06-30','interim')]
    assert docs[0]['url'].endswith('annual.pdf')
    source=tmp_path/'old.pdf';source.write_bytes(b'%PDF-old');svc.import_report('600900','2019-12-31',source)
    cached=svc.cached_documents('600900')
    assert cached[0]['report_period']=='2019-12-31' and cached[0]['outdated']
    assert cached[0]['state']=='unparsed' and cached[1]['state']=='missing'
    overview=checklist_service(database).overview('600900');assert overview['company_reports']==cached
