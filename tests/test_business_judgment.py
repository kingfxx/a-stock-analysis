import json
import time
from threading import Event, Thread
from http.server import ThreadingHTTPServer
from uuid import uuid4

import pytest
import requests
from quarterly_dashboard import business_judgment as business, server
from quarterly_dashboard.analysis_service import AnalysisService
from quarterly_dashboard.analysis_snapshot import encoded
from quarterly_dashboard.analysis_repository import AnalysisRepository
from quarterly_dashboard.ai_provider import ChatGPTProvider, PLAN_SCOPE, ProviderError
from quarterly_dashboard.storage import Database
from test_ai_assessment import FakeProvider, MemoryCredentials, StreamResponse, wait_terminal


@pytest.fixture
def db(tmp_path,monkeypatch):
    monkeypatch.setattr('quarterly_dashboard.industry_position.collect',lambda *args:None)
    item = Database(tmp_path/'isolated.sqlite3'); item.initialize()
    item.ensure_instrument('600900', '测试公司'); item.ensure_instrument('601919', '中远海控')
    return item


def result(data):
    return {'schema_version': business.OUTPUT_VERSION, 'code': data['instrument']['code'], 'as_of': data['as_of'],
        'industry_position': {'markets': [{'market':'核心市场','selection_reason':'主要业务','scope':'全球；具体渠道覆盖未知','period':data['as_of'],'measure':'运营份额，数字缺失','company_entity':data['instrument']['code'],'company_rank':None,'company_share_pct':None,'competitors':[],'cr3_pct':None,'cr5_pct':None,'status':'missing','basis':'本次来源未给出份额。','limitations':'未取得公司排名和同行份额。','evidence_ids':['web.1']}]},
        'summary': '规模支持竞争地位，但盈利主要仍由运价周期决定。',
        'frameworks': {key: [{'category': category, 'judgment': '影响较强', 'analysis': '行业资料披露运力扩张。', 'impact': '规模有助于网络效率，利润仍由供需决定。', 'risk_verification': '观察新船交付与装载率，若供给增加且装载率下降则优势不能转化为利润。', 'status': 'limited', 'evidence_ids': ['web.1']} for category in categories] for key,categories in {**business.FRAMEWORKS, 'capabilities': ('护城河','运营能力','客户关系','网络布局','资本配置')}.items()},
        'rows': [{'question': f'问题{i}', 'judgment': '有竞争优势', 'reasoning': '规模支持份额，但不能决定全行业价格。',
                  'impact': '按周期企业判断。', 'status': 'limited', 'evidence_ids': ['web.1'], 'framework_refs': ['five_forces.行业竞争强度', 'capabilities.护城河']} for i in range(5)],
        'change_conditions': [{'condition': '下期运价持续下行', 'impact': '下调盈利持续性判断', 'evidence_ids': ['web.2']} for _ in range(2)],
        'unknowns': ['最新运价尚需核实'],
        'sources': [{'id': f'web.{i}', 'title': '行业来源', 'url': f'https://example.com/{i}', 'published_on': None, 'excerpt': '行业周期影响定价。'} for i in range(1,4)]}


class Provider(FakeProvider):
    def infer(self, run_id, model, instructions, data, cancelled, *, web_search=False):
        if not web_search:
            return super().infer(run_id, model, instructions, data, cancelled)
        self.calls += 1; self.entered.set(); assert self.release.wait(5)
        r = result(data)
        return {'text': encoded(r), 'web_sources': [{'url': s['url']} for s in r['sources']], 'model': model}


def service(db, provider=None, shared=None):
    # Legacy-response fixtures continue to test the retained v14 validator.
    item=AnalysisService(db, provider or Provider(), snapshotter=business.capture, prompt_factory=lambda:{**business.prompt(),'version':'business_judgment_prompt_v14'},
                         validator=business.validate, output_version=business.OUTPUT_VERSION, shared=shared)
    item.repository.set_model('test-model'); return item


def test_capture_without_local_reports_is_read_only_and_day_sensitive(db):
    with db.connection() as conn:
        before=conn.execute('SELECT count(*) FROM ai_analysis_snapshots').fetchone()[0]
    a=business.capture(db,'601919',as_of='2026-10-10'); b=business.capture(db,'601919',as_of='2026-10-11')
    assert not a['input']['evidence'] and a['quality']['limited'] and a['hash'] != b['hash']
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM ai_analysis_snapshots').fetchone()[0]==before


def test_equivalent_urls_are_matched_and_original_tool_url_is_saved(db):
    data=business.capture(db,'601919')['input'];r=result(data)
    data['web_sources']=[{'url':s['url']} for s in r['sources']]
    r['sources'][0]['url']='https://EXAMPLE.com:443/1#section'
    saved=business.validate(encoded(r),data)
    assert saved['sources'][0]['url']=='https://example.com/1'
    assert len(saved['retrieval']['url_normalizations'])==1


@pytest.mark.parametrize('filename',['食品安全目录.pdf','%E9%A3%9F%E5%93%81安全目录.pdf','%e9%a3%9f%e5%93%81安全目录.pdf','%E9%A3%9F%E5%93%81%E安全目录.pdf'])
def test_utf8_pdf_filename_uses_exact_tool_url_and_keeps_source_checks(db,filename):
    from urllib.parse import quote
    data=business.capture(db,'601919')['input'];r=result(data)
    actual='https://example.com/202601/document-id/'+quote('食品安全目录.pdf')
    r['sources'][0]['url']=actual
    data['web_sources']=[{'url':s['url']} for s in r['sources']]
    r['sources'][0]['url']='https://example.com/202601/document-id/'+filename
    saved=business.validate(encoded(r),data)
    assert saved['sources'][0]['url']==actual
    assert saved['sources'][0]['origin']=='web_search'
    if '%E安' in filename:
        assert saved['retrieval']['url_normalizations'][0]['reason']=='pdf_utf8_boundary_partial_escape'
    data['web_sources'].pop(0)
    with pytest.raises(ValueError):business.validate(encoded(r),data)


@pytest.mark.parametrize('path',['/202602/document-id/','/202601/other-id/'])
def test_pdf_encoding_repair_never_changes_document_path(db,path):
    from urllib.parse import quote
    data=business.capture(db,'601919')['input'];r=result(data)
    r['sources'][0]['url']='https://example.com/202601/document-id/'+quote('食品安全目录.pdf')
    data['web_sources']=[{'url':s['url']} for s in r['sources']]
    r['sources'][0]['url']='https://example.com'+path+'%E9%A3%9F%E5%93%81%E安全目录.pdf'
    with pytest.raises(ValueError):business.validate(encoded(r),data)


@pytest.mark.parametrize('filename',['%E9%A3%9F%E5%93%81%E另一目录.pdf','%E9%A3%9F%E5%93%81%安全目录.pdf','%E9%A3%9F%E5%93%81%E5安全目录.pdf','%E9%A3%9F%E5%93%81%E安全%E目录.pdf','%E9%A3%9F%E5%93%81%E安全目录.shtml'])
def test_pdf_encoding_repair_refuses_changed_filename_and_other_damage(db,filename):
    from urllib.parse import quote
    data=business.capture(db,'601919')['input'];r=result(data)
    r['sources'][0]['url']='https://example.com/'+quote('食品安全目录.pdf')
    data['web_sources']=[{'url':s['url']} for s in r['sources']]
    r['sources'][0]['url']='https://example.com/'+filename
    with pytest.raises(ValueError):business.validate(encoded(r),data)


def test_utf8_normalization_preserves_reserved_path_and_query_identity():
    assert business.url_key('https://example.com/a%2Fb.pdf') != business.url_key('https://example.com/a/b.pdf')
    assert business.url_key('https://example.com/食品.pdf?v=1') != business.url_key('https://example.com/食品.pdf?v=2')


@pytest.mark.parametrize('url',['https://example.com/other','https://example.com/1?version=2','http://example.com/1'])
def test_url_matching_does_not_allow_other_pages_or_queries(db,url):
    data=business.capture(db,'601919')['input'];r=result(data)
    data['web_sources']=[{'url':s['url']} for s in r['sources']];r['sources'][0]['url']=url
    with pytest.raises(ValueError,match='未出现在本次实际检索') as caught:business.validate(encoded(r),data)
    assert caught.value.diagnostic['unmatched_url']==url


def test_failed_source_validation_keeps_specific_provenance_diagnostic(db):
    class BrokenSource(Provider):
        def infer(self,*args,**kwargs):
            output=super().infer(*args,**kwargs);r=json.loads(output['text'])
            r['sources'][0]['url']='https://example.com/not-searched';output['text']=encoded(r);return output
    item=service(db,BrokenSource());run=item.create({'code':'601919','request_key':uuid4().hex})
    failed=wait_terminal(item,run['id']);assert failed['status']=='failed'
    with db.connection() as conn:diagnostic=json.loads(conn.execute('SELECT diagnostic_json FROM ai_analysis_runs WHERE id=?',(run['id'],)).fetchone()[0])
    assert diagnostic['validation']['field']=='sources.web.1'
    assert diagnostic['validation']['unmatched_url']=='https://example.com/not-searched'
    assert len(diagnostic['validation']['retrieved_sources'])==3 and item.provider.calls==1


@pytest.mark.parametrize('fault', ['invented_url', 'private_url', 'future_date', 'unknown_ref', 'missing_gaps', 'wrong_code', 'duplicate_question', 'missing_framework', 'missing_dimension', 'framework_unknown_ref', 'invalid_framework_link', 'missing_risk_verification'])
def test_validation_refuses_untraceable_or_inconsistent_output(db, fault):
    data=business.capture(db,'601919')['input']; r=result(data)
    data['web_sources']=[{'url': s['url']} for s in r['sources']]
    if fault=='invented_url': r['sources'][0]['url']='https://example.com/invented'
    if fault=='private_url': r['sources'][0]['url']='http://127.0.0.1/private'; data['web_sources'].append({'url':r['sources'][0]['url']})
    if fault=='future_date': r['sources'][0]['published_on']='2099-01-01'
    if fault=='unknown_ref': r['rows'][0]['evidence_ids']=['invented']
    if fault=='missing_gaps': r['unknowns']=[]
    if fault=='wrong_code': r['code']='600900'
    if fault=='duplicate_question': r['rows'][1]['question']=r['rows'][0]['question']
    if fault=='missing_framework': del r['frameworks']['capabilities']
    if fault=='missing_dimension': r['frameworks']['pestel'].pop()
    if fault=='framework_unknown_ref': r['frameworks']['pestel'][0]['evidence_ids']=['invented']
    if fault=='invalid_framework_link': r['rows'][0]['framework_refs']=['capabilities.虚构能力']
    if fault=='missing_risk_verification': r['frameworks']['pestel'][0].pop('risk_verification')
    with pytest.raises(ValueError): business.validate(encoded(r), data)


def test_shared_worker_dispatches_both_types_and_keeps_history_separate(db):
    # Legacy task uses its own synthetic input; new module works without local finance.
    from quarterly_dashboard.analysis_validation import prompt
    provider=Provider(); provider.release.clear()
    old=AnalysisService(db,provider, snapshotter=business.capture, prompt_factory=prompt, validator=lambda text,data:{'summary':'旧类型测试'})
    new=service(db,provider,shared=old)
    first=new.create({'code':'601919','request_key':uuid4().hex})
    assert provider.entered.wait(3)
    with pytest.raises(ValueError, match='其他类型'):
        old.create({'code':'601919','request_key':uuid4().hex})
    second=old.create({'code':'600900','model':'test-model','request_key':uuid4().hex})
    # A different type is claimed by the already running worker and validated by its own handler.
    def infer(run_id,model,instructions,data,cancelled,**kwargs):
        if kwargs:return Provider.infer(provider,run_id,model,instructions,data,cancelled,**kwargs)
        return {'text':'{}'}
    provider.infer=infer
    provider.release.set()
    assert wait_terminal(new,first['id'])['status']=='succeeded'
    assert wait_terminal(old,second['id'])['status']=='succeeded'
    assert new.repository.history('600900')['items']==[]
    with pytest.raises(ValueError):new.repository.run(second['id'])
    with pytest.raises(ValueError):new.repository.delete_history('600900',[second['id']])
    saved=new.repository.run(first['id'])
    assert saved['result']['retrieval']['sources'] and 'web_sources' not in saved['input']
    assert len(saved['result']['frameworks']['pestel'])==6 and len(saved['result']['frameworks']['five_forces'])==5
    assert new.create({'code':'601919','request_key':uuid4().hex})['id']==first['id']
    assert new.repository.delete_history('601919',[first['id']])['deleted_runs']==1
    assert old.repository.run(second['id'])['status']=='succeeded'


@pytest.mark.parametrize('search', [True,False])
def test_provider_requires_real_completed_search_and_requests_correct_tools(tmp_path,search):
    output=[{'type':'message','content':[{'type':'output_text','text':'{}'}]}]
    if search:output.insert(0,{'type':'web_search_call','status':'completed','action':{'type':'search','sources':[{'url':'https://example.com/report','title':'来源'}]}})
    stream=StreamResponse([{'type':'response.completed','response':{'status':'completed','output':output}}])
    class Session:
        def post(self,*args,**kwargs):self.payload=kwargs['json'];return stream
    credentials=MemoryCredentials(); credentials.value={'access_token':'secret','refresh_token':'secret2','expires_at':time.time()+3600,'scopes':[PLAN_SCOPE],'subject':'subject','client_id':'oaiapp_test'}
    session=Session(); provider=ChatGPTProvider(tmp_path,session=session,credentials=credentials)
    if search:
        assert provider.infer('run','model','prompt',{},Event(),web_search=True)['web_sources'][0]['url']=='https://example.com/report'
    else:
        with pytest.raises(ProviderError,match='未完成联网搜索'):provider.infer('run','model','prompt',{},Event(),web_search=True)
    assert session.payload['tools']==[{'type':'web_search'}]
    assert session.payload['tool_choice']=='required' and session.payload['include']==['web_search_call.action.sources']
    assert 'max_tool_calls' not in session.payload and stream.closed


@pytest.mark.parametrize('kind',['open_page','find_in_page','item_annotation','annotation_event'])
def test_provider_collects_page_actions_and_streamed_citations(tmp_path,kind):
    url='https://example.com/actual-page';events=[]
    call={'type':'web_search_call','status':'completed','action':{'type':'search','sources':[]}}
    if kind in ('open_page','find_in_page'):
        events.append({'type':'response.output_item.done','item':{'type':'web_search_call','status':'completed','action':{'type':kind,'url':url}}})
    elif kind=='item_annotation':
        events.append({'type':'response.output_item.done','item':{'type':'message','content':[{'type':'output_text','annotations':[{'type':'url_citation','url':url,'title':'Actual'}]}]}})
    else:events.append({'type':'response.output_text.annotation.added','annotation':{'type':'url_citation','url':url,'title':'Actual'}})
    events.append({'type':'response.completed','response':{'status':'completed','output':[call,{'type':'message','content':[{'type':'output_text','text':'{}'}]}]}})
    stream=StreamResponse(events)
    class Session:
        def post(self,*args,**kwargs):return stream
    credentials=MemoryCredentials();credentials.value={'access_token':'secret','refresh_token':'secret2','expires_at':time.time()+3600,'scopes':[PLAN_SCOPE],'subject':'subject','client_id':'oaiapp_test'}
    provider=ChatGPTProvider(tmp_path,session=Session(),credentials=credentials)
    assert provider.infer('run','model','prompt',{},Event(),web_search=True)['web_sources'][0]['url']==url


def test_business_http_no_inference_on_reads_and_csrf_protection(db,monkeypatch):
    item=service(db);monkeypatch.setattr(server,'business_service',lambda:item);monkeypatch.setattr(server,'ai_service',lambda:item)
    http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);thread=Thread(target=http.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{http.server_port}'
    try:
        for path in ('/api/business-judgment?code=601919','/api/business-judgment/history?code=601919'):
            assert requests.get(base+path).status_code==200
        assert item.provider.calls==0
        command={'code':'601919','request_key':uuid4().hex}
        assert requests.post(base+'/api/business-judgment',json=command).status_code==403
        token=requests.get(base+'/api/ai/status').json()['session_token']
        headers={'Origin':base,'X-Local-Session':token}
        created=requests.post(base+'/api/business-judgment',json=command,headers=headers)
        assert created.status_code==202 and wait_terminal(item,created.json()['id'])['status']=='succeeded'
        assert requests.get(base+'/api/business-judgment/history?code=601919').json()['items']
        assert requests.get(base+'/api/business-judgment?code=601919',headers={'Host':'evil.example'}).status_code==400
    finally:
        http.shutdown();http.server_close();thread.join(3)


def test_browser_generation_history_evidence_and_layout(db):
    from pathlib import Path
    from urllib.parse import urlsplit
    playwright=pytest.importorskip('playwright.sync_api')
    class TextProvider(Provider):
        def infer(self,*args,**kwargs):
            output=super().infer(*args,**kwargs)
            r=json.loads(output['text']); r['summary']='<img src=x onerror="window.hacked=true">规模支持份额，但利润取决于运价。'
            r['rows'][0]['evidence_ids'].append('financial.2026-06-30')
            output['text']=encoded(r); return output
    item=service(db,TextProvider()); root=Path(__file__).resolve().parents[1]; base='http://127.0.0.1:8768'
    capture=item.snapshotter
    def with_finance(database,code):
        snapshot=capture(database,code)
        snapshot['input']['evidence'].append({'id':'financial.2026-06-30','metric':'financial_period','observed_on':'2026-06-30','source':'sina/normalized/manual','value':{'cash':141400000000,'cash_flow':0},'basis':{'cash':{'report_type':'fzb','unit':'元'},'cash_flow':{'report_type':'gjzb','unit':'元'}},'methodology':'现金为期末余额，经营现金流为半年累计。'})
        return snapshot
    item.snapshotter=with_finance
    with playwright.sync_playwright() as p:
        try:
            browser=p.chromium.launch(channel='msedge',headless=True)
        except playwright.Error as exc:
            pytest.skip('Headless Edge unavailable: '+str(exc).splitlines()[0])
        with browser:
            page=browser.new_page(viewport={'width':1280,'height':900});errors=[]
            legacy=[False]
            old_framework=[False]
            active=[None]
            held_status=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            def route(request):
                path=urlsplit(request.request.url).path
                if path=='/':
                    request.fulfill(content_type='text/html',body='<html><body><div class="context"></div></body></html>');return
                if path=='/api/ai/status':
                    held_status.append(request);return
                elif path=='/api/business-judgment' and request.request.method=='POST':data=item.create(request.request.post_data_json)
                elif path=='/api/business-judgment':
                    data=item.overview('601919')
                    if data.get('report'):
                        from quarterly_dashboard.industry_position import parse
                        sample=json.loads((root/'tests/fixtures/alphaliner-top100-20261010.json').read_text(encoding='utf-8'))
                        data['report']['result']['industry_position']=parse(sample,captured_at='2026-10-10T04:00:00+00:00')
                    if active[0]:data['active']=active[0]
                    if legacy[0] and data.get('report'):
                        data['report']['result'].pop('frameworks',None)
                        for row in data['report']['result']['rows']:row.pop('framework_refs',None)
                    if old_framework[0] and data.get('report'):
                        for rows in data['report']['result'].get('frameworks',{}).values():
                            for row in rows:row.pop('risk_verification',None)
                elif path=='/api/business-judgment/history/delete':data=item.repository.delete_history(**request.request.post_data_json)
                elif path=='/api/business-judgment/history':data=item.repository.history('601919')
                elif path.startswith('/api/business-judgment/runs/'):data=item.repository.run(path.rsplit('/',1)[-1])
                else:request.abort();return
                request.fulfill(content_type='application/json',body=encoded(data))
            page.route('**/*',route);page.goto(base)
            page.add_style_tag(path=str(root/'web/ai-checklist.css'));page.add_script_tag(path=str(root/'web/business-judgment.js'))
            page.evaluate("document.querySelector('.context').innerHTML='<button>Checklist</button><button>AI 研报</button><button>AI 设置</button>';window.initBusinessJudgment({code:'601919'},document.querySelector('.context'),()=>{});")
            assert page.locator('.context > button').all_text_contents()==['Checklist','经营判断','AI 研报','AI 设置']
            page.get_by_role('button',name='经营判断',exact=True).click()
            drawer=page.get_by_role('dialog',name='经营判断',exact=True)
            page.get_by_role('button',name='生成经营判断',exact=True).wait_for();assert item.provider.calls==0
            page.get_by_role('button',name='生成经营判断',exact=True).click()
            page.get_by_text('正在提交 · 已用 0 秒',exact=True).wait_for()
            assert page.get_by_role('button',name='生成经营判断',exact=True).is_disabled()
            page.wait_for_function("()=>document.querySelector('.business-progress')?.textContent.includes('已用 1 秒')")
            assert item.provider.calls==0
            held_status.pop(0).fulfill(content_type='application/json',body=encoded({**item.status(),'session_token':'test'}))
            page.wait_for_timeout(50)
            if held_status:held_status.pop(0).fulfill(content_type='application/json',body=encoded({**item.status(),'session_token':'test'}))
            page.locator('.business-conclusion-table tbody tr').first.wait_for(timeout=15000)
            page.mouse.click(5,450)
            assert not page.locator('.business-dialog').evaluate('(n)=>n.open')
            page.get_by_role('button',name='经营判断',exact=True).click()
            page.locator('.business-conclusion-table tbody tr').first.wait_for()
            assert page.locator('.business-conclusion-table > tbody > tr').count()==5 and item.provider.calls==1
            assert page.locator('.business-framework-table').count()==3
            assert page.locator('.business-position-table > tbody > tr').count()==5
            assert '10.6%' in page.locator('.business-position-company').inner_text()
            assert '3,670,793' in page.locator('.business-position-company').inner_text()
            assert page.locator('[data-framework="pestel"] > tbody > tr').count()==6
            assert page.locator('[data-framework="five_forces"] > tbody > tr').count()==5
            assert page.locator('[data-framework="capabilities"] > tbody > tr').count()==5
            assert drawer.evaluate('(n)=>getComputedStyle(n).backgroundColor')=='rgb(238, 246, 233)'
            assert drawer.evaluate('(n)=>getComputedStyle(n).color')=='rgb(32, 33, 36)'
            assert page.locator('[data-framework="pestel"] thead').inner_text().strip().endswith('风险与后续验证点')
            assert '反证与验证点' in page.locator('[data-framework="five_forces"] thead').inner_text()
            assert '优势如何转化为经营结果' in page.locator('[data-framework="capabilities"] thead').inner_text()
            cells=page.locator('[data-framework="capabilities"] > tbody > tr').first.locator(':scope > td')
            assert '行业资料披露' in cells.nth(1).inner_text() and cells.nth(1).locator('.business-inline-sources').get_by_role('link',name='行业来源').count()==1
            assert '规模有助于网络效率' in cells.nth(2).inner_text() and '观察新船交付' in cells.nth(3).inner_text()
            assert page.locator('.business-thesis').inner_text().startswith('<img')
            assert page.locator('.business-thesis img').count()==0 and page.evaluate('window.hacked') is None
            detail=page.locator('.business-conclusion-table details').first;detail.locator('summary').click()
            assert detail.get_by_role('link',name='行业来源').get_attribute('href')=='https://example.com/1'
            assert '货币资金' in detail.inner_text() and '1,414 亿元' in detail.inner_text() and '0 元' in detail.inner_text()
            assert '2026-06-30' in detail.inner_text() and '期末余额' in detail.inner_text()
            assert detail.locator('pre').count()==0 and 'CURFDS' not in detail.inner_text()
            for width in (1280,390):
                page.set_viewport_size({'width':width,'height':900})
                assert drawer.evaluate('(n)=>n.scrollWidth<=n.clientWidth+1')
            page.get_by_role('button',name='历史记录',exact=True).click()
            page.get_by_role('button',name='删除所选',exact=True).wait_for();assert item.provider.calls==1
            assert page.get_by_role('button',name='删除所选',exact=True).is_disabled()
            page.get_by_role('button',name='全选已加载',exact=True).click()
            assert page.locator('.ai-history-select:checked').count()==1
            page.get_by_role('button',name='清空选择',exact=True).click()
            assert page.locator('.ai-history-select:checked').count()==0
            page.get_by_role('button',name=__import__('re').compile('succeeded')).click()
            page.locator('.business-conclusion-table').wait_for()
            assert page.locator('.business-framework-table').count()==3 and item.provider.calls==1
            position=page.locator('[data-framework="industry_position"]')
            assert position.locator('tbody tr').count()==1
            assert '排名未取得 / 份额未取得' in position.inner_text() and 'CR3：不可计算' in position.inner_text()
            assert page.evaluate("()=>{const a=document.querySelector('[data-framework=five_forces]'),b=document.querySelector('[data-framework=industry_position]'),c=document.querySelector('[data-framework=capabilities]');return !!(a.compareDocumentPosition(b)&4)&&!!(b.compareDocumentPosition(c)&4);}")
            for width in (1280,390):
                page.set_viewport_size({'width':width,'height':900})
                assert drawer.evaluate('(n)=>n.scrollWidth<=n.clientWidth+1')
            page.get_by_role('button',name='当前结果',exact=True).click();page.locator('.business-conclusion-table').wait_for()
            assert item.provider.calls==1 and not errors
            old_framework[0]=True
            page.get_by_role('button',name='当前结果',exact=True).click()
            page.get_by_text('旧版未单独保存限制、反证与验证点；重新生成可补齐。',exact=True).first.wait_for()
            assert page.locator('.business-framework-table').count()==3 and item.provider.calls==1
            old_framework[0]=False
            legacy[0]=True
            page.get_by_role('button',name='当前结果',exact=True).click()
            page.get_by_text('此历史版本仅保存了结论表，没有三张前置分析表。点击重新生成可保存完整分析。',exact=True).wait_for()
            assert page.locator('.business-framework-table').count()==0 and item.provider.calls==1
            old=page.locator('.business-conclusion-table details').first;old.locator('summary').click()
            assert '1,414 亿元' in old.inner_text() and old.locator('pre').count()==0
            from datetime import datetime,timedelta,timezone
            legacy[0]=False
            active[0]={'id':'test-active','status':'running','started_at':(datetime.now(timezone.utc)-timedelta(seconds=30)).isoformat()}
            page.get_by_role('button',name='当前结果',exact=True).click();page.locator('.business-progress').wait_for()
            before=page.locator('.business-progress').inner_text();assert '已用' in before and '秒' in before
            initial=int(__import__('re').search(r'已用 (\d+) 秒',before)[1])
            page.wait_for_function('(initial)=>Number(document.querySelector(".business-progress")?.textContent.match(/已用 (\\d+) 秒/)?.[1])>initial',arg=initial)
            page.get_by_role('button',name='关闭',exact=True).click()
            page.get_by_role('button',name='经营判断',exact=True).click();page.locator('.business-progress').wait_for()
            assert int(__import__('re').search(r'已用 (\d+) 秒',page.locator('.business-progress').inner_text())[1])>initial
            active[0]={'id':'test-active','status':'queued','created_at':(datetime.now(timezone.utc)-timedelta(seconds=10)).isoformat()}
            page.get_by_role('button',name='当前结果',exact=True).click();page.get_by_text(__import__('re').compile('已等待.*秒')).wait_for()
            assert item.provider.calls==1 and not errors
            active[0]=None
            extra=item.create({'code':'601919','request_key':uuid4().hex,'force':True})
            assert wait_terminal(item,extra['id'])['status']=='succeeded'
            page.get_by_role('button',name='历史记录',exact=True).click()
            page.locator('.ai-history-select').nth(1).wait_for()
            page.get_by_role('button',name='全选已加载',exact=True).click()
            assert page.locator('.ai-history-select:checked').count()==2
            page.on('dialog',lambda d:d.accept())
            page.get_by_role('button',name='删除所选',exact=True).click()
            page.get_by_text('暂无历史记录。',exact=True).wait_for()
            assert item.repository.history('601919')['items']==[] and item.provider.calls==2


def test_reviewed_v6_prompt_is_loaded():
    config = business.prompt()
    assert config['version'] == 'business_judgment_prompt_v15'
    assert '中远海控的建议六行' in config['instructions']
    assert '护城河；规模与网络优势' in config['instructions']
    assert '宏观经济对公司的影响' in config['instructions']
    assert '研究对象排名与份额' in config['instructions']
    assert '订单不加进现有份额' in config['instructions']


def test_conflicting_market_ranks_are_not_adopted_and_originals_are_preserved(db):
    data=business.capture(db,'601919')['input'];r=result(data)
    data['web_sources']=[{'url':s['url']} for s in r['sources']]
    market=r['industry_position']['markets'][0]
    market.update(company_rank=2,company_share_pct=8.73,status='ready',cr3_pct=28,
                  competitors=[{'name':'不同指标的同行A','rank':1,'share_pct':11},
                               {'name':'不同指标的同行B','rank':1,'share_pct':9}])
    saved=business.validate(encoded(r),data)
    row=saved['industry_position']['markets'][0]
    assert row['company_rank'] is None and row['company_share_pct'] is None
    assert row['cr3_pct'] is None and row['status']=='limited'
    assert all(p['rank'] is None and p['share_pct'] is None for p in row['competitors'])
    conflict=saved['retrieval']['market_conflicts'][0]
    assert conflict['company_share_pct']==8.73 and conflict['competitors'][1]['share_pct']==9
    assert '未采用' in row['limitations'] and '统计口径冲突' in saved['unknowns'][-1]


def test_market_conflict_does_not_exceed_unknowns_limit(db):
    data=business.capture(db,'601919')['input'];r=result(data)
    data['web_sources']=[{'url':s['url']} for s in r['sources']]
    r['unknowns']=[f'缺口{i}' for i in range(8)]
    r['industry_position']['markets'][0].update(company_rank=1,
        competitors=[{'name':'同行','rank':1,'share_pct':20}])
    saved=business.validate(encoded(r),data)
    assert len(saved['unknowns'])==8 and saved['retrieval']['market_conflicts']


def test_url_path_typo_is_rejected_with_readable_diagnostic(db):
    class TypoProvider(Provider):
        def infer(self,*args,**kwargs):
            output=super().infer(*args,**kwargs);result=json.loads(output['text'])
            result['sources'][0]['url']='https://unctad.org/news/maritime-trade-under-pressure-growth-stall-2025'
            output['web_sources'][0]['url']='https://unctad.org/news/maritime-trade-under-pressure-growth-set-stall-2025'
            output['text']=encoded(result);return output
    item=service(db,TypoProvider());created=item.create({'code':'601919','request_key':uuid4().hex})
    failed=wait_terminal(item,created['id'])
    assert failed['status']=='failed' and failed['result'] is None
    assert failed['source_error']['model_url'].endswith('growth-stall-2025')
    assert failed['source_error']['retrieved_candidates'][0].endswith('growth-set-stall-2025')
    assert item.provider.calls==1


def test_supplied_local_report_url_is_not_misclassified_as_unretrieved():
    data={'instrument':{'code':'601919'},'as_of':'2026-10-10','evidence':[
        {'id':'company.page.4.8','metric':'report_excerpt','document_id':4,'file_hash':'a'*64,
         'source':'https://static.cninfo.com.cn/finalpage/2026-08-29/1225531118.PDF','published_on':'2026-08-29'}]}
    r=result(data);data['web_sources']=[{'url':s['url']} for s in r['sources']]
    r['sources'].append({'id':'web.4','title':'本地半年报','url':data['evidence'][0]['source'],
                         'published_on':None,'excerpt':'来自已提供的财报片段。'})
    r['rows'][0]['evidence_ids']=['web.4']
    saved=business.validate(encoded(r),data)
    assert saved['sources'][3]['origin']=='local_report'
    assert saved['sources'][3]['local_evidence_ids']==['company.page.4.8']
    assert saved['sources'][3]['published_on']=='2026-08-29'
    assert all(s['origin']=='web_search' for s in saved['sources'][:3])
    r['sources'][3]['url']='https://static.cninfo.com.cn/finalpage/2026-08-22/1225493796.PDF'
    with pytest.raises(ValueError,match='未匹配输入的本地财报'):business.validate(encoded(r),data)
    r['sources'][3]['url']=data['evidence'][0]['source']
    data['evidence'][0]['metric']='company'
    with pytest.raises(ValueError,match='未匹配输入的本地财报'):business.validate(encoded(r),data)
    data['evidence'][0]['metric']='report_excerpt'
    data['web_sources']=data['web_sources'][:2]
    # Third source also supplied as a local PDF must not count toward the network minimum.
    data['evidence'].append({**data['evidence'][0],'id':'company.page.5.1','document_id':5,'source':r['sources'][2]['url']})
    with pytest.raises(ValueError,match='至少需要3份'):business.validate(encoded(r),data)


def test_public_industry_rpc_sample_and_generation_persistence(db,monkeypatch):
    from pathlib import Path
    from quarterly_dashboard.industry_position import parse,PAGE
    raw=json.loads((Path(__file__).resolve().parents[1]/'tests/fixtures/alphaliner-top100-20261010.json').read_text(encoding='utf-8'))
    value=parse(raw,captured_at='2026-10-10T04:00:00+00:00')
    assert value['company']=={'rank':4,'name':'COSCO Group','teu':3670793,'share_pct':10.6}
    assert value['cr3_pct']==48.1 and value['cr5_pct']==65.7
    assert value['statistics_date'] is None
    value['response_sha256']='a'*64
    evidence={'id':'industry.position','metric':'industry_position','source':PAGE,'observed_on':'2026-10-10','value':value}
    calls=[]
    monkeypatch.setattr('quarterly_dashboard.industry_position.collect',lambda *args:calls.append(1) or evidence)
    class WithIndustry(Provider):
        def infer(self,*args,**kwargs):
            output=super().infer(*args,**kwargs);data=args[3]
            assert data['evidence'][-1]['id']=='industry.position'
            r=json.loads(output['text']);r['frameworks']['five_forces'][-1]['evidence_ids']=['industry.position']
            output['text']=encoded(r);return output
    item=service(db,WithIndustry())
    item.overview('601919');assert not calls
    created=item.create({'code':'601919','request_key':uuid4().hex})
    saved=wait_terminal(item,created['id'])
    assert saved['status']=='succeeded' and saved['result']['industry_position_direct']['company']['rank']==4
    assert saved['input']['evidence'][-1]==evidence
    assert len(calls)==1 and item.provider.calls==1
    item.overview('601919');assert len(calls)==1


def test_invalid_industry_ranking_is_not_accepted():
    from quarterly_dashboard.industry_position import parse
    payload=[{'action':'top100','type':'rpc','method':'getTop100Table','result':[
        {'rank':1,'operator':'COSCO Group','totalTeu':1,'percent':10.6}]*5}]
    with pytest.raises(ValueError):parse(payload,captured_at='2026-10-10T04:00:00+00:00')


@pytest.mark.parametrize('fault',['missing_table','duplicate_market','negative_share','bool_rank','nan_share','foreign_evidence','industry_only_conclusion'])
def test_universal_position_rejects_unreliable_structure(db,fault):
    data=business.capture(db,'601919')['input'];data['require_industry_position']=True;r=result(data)
    data['web_sources']=[{'url':x['url']} for x in r['sources']]
    row=r['industry_position']['markets'][0]
    if fault=='missing_table':r.pop('industry_position')
    if fault=='duplicate_market':r['industry_position']['markets'].append(dict(row))
    if fault=='negative_share':row['company_share_pct']=-1
    if fault=='bool_rank':row['company_rank']=True
    if fault=='nan_share':row['company_share_pct']=float('nan')
    if fault=='foreign_evidence':row['evidence_ids']=['web.99']
    if fault=='industry_only_conclusion':r['rows'][0]['framework_refs']=['industry_position.核心市场']
    with pytest.raises(ValueError):business.validate(encoded(r),data)


def test_universal_position_preserves_zero_and_legacy_compatibility(db):
    data=business.capture(db,'601919')['input'];r=result(data)
    data['web_sources']=[{'url':x['url']} for x in r['sources']]
    r['industry_position']['markets'][0]['company_share_pct']=0
    r['rows'][0]['framework_refs'].append('industry_position.核心市场')
    assert business.validate(encoded(r),data)['industry_position']['markets'][0]['company_share_pct']==0
    r.pop('industry_position');r['rows'][0]['framework_refs'].pop()
    assert 'industry_position' not in business.validate(encoded(r),data)


@pytest.mark.parametrize('wrapper',[lambda x:x,lambda x:'```json\n'+x+'\n```',lambda x:'\ufeff```JSON\r\n'+x+'\r\n```'])
def test_business_json_accepts_only_complete_object_or_fence(db,wrapper):
    data=business.capture(db,'601919')['input'];r=result(data)
    data['web_sources']=[{'url':x['url']} for x in r['sources']]
    assert business.validate(wrapper(encoded(r)),data)['code']=='601919'
    r['sources'][0]['url']='https://example.com/invented'
    with pytest.raises(ValueError,match='未出现在本次实际检索'):business.validate(wrapper(encoded(r)),data)


@pytest.mark.parametrize('text',['说明\n{}','{} {}','```json\n{}','{"code":"601919",}','{"code":'])
def test_business_json_rejects_prose_truncation_and_syntax_errors(db,text):
    from quarterly_dashboard.analysis_validation import ReportValidationError
    with pytest.raises(ReportValidationError,match='不是有效 JSON') as caught:
        business.validate(text,business.capture(db,'601919')['input'])
    assert caught.value.diagnostic['format_error']['line']>=1
    assert 'raw_output' not in caught.value.diagnostic


@pytest.mark.parametrize('phase',['final_answer',None,'streamed'])
def test_provider_selects_final_search_message_without_preamble(tmp_path,phase):
    first={'type':'message','phase':'commentary','content':[{'type':'output_text','text':'正在查找来源。','annotations':[{'type':'url_citation','url':'https://example.com/preamble'}]}]}
    last={'type':'message','phase':'final_answer' if phase=='streamed' else phase,'content':[{'type':'output_text','text':'{"answer":1}'}]}
    call={'type':'web_search_call','status':'completed','action':{'type':'search','sources':[{'url':'https://example.com/report'}]}}
    events=[]
    if phase=='streamed':events=[{'type':'response.output_item.done','item':first},{'type':'response.output_item.done','item':last}]
    events.append({'type':'response.completed','response':{'status':'completed','output':[call] if phase=='streamed' else [first,call,last]}})
    stream=StreamResponse(events)
    class Session:
        def post(self,*args,**kwargs):return stream
    credentials=MemoryCredentials();credentials.value={'access_token':'secret','refresh_token':'secret2','expires_at':time.time()+3600,'scopes':[PLAN_SCOPE],'subject':'subject','client_id':'oaiapp_test'}
    provider=ChatGPTProvider(tmp_path,session=Session(),credentials=credentials)
    output=provider.infer('run','model','prompt',{},Event(),web_search=True)
    assert output['text']=='{"answer":1}' and output['diagnostic']['output_message_count']==2
    assert any(x['url']=='https://example.com/preamble' for x in output['web_sources'])



def test_invalid_json_failure_keeps_format_and_provider_diagnostics(db):
    class InvalidJson(Provider):
        def infer(self,*args,**kwargs):
            output=super().infer(*args,**kwargs)
            output['text']='{"code":'
            output['diagnostic']={'terminal_event':'response.completed','output_message_count':2}
            return output
    item=service(db,InvalidJson());run=item.create({'code':'601919','request_key':uuid4().hex})
    failed=wait_terminal(item,run['id'])
    assert failed['status']=='failed' and item.provider.calls==1
    diagnostic=json.loads(item.repository.run(run['id'],internal=True)['diagnostic_json'])
    assert diagnostic['validation']['format_error']['column']>0
    assert diagnostic['validation']['provider']['output_message_count']==2
    assert 'raw_output' not in diagnostic['validation']
    assert diagnostic['replay']['text']=='{"code":'
    assert len(diagnostic['replay']['web_sources'])==3
    assert 'replay' not in item.repository.run(run['id'])


def test_provider_rejects_commentary_without_final_json(tmp_path):
    output=[{'type':'web_search_call','status':'completed','action':{'type':'search','sources':[{'url':'https://example.com/report'}]}},{'type':'message','phase':'commentary','content':[{'type':'output_text','text':'正在搜索'}]}]
    stream=StreamResponse([{'type':'response.output_text.delta','delta':'正在搜索'},{'type':'response.completed','response':{'status':'completed','output':output}}])
    class Session:
        def post(self,*args,**kwargs):return stream
    credentials=MemoryCredentials();credentials.value={'access_token':'secret','refresh_token':'secret2','expires_at':time.time()+3600,'scopes':[PLAN_SCOPE],'subject':'subject','client_id':'oaiapp_test'}
    provider=ChatGPTProvider(tmp_path,session=Session(),credentials=credentials)
    with pytest.raises(ProviderError,match='缺少最终经营判断'):provider.infer('run','model','prompt',{},Event(),web_search=True)



def test_duplicate_document_merges_excerpts_and_all_reference_groups(db):
    data=business.capture(db,'601919')['input'];r=result(data)
    data['web_sources']=[{'url':x['url']} for x in r['sources']]
    r['sources'].append({**r['sources'][0],'id':'web.4','url':'https://EXAMPLE.com:443/1#customers','excerpt':'年报披露前五客户比例。'})
    for group in r['frameworks'].values():group[0]['evidence_ids']=['web.4','web.1']
    r['industry_position']['markets'][0]['evidence_ids']=['web.4']
    r['rows'][0]['evidence_ids']=['web.4']
    r['change_conditions'][0]['evidence_ids']=['web.4']
    saved=business.validate(encoded(r),data)
    assert len(saved['sources'])==3 and saved['retrieval']['source_aliases']=={'web.4':'web.1'}
    assert '行业周期影响定价。' in saved['sources'][0]['excerpt'] and '前五客户比例' in saved['sources'][0]['excerpt']
    for group in saved['frameworks'].values():assert group[0]['evidence_ids']==['web.1']
    assert saved['industry_position']['markets'][0]['evidence_ids']==['web.1']
    assert saved['rows'][0]['evidence_ids']==['web.1']
    assert saved['change_conditions'][0]['evidence_ids']==['web.1']


def test_duplicate_network_document_does_not_inflate_minimum_sources(db):
    data=business.capture(db,'601919')['input'];r=result(data)
    data['web_sources']=[{'url':x['url']} for x in r['sources']]
    r['sources'][1]['url']=r['sources'][0]['url']
    with pytest.raises(ValueError,match='去重后'):business.validate(encoded(r),data)


def test_duplicate_document_does_not_merge_different_query_or_invalid_source(db):
    data=business.capture(db,'601919')['input'];r=result(data)
    r['sources'].append({**r['sources'][0],'id':'web.4','url':'https://example.com/1?version=2'})
    data['web_sources']=[{'url':x['url']} for x in r['sources']]
    assert len(business.validate(encoded(r),data)['sources'])==4
    data['web_sources'].pop()
    with pytest.raises(ValueError,match='未出现在本次实际检索'):business.validate(encoded(r),data)



@pytest.mark.parametrize('suffix',['}', ' } \n'])
def test_single_extra_final_brace_preserves_whole_report_and_source_checks(db,suffix):
    data=business.capture(db,'601919')['input'];r=result(data)
    data['web_sources']=[{'url':x['url']} for x in r['sources']]
    saved=business.validate(encoded(r)+suffix,data)
    assert saved['summary']==r['summary'] and saved['frameworks']==r['frameworks']
    assert saved['industry_position']==r['industry_position'] and saved['rows']==r['rows']
    assert saved['retrieval']['output_normalizations']==['removed_single_extra_closing_brace']
    r['sources'][0]['url']='https://example.com/invented'
    with pytest.raises(ValueError,match='未出现在本次实际检索'):business.validate(encoded(r)+suffix,data)


@pytest.mark.parametrize('suffix',[',"final":true}', ', "final" : true }\n'])
def test_trailing_completion_marker_preserves_report_and_source_checks(db,suffix):
    data=business.capture(db,'601919')['input'];r=result(data)
    data['web_sources']=[{'url':x['url']} for x in r['sources']]
    saved=business.validate(encoded(r)+suffix,data)
    baseline=business.validate(encoded(r),data)
    assert {k:v for k,v in saved.items() if k!='retrieval'}=={k:v for k,v in baseline.items() if k!='retrieval'}
    assert saved['retrieval']['output_normalizations']==['removed_trailing_final_true_marker']
    r['sources'][0]['url']='https://example.com/invented'
    with pytest.raises(ValueError,match='未出现在本次实际检索'):business.validate(encoded(r)+suffix,data)


@pytest.mark.parametrize('count',[5,8,9,0])
def test_multi_business_market_count_keeps_every_market_and_bound(db,count):
    data=business.capture(db,'601919')['input'];r=result(data)
    data['web_sources']=[{'url':x['url']} for x in r['sources']]
    market=r['industry_position']['markets'][0]
    r['industry_position']['markets']=[{**market,'market':f'市场{i}'} for i in range(count)]
    if 1<=count<=8:
        assert len(business.validate(encoded(r),data)['industry_position']['markets'])==count
    else:
        with pytest.raises(ValueError,match='1至8') as caught:business.validate(encoded(r),data)
        assert caught.value.diagnostic['field']=='industry_position'


def test_known_framework_reference_moves_without_fabricating_source(db):
    data=business.capture(db,'601919')['input'];r=result(data)
    data['web_sources']=[{'url':x['url']} for x in r['sources']]
    ref='five_forces.行业竞争强度'
    r['rows'][0]['evidence_ids'].append(ref)
    saved=business.validate(encoded(r),data)
    assert saved['rows'][0]['evidence_ids']==['web.1']
    assert ref in saved['rows'][0]['framework_refs']
    assert saved['retrieval']['reference_moves']==[{'row':0,'moved_to_framework_refs':[ref]}]
    r['rows'][0]['evidence_ids'].append('web.99')
    with pytest.raises(ValueError,match='web.99'):business.validate(encoded(r),data)


@pytest.mark.parametrize('suffix',['}}', '{}', '1', '。', 'x', ']', ',"final":false}', ',"final":null}', ',"final":true,"other":1}', ',"summary":"replacement"}', ',"final":true}{}'])
def test_other_trailing_values_or_text_remain_invalid(db,suffix):
    data=business.capture(db,'601919')['input']
    with pytest.raises(ValueError,match='不是有效 JSON') as caught:business.validate(encoded(result(data))+suffix,data)
    assert caught.value.diagnostic['format_error']['error_codepoints'][0]==ord(suffix[0])



@pytest.mark.parametrize('complete',[True,False])
def test_unverified_concentration_becomes_gap_not_report_failure(db,complete):
    data=business.capture(db,'601919')['input'];r=result(data)
    data['web_sources']=[{'url':x['url']} for x in r['sources']]
    row=r['industry_position']['markets'][0];row.update(company_rank=1,company_share_pct=20,status='ready',cr3_pct=60,cr5_pct=80)
    if complete:row['competitors']=[{'name':'同行2','rank':2,'share_pct':15},{'name':'同行3','rank':3,'share_pct':10}]
    saved=business.validate(encoded(r),data);market=saved['industry_position']['markets'][0]
    assert market['company_rank']==1 and market['company_share_pct']==20
    assert market['cr3_pct'] is None and market['cr5_pct'] is None and market['status']=='limited'
    assert 'CR3未采用' in market['limitations'] and len(saved['retrieval']['concentration_checks'])==2


def test_verified_concentration_is_retained(db):
    data=business.capture(db,'601919')['input'];r=result(data)
    data['web_sources']=[{'url':x['url']} for x in r['sources']]
    row=r['industry_position']['markets'][0];row.update(company_rank=1,company_share_pct=20,cr3_pct=45)
    row['competitors']=[{'name':'同行2','rank':2,'share_pct':15},{'name':'同行3','rank':3,'share_pct':10}]
    saved=business.validate(encoded(r),data)
    assert saved['industry_position']['markets'][0]['cr3_pct']==45 and not saved['retrieval']['concentration_checks']



def test_real_response_category_alias_order_and_missing_measure(db):
    data=business.capture(db,'601919')['input'];r=result(data)
    data['web_sources']=[{'url':x['url']} for x in r['sources']]
    r['frameworks']['five_forces'][-1]['category']='现有竞争强度'
    r['frameworks']['five_forces'].reverse()
    r['rows'][0]['framework_refs']=['five_forces.现有竞争强度','capabilities.护城河']
    market=r['industry_position']['markets'][0];market.update(company_rank=1,status='ready',measure=None)
    saved=business.validate(encoded(r),data)
    assert tuple(x['category'] for x in saved['frameworks']['five_forces'])==business.FRAMEWORKS['five_forces']
    assert saved['rows'][0]['framework_refs'][0]=='five_forces.行业竞争强度'
    assert saved['industry_position']['markets'][0]['measure']=='份额指标及分母口径未披露'
    assert saved['industry_position']['markets'][0]['status']=='limited'



def test_stable_nonconsecutive_source_ids_and_precise_missing_reference(db):
    data=business.capture(db,'601919')['input'];r=result(data)
    data['web_sources']=[{'url':s['url']} for s in r['sources']]
    r['sources'][1]['id']='web.6'
    r['change_conditions'][0]['evidence_ids']=['web.6']
    r['change_conditions'][1]['evidence_ids']=['web.6']
    assert business.validate(encoded(r),data)['sources'][1]['id']=='web.6'
    r['frameworks']['five_forces'][0]['evidence_ids']=['web.99']
    with pytest.raises(ValueError,match='来源编号不存在') as caught:business.validate(encoded(r),data)
    assert caught.value.diagnostic['field']=='frameworks.five_forces[0].evidence_ids'
    assert caught.value.diagnostic['unknown_evidence_ids']==['web.99']


def test_ambiguous_duplicate_source_id_remains_rejected(db):
    data=business.capture(db,'601919')['input'];r=result(data)
    data['web_sources']=[{'url':s['url']} for s in r['sources']]
    r['sources'][1]['id']='web.1'
    with pytest.raises(ValueError,match='唯一web编号'):business.validate(encoded(r),data)



def test_annual_bargaining_is_read_without_checklist_and_reserved_before_budget(db,tmp_path,monkeypatch):
    import pdfplumber
    from quarterly_dashboard.company_report_service import CompanyReportService
    from quarterly_dashboard.analysis_snapshot import digest
    text='600900 2025 年度报告\n主要销售客户及主要供应商情况\n前五名客户销售额5380132.68万元，占年度销售总额35.40%；前五名供应商采购额6293907.69万元，占年度采购总额47.37%；其中关联方占年度采购总额42.49%。\n'+('年度说明 '*150)
    class Page:
        def extract_text(self):return text
    class PDF:
        pages=[Page()]
        def __enter__(self):return self
        def __exit__(self,*args):pass
    monkeypatch.setattr(pdfplumber,'open',lambda *args:PDF())
    original=tmp_path/'original.pdf';original.write_bytes(b'%PDF-annual-priority')
    svc=CompanyReportService(db);doc=svc.import_report('600900','2025-12-31',original,url='https://example.com/full-annual.pdf',published_on='2026-04-01');svc.parse(doc)
    first=business.capture(db,'600900',as_of='2026-10-10')
    assert first['input']['annual_report_check']['state']=='included'
    assert first['input']['evidence'][0]['document_id']==doc['id'] and '47.37%' in first['input']['evidence'][0]['value'][0]['text']
    old={'instrument':{'code':'600900'},'evidence':[{'id':'old.interim','metric':'report_excerpt','value':'中报其他资料'*3000}]}
    snapshot={'instrument_id':db.ensure_instrument('600900'),'input':old,'hash':digest(old),'manifest':{},'quality':{},'captured_at':'2026-10-10T00:00:00+00:00'}
    repo=AnalysisRepository(db);run=repo.enqueue(snapshot,uuid4().hex,'test','account',{'version':'test','output_version':'stock_checklist_output_v1','instructions':'test'})
    assert repo.transition(run['id'],'queued','running') and repo.transition(run['id'],'running','validating')
    assert repo.transition(run['id'],'validating','succeeded',result_json='{}',verdict='checklist',summary='test')
    latest=business.capture(db,'600900',as_of='2026-10-10')
    assert latest['input']['evidence'][0]['document_id']==doc['id']
    assert latest['input']['annual_report_check']['evidence_ids'][0] in {e['id'] for e in latest['input']['evidence']}
    assert business.capture(db,'600900',as_of='2026-03-01')['input']['annual_report_check']['state']=='local_annual_missing'
