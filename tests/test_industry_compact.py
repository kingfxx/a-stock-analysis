import copy
import json

from quarterly_dashboard.industry_compact import compact_financial_rows, fingerprint, prune_extension_predecessors
from quarterly_dashboard.industry_service import IndustryService
from quarterly_dashboard.industry_sources import attach_financial_source, financial_rows
from quarterly_dashboard.industry_storage import latest_financial_rows
from quarterly_dashboard.storage import Database
from test_industries import fixture_bundle


def test_multiple_extension_stages_removed_but_changed_amount_retained(tmp_path):
    service=make_service(tmp_path)
    bundle=copy.deepcopy(fixture_bundle());bundle['financials']=bundle['financials'][:1]
    bundle['manifest'].update(action='financial_extensions',base_metrics_preserved=True)
    bundle['financials'][0]['operating_cost']=0
    service.import_bundle(copy.deepcopy(bundle),capture_local=False)
    bundle['manifest']['action']='performance_fields'
    bundle['financials'][0]['weighted_roe']=0
    service.import_bundle(copy.deepcopy(bundle),capture_local=False)
    bundle['financials'][0]['operating_cost']=2
    service.import_bundle(bundle,capture_local=False)
    with service.db.connection(write=True) as conn:
        before=fingerprint(conn)
        assert prune_extension_predecessors(conn)==2
        assert fingerprint(conn)==before
        versions=conn.execute("SELECT operating_cost,weighted_roe FROM sw_financial_facts WHERE stock_code='300750' AND period='2024-06-30' ORDER BY import_id").fetchall()
        assert [tuple(r) for r in versions]==[(0,0),(2,0)]


def test_missing_publication_date_is_completion_but_known_date_change_is_revision(tmp_path):
    db=Database(tmp_path/'dates.sqlite3');db.initialize()
    service=IndustryService(db)
    bundle=fixture_bundle();bundle['financials']=bundle['financials'][:1]
    bundle['financials'][0]['notice_date']=None
    service.import_bundle(copy.deepcopy(bundle),capture_local=False)
    bundle['manifest'].update(action='performance_fields',base_metrics_preserved=True)
    bundle['financials'][0]['notice_date']='2024-08-01'
    bundle['financials'][0]['weighted_roe']=0
    service.import_bundle(copy.deepcopy(bundle),capture_local=False)
    bundle['financials'][0]['notice_date']='2024-08-02'
    service.import_bundle(bundle,capture_local=False)
    with db.connection(write=True) as conn:
        before=fingerprint(conn)
        assert prune_extension_predecessors(conn)==1
        assert fingerprint(conn)==before
        assert conn.execute('SELECT count(*) FROM sw_financial_facts').fetchone()[0]==2


def make_service(tmp_path):
    from industry_financial_fixture import publish_financials
    db=Database(tmp_path/'compact.sqlite3');db.initialize()
    service=IndustryService(db);service.import_bundle(fixture_bundle(),capture_local=False)
    publish_financials(db,fixture_bundle()['financials'])
    return service


def test_shared_sources_and_repeat_import_do_not_duplicate_facts(tmp_path):
    service=make_service(tmp_path)
    before=service.read(industry='630701',mode='ytd',period='2025-06-30')
    service.import_bundle(fixture_bundle(),capture_local=False)
    with service.db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM sw_financial_facts').fetchone()[0]==6
        assert conn.execute('SELECT count(*) FROM sw_financial_provenance').fetchone()[0]==1
        assert conn.execute("SELECT count(*) FROM sw_financial_facts WHERE provenance_json!='{}' OR provenance_id IS NULL").fetchone()[0]==0
    after=service.read(industry='630701',mode='ytd',period='2025-06-30')
    assert before['series']==after['series']


def test_only_extension_predecessor_removed_and_true_revision_preserved(tmp_path):
    service=make_service(tmp_path)
    revision=copy.deepcopy(fixture_bundle());revision['financials'][0]['revenue']=11
    service.import_bundle(revision,capture_local=False)
    extension=copy.deepcopy(revision);extension['financials']=extension['financials'][:1]
    extension['financials'][0]['operating_cost']=0
    extension['manifest'].update(action='financial_extensions',base_metrics_preserved=True)
    service.import_bundle(extension,capture_local=False)
    with service.db.connection(write=True) as conn:
        before=fingerprint(conn)
        assert prune_extension_predecessors(conn)==1
        assert fingerprint(conn)==before
        values=[r[0] for r in conn.execute("SELECT revenue FROM sw_financial_facts WHERE stock_code='300750' AND period='2024-06-30' ORDER BY import_id")]
        assert values==[10,11]
        assert latest_financial_rows(conn,'2024-06-30')[0]['provenance_json']
        assert prune_extension_predecessors(conn)==0


def test_legacy_provenance_converted_without_value_or_source_change(tmp_path):
    service=make_service(tmp_path)
    with service.db.connection(write=True) as conn:
        source=conn.execute('SELECT provenance_json FROM sw_financial_provenance').fetchone()[0]
        conn.execute('UPDATE sw_financial_facts SET provenance_json=?,provenance_id=NULL',(source,))
        before=fingerprint(conn)
        assert compact_financial_rows(conn)==6
        assert fingerprint(conn)==before
        assert compact_financial_rows(conn)==0
        assert json.loads(latest_financial_rows(conn)[0]['provenance_json'])==json.loads(source)


def test_missing_new_cost_keeps_zero_and_its_original_source(tmp_path):
    service=make_service(tmp_path)
    bundle=copy.deepcopy(fixture_bundle());bundle['financials']=bundle['financials'][:1]
    row=bundle['financials'][0];row['operating_cost']=0
    row['provenance']['extension_source']={'file':'old.json','sha256':'original'}
    row['provenance']['operating_cost']={'source':'eastmoney','field':'OPERATE_COST','source_ref':'extension_source'}
    service.import_bundle(bundle,capture_local=False)
    row['operating_cost']=None;row['revenue']=12
    row['provenance']={'source':'test','field':'TOTAL_OPERATE_INCOME','extension_source':{'file':'new.json'}}
    service.import_bundle(bundle,capture_local=False)
    with service.db.connection() as conn:
        current=next(r for r in latest_financial_rows(conn,'2024-06-30') if r['stock_code']=='300750')
    assert current['operating_cost']==0 and current['revenue']==12
    provenance=json.loads(current['provenance_json'])
    assert provenance[provenance['operating_cost']['source_ref']]['file']=='old.json'


def test_parsed_responses_share_evidence_without_embedding_raw_amounts(tmp_path):
    service=make_service(tmp_path)
    raw=[{'SECURITY_CODE':stock,'REPORT_DATE':'2026-06-30','TOTAL_OPERATE_INCOME':100,
          'PARENT_NETPROFIT':10,'OPERATE_COST':cost} for stock,cost in [('300750',0),('000001',30)]]
    rows=financial_rows(raw,'2026-10-04')
    for row in rows:
        attach_financial_source(row,{'file':'response.json','sha256':'page-hash','params':{'pageNumber':1}})
        assert 'raw' not in row['provenance']
    bundle=copy.deepcopy(fixture_bundle());bundle.update(financials=rows,caps=[])
    service.import_bundle(bundle,capture_local=False)
    with service.db.connection() as conn:
        current=latest_financial_rows(conn,'2026-06-30')
        assert len({r['provenance_id'] for r in current})==1
        assert sorted(r['operating_cost'] for r in current)==[0,30]
