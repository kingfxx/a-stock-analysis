"""Lightweight business judgments; web provenance is required, never invented."""
import json
import re
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit

from .analysis_snapshot import digest, encoded, SnapshotError
from .storage import utc_now
from .sources import normalize_code
from .analysis_validation import ReportValidationError

OUTPUT_VERSION = 'business_judgment_v1'
PROFILE = {'style': 'business_judgment', 'frameworks': ['PESTEL', '五力', '资源与能力']}
FRAMEWORKS = {
    'pestel': ('政策', '经济', '社会需求', '技术', '环境', '法律'),
    'five_forces': ('供应商的议价能力', '购买者的议价能力', '潜在进入者的威胁', '替代品的威胁', '行业竞争强度'),
}


def public_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return False
    try:
        u = urlsplit(value)
        import ipaddress
        if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password:
            return False
        host = u.hostname.lower()
        if host == 'localhost' or '.' not in host or host.endswith(('.local', '.localhost')):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return not any(c.isspace() for c in value)
    except ValueError:
        return False


def url_key(value):
    """Only equivalent URL syntax; retain scheme, path and query semantics."""
    if not public_url(value):
        return None
    u = urlsplit(value)
    try:
        port = u.port
    except ValueError:
        return None
    host = u.hostname.lower()
    if ':' in host:
        host = '[' + host + ']'
    authority = host + (':' + str(port) if port and (u.scheme, port) not in (('http', 80), ('https', 443)) else '')
    path = u.path or '/'
    # Unicode filenames and their UTF-8 escapes identify the same path. Keep
    # escaped ASCII reserved characters escaped (%2F must not become '/').
    if re.search(r'%(?![0-9a-fA-F]{2})',path):
        return None
    def unicode_escape(match):
        decoded = bytes.fromhex(match[0].replace('%','')).decode('utf-8',errors='strict')
        return ''.join(c if ord(c)>127 else f'%{ord(c):02X}' for c in decoded)
    try:
        path = re.sub(r'(?:%[0-9a-fA-F]{2})+',unicode_escape,path)
    except UnicodeDecodeError:
        return None
    return (u.scheme.lower(), authority, path, u.query)


def pdf_encoding_boundary_key(value):
    """One incomplete escape at encoded/Unicode filename boundary, no guessing."""
    if not public_url(value):
        return None
    u = urlsplit(value)
    if not u.path.lower().endswith('.pdf'):
        return None
    path, count = re.subn(r'(?<=%[0-9a-fA-F]{2})%[0-9a-fA-F](?=[^\x00-\x7f])','',u.path)
    if count != 1:
        return None
    return url_key(u._replace(path=path).geturl())


def annual_bargaining(db, identity, today):
    """Read full annual-report page cache independently of Checklist budgets."""
    from .company_report_service import CompanyReportService, PARSER_VERSION
    with db.connection() as conn:
        doc = conn.execute("SELECT d.*,p.relative_path AS text_path FROM company_report_documents d JOIN company_report_parses p ON p.document_id=d.id WHERE d.instrument_id=? AND d.report_type='annual' AND d.report_period<=? AND (d.published_on IS NULL OR d.published_on<=?) AND p.status='succeeded' AND p.parser_version=? ORDER BY d.report_period DESC,d.id DESC LIMIT 1",(identity,today,today,PARSER_VERSION)).fetchone()
    if not doc:
        return [], {'state':'local_annual_missing','note':'本地尚无可用完整年报解析；须联网读取最近完整年报，不能以中报或摘要代替。'}
    doc = dict(doc)
    check = {'document_id':doc['id'],'period':doc['report_period'],'source':doc['source_url'],'state':'section_missing'}
    service = CompanyReportService(db)
    try:
        import hashlib
        if hashlib.sha256(service.resolve(doc['relative_path']).read_bytes()).hexdigest() != doc['content_hash']:
            raise ValueError('财报原件哈希不符')
        pages = [json.loads(line) for line in service.resolve(doc['text_path']).read_text(encoding='utf-8').splitlines() if line]
    except (OSError,ValueError,TypeError):
        return [], {**check,'state':'cache_unavailable','note':'完整年报原件或逐页缓存不可用，不能宣称已读取该章节。'}
    if '年度报告摘要' in re.sub(r'\s+','', ''.join(p.get('text','') for p in pages[:12])):
        return [], {**check,'state':'summary_rejected','note':'该文件为年报摘要，不作为完整年报集中度依据。'}
    pattern = re.compile(r'主要销售客户及主要供应商|公司主要销售客户|前[五5][名大]?(?:客户销售额|供应商采购额)')
    selected = {}
    for index,page in enumerate(pages):
        text = page.get('text','')
        if not pattern.search(re.sub(r'\s+','',text)) or '目录' in text:
            continue
        selected[page['pdf_page']] = page
        if index+1 < len(pages) and re.search(r'供应商|前[五5]|关联方',pages[index+1].get('text','')):
            neighbor = pages[index+1];selected[neighbor['pdf_page']] = neighbor
        if len(selected) >= 4:
            break
    evidence, remaining = [], 12000
    for number,page in sorted(selected.items()):
        text = page['text']
        if len(text) > 5000:
            matches = list(re.finditer(r'主要销售客户|主要供应商|前[五5]',text))
            start = max(0,matches[0].start()-300) if matches else 0
            text = text[start:start+5000]
        item = {'id':f"company.page.{doc['id']}.{number}",'metric':'report_excerpt','value':[{'pdf_page':number,'text':text}],
                'source':doc['source_url'],'observed_on':doc['report_period'],'published_on':doc['published_on'],
                'document_id':doc['id'],'file_hash':doc['content_hash'],'relative_path':doc['relative_path'],
                'parser_version':PARSER_VERSION,'extraction_version':'business_annual_bargaining_v1',
                'report_type':'annual','topics':['bargaining'],'label':f"完整年报 {doc['report_period']} · PDF 第 {number} 页"}
        size = len(encoded(item))
        if size <= remaining:
            evidence.append(item);remaining -= size
    return evidence, {**check,'state':'included' if evidence else 'section_missing','evidence_ids':[e['id'] for e in evidence],
                      'note':'来自最近本地已解析完整年报；全年销售/采购集中度不可与半年或应收账款集中度混用。'}


def capture(db, code, *, as_of=None):
    code = normalize_code(code)
    today = as_of or datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    with db.connection() as conn:
        instrument = conn.execute('SELECT id,name FROM instruments WHERE code=?', (code,)).fetchone()
        if not instrument:
            raise SnapshotError('请先添加股票')
        updating = bool(conn.execute("SELECT 1 FROM sync_runs WHERE instrument_id=? AND status='running'", (instrument['id'],)).fetchone())
        # Reuse evidence, never reuse an earlier model conclusion as a fact.
        row = conn.execute("SELECT s.input_json,s.snapshot_hash FROM ai_analysis_runs r JOIN ai_analysis_snapshots s ON s.id=r.snapshot_id WHERE r.instrument_id=? AND r.status='succeeded' AND r.output_schema_version='stock_checklist_output_v1' ORDER BY r.created_at DESC,r.id DESC LIMIT 1", (instrument['id'],)).fetchone()
    annual, annual_check = annual_bargaining(db,instrument['id'],today)
    evidence, budget = list(annual), 16000-sum(len(encoded(e)) for e in annual)
    existing = {e['id'] for e in evidence}
    if row:
        for item in json.loads(row['input_json']).get('evidence', []):
            if item.get('metric') not in ('company', 'financial_period', 'yoy', 'report_excerpt'):
                continue
            if item['id'] in existing:
                continue
            size = len(encoded(item))
            if size <= budget and len(evidence) < 24:
                evidence.append(item)
                existing.add(item['id'])
                budget -= size
    limitations = ['客户和供应商章节优先独立读取本地完整年报，其余证据复用最近保存的 Checklist 输入，可能落后于当前财报。',
                    '来源链接由检索工具提供，摘要为模型提取，未逐字独立核验；不构成估值或买卖结论。']
    data = {'schema_version': 'business_judgment_input_v1', 'calculation_version': 'business_judgment_capture_v2',
            'as_of': today, 'instrument': {'code': code, 'name': instrument['name']}, 'analysis_profile': PROFILE,
            'evidence': evidence, 'limitations': limitations,'annual_report_check':annual_check}
    quality = {'updating': updating, 'limited': not bool(evidence), 'missing': [] if evidence else ['本地经营证据'], 'dates': {}}
    return {'instrument_id': instrument['id'], 'input': data, 'quality': quality, 'hash': digest(data),
            'manifest': {'checklist_snapshot_hash': row['snapshot_hash'] if row else None}, 'captured_at': utc_now()}


def prompt():
    instructions = (Path(__file__).parent / 'prompts' / 'business_judgment_v14.txt').read_text(encoding='utf-8')
    instructions += '\n' + (Path(__file__).parent / 'prompts' / 'business_judgment_v15.txt').read_text(encoding='utf-8')
    return {'version': 'business_judgment_prompt_v15', 'output_version': OUTPUT_VERSION, 'instructions': instructions}


def validate(text, input_data):
    if not isinstance(text, str) or len(text) > 65536:
        raise ReportValidationError('output', '经营判断输出为空或超过大小限制')
    raw = text
    output_normalizations = []
    text = text.strip().lstrip('\ufeff').strip()
    # Only unwrap one complete fence; never repair syntax or extract an
    # arbitrary object from prose, which could silently discard conflicts.
    fence = re.fullmatch(r'```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```', text, re.IGNORECASE)
    if fence:
        text = fence.group(1)
        output_normalizations.append('unwrapped_complete_json_code_fence')
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        # A complete object plus exactly one redundant closing brace has no
        # additional value. Preserve the whole object; all content and source
        # checks below still apply. Never repair an internal syntax error.
        trailer = text[exc.pos:].strip()
        # Some responses append a final=true completion marker outside an
        # already complete report. Only that exact marker is non-report data;
        # additional fields, false/null values and malformed reports still fail.
        completion_marker = bool(re.fullmatch(r',\s*"final"\s*:\s*true\s*}', trailer))
        if exc.msg == 'Extra data' and (trailer == '}' or completion_marker):
            result = json.loads(text[:exc.pos])
            if not isinstance(result, dict):
                raise ReportValidationError('output', '经营判断必须是单个完整JSON对象') from None
            output_normalizations.append('removed_trailing_final_true_marker' if completion_marker else 'removed_single_extra_closing_brace')
        else:
            raise ReportValidationError('output', f'经营判断输出不是有效 JSON（第{exc.lineno}行，第{exc.colno}列），本次未保存',
                format_error={'line':exc.lineno,'column':exc.colno,'reason':exc.msg,
                              'length':len(raw),'sha256':digest(raw),'complete_code_fence':bool(fence),
                              'error_codepoints':[ord(c) for c in text[exc.pos:exc.pos+8]]}) from None
    context = 'output'
    def require(ok, message='经营判断格式或证据无效'):
        if not ok:
            raise ReportValidationError(context, message)
    def string(value, maximum):
        return isinstance(value, str) and 0 < len(value.strip()) <= maximum
    require(isinstance(result, dict))
    fields = {'schema_version', 'code', 'as_of', 'summary', 'frameworks', 'rows', 'change_conditions', 'unknowns', 'sources'}
    require(set(result) in (fields, fields | {'industry_position'}))
    require(not input_data.get('require_industry_position') or 'industry_position' in result, '新版经营判断必须提供行业地位表')
    require(result['schema_version'] == OUTPUT_VERSION and result['code'] == input_data['instrument']['code'] and result['as_of'] == input_data['as_of'])
    require(string(result['summary'], 400))
    require(isinstance(result['sources'], list) and 3 <= len(result['sources']) <= 12)
    actual_urls = {url_key(s.get('url')): s['url'] for s in input_data.get('web_sources', []) if url_key(s.get('url'))}
    local_reports = {}
    for item in input_data['evidence']:
        # Only immutable supplied PDF excerpts with document identity; not arbitrary links.
        key = url_key(item.get('source'))
        file_hash = item.get('file_hash')
        if (item.get('metric') == 'report_excerpt' and key and item.get('document_id')
                and isinstance(file_hash, str) and len(file_hash) == 64
                and all(c in '0123456789abcdef' for c in file_hash)):
            local_reports.setdefault(key, []).append(item)
    collected_urls = {url_key(e.get('source')): e['source'] for e in input_data['evidence'] if e.get('metric') == 'industry_position' and e.get('id') == 'industry.position' and e.get('value', {}).get('response_sha256')}
    normalized_urls = []
    network_count = 0
    ids = {e['id'] for e in input_data['evidence']}
    urls = set()
    unique_sources, source_by_url, excerpts_by_url, aliases = [], {}, {}, {}
    source_ids = set()
    for i, source in enumerate(result['sources'], 1):
        context = f'sources[{i-1}]'
        require(isinstance(source, dict) and set(source) == {'id', 'title', 'url', 'published_on', 'excerpt'})
        require(isinstance(source['id'],str) and re.fullmatch(r'web\.[1-9]\d{0,2}',source['id']) and source['id'] not in source_ids, '来源编号必须是唯一web编号；可不连续')
        source_ids.add(source['id'])
        require(string(source['title'], 300) and string(source['excerpt'], 600), '来源标题或摘要无效')
        key = url_key(source['url'])
        encoding_repair = False
        if key is None:
            candidate = pdf_encoding_boundary_key(source['url'])
            # Accept only exact full-path identity from this run's tool ledger.
            # Never alter dates, opaque document identifiers or filename text.
            if candidate in actual_urls:
                key = candidate
                encoding_repair = True
        require(key is not None, f"来源 {source['id']} 的网址格式或访问范围无效")
        if key not in actual_urls and key not in local_reports and key not in collected_urls:
            raise ReportValidationError('sources.' + source['id'], '来源既未出现在本次实际检索中，也未匹配输入的本地财报，拒绝保存',
                unmatched_url=source['url'], retrieved_sources=input_data.get('web_sources', []))
        if key in actual_urls:
            network_count += int(key not in urls)
            canonical_url = actual_urls[key]
            source['origin'] = 'web_search'
        elif key in collected_urls:
            canonical_url = collected_urls[key]
            source['origin'] = 'collected_industry'
            source['local_evidence_ids'] = ['industry.position']
        else:
            supplied = local_reports[key]
            canonical_url = supplied[0]['source']
            source['origin'] = 'local_report'
            source['local_evidence_ids'] = [item['id'] for item in supplied]
            dates = {item.get('published_on') for item in supplied if item.get('published_on')}
            if dates:
                require(source['published_on'] is None or source['published_on'] in dates, '本地财报发布日期与输入不符')
                source['published_on'] = supplied[0].get('published_on') or source['published_on']
        if source['url'] != canonical_url:
            normalized_urls.append({'model_url': source['url'], 'retrieved_url': canonical_url, 'origin': source['origin'],
                **({'reason':'pdf_utf8_boundary_partial_escape'} if encoding_repair else {})})
            source['url'] = canonical_url
        published = source['published_on']
        if published is not None:
            from datetime import date
            try:
                require(date.fromisoformat(published).isoformat() == published and published <= result['as_of'])
            except (ValueError, TypeError):
                raise ValueError('来源发布日期无效') from None
        if key in source_by_url:
            existing = source_by_url[key]
            require(not existing['published_on'] or not published or existing['published_on'] == published, '同一资料的发布日期冲突，请核实来源')
            if not existing['published_on']:
                existing['published_on'] = published
            # Multiple sections from one annual report are one document, but
            # retain every supplied excerpt rather than discarding evidence.
            if source['excerpt'] not in excerpts_by_url[key]:
                existing['excerpt'] += '\n\n' + source['excerpt']
                excerpts_by_url[key].add(source['excerpt'])
            aliases[source['id']] = existing['id']
        else:
            source_by_url[key] = source
            excerpts_by_url[key] = {source['excerpt']}
            unique_sources.append(source)
            ids.add(source['id'])
        urls.add(key)
    result['sources'] = unique_sources
    require(network_count >= 3, '至少需要3份去重后的本次实际检索网络资料；同一资料的不同章节只计1份，本地财报不计入联网来源数量')
    def refs(value):
        if not isinstance(value, list) or not 1 <= len(value) <= 12 or not all(isinstance(v, str) for v in value):
            return False
        value[:] = list(dict.fromkeys(aliases.get(v, v) for v in value))
        missing = [v for v in value if v not in ids]
        if missing:
            raise ReportValidationError(context+'.evidence_ids', '引用的来源编号不存在：'+', '.join(missing),
                unknown_evidence_ids=missing, available_evidence_ids=sorted(ids))
        return True
    frameworks = result['frameworks']
    require(isinstance(frameworks, dict) and set(frameworks) == {'pestel', 'five_forces', 'capabilities'}, '必须提供三张前置分析表')
    framework_ids, framework_rows = set(), []
    framework_aliases = {}
    force_aliases = {'供应商议价能力':'供应商的议价能力','购买者议价能力':'购买者的议价能力',
                     '现有竞争强度':'行业竞争强度','现有竞争者的竞争强度':'行业竞争强度'}
    for key, rows in frameworks.items():
        require(isinstance(rows, list) and (len(rows) == len(FRAMEWORKS[key]) if key in FRAMEWORKS else 5 <= len(rows) <= 7), '前置分析表项目不完整')
        categories = []
        for index,row in enumerate(rows):
            context = f'frameworks.{key}[{index}]'
            require(isinstance(row, dict) and set(row) == {'category', 'judgment', 'analysis', 'impact', 'risk_verification', 'status', 'evidence_ids'})
            if key == 'five_forces' and isinstance(row['category'],str) and row['category'] in force_aliases:
                original = row['category']
                row['category'] = force_aliases[original]
                framework_aliases[key+'.'+original] = key+'.'+row['category']
            require(all(string(row[k], n) for k, n in [('category', 80), ('judgment', 200), ('analysis', 400), ('impact', 200), ('risk_verification', 400)]))
            require(row['status'] in ('ready', 'limited', 'missing') and refs(row['evidence_ids']))
            categories.append(row['category']); framework_ids.add(key + '.' + row['category']); framework_rows.append(row)
        require(len(set(categories)) == len(categories))
        if key in FRAMEWORKS:
            require(set(categories) == set(FRAMEWORKS[key]), 'PESTEL或波特五力分类不完整')
            rows.sort(key=lambda row: FRAMEWORKS[key].index(row['category']))
    position_rows = []
    concentration_checks = []
    market_conflicts = []
    if 'industry_position' in result:
        context = 'industry_position'
        position = result['industry_position']
        require(isinstance(position, dict) and set(position) == {'markets'} and isinstance(position['markets'], list) and 1 <= len(position['markets']) <= 8, '行业地位必须按1至8个核心市场列示')
        names = set()
        def percentage(value):
            return value is None or (type(value) in (int, float) and 0 <= value <= 100)
        def rank(value):
            return value is None or (type(value) is int and value > 0)
        for index,row in enumerate(position['markets']):
            context = f'industry_position.markets[{index}]'
            require(isinstance(row, dict) and set(row) == {'market','selection_reason','scope','period','measure','company_entity','company_rank','company_share_pct','competitors','cr3_pct','cr5_pct','status','basis','limitations','evidence_ids'}, '行业地位字段无效')
            for field,missing in [('scope','市场边界未披露'),('period','统计时期未披露'),('measure','份额指标及分母口径未披露')]:
                if row[field] is None:
                    row[field] = missing
                    if row['status'] == 'ready':
                        row['status'] = 'limited'
            require(all(string(row[key], length) for key,length in [('market',80),('selection_reason',300),('scope',400),('period',100),('measure',300),('company_entity',150),('basis',600),('limitations',600)]), '行业地位必须说明市场边界、时期、指标及缺口')
            require(row['market'] not in names, '行业地位市场重复'); names.add(row['market'])
            require(rank(row['company_rank']) and percentage(row['company_share_pct']), '行业排名或份额无效')
            require(row['status'] in ('ready','limited','missing') and refs(row['evidence_ids']), '行业地位必须关联真实依据')
            require(row['company_rank'] is not None or row['company_share_pct'] is not None or row['status'] != 'ready', '行业排名和份额均未知时必须标记缺口')
            require(isinstance(row['competitors'], list) and len(row['competitors']) <= 10, '同行列表无效')
            peers = set()
            shares = {}
            seen_ranks = set()
            rank_conflict = False
            if row['company_rank'] is not None:
                seen_ranks.add(row['company_rank'])
            if row['company_rank'] is not None and row['company_share_pct'] is not None:
                shares[row['company_rank']] = row['company_share_pct']
            for peer in row['competitors']:
                require(isinstance(peer, dict) and set(peer) == {'name','rank','share_pct'} and string(peer['name'],150) and rank(peer['rank']) and percentage(peer['share_pct']), '同行排名或份额无效')
                require(peer['name'] not in peers, '同行重复'); peers.add(peer['name'])
                if peer['rank'] is not None:
                    rank_conflict |= peer['rank'] in seen_ranks
                    seen_ranks.add(peer['rank'])
                if peer['rank'] is not None and peer['share_pct'] is not None:
                    shares[peer['rank']] = peer['share_pct']
            if rank_conflict:
                from copy import deepcopy
                market_conflicts.append({'market':row['market'],'reason':'排名不唯一，可能混用统计口径或主体，未采用该行定量排行',
                    'company_entity':row['company_entity'],'company_rank':row['company_rank'],
                    'company_share_pct':row['company_share_pct'],'competitors':deepcopy(row['competitors']),
                    'measure':row['measure'],'period':row['period'],'evidence_ids':list(row['evidence_ids']),
                    'basis':row['basis'],'limitations':row['limitations']})
                row['company_rank']=row['company_share_pct']=None
                for peer in row['competitors']:peer['rank']=peer['share_pct']=None
                row['cr3_pct']=row['cr5_pct']=None
                row['status']='limited'
                shares={}
                note='排名不唯一，可能混用统计口径或主体：本行定量排行与集中度未采用；原输出另列供核对。'
                row['limitations']=note+' '+row['limitations'][:600-len(note)-1]
            for field,count in [('cr3_pct',3),('cr5_pct',5)]:
                require(percentage(row[field]), '行业集中度无效')
                if row[field] is not None:
                    complete = all(i in shares for i in range(1,count+1))
                    if not complete or abs(row[field]-sum(shares.get(i,0) for i in range(1,count+1))) > 0.1:
                        reason = '缺少同口径完整前列份额' if not complete else '集中度与列示前列份额加总不符'
                        concentration_checks.append({'market':row['market'],'metric':f'CR{count}','reason':reason})
                        row[field] = None
                        row['limitations'] += f'；CR{count}未采用：{reason}，涉及该数字的文字判断待核实。'
                        if row['status'] == 'ready':
                            row['status'] = 'limited'
            framework_ids.add('industry_position.' + row['market']); position_rows.append(row)
    if input_data.get('require_industry_position'):
        require(frameworks['capabilities'][0]['category'] == '护城河', '公司能力首行必须为护城河')
    require(isinstance(result['rows'], list) and 5 <= len(result['rows']) <= 7)
    questions = set()
    reference_moves = []
    for index,row in enumerate(result['rows']):
        context = f'rows[{index}]'
        require(isinstance(row, dict) and set(row) == {'question', 'judgment', 'reasoning', 'impact', 'status', 'evidence_ids', 'framework_refs'})
        require(all(string(row[k], n) for k, n in [('question', 100), ('judgment', 250), ('reasoning', 600), ('impact', 250)]))
        if isinstance(row['evidence_ids'],list) and isinstance(row['framework_refs'],list) and all(isinstance(v,str) for v in row['framework_refs']):
            misplaced = [v for v in row['evidence_ids'] if isinstance(v,str) and v in framework_ids]
            if misplaced:
                row['evidence_ids'] = [v for v in row['evidence_ids'] if v not in misplaced]
                row['framework_refs'] = list(dict.fromkeys(row['framework_refs'] + misplaced))
                reference_moves.append({'row':index,'moved_to_framework_refs':misplaced})
        require(row['status'] in ('ready', 'limited', 'missing') and refs(row['evidence_ids']))
        if isinstance(row['framework_refs'], list) and all(isinstance(v,str) for v in row['framework_refs']):
            row['framework_refs'] = [framework_aliases.get(v,v) for v in row['framework_refs']]
        require(isinstance(row['framework_refs'], list) and 1 <= len(row['framework_refs']) <= 8 and all(isinstance(v, str) and v in framework_ids for v in row['framework_refs']) and any(not v.startswith('industry_position.') for v in row['framework_refs']), '结论必须关联有效的前置分析项')
        require(row['question'] not in questions); questions.add(row['question'])
    require(isinstance(result['change_conditions'], list) and 2 <= len(result['change_conditions']) <= 3)
    for index,item in enumerate(result['change_conditions']):
        context = f'change_conditions[{index}]'
        require(isinstance(item, dict) and set(item) == {'condition', 'impact', 'evidence_ids'})
        require(string(item['condition'], 300) and string(item['impact'], 300) and refs(item['evidence_ids']))
    require(isinstance(result['unknowns'], list) and len(result['unknowns']) <= 8 and all(string(x, 400) for x in result['unknowns']))
    if concentration_checks and not result['unknowns']:
        result['unknowns'].append('行业集中度未核验：相关CR数字不采用；引用这些数字的分析与结论需要补充同行份额后验证。')
    if market_conflicts:
        note='行业地位存在排名或统计口径冲突：相应定量排行未采用，相关文字判断须按单一指标、时期及主体复核。'
        if note not in result['unknowns'] and len(result['unknowns']) < 8:result['unknowns'].append(note)
    require(all(row['status'] == 'ready' for row in result['rows'] + framework_rows + position_rows) or bool(result['unknowns']), '资料不足必须说明关键缺口')
    result['retrieval'] = {'sources': input_data.get('web_sources', []), 'captured_at': utc_now(), 'excerpt_basis': 'model_extracted', 'url_normalizations': normalized_urls,
                           'source_aliases': aliases, 'output_normalizations': output_normalizations, 'concentration_checks': concentration_checks,
                           'framework_aliases': framework_aliases, 'reference_moves':reference_moves,'market_conflicts':market_conflicts}
    return result
