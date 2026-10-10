import json
from threading import Event
from uuid import uuid4

import pytest
from quarterly_dashboard import business_judgment as business
from quarterly_dashboard.business_contract import source_catalog, report_schema, bind_report, check_schema, numbered_notes
from quarterly_dashboard.analysis_service import AnalysisService
from quarterly_dashboard.analysis_snapshot import encoded
from quarterly_dashboard.ai_provider import ChatGPTProvider
from test_business_judgment import db, result
from test_ai_assessment import FakeProvider, StreamResponse, MemoryCredentials, wait_terminal


def prepare(db):
    data=business.capture(db,'601919')['input'];raw=result(data)
    ledger=[{'url':s['url'],'title':f'真实标题{i}'} for i,s in enumerate(raw['sources'])]
    catalog=source_catalog(data,ledger)
    wire=json.loads(encoded(raw))
    wire['sources']=[{'id':s['id'],'excerpt':s['excerpt']} for s in raw['sources']]
    for row in wire['rows']:row['framework_refs']=[{'framework':'five_forces','row_index':4}]
    return data,raw,wire,ledger,catalog


def test_backend_binds_identity_and_schema_excludes_links(db):
    data,raw,wire,ledger,catalog=prepare(db)
    schema=report_schema(data,catalog)
    assert set(schema['properties']['sources']['items']['properties'])=={'id','excerpt'}
    saved=bind_report(encoded(wire),data,catalog)
    assert [s['url'] for s in saved['sources']]==[s['url'] for s in ledger]
    assert saved['sources'][0]['title']=='真实标题0'
    assert saved['sources'][0]['published_on'] is None
    assert saved['rows'][0]['framework_refs']==['five_forces.行业竞争强度']
    validated=business.validate(encoded(saved),dict(data,web_sources=ledger,require_industry_position=True))
    assert validated['sources'][0]['origin']=='web_search'


@pytest.mark.parametrize('fault',['url','title','unknown_id','extra_final','tail','bad_pointer','framework_as_evidence','overlong','missing_field'])
def test_strict_contract_rejects_invalid_wire_before_binding(db,fault):
    data,raw,wire,ledger,catalog=prepare(db)
    if fault in ('url','title'):wire['sources'][0][fault]='invented'
    elif fault=='unknown_id':wire['sources'][0]['id']='web.99'
    elif fault=='extra_final':wire['final']=True
    elif fault=='bad_pointer':wire['rows'][0]['framework_refs'][0]['row_index']=7
    elif fault=='framework_as_evidence':wire['rows'][0]['evidence_ids']=['five_forces.行业竞争强度']
    elif fault=='overlong':wire['frameworks']['pestel'][0]['analysis']='x'*401
    elif fault=='missing_field':wire['frameworks']['pestel'][0].pop('risk_verification')
    text=encoded(wire)+(',"final":true}' if fault=='tail' else '')
    with pytest.raises(ValueError):bind_report(text,data,catalog)


def test_catalog_deduplicates_and_marks_unavailable_title(db):
    data,raw,wire,ledger,catalog=prepare(db)
    ledger[0]['title']='';ledger.append({'url':ledger[0]['url']+'#section','title':'正式文件标题'})
    catalog=source_catalog(data,ledger)
    assert len(catalog)==3 and catalog[0]['title']=='正式文件标题'
    ledger[-1]['title']='';catalog=source_catalog(data,ledger)
    assert catalog[0]['title_missing'] and '未返回原文标题' in catalog[0]['title']


def test_annotation_offsets_bind_to_backend_number(db):
    *_,catalog=prepare(db)
    assert numbered_notes({'text':'事实[引用]尾部','web_annotations':[{'url':catalog[0]['url'],'start_index':2,'end_index':6}]},catalog)=='事实[web.1]尾部'


def test_analysis_failure_preserves_research_without_repair_or_retry(db):
    from quarterly_dashboard.business_contract import generate
    from quarterly_dashboard.ai_provider import ProviderError
    data,_,_,ledger,_=prepare(db)
    class Failing:
        calls=0
        def infer(self,*args,**kwargs):
            self.calls+=1
            if kwargs.get('web_search'):return {'text':'检索备忘录','web_sources':ledger,'response_id':'research'}
            raise ProviderError('响应流中断')
    provider=Failing()
    with pytest.raises(ProviderError) as caught:
        generate(provider,{'id':'run','model':'model'},'instructions',data,Event())
    assert provider.calls==2
    assert caught.value.diagnostic['stage']=='strict_analysis'
    assert caught.value.diagnostic['research_replay']['research']['response_id']=='research'


def test_new_pipeline_uses_two_requests_and_saves_catalog(db):
    class TwoStage(FakeProvider):
        def infer(self,run_id,model,instructions,data,cancelled,*,web_search=False,output_schema=None):
            self.calls+=1
            _,_,wire,ledger,_=prepare(db)
            if web_search:
                assert output_schema is None
                return {'text':'检索事实备忘录','web_sources':ledger,'response_id':'research','usage':{'input_tokens':10}}
            assert output_schema is not None and 'source_catalog' in data
            check_schema(wire,output_schema)
            return {'text':encoded(wire),'model':model,'response_id':'analysis','diagnostic':{'strict_schema_requested':True}}
    item=AnalysisService(db,TwoStage(),snapshotter=business.capture,prompt_factory=business.prompt,validator=business.validate,output_version=business.OUTPUT_VERSION)
    item.repository.set_model('test-model')
    saved=wait_terminal(item,item.create({'code':'601919','request_key':uuid4().hex})['id'])
    assert saved['status']=='succeeded',saved['error']
    assert item.provider.calls==2
    assert saved['result']['retrieval']['pipeline']['strict_schema']
    assert saved['result']['retrieval']['source_catalog'][0]['title']=='真实标题0'
    assert saved['usage']['stages'][0]['input_tokens']==10


def test_provider_sends_strict_format_without_search_tool(tmp_path):
    stream=StreamResponse([{'type':'response.completed','response':{'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'{}'}]}]}}])
    class Session:
        def post(self,*args,**kwargs):self.payload=kwargs['json'];return stream
    session=Session();provider=ChatGPTProvider(tmp_path,credentials=MemoryCredentials(),session=session)
    provider._access=lambda:'test-token';provider.record={};schema={'type':'object','properties':{},'required':[],'additionalProperties':False}
    output=provider.infer('run','test-model','instructions',{},Event(),output_schema=schema)
    assert session.payload['text']['format']=={'type':'json_schema','name':'business_judgment','strict':True,'schema':schema}
    assert 'tools' not in session.payload
    assert output['diagnostic']['strict_schema_requested']


@pytest.mark.parametrize('terminal',['completed','failed','delta_only'])
def test_eof_flush_requires_actual_success_terminal(tmp_path,terminal):
    class NoSeparator(StreamResponse):
        def iter_lines(self,**kwargs):
            event={'type':'response.'+terminal,'response':{'status':terminal,'output':[{'type':'message','content':[{'type':'output_text','text':'{}'}]}]}}
            if terminal=='delta_only':event={'type':'response.output_text.delta','delta':'{}'}
            yield b'data: '+json.dumps(event).encode()
    from quarterly_dashboard.ai_provider import ProviderError
    stream=NoSeparator([])
    class Session:
        def post(self,*args,**kwargs):return stream
    provider=ChatGPTProvider(tmp_path,credentials=MemoryCredentials(),session=Session());provider._access=lambda:'test';provider.record={}
    if terminal=='completed':assert provider.infer('run','model','prompt',{},Event())['text']=='{}'
    else:
        with pytest.raises(ProviderError):provider.infer('run','model','prompt',{},Event())
