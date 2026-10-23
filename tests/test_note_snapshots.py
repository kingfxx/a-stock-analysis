import pytest
import re

from quarterly_dashboard import note_snapshots as snapshots
from quarterly_dashboard.storage import Database


@pytest.fixture
def local(tmp_path, monkeypatch):
    db = Database(tmp_path / 'isolated.sqlite3'); db.initialize()
    import requests
    monkeypatch.setattr(requests.sessions.Session, 'request', lambda *a, **k: pytest.fail('note triggered network'))
    return db, tmp_path


def get(local, **command):
    db, path = local
    return snapshots.catalog(db, {'code': '300750', **command}, path, path, path)


def test_quarter_yoy_uses_quarter_not_cumulative_and_keeps_zero_negative(local, monkeypatch):
    reports = [
        {'period': '2025-03-31', 'profit_ytd': 10e8, 'revenue_ytd': 100e8},
        {'period': '2025-06-30', 'profit_ytd': 30e8, 'revenue_ytd': 200e8},
        {'period': '2026-03-31', 'profit_ytd': 20e8, 'revenue_ytd': 100e8},
        {'period': '2026-06-30', 'profit_ytd': 40e8, 'revenue_ytd': 200e8,
         'operating_cash_flow_ytd': -1e8, 'publish_date': '2026-08-28'},
    ]
    monkeypatch.setattr(snapshots.FundamentalService, 'read', lambda *a: {'reports': reports})
    quarter = {r['id']: r for r in get(local, group='financial', period='2026-06-30', mode='quarter')['items']}
    cumulative = {r['id']: r for r in get(local, group='financial', period='2026-06-30', mode='ytd')['items']}
    assert quarter['profit_growth']['value'] == '0.00%'
    assert cumulative['profit_growth']['value'] == '33.33%'
    assert quarter['profit']['value'] == '20.00亿元'
    assert cumulative['operating_cash_flow']['value'] == '-1.00亿元'
    assert not quarter['operating_cash_flow']['available']  # Missing Q1 cash flow must not become zero.


def test_percentile_scale_and_actual_dates(local, monkeypatch):
    from quarterly_dashboard import server
    monkeypatch.setattr(snapshots.ValuationService, 'read', lambda *a: {'updated_on': '2026-10-23'})
    monkeypatch.setattr(server, '_p4_valuation_payload', lambda *a, **k: {'views': {'10': {
        'pe': {'current': 12.0, 'percentile': .8, 'current_date': '2026-10-21', 'sample_frequency': 'day'},
        'pb': {'current': 0, 'percentile': None, 'current_date': '2026-10-20'},
    }}})
    result = get(local, group='valuation')
    assert result['valuation_updated_on'] == '2026-10-23'
    assert re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', result['captured_at'])
    values = {r['id']: r for r in result['items']}
    assert values['pe_percentile']['value'] == '0.80%'
    assert values['pe']['date'] == '2026-10-21'
    assert values['pb']['date'] == '2026-10-20'
    assert not values['pb_percentile']['available']
    assert values['pb_percentile']['value'] == '暂无数据'


def test_missing_groups_do_not_invent_data_or_write_business_facts(local):
    db, _ = local
    with db.connection() as conn:
        before = conn.execute('SELECT count(*) FROM instruments').fetchone()[0]
    for group in ('financial', 'valuation', 'chips', 'business', 'statements'):
        result = get(local, group=group)
        assert all(not row['available'] for row in result['items'])
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM instruments').fetchone()[0] == before


def test_business_source_ratios_and_revision(local, monkeypatch):
    monkeypatch.setattr(snapshots.BusinessSegmentService, 'read', lambda *a: {'periods': [{
        'report_date': '2026-06-30', 'source': 'eastmoney:sample', 'revision': 2,
        'content_hash': 'hash-2', 'obtained_at': '2026-08-29T00:00:00Z', 'dimensions': {'2': [{
            'id': 7, 'item_name': '产品甲', 'revenue': 2e8, 'cost': None, 'gross_profit': -1e8,
            'revenue_share': .8, 'gross_margin': -.5,
        }]},
    }]})
    result = get(local, group='business')
    values = {r['id']: r for r in result['items']}
    assert values['7:revenue_share']['value'] == '80.00%'
    assert values['7:gross_margin']['value'] == '-50.00%'
    assert not values['7:cost']['available']
    assert 'hash-2' in values['7:revenue']['source'] and '修订 2' in values['7:revenue']['source']
