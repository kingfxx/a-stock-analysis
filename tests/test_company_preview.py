"""Focused offline checks for preview calculations and its read-only HTTP boundary."""
import subprocess
import sys
from types import SimpleNamespace

import pytest

from stock_analysis.domains.financials.company_preview_metrics import build_series, flow_value
from stock_analysis.domains.industries.company_preview import read_company_preview
from stock_analysis.infrastructure.storage import Database
from stock_analysis.domains.industries.industry_service import IndustryService
from industry_financial_fixture import publish_financials
from test_industries import fixture_bundle
from test_http_source_boundaries import protocol_server


@pytest.mark.parametrize('mode,expected', [('quarter', 60), ('ytd', 100), ('ttm', 240), ('annual', 100)])
def test_period_calculation(mode, expected):
    facts = {p: {'revenue': v} for p, v in [('2024-06-30', 80), ('2024-12-31', 220),
                                          ('2025-03-31', 40), ('2025-06-30', 100)]}
    assert flow_value(facts, '2025-06-30', 'revenue', mode) == expected
    assert flow_value(facts, '2025-03-31', 'revenue', 'quarter') == 40
    assert flow_value(facts, '2024-12-31', 'revenue', 'quarter') is None
    assert all(r['period'].endswith('12-31') for r in build_series(facts, {}, 'annual'))


def test_ratios_balances_gaps_and_missing():
    facts = {'2024-03-31': {'revenue': 0, 'profit': -1},
             '2025-03-31': {'revenue': 100, 'profit': 0, 'capex': -2, 'operating_cash_flow': 0,
                            'margin_revenue': 80, 'cost': 60, 'net_profit': 8, 'roe': 3, 'net_assets': 500}}
    rows = {r['period']: r['values'] for r in build_series(facts, {'2025-06-30': {'market_cap': 900}}, 'quarter')}
    row = rows['2025-03-31']
    assert row['revenue_growth'] is None and row['profit_growth'] is None
    assert row['profit'] == 0 and row['free_cash_flow'] == 2
    assert row['gross_margin'] == 25 and row['net_margin'] == 10
    assert row['roe'] == 3 and row['net_assets'] == 500
    assert rows['2024-06-30']['revenue'] is None
    assert rows['2025-06-30']['market_cap'] == 900 and rows['2025-06-30']['profit'] is None
    ttm = {r['period']: r['values'] for r in build_series(facts, {}, 'ttm')}
    assert ttm['2025-03-31']['revenue'] is None and ttm['2025-03-31']['roe'] == 3


def test_pure_module_does_not_import_io():
    result = subprocess.run([sys.executable, '-c',
        "import sys; from stock_analysis.domains.financials.company_preview_metrics import build_series; "
        "assert build_series({}, {}, 'ttm') == []; "
        "assert not {'sqlite3','requests','stock_analysis.application'} & set(sys.modules)"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.fixture
def preview_db(tmp_path):
    db = Database(tmp_path / 'preview.sqlite3'); db.initialize()
    bundle = fixture_bundle()
    IndustryService(db).import_bundle({**bundle, 'financials': []}, capture_local=False)
    publish_financials(db, bundle['financials'])
    with db.connection(write=True) as conn:
        conn.execute('UPDATE market_financial_income SET total_profit=10,income_tax=2,operate_cost=5')
        conn.execute('UPDATE market_financial_balance SET total_assets=100,total_liabilities=40')
        conn.execute('UPDATE market_financial_cashflow SET netcash_operate=0,construct_long_asset=0,sales_services=20')
    return db


def test_query_financial_classification_and_sources(preview_db, monkeypatch):
    import requests
    def forbidden(*args, **kwargs):
        pytest.fail('Preview must not request external data')
    monkeypatch.setattr(requests.Session, 'request', forbidden)
    db = preview_db
    before = db.path.read_bytes()
    data = read_company_preview(db, '300750', 'ytd')
    row = data['rows'][-1]['values']
    assert row['net_assets'] == 60 and row['net_margin'] == pytest.approx(8 / 15 * 100)
    assert row['free_cash_flow'] == 0
    assert data['sources']['2025-06-30']['income']['source_id']
    assert len(data['metrics']) == 15
    assert before == db.path.read_bytes()  # No instrument, recent visit, task or cache write.
    with db.connection(write=True) as conn:
        conn.execute("UPDATE sw_industries SET name='银行' WHERE level=1")
    bank = read_company_preview(db, '300750')
    assert bank['financial_company'] and len(bank['metrics']) == 11
    assert 'free_cash_flow' not in bank['rows'][-1]['values']
    with db.connection(write=True) as conn:
        conn.execute('UPDATE sw_memberships SET industry_code=NULL')
    unknown = read_company_preview(db, '300750', 'ytd')
    assert not unknown['classification_known'] and unknown['rows'][-1]['values']['gross_margin'] is None


def test_latest_publication_and_exchange_are_not_blended(preview_db):
    with preview_db.connection(write=True) as conn:
        row = dict(conn.execute("SELECT * FROM market_financial_income WHERE security_code='300750' AND report_date='2025-06-30' AND is_latest=1").fetchone())
        conn.execute('UPDATE market_financial_income SET is_latest=0 WHERE id=?', (row.pop('id'),))
        row.update(version=row['version'] + 1, total_operate_income=90, parent_netprofit=-4)
        conn.execute('INSERT INTO market_financial_income (' + ','.join(row) + ') VALUES (' + ','.join('?' for _ in row) + ')', tuple(row.values()))
    data = read_company_preview(preview_db, '300750', 'ytd')
    assert data['rows'][-1]['values']['revenue'] == 90
    assert data['rows'][-1]['values']['profit'] == -4
    with preview_db.connection(write=True) as conn:
        conn.execute("UPDATE market_financial_income SET exchange='sh' WHERE security_code='300750' AND report_date='2024-06-30' AND is_latest=1")
    with pytest.raises(ValueError, match='交易所不唯一'):
        read_company_preview(preview_db, '300750')


def test_api_is_read_only_without_stock_or_industry_initialization(preview_db, protocol_server, monkeypatch):
    from stock_analysis import application
    _, session, base = protocol_server
    monkeypatch.setattr(application, 'settings', SimpleNamespace(DATABASE_PATH=preview_db.path))
    def forbidden(*args, **kwargs):
        pytest.fail('Preview must not initialize or invoke the full industry/stock service')
    monkeypatch.setattr(application, 'industry_service', forbidden)
    monkeypatch.setattr(Database, 'initialize', forbidden)
    before = preview_db.path.read_bytes()
    for mode in ('quarter', 'ytd', 'annual', 'ttm'):
        response = session.get(base + '/api/industry-company-preview', params={'code': '300750', 'mode': mode})
        assert response.status_code == 200 and response.json()['mode'] == mode
        assert response.headers['Cache-Control'] == 'no-store'
    for params in ({'code': 'bad'}, {'code': '300750', 'mode': 'bad'}):
        assert session.get(base + '/api/industry-company-preview', params=params).status_code == 400
    assert before == preview_db.path.read_bytes()
