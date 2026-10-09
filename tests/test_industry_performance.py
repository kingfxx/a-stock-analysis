import copy
import hashlib
import json

import pytest

from quarterly_dashboard.industry_performance import attach_performance
from quarterly_dashboard.industry_service import IndustryService
from quarterly_dashboard.industry_snapshot import export
from quarterly_dashboard.industry_storage import latest_financial_rows
from quarterly_dashboard.storage import Database
from test_industries import fixture_bundle


def test_reported_metrics_zero_missing_future_and_scope():
    rows=[{'stock_code':'000001','period':'2025-06-30','provenance':{}}]
    raw=[{'SECURITY_CODE':'000001','REPORTDATE':'2025-06-30','NOTICE_DATE':'2025-08-01',
          'BASIC_EPS':0,'BPS':10,'WEIGHTAVG_ROE':'','MGJYXJJE':-2,'DEDUCT_BASIC_EPS':None,
          'ZXGXL':3,'XSMLL':'NaN'}]
    attach_performance(rows,raw,[{'file':'sample.json','sha256':'abc'}],'2025-06-30','2025-08-02')
    row=rows[0]
    assert row['basic_eps']==0 and row['operating_cashflow_per_share']==-2
    assert row['weighted_roe'] is None and row['reported_gross_margin'] is None
    assert row['performance_notice_date']=='2025-08-01'
    assert '未核实' in row['provenance']['dividend_yield']['basis']
    future=[{'stock_code':'000001','period':'2025-06-30','provenance':{}}]
    attach_performance(future,raw,[{}],'2025-06-30','2025-07-01')
    assert 'basic_eps' not in future[0]
    with pytest.raises(ValueError,match='报告期'):
        attach_performance(rows,raw,[{}],'2024-06-30','2025-08-02')
    with pytest.raises(ValueError,match='重复'):
        attach_performance(rows,raw*2,[{}],'2025-06-30','2025-08-02')


def test_migration_versioning_fallback_and_snapshot(tmp_path):
    db=Database(tmp_path/'test.sqlite3');db.initialize()
    service=IndustryService(db)
    bundle=fixture_bundle();service.import_bundle(copy.deepcopy(bundle),capture_local=False)
    next_bundle=copy.deepcopy(bundle)
    for row in next_bundle['financials']:
        attach_performance([row],[{'SECURITY_CODE':row['stock_code'],'REPORTDATE':row['period'],
            'NOTICE_DATE':'2025-08-01','WEIGHTAVG_ROE':0,'BASIC_EPS':1}],
            [{'file':'sample.json'}],row['period'],'2026-10-08')
    service.import_bundle(copy.deepcopy(next_bundle),capture_local=False)
    with db.connection() as conn:
        count=conn.execute('SELECT count(*) FROM sw_financial_facts').fetchone()[0]
        assert all(r['weighted_roe']==0 for r in latest_financial_rows(conn))
    service.import_bundle(copy.deepcopy(next_bundle),capture_local=False)
    service.import_bundle(copy.deepcopy(bundle),capture_local=False)
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM sw_financial_facts').fetchone()[0]==count
    revised=copy.deepcopy(next_bundle);revised['financials'][0]['weighted_roe']=2
    service.import_bundle(revised,capture_local=False)
    manifest=export(db.path,tmp_path/'snapshot')
    financial_files=[f for f in manifest['files'] if f['table']=='sw_financial_facts']
    assert sum(f['rows'] for f in financial_files)==count+1
    assert sum(f['latest_rows'] for f in financial_files)==len(bundle['financials'])
    for item in manifest['files']:
        path=tmp_path/'snapshot'/item['file']
        assert hashlib.sha256(path.read_bytes()).hexdigest()==item['sha256']
        assert len(json.loads(path.read_text(encoding='utf-8')))==item['rows']
    with pytest.raises(FileExistsError):
        export(db.path,tmp_path/'snapshot')


def test_partial_refresh_preserves_missing_value_and_original_evidence(tmp_path):
    db=Database(tmp_path/'test.sqlite3');db.initialize()
    service=IndustryService(db)
    bundle=fixture_bundle();bundle['financials']=bundle['financials'][:1]
    row=bundle['financials'][0]
    attach_performance([row],[{'SECURITY_CODE':row['stock_code'],'REPORTDATE':row['period'],
        'BPS':10,'WEIGHTAVG_ROE':1}],[{'file':'original.json'}],row['period'],'2026-10-08')
    service.import_bundle(copy.deepcopy(bundle),capture_local=False)
    attach_performance([row],[{'SECURITY_CODE':row['stock_code'],'REPORTDATE':row['period'],
        'BPS':None,'WEIGHTAVG_ROE':2}],[{'file':'refresh.json'}],row['period'],'2026-10-08')
    service.import_bundle(bundle,capture_local=False)
    with db.connection() as conn:
        result=latest_financial_rows(conn)[0]
        assert result['bps']==10 and result['weighted_roe']==2
        source=json.loads(result['provenance_json'])
        assert source[source['bps']['source_ref']]['file']=='original.json'
        assert 'file' not in source['bps']
        assert source['performance_source']['file']=='refresh.json'


def test_completed_performance_task_status(tmp_path):
    db=Database(tmp_path/'test.sqlite3');db.initialize()
    with db.connection(write=True) as conn:
        conn.execute("INSERT INTO sw_update_runs(action,target,recheck,started_at,status,result_json) "
            "VALUES('performance_fields','2026-06-30',0,'2026-10-08T00:00:00+00:00','complete',?)",
            (json.dumps({'periods':[{}],'start':'2026-06-30','end':'2026-06-30'}),))
    status=IndustryService(db).status()
    assert not status['running'] and '业绩指标补采完成' in status['message']


def test_company_roe_uses_selected_period_without_flow_arithmetic(tmp_path):
    from industry_financial_fixture import publish_financials
    db=Database(tmp_path/'test.sqlite3');db.initialize()
    service=IndustryService(db)
    bundle=fixture_bundle()
    for row in bundle['financials']:
        row['weighted_roe']=0 if row['stock_code']=='300750' else -2 if row['stock_code']=='000001' else None
        if row['stock_code']=='300750' and row['period']=='2024-06-30':
            row['weighted_roe']=8
    service.import_bundle(bundle,capture_local=False)
    publish_financials(db,bundle['financials'])
    for mode in ('ytd','quarter','ttm'):
        result=service.read(industry='630700',period='2025-06-30',mode=mode)
        values={c['code']:c['weighted_roe'] for c in result['companies']}
        assert values=={'300750':0,'000001':-2,'000002':None}
    earlier=service.read(industry='630700',period='2024-06-30',mode='ttm')
    assert next(c for c in earlier['companies'] if c['code']=='300750')['weighted_roe']==8
