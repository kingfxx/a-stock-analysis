import copy
import pytest

from quarterly_dashboard.industry_financial_reader import required_periods
from quarterly_dashboard.industry_service import IndustryService
from quarterly_dashboard.maintenance import MaintenanceService
from quarterly_dashboard.storage import Database
from industry_financial_fixture import publish_financials
from test_industries import fixture_bundle


@pytest.fixture
def setup(tmp_path):
    db=Database(tmp_path/'reader.sqlite3');db.initialize()
    service=IndustryService(db)
    bundle=fixture_bundle()
    service.import_bundle({**bundle,'financials':[]},capture_local=False)
    for row in bundle['financials']:
        row.update(operating_cost=row['revenue']*.6,weighted_roe=0 if row['stock_code']=='300750' else -2)
    publish_financials(db,bundle['financials'])
    return db,service,bundle


def test_all_industry_consumers_ignore_legacy_and_share_new_values(setup,monkeypatch):
    db,service,bundle=setup
    # Contradictory legacy observations must never affect the read path.
    wrong=copy.deepcopy(bundle)
    for row in wrong['financials']:row.update(revenue=99999,parent_profit=99999,weighted_roe=99)
    service.import_bundle(wrong,capture_local=False)
    monkeypatch.setattr('quarterly_dashboard.industry_service.latest_financial_rows',
                        lambda *a,**k:pytest.fail('legacy financial read'))
    result=service.read(code='300750',industry='630000',level=1,mode='ytd',period='2025-06-30')
    assert result['ranking'][0]['revenue']==53
    assert result['child_ranking'][0]['revenue']==53
    assert result['series'][-1]['revenue']==53
    assert result['stock_metrics']['revenue']==15
    assert result['stock_metrics']['gross_margin']==40
    assert result['stock_provenance']['revenue']['field']=='TOTAL_OPERATE_INCOME'
    assert result['stock_provenance']['revenue']['source_id']
    companies={r['code']:r for r in result['companies']}
    assert companies['300750']['weighted_roe']==0
    assert companies['000001']['weighted_roe']==-2
    assert result['obtained_at'] and result['financial_source']=='market_financial_industry'
    assert result['membership_obtained_at']==bundle['obtained_at']


def test_new_tables_work_with_empty_legacy_and_missing_performance(setup):
    db,service,bundle=setup
    with db.connection(write=True) as c:
        c.execute('DELETE FROM market_financial_performance')
        assert c.execute('SELECT count(*) FROM sw_financial_facts').fetchone()[0]==0
    result=service.read(code='300750',mode='ytd',period='2025-06-30')
    assert result['stock_metrics']['revenue']==15
    assert all(r['weighted_roe'] is None for r in result['companies'])


def test_new_publication_invalidates_warm_cache_without_sw_import(setup):
    db,service,bundle=setup
    before=service.read(code='300750',mode='ytd')
    rows=copy.deepcopy(bundle['financials'])
    for row in rows:
        if row['stock_code']=='300750' and row['period']=='2025-06-30':
            row.update(revenue=0,parent_profit=-5,weighted_roe=3)
    publish_financials(db,rows)
    after=service.read(code='300750',mode='ytd')
    assert after['import_id']==before['import_id']
    assert after['stock_metrics']['revenue']==0
    assert after['stock_metrics']['parent_profit']==-5
    assert after['series'][-1]['revenue']==30
    assert next(r for r in after['companies'] if r['code']=='300750')['weighted_roe']==3


@pytest.mark.parametrize('mode,expected',[
    ('ytd',{'2025-06-30','2024-06-30'}),
    ('quarter',{'2025-06-30','2025-03-31','2024-06-30','2024-03-31'}),
    ('ttm',{'2025-06-30','2024-06-30','2024-12-31','2023-06-30','2023-12-31'}),
    ('annual',{'2025-06-30','2024-06-30'})])
def test_required_inputs(mode,expected):
    assert set(required_periods('2025-06-30',mode))==expected
    assert set(required_periods('2025-03-31','quarter'))=={'2025-03-31','2024-03-31'}
    assert set(required_periods('2025-12-31','ttm'))=={'2025-12-31','2024-12-31'}


def test_bounded_cache_history_not_kept_as_full_facts(setup):
    db,service,bundle=setup
    for period in ['2024-06-30','2025-06-30']:
        for mode in ['ytd','quarter','ttm']:
            service.read(code='300750',period=period,mode=mode)
    reader=service._financial_reader
    assert len(reader._facts)<=2 and len(reader._series)<=8
    assert 'facts' not in service._load()
    for key,facts in reader._facts.items():
        assert all(period in key[2] for stock,period in facts)


def test_view_is_logical_registered_and_reports_actual_time(setup):
    db,service,bundle=setup
    data=MaintenanceService(db).read()
    view=next(v for v in data['views'] if v['name']=='market_financial_industry')
    assert view['total_bytes']==0 and view['updated_at'] and view['time_basis']=='updated_at'
    assert view['category']=='行业数据' and '尚未登记' not in view['description']
    assert not any(t['name']==view['name'] for t in data['tables'])


def test_financial_industry_exclusion_reloads_on_classification_update(setup):
    db,service,bundle=setup
    service.read(code='300750',mode='ytd')
    bank=copy.deepcopy(bundle);bank['taxonomy'][0]['name']='银行';bank['financials']=[]
    service.import_bundle(bank,capture_local=False)
    result=service.read(code='300750',mode='ytd')
    assert all(r['gross_margin'] is None for r in result['ranking'])
    assert all(r['metrics']['gross_margin'] is None for r in result['companies'])


def test_partial_new_period_default_keeps_latest_covered_period(setup):
    db,service,bundle=setup
    row={**bundle['financials'][-1],'period':'2025-09-30'}
    publish_financials(db,[row])
    assert service.read(code='300750',mode='ytd')['period']=='2025-06-30'
    partial=service.read(industry='630000',level=1,mode='ytd',period='2025-09-30')['ranking'][0]
    assert partial['revenue'] is None and partial['revenue_count']==1
    assert not partial['rank_eligible']


def test_flow_windows_match_ttm_quarter_and_same_cohort_yoy(tmp_path):
    db=Database(tmp_path/'flows.sqlite3');db.initialize()
    service=IndustryService(db)
    service.import_bundle({**fixture_bundle(),'financials':[]},capture_local=False)
    values={'2023-06-30':30,'2023-12-31':80,'2024-03-31':10,
            '2024-06-30':40,'2024-12-31':100,'2025-03-31':20,'2025-06-30':60}
    rows=[dict(stock_code='300750',period=p,revenue=v,parent_profit=v/10,
               operating_cost=v*.6,weighted_roe=7) for p,v in values.items()]
    publish_financials(db,rows)
    for mode,amount in [('ttm',120),('quarter',40)]:
        result=service.read(code='300750',period='2025-06-30',mode=mode)
        assert result['stock_metrics']['revenue']==amount
        assert result['stock_metrics']['revenue_yoy']==pytest.approx(100/3)
        assert result['stock_metrics']['gross_margin']==40
        assert result['series'][-1]['revenue_known']==amount
        assert next(r for r in result['companies'] if r['code']=='300750')['weighted_roe']==7
    early=service.read(code='300750',period='2023-06-30',mode='ttm')
    assert early['stock_metrics']['revenue'] is None


def test_empty_view_time_and_absent_new_financial_data_never_use_legacy(tmp_path):
    db=Database(tmp_path/'empty.sqlite3');db.initialize()
    service=IndustryService(db);service.import_bundle(fixture_bundle(),capture_local=False)
    result=service.read(code='300750')
    assert result['periods']==[] and result['period'] is None and result['obtained_at'] is None
    assert all(r['weighted_roe'] is None for r in result['companies'])
    view=next(v for v in MaintenanceService(db).read()['views'] if v['name']=='market_financial_industry')
    assert view['updated_at'] is None and view['time_basis']=='updated_at'


def test_extended_history_periods_appear_in_industry_page(setup):
    db,service,bundle=setup
    row={**bundle['financials'][0],'period':'2014-09-30'}
    publish_financials(db,[row])
    result=service.read(code=row['stock_code'],period='2014-09-30',mode='ytd')
    assert result['periods'][0]=='2014-09-30'
    assert result['stock_metrics']['revenue']==row['revenue']


def test_financial_validation_extended_without_extending_cap_scope():
    from datetime import date
    from quarterly_dashboard.industry_updates import validate_request
    validate_request('financial_period','2014-09-30',today=date(2026,10,9))
    with pytest.raises(ValueError):
        validate_request('financial_period','2013-12-31',today=date(2026,10,9))
    with pytest.raises(ValueError):
        validate_request('cap_quarter','2014Q3',today=date(2026,10,9))
