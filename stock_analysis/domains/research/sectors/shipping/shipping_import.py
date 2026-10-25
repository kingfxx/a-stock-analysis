"""Explicit, idempotent migration of the existing CCFI research evidence."""
import argparse
import hashlib
import json
from pathlib import Path
from stock_analysis.collection.collection import begin_batch, archive_source, parsed_source, finish_batch, encoded
from stock_analysis.domains.research.reports.company_report_service import CompanyReportService
from stock_analysis.domains.research.sectors.shipping.shipping_forecast import publish_rates
from stock_analysis.infrastructure.storage import Database, utc_now


def import_research(db, root, workbook, *, operation_id=None, cancelled=lambda:False):
    root=Path(root);workbook=Path(workbook)
    base=root/'data/verification/reports/ccfi-profit-lab'
    snapshot=json.loads((base/'snapshot.json').read_text(encoding='utf8'))
    study=json.loads((base/'weekly.json').read_text(encoding='utf8'))
    calibration=json.loads((base/'calibration.json').read_text(encoding='utf8'))
    raw=workbook.read_bytes();sha=hashlib.sha256(raw).hexdigest()
    if sha!=snapshot['workbook']['hash'] or sha!=study['workbook']['hash']:
        raise ValueError('Excel与既有研究哈希不符，请先核对输入')
    # Hash-verified reruns avoid creating duplicate sources and batches.
    with db.connection() as conn:
        existing=conn.execute("SELECT id FROM collection_batches WHERE dataset='shipping.excel' AND idempotency_key=? AND status='complete'",(sha,)).fetchone()
    if not existing:
        batch=begin_batch(db,operation_id or 'shipping-import-'+sha[:16],'shipping.excel','user_excel',domain='research_shipping',
                          scope=dict(workbook=workbook.name,sha256=sha,date_status=study['date_status']),
                          parser_version='ccfi-research-import-v1',idempotency_key=sha)
        source=archive_source(db,batch,'user_excel','shipping.excel',dict(transport='local_import',name=workbook.name),1,raw=raw)
        # The derived calendar evidence is separately archived, never presented as an official historical feed.
        calendar_source=archive_source(db,batch,'user_excel','shipping.calendar',dict(transport='research_derivation',parent_source=source),1,
                       raw=encoded(dict(calendars=study['calendars'],observations=study['observations'])).encode())
        parsed_source(db,calendar_source,fields=['calendar','original_notes','inferred_dates'])
        points=[]
        from calendar import monthrange
        for row in snapshot['monthly']:
            if row['value'] is None:continue
            month=row['month'];y,m=map(int,month.split('-'))
            points.append(dict(index_name='CCFI',frequency='monthly',route='综合',statistic='monthly_mean',
                period_start=month+'-01',period_end=f'{month}-{monthrange(y,m)[1]}',value=row['value'],unit='点',
                published_on=None,quality='transcribed_unverified',channel='user_excel'))
        for row in study['observations']:
            if row['value'] is None or not row.get('inferred_publication_date'):continue
            day=row['inferred_publication_date']
            points.append(dict(index_name='CCFI',frequency='weekly',route='综合',statistic='release',
                period_start=day,period_end=day,value=row['value'],unit='点',published_on=None,
                quality='calendar_inferred_flagged' if row['uncertainty'] else 'calendar_inferred',channel='user_excel'))
        count=publish_rates(db,points,batch,source)
        parsed_source(db,source,fields=['monthly','weekly','calendar_inferred'])
        finish_batch(db,batch,'complete','partial',result=dict(published=count,note='转录及日期推定，不宣称官方覆盖'))
    manifest=json.loads((root/'data/verification/samples/ccfi-profit-lab/calibration_manifest.json').read_text(encoding='utf8'))
    service=CompanyReportService(db)
    imported=[]
    for m in manifest:
        if cancelled():
            from stock_analysis.collection.collection import Cancelled
            raise Cancelled('用户停止，已导入报告保留')
        path=root/m['path']
        if hashlib.sha256(path.read_bytes()).hexdigest()!=m['sha256']:
            raise ValueError('研究报告原件哈希不符：'+m['period'])
        doc=service.import_report('601919',m['period'],path,url=m['url'],published_on=m['adjunct_url'].split('/')[1])
        # Reuse the existing page extraction, registered under its actual provenance.
        pages=json.loads((root/m['pages_path']).read_text(encoding='utf8'))
        target=service.resolve(doc['relative_path']).with_name('research-pages-v1.json')
        target.write_text(encoded(pages),encoding='utf8')
        with db.connection(write=True) as conn:
            conn.execute("INSERT INTO company_report_parses(document_id,parser_version,status,page_count,relative_path,quality_json,created_at) VALUES(?,?,'succeeded',?,?,?,?) ON CONFLICT(document_id,parser_version) DO NOTHING",
                (doc['id'],'ccfi_research_pages_v1',len(pages),service._relative(target),encoded(dict(imported_from=m['pages_path'],pdf_hash=m['sha256'])),utc_now()))
            parse_id=conn.execute('SELECT id FROM company_report_parses WHERE document_id=? AND parser_version=?',(doc['id'],'ccfi_research_pages_v1')).fetchone()[0]
            extraction=next(r for r in calibration['extraction'] if r['period']==m['period'])
            for metric, table in extraction['tables'].items():
                for scope in ('total','domestic'):
                    conn.execute('INSERT OR IGNORE INTO research_shipping_report_metrics(document_id,parse_id,metric,scope,basis,value,unit,currency,evidence_page,extractor_version,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (doc['id'],parse_id,metric,scope,'cumulative',table[scope],'TEU' if metric=='teu' else '千元',
                         {'rmb':'CNY','usd':'USD','teu':''}[metric],table['page_candidates'][0],'ccfi-business-v1',utc_now()))
        imported.append(dict(period=m['period'],document_id=doc['id'],parse_id=parse_id,sha256=m['sha256']))
    return dict(reports=imported,workbook_hash=sha)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--database',required=True);p.add_argument('--workbook',required=True);p.add_argument('--root',default='.')
    args=p.parse_args();db=Database(args.database);db.initialize()
    print(encoded(import_research(db,args.root,args.workbook)))
