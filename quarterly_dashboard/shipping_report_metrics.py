"""Validated COSCO business table extractor, promoted from the existing research."""
import re
import json
from .storage import utc_now
from .company_report_service import CompanyReportService


def prepare_shipping_report(db, period):
    service=CompanyReportService(db)
    prepared=service.prepare_period('601919',period)
    doc=prepared['document'];parse=prepared['parse']
    path=service.resolve(parse['relative_path'])
    pages=[json.loads(line) for line in path.read_text(encoding='utf8').splitlines() if line]
    tables={metric:extract_table(pages,header,period.endswith('09-30')) for metric,header in HEADERS.items()}
    with db.connection(write=True) as conn:
        for metric,table in tables.items():
            if not table['page_candidates']:raise ValueError('业务表缺证据页')
            for scope in ('total','domestic'):
                conn.execute('INSERT OR IGNORE INTO research_shipping_report_metrics(document_id,parse_id,metric,scope,basis,value,unit,currency,evidence_page,extractor_version,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (doc['id'],parse['id'],metric,scope,'cumulative',table[scope],'TEU' if metric=='teu' else '千元',
                     {'rmb':'CNY','usd':'USD','teu':''}[metric],table['page_candidates'][0],'ccfi-business-v1',utc_now()))
    return dict(document_id=doc['id'],parse_id=parse['id'],metrics=6,cached=prepared['cached'])

HEADERS={'teu':'本集团货运量（标准箱）','rmb':'本集团航线收入（人民币千元）','usd':'本集团航线收入（折算美元千元）'}
ROUTES=['跨太平洋','亚欧（包括地中海）','亚洲区内（包括澳洲）','其他国际（包括大西洋）','中国大陆','合计']
def extract_table(pages,header,q3=False):
    text='\n'.join(re.sub(r'(?m)^(?:中远海运控股股份有限公司\s*)?20\d{2}\s*年[^\n]*报告\s*$','',re.sub(r'\b\d+\s*/\s*\d+\b',' ',p['text'])) for p in pages)
    text=re.sub(r'\s+',' ',text).replace(' ','\u00a0')
    # Preserve separators between numeric columns; remove only whitespace within CJK labels.
    text=re.sub(r'(?<=[\u4e00-\u9fff（）])\u00a0(?=[\u4e00-\u9fff（）])','',text).replace('\u00a0',' ')
    if header not in text: raise ValueError('Missing header '+header)
    start=text.index(header)
    section=text[start:]
    end=section.index('合计')
    # Include the first total row, but no following subsidiary table.
    end += len('合计')
    tail=re.match(r'\s*[-\d,.\s]+',section[end:])
    section=section[:end+len(tail[0])]
    values={}
    for name in ROUTES:
        match=re.search(re.escape(name)+r'\s+([-\d,.\s]+)',section)
        if not match:raise ValueError('Missing route '+name+' in '+header)
        nums=[float(v.replace(',','')) for v in re.findall(r'-?\d[\d,]*(?:\.\d+)?',match[1])]
        if len(nums) not in [3,6]:raise ValueError(f'Unexpected table width {header} {name}: {nums}')
        values[name]=nums
    actual_q3=len(values['合计'])==6
    if q3!=actual_q3:raise ValueError('Unexpected quarter/cumulative layout')
    current=3 if q3 else 0
    previous=4 if q3 else 1
    for col in [current,previous]:
        if abs(sum(values[n][col] for n in ROUTES[:-1])-values['合计'][col])>3:
            raise ValueError('Route total reconciliation failed')
    page_ids=[p['pdf_page'] for p in pages if header in re.sub(r'\s+','',p['text'])]
    return dict(total=values['合计'][current],domestic=values['中国大陆'][current],prior_total=values['合计'][previous],prior_domestic=values['中国大陆'][previous],direct_q3_total=values['合计'][0] if q3 else None,direct_q3_domestic=values['中国大陆'][0] if q3 else None,page_candidates=page_ids,raw=section)
