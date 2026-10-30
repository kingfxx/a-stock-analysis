import json

import pytest

from stock_analysis.infrastructure.storage import Database, utc_now
from stock_analysis.domains.research.reports.company_report_service import CompanyReportService
from stock_analysis.domains.research.sectors.shipping.shipping_report_metrics import HEADERS, ROUTES
from stock_analysis.domains.research.sectors.shipping.shipping_route_backfill import documents, extract_document, publish_document
from stock_analysis.domains.research.sectors.shipping.shipping_forecast import business_quarters
from stock_analysis.domains.research.sectors.shipping import shipping_collection


def setup(db, period='2026-03-31'):
    service=CompanyReportService(db)
    pdf=db.path.parent/'fixture.pdf';pdf.write_bytes(b'%PDF-test')
    doc=service.import_report('601919',period,pdf)
    pages=[]; factor=2 if period.endswith('06-30') else 1
    for number,(metric,header) in enumerate(HEADERS.items(),1):
        rows=[f'{name} {value*factor} {value*factor} 0' for name,value in zip(ROUTES,[1,2,3,4,5,15])]
        pages.append({'pdf_page':number,'text':header+'\n'+'\n'.join(rows)})
    path=service.resolve(doc['relative_path']).parent/'fixture-pages.json'
    path.write_text(json.dumps(pages,ensure_ascii=False),encoding='utf8')
    with db.connection(write=True) as conn:
        parse=conn.execute("INSERT INTO company_report_parses(document_id,parser_version,status,page_count,relative_path,quality_json,created_at) VALUES(?,?,'succeeded',3,?,'{}',?)",(doc['id'],'fixture',service._relative(path),utc_now())).lastrowid
        for metric in HEADERS:
            for scope,value in [('total',15*factor),('domestic',5*factor)]:
                conn.execute("INSERT INTO research_shipping_report_metrics(document_id,parse_id,metric,scope,basis,value,unit,currency,evidence_page,extractor_version,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(doc['id'],parse,metric,scope,'cumulative',value,'TEU' if metric=='teu' else '千元',{'teu':'','rmb':'CNY','usd':'USD'}[metric],1,'ccfi-business-v1',utc_now()))
    return doc,path


def test_local_backfill_idempotency_view_and_corrupt_evidence(tmp_path):
    db=Database(tmp_path/'test.sqlite3');db.initialize();doc,path=setup(db)
    assert len(extract_document(db,doc))==12
    assert publish_document(db,doc)==12
    assert publish_document(db,doc)==0
    with db.connection() as conn:
        row=conn.execute("SELECT report_period,scope_name,metric_name,evidence_page FROM research_shipping_report_metrics_readable WHERE scope='transpacific' AND metric='rmb'").fetchone()
        assert tuple(row)==('2026-03-31','跨太平洋','人民币航线收入',2)
    source=json.loads(path.read_text(encoding='utf8'));source[0]['text']=source[0]['text'].replace('合计 15','合计 16')
    path.write_text(json.dumps(source,ensure_ascii=False),encoding='utf8')
    with pytest.raises(ValueError):publish_document(db,doc)
    with db.connection() as conn: assert conn.execute("SELECT count(*) FROM research_shipping_report_metrics WHERE extractor_version='ccfi-routes-v1'").fetchone()[0]==12


def test_missing_route_history_does_not_hide_existing_quarter(tmp_path):
    db=Database(tmp_path/'test.sqlite3');db.initialize();setup(db);doc,_=setup(db,'2026-06-30')
    with db.connection() as conn: before=business_quarters(conn)[0]
    publish_document(db,doc)
    with db.connection() as conn: after=business_quarters(conn)[0]
    assert before==after and '2026-06-30' in after


def test_managed_backfill_records_per_document_and_reuses(tmp_path):
    db=Database(tmp_path/'test.sqlite3');db.initialize();setup(db)
    assert len(documents(db))==1
    for expected in (12,0):
        with db.connection(write=True) as conn: ref=shipping_collection.reserve(conn,'shipping_route_backfill',{'mode':'incremental'})
        result=shipping_collection.run(db,ref)
        assert result['status']=='complete'
        assert result['results'][0]['published']==expected
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM collection_batches WHERE dataset='shipping.report_routes' AND status='complete'").fetchone()[0]==2
