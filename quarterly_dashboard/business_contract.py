"""Frozen source identities and strict wire schema for business judgments."""
import json
import re
from urllib.parse import unquote, urlsplit

from .analysis_snapshot import encoded, digest
from .analysis_validation import ReportValidationError
from .business_judgment import FRAMEWORKS, OUTPUT_VERSION, public_url, url_key

VERSION = 'business_contract_v2'


def source_catalog(data, sources):
    catalog, by_key = [], {}
    def add(url, title, published=None, origin='web_search'):
        key = url_key(url)
        if not public_url(url) or key is None:
            return
        if key in by_key:
            if title and by_key[key]['title_missing']:
                by_key[key].update(title=title[:300],title_missing=False)
            return
        missing = not bool(title)
        if missing:
            u = urlsplit(url)
            title = f'未返回原文标题 · {u.hostname} · {unquote(u.path.rsplit("/",1)[-1])}'[:300]
        item = {'id':f'web.{len(catalog)+1}','url':url,'title':title[:300],
                'published_on':published,'origin':origin,'title_missing':missing}
        catalog.append(item);by_key[key]=item
    for source in sources:
        add(source.get('url'),str(source.get('title') or ''))
    for e in data['evidence']:
        if e.get('metric')=='report_excerpt' and e.get('document_id') and re.fullmatch(r'[0-9a-f]{64}',e.get('file_hash','')):
            kind='年度报告' if e.get('report_type')=='annual' else '财报'
            add(e.get('source'),f"{data['instrument']['name']} · {e.get('observed_on','')} {kind}（本地原件）",e.get('published_on'),'local_report')
        elif e.get('id')=='industry.position' and e.get('value',{}).get('response_sha256'):
            add(e.get('source'),'Alphaliner TOP 100 · 本次直接采集',origin='collected_industry')
    if sum(s['origin']=='web_search' for s in catalog)<3:
        raise ReportValidationError('retrieval','实际检索资料不足3份，尚未调用分析模型；请补充检索。')
    return catalog


def obj(fields):
    return {'type':'object','properties':fields,'required':list(fields),'additionalProperties':False}


def arr(item,minimum=1,maximum=12):
    return {'type':'array','items':item,'minItems':minimum,'maxItems':maximum}


def string(maximum=600):
    return {'type':'string','minLength':1,'maxLength':maximum}


def report_schema(data, catalog):
    ids=sorted({e['id'] for e in data['evidence']}|{s['id'] for s in catalog})
    refs=arr({'$ref':'#/$defs/evidence_id'})
    status={'type':'string','enum':['ready','limited','missing']}
    def framework(categories=None):
        return obj({'category':{'type':'string','enum':list(categories)} if categories else string(80),
                    'judgment':string(200),'analysis':string(400),'impact':string(200),
                    'risk_verification':string(400),'status':status,'evidence_ids':refs})
    rank={'type':['integer','null'],'minimum':1}
    share={'type':['number','null'],'minimum':0,'maximum':100}
    market=obj({'market':string(80),'selection_reason':string(300),'scope':string(400),
                'period':string(100),'measure':string(300),'company_entity':string(150),
                'company_rank':rank,'company_share_pct':share,
                'competitors':arr(obj({'name':string(150),'rank':rank,'share_pct':share}),0,10),
                'cr3_pct':share,'cr5_pct':share,'status':status,'basis':string(600),
                'limitations':string(600),'evidence_ids':refs})
    pointer=obj({'framework':{'type':'string','enum':['pestel','five_forces','capabilities','industry_position']},
                 'row_index':{'type':'integer','minimum':0,'maximum':7}})
    schema=obj({'schema_version':{'type':'string','enum':[OUTPUT_VERSION]},
                'code':{'type':'string','enum':[data['instrument']['code']]},
                'as_of':{'type':'string','enum':[data['as_of']]},'summary':string(400),
                'frameworks':obj({'pestel':arr(framework(FRAMEWORKS['pestel']),6,6),
                                  'five_forces':arr(framework(FRAMEWORKS['five_forces']),5,5),
                                  'capabilities':arr(framework(),5,7)}),
                'industry_position':obj({'markets':arr(market,1,8)}),
                'rows':arr(obj({'question':string(100),'judgment':string(250),'reasoning':string(600),
                               'impact':string(250),'status':status,'evidence_ids':refs,'framework_refs':arr(pointer,1,8)}),5,7),
                'change_conditions':arr(obj({'condition':string(300),'impact':string(300),'evidence_ids':refs}),2,3),
                'unknowns':arr(string(400),0,8),
                'sources':arr(obj({'id':{'type':'string','enum':[s['id'] for s in catalog]},'excerpt':string(600)}),3,12)})
    schema['$defs']={'evidence_id':{'type':'string','enum':ids}}
    return schema


def check_schema(value, schema, path='output', root=None):
    """Check the small JSON Schema subset generated above, independently of API."""
    root=root or schema
    if '$ref' in schema:
        return check_schema(value,root['$defs'][schema['$ref'].split('/')[-1]],path,root)
    kinds=schema['type'] if isinstance(schema['type'],list) else [schema['type']]
    types={'object':isinstance(value,dict),'array':isinstance(value,list),'string':isinstance(value,str),
           'integer':type(value) is int,'number':type(value) in (int,float),'null':value is None}
    def require(ok,message='结构化输出不符合约定'):
        if not ok:raise ReportValidationError(path,message)
    require(any(types[k] for k in kinds))
    if 'enum' in schema:require(value in schema['enum'],'引用编号或枚举值不在本次允许范围')
    if isinstance(value,dict):
        require(set(value)==set(schema['required']),'结构化输出字段缺失或包含额外字段')
        for k,v in value.items():check_schema(v,schema['properties'][k],path+'.'+k,root)
    elif isinstance(value,list):
        require(schema['minItems']<=len(value)<=schema['maxItems'],'结构化输出列表长度不符')
        for i,v in enumerate(value):check_schema(v,schema['items'],f'{path}[{i}]',root)
    elif isinstance(value,str):
        require(schema.get('minLength',0)<=len(value)<=schema.get('maxLength',65536))
    elif type(value) in (int,float):
        import math
        require(math.isfinite(value) and schema.get('minimum',float('-inf'))<=value<=schema.get('maximum',float('inf')))


def bind_report(text, data, catalog):
    try:
        raw=json.loads(text)
    except (ValueError,TypeError):
        raise ReportValidationError('output','严格结构化输出不是完整JSON；未使用兼容修补，本次未保存') from None
    check_schema(raw,report_schema(data,catalog))
    by_id={s['id']:s for s in catalog}
    used=set()
    for s in raw['sources']:
        if s['id'] in used:raise ReportValidationError('sources','来源编号重复')
        used.add(s['id'])
        authoritative=by_id[s['id']]
        s.update({k:authoritative[k] for k in ('url','title','published_on')})
    for row in raw['rows']:
        refs=[]
        for p in row['framework_refs']:
            group=p['framework'];rows=raw['industry_position']['markets'] if group=='industry_position' else raw['frameworks'][group]
            if p['row_index']>=len(rows):raise ReportValidationError('framework_refs','引用的前置分析行不存在')
            target=rows[p['row_index']]
            refs.append(group+'.'+target['market' if group=='industry_position' else 'category'])
        row['framework_refs']=list(dict.fromkeys(refs))
    return raw


def numbered_notes(research, catalog):
    text=research['text'];lookup={url_key(s['url']):s['id'] for s in catalog}
    spans=[]
    for a in research.get('web_annotations',[]):
        start,end=a.get('start_index'),a.get('end_index');ref=lookup.get(url_key(a.get('url')))
        if ref and type(start) is int and type(end) is int and 0<=start<end<=len(text):spans.append((start,end,ref))
    boundary=len(text)
    for start,end,ref in sorted(set(spans),reverse=True):
        if end<=boundary:text=text[:start]+f'[{ref}]'+text[end:];boundary=start
    for s in catalog:text=text.replace(s['url'],f"[{s['id']}]")
    return text


def generate(provider, run, instructions, data, cancelled):
    from pathlib import Path
    retrieval=(Path(__file__).parent/'prompts'/'business_retrieval_v1.txt').read_text(encoding='utf-8')
    research=provider.infer(run['id'],run['model'],retrieval,data,cancelled,web_search=True)
    if cancelled.is_set():
        from .ai_provider import ProviderError
        raise ProviderError('分析已取消')
    catalog=source_catalog(data,research.get('web_sources',[]))
    analysis_input={**data,'source_catalog':catalog,'research_notes':numbered_notes(research,catalog)}
    try:
        output=provider.infer(run['id'],run['model'],instructions,analysis_input,cancelled,output_schema=report_schema(data,catalog))
    except Exception as exc:
        from .ai_provider import ProviderError, redact_diagnostic
        if isinstance(exc,ProviderError):
            exc.diagnostic=redact_diagnostic({**(exc.diagnostic or {}),'stage':'strict_analysis',
                'research_replay':{'research':research,'source_catalog':catalog}},getattr(provider,'record',None) or {})
        raise
    output.update(web_sources=research['web_sources'],source_catalog=catalog,contract_input=data)
    output['pipeline']={'version':VERSION,'strict_schema':True,'schema_sha256':digest(report_schema(data,catalog)),
                        'model_calls':2,'response_ids':[research.get('response_id'),output.get('response_id')],
                        'research_provider':research.get('diagnostic'),'analysis_provider':output.get('diagnostic')}
    output['usage']={'stages':[research.get('usage'),output.get('usage')]}
    return output
