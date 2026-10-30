from stock_analysis.infrastructure.storage import Database, utc_now
from stock_analysis.domains.research.reports.company_report_service import CompanyReportService
from stock_analysis.domains.research.sectors.shipping.shipping_route_trend import read_route_revenues


def insert(db, period, value, scope='transpacific', revision=False):
    service=CompanyReportService(db)
    source=db.path.parent/(period+('-revision' if revision else '')+'.pdf')
    source.write_bytes(b'%PDF-'+period.encode()+(b'-revision' if revision else b''))
    doc=service.import_report('601919',period,source)
    with db.connection(write=True) as conn:
        parse=conn.execute("INSERT INTO company_report_parses(document_id,parser_version,status,page_count,relative_path,quality_json,created_at) VALUES(?,?,'succeeded',1,'test','{}',?)",(doc['id'],'test',utc_now())).lastrowid
        if value is not None:
            conn.execute("INSERT INTO research_shipping_report_metrics(document_id,parse_id,metric,scope,basis,value,unit,currency,evidence_page,extractor_version,created_at) VALUES(?,?,'rmb',?,'cumulative',?,'千元','CNY',1,'ccfi-routes-v1',?)",(doc['id'],parse,scope,value*100000,utc_now()))


def test_period_conversion_yoy_and_missing_previous_quarter(tmp_path):
    db=Database(tmp_path/'test.sqlite3');db.initialize()
    for period,value in [('2024-03-31',10),('2024-06-30',30),('2024-09-30',50),('2024-12-31',80),('2025-03-31',20),('2025-06-30',50),('2025-12-31',100)]: insert(db,period,value)
    result=read_route_revenues(db)['series']
    quarter={r['label']:r for r in result['quarter']}
    assert quarter['2025Q1']['value']==20 and quarter['2025Q1']['yoy']==100
    assert quarter['2025Q2']['value']==30 and quarter['2025Q2']['yoy']==50
    assert '2025Q4' not in quarter
    assert {r['label']:r['value'] for r in result['annual']}=={'2024':80,'2025':100}
    assert result['annual'][-1]['yoy']==25
    assert next(r for r in result['ytd'] if r['label']=='2025Q2')['value']==50


def test_zero_baseline_and_new_revision_does_not_backfill_older_document(tmp_path):
    db=Database(tmp_path/'test.sqlite3');db.initialize()
    insert(db,'2024-03-31',0);insert(db,'2025-03-31',9)
    insert(db,'2025-06-30',10);insert(db,'2025-06-30',None,revision=True)
    rows=read_route_revenues(db)['series']
    assert next(r for r in rows['quarter'] if r['label']=='2025Q1')['yoy'] is None
    assert not any(r['label']=='2025Q2' for r in rows['ytd'])


def test_volume_and_unit_revenue_use_period_flows_and_handle_zero_and_missing(tmp_path):
    db=Database(tmp_path/'test.sqlite3');db.initialize()
    for period,value in [('2024-03-31',1),('2024-06-30',2),('2025-03-31',1),('2025-06-30',2),('2025-09-30',3)]: insert(db,period,value)
    with db.connection(write=True) as conn:
        for period,teu,usd in [('2024-03-31',10000,10000),('2024-06-30',30000,40000),('2025-03-31',10000,12000),('2025-06-30',40000,72000),('2025-09-30',40000,74000)]:
            parse=conn.execute("SELECT p.id,p.document_id FROM company_report_parses p JOIN company_report_documents d ON d.id=p.document_id WHERE d.report_period=?",(period,)).fetchone()
            for metric,value,unit,currency in [('teu',teu,'TEU',''),('usd',usd,'千元','USD')]:
                conn.execute("INSERT INTO research_shipping_report_metrics(document_id,parse_id,metric,scope,basis,value,unit,currency,evidence_page,extractor_version,created_at) VALUES(?,?,?,'transpacific','cumulative',?,?,?,1,'ccfi-routes-v1',?)",(parse['document_id'],parse['id'],metric,value,unit,currency,utc_now()))
    result=read_route_revenues(db)['metrics']
    q={r['label']:r for r in result['unit_revenue']['series']['quarter']}
    assert q['2025Q2']['value']==2000
    assert q['2025Q2']['yoy']==(2000/1500-1)*100
    assert '2025Q3' not in q
    assert next(r for r in result['volume']['series']['quarter'] if r['label']=='2025Q2')['value']==3
    assert next(r for r in result['unit_revenue']['series']['ytd'] if r['label']=='2025Q2')['value']==1800
