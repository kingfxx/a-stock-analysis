"""Local, per-document route extraction from registered report evidence."""
import hashlib
import json

from stock_analysis.collection.collection import begin_batch, finish_batch
from stock_analysis.domains.research.reports.company_report_service import CompanyReportService
from stock_analysis.domains.research.sectors.shipping.shipping_report_metrics import HEADERS, extract_table, route_evidence_pages
from stock_analysis.infrastructure.storage import utc_now


def documents(db):
    with db.connection() as conn:
        return [dict(r) for r in conn.execute("""
            SELECT DISTINCT d.* FROM company_report_documents d
            JOIN instruments i ON i.id=d.instrument_id
            JOIN research_shipping_report_metrics m ON m.document_id=d.id
            WHERE i.code='601919' AND m.extractor_version='ccfi-business-v1'
            ORDER BY d.report_period,d.id
        """)]


def extract_document(db, document):
    service=CompanyReportService(db)
    pdf=service.resolve(document['relative_path'])
    if hashlib.sha256(pdf.read_bytes()).hexdigest()!=document['content_hash']:
        raise ValueError('正式PDF哈希不匹配')
    with db.connection() as conn:
        parsed=conn.execute("""SELECT p.* FROM company_report_parses p
            JOIN research_shipping_report_metrics m ON m.parse_id=p.id AND m.document_id=p.document_id
            WHERE p.document_id=? AND p.status='succeeded' AND m.extractor_version='ccfi-business-v1'
            ORDER BY p.id LIMIT 1""",(document['id'],)).fetchone()
        base={(r['metric'],r['scope']):r['value'] for r in conn.execute(
            "SELECT metric,scope,value FROM research_shipping_report_metrics WHERE document_id=? AND parse_id=? AND extractor_version='ccfi-business-v1' AND basis='cumulative'",(document['id'],parsed['id']))} if parsed else {}
    if not parsed: raise ValueError('缺少与原有业务指标对应的成功解析')
    source=service.resolve(parsed['relative_path']).read_text(encoding='utf8')
    pages=json.loads(source) if source.lstrip().startswith('[') else [json.loads(line) for line in source.splitlines() if line]
    tables={metric:extract_table(pages,header,document['report_period'].endswith('09-30')) for metric,header in HEADERS.items()}
    entries=[]
    for metric,table in tables.items():
        for scope in ('total','domestic'):
            if base.get((metric,scope)) != table[scope]: raise ValueError('已登记指标与财报解析不一致')
        evidence=route_evidence_pages(pages,HEADERS[metric],table)
        for scope,value in table['route_values'].items():
            entries.append((document['id'],parsed['id'],metric,scope,'cumulative',value,
                            'TEU' if metric=='teu' else '千元',{'rmb':'CNY','usd':'USD','teu':''}[metric],
                            evidence[scope],'ccfi-routes-v1',utc_now()))
    return entries


def publish_document(db, document):
    entries=extract_document(db,document)
    with db.connection(write=True) as conn:
        for entry in entries:
            old=conn.execute("SELECT value,unit,currency,evidence_page FROM research_shipping_report_metrics WHERE document_id=? AND parse_id=? AND metric=? AND scope=? AND basis=? AND extractor_version=?",(entry[0],entry[1],entry[2],entry[3],entry[4],entry[9])).fetchone()
            if old and tuple(old)!=(entry[5],entry[6],entry[7],entry[8]):
                raise ValueError('已有航线明细与本次提取冲突，须核查来源及提取版本')
        before=conn.total_changes
        conn.executemany('INSERT OR IGNORE INTO research_shipping_report_metrics(document_id,parse_id,metric,scope,basis,value,unit,currency,evidence_page,extractor_version,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',entries)
        return conn.total_changes-before


def run_document(db, operation_id, document):
    batch=begin_batch(db,operation_id,'shipping.report_routes','company_reports',document['report_period'],document['report_period'],
        scope={'document_id':document['id'],'pdf_sha256':document['content_hash']},domain='research_shipping',
        parser_version='ccfi-routes-v1',frequency='quarterly')
    try:
        count=publish_document(db,document)
        finish_batch(db,batch,'complete','complete',result={'published':count,'document_id':document['id']})
        return count
    except Exception as exc:
        finish_batch(db,batch,'failed','unknown',error=exc)
        raise
