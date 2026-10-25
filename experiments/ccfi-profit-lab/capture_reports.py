"""Explicit CNINFO evidence capture, using shared provider pacing and archives."""
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from pypdf import PdfReader

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from quarterly_dashboard.storage import Database
from quarterly_dashboard.collection import RequestCollector, begin_batch, finish_batch, parsed_source

OUT=ROOT/'data/verification/samples/ccfi-profit-lab'


def capture():
    OUT.mkdir(parents=True,exist_ok=True)
    db=Database(ROOT/'data/stock_analysis.sqlite3')
    with db.connection() as conn:
        records=[dict(r) for r in conn.execute("SELECT DISTINCT period,title,adjunct_url FROM stock_announcement_metadata WHERE instrument_id=2 AND period BETWEEN '2023-01-01' AND '2026-06-30' AND title_category='formal_report' ORDER BY period")]
    assert len(records)==14,'Expected quarterly cumulative reports through 2026H1'
    collector=RequestCollector(db,progress=lambda msg:print(msg,flush=True))
    operation='ccfi-elasticity:'+datetime.now(timezone.utc).isoformat()
    manifest=[]
    for record in records:
        period=record['period']
        target=OUT/(period+'.pdf')
        existing=ROOT/f'data/company_reports/sh601919/{period}/v1/report.pdf'
        old=OUT/({'2024-12-31':'2024FY','2026-03-31':'2026Q1'}.get(period,'nonexistent')+'.pdf')
        source=existing if existing.exists() else old if old.exists() else target
        url='https://static.cninfo.com.cn/'+record['adjunct_url']
        evidence=target.with_suffix('.source.json')
        batch=None; sid=None
        if not source.exists():
            batch=begin_batch(db,operation,'research.ccfi_reports','cninfo',period,period,scope={'code':'601919','period':period},domain='stock',parser_version='pypdf-pages-v1',definition_version='ccfi-elasticity-v1')
            try:
                response,sid=collector.get(batch,'cninfo','research.ccfi_reports',url,expect='pdf',healthy_response=lambda r:r.content.startswith(b'%PDF'))
                if not response.content.startswith(b'%PDF'): raise ValueError('Source is not a PDF')
                target.write_bytes(response.content)
                evidence.write_text(json.dumps(dict(url=url,sha256=hashlib.sha256(response.content).hexdigest(),obtained_at=datetime.now(timezone.utc).isoformat(),batch_id=batch,source_id=sid),indent=2),encoding='utf-8')
            except BaseException as exc:
                finish_batch(db,batch,'failed','unknown',error=type(exc).__name__)
                raise
        try:
            saved=json.loads(evidence.read_text(encoding='utf-8')) if evidence.exists() else {}
            if saved.get('sha256') and saved['sha256']!=hashlib.sha256(source.read_bytes()).hexdigest():
                raise ValueError('Cached report evidence hash mismatch')
            parsed=OUT/(period+'.pages.json')
            if parsed.exists():
                pages=json.loads(parsed.read_text(encoding='utf-8'))
            else:
                pages=[dict(pdf_page=i+1,text=p.extract_text()) for i,p in enumerate(PdfReader(source).pages)]
                parsed.write_text(json.dumps(pages,ensure_ascii=False),encoding='utf-8')
            first=''.join(p['text'] for p in pages[:3])
            if '601919' not in first or '中远' not in first: raise ValueError('Company identity mismatch')
            expected=period[:4]+'年'+{'03-31':'第一季度报告','06-30':'半年度报告','09-30':'第三季度报告','12-31':'年度报告'}[period[5:]]
            if expected not in re.sub(r'\s+','',first):raise ValueError('Report period/type mismatch')
            if sid:
                parsed_source(db,sid,fields=('company_identity','report_year_and_kind','pdf_pages'))
                finish_batch(db,batch,'complete','complete',result={'period':period,'pages':len(pages)})
            elif saved.get('source_id'):
                parsed_source(db,saved['source_id'],fields=('company_identity','report_year_and_kind','pdf_pages'))
            m=dict(**record,url=url,path=str(source.relative_to(ROOT)).replace('\\','/'),pages_path=str(parsed.relative_to(ROOT)).replace('\\','/'),sha256=hashlib.sha256(source.read_bytes()).hexdigest())
            if evidence.exists():m.update(collection=json.loads(evidence.read_text(encoding='utf-8')))
            manifest.append(m)
            print(period,len(pages),'pages',flush=True)
        except BaseException as exc:
            if sid:parsed_source(db,sid,error=type(exc).__name__)
            if batch:finish_batch(db,batch,'failed','unknown',error=type(exc).__name__)
            raise
    (OUT/'calibration_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    capture()
