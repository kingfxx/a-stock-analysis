import pytest
import re

from stock_analysis.domains.notes import note_snapshots as snapshots
from stock_analysis.infrastructure.storage import Database


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
    from datetime import date, timedelta
    end = date(2026, 10, 21)
    rows = [{'date': (end - timedelta(days=124-i)).isoformat(), 'pe': 11 if i == 0 else 12 if i == 124 else 13}
            for i in range(125)]
    rows[-2]['pb'] = 2
    monkeypatch.setattr(snapshots.ValuationService, 'read', lambda *a: {'updated_on': '2026-10-23', 'observation_rows': rows})
    result = get(local, group='valuation')
    assert result['valuation_updated_on'] == '2026-10-23'
    assert re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', result['captured_at'])
    values = {r['id']: r for r in result['items']}
    assert values['pe_percentile']['value'] == '0.80%'
    assert values['pe_percentile']['position'] == {'label': '极低', 'marker': '🟢'}
    assert values['pe']['date'] == '2026-10-21'
    assert result['as_of'] == '2026-10-21'
    assert values['pb']['date'] == '2026-10-21'
    assert not values['pb']['available']  # Do not carry forward yesterday's PB.
    assert not values['pb_percentile']['available']
    assert values['pb_percentile']['value'] == '暂无数据'
    assert 'position' not in values['pb_percentile']


@pytest.mark.parametrize('percentile,label,yield_label,marker,yield_marker', [
    (0, '极低', '极低', '🟢', '🔴'), (5, '极低', '极低', '🟢', '🔴'),
    (5.1, '低位', '低息', '🟢', '🔴'), (20, '低位', '低息', '🟢', '🔴'),
    (20.1, '偏低', '偏低', '🔵', '🟠'), (39.9, '偏低', '偏低', '🔵', '🟠'),
    (40, '中值', '中值', '🟡', '🟡'), (60, '中值', '中值', '🟡', '🟡'),
    (60.1, '偏高', '偏高', '🟠', '🔵'), (79.9, '偏高', '偏高', '🟠', '🔵'),
    (80, '高位', '高息', '🔴', '🟢'), (94.9, '高位', '高息', '🔴', '🟢'),
    (95, '极高', '极高', '🔴', '🟢'), (100, '极高', '极高', '🔴', '🟢'),
])
def test_percentile_position_matches_card_bands(percentile, label, yield_label, marker, yield_marker):
    for metric in ('pe', 'pb'):
        assert snapshots.percentile_position(percentile, metric)['position'] == {'label': label, 'marker': marker}
    assert snapshots.percentile_position(percentile, 'dividend_yield')['position'] == {'label': yield_label, 'marker': yield_marker}


@pytest.mark.parametrize('value', [None, -1, 101, float('nan'), float('inf'), True])
def test_missing_or_invalid_percentile_has_no_position(value):
    assert snapshots.percentile_position(value, 'pe') == {}


def test_valuation_selected_day_excludes_future_samples_and_dividends(local, monkeypatch):
    rows = [{'date': '2024-10-21', 'pe': 5}, {'date': '2026-10-21', 'pe': 20},
            {'date': '2026-10-23', 'pe': 10}, {'date': '2099-01-01', 'pe': 100}]
    monkeypatch.setattr(snapshots.ValuationService, 'read', lambda *a: {'observation_rows': rows})
    monkeypatch.setattr(snapshots.PriceService, 'read', lambda *a: [
        {'date': '2025-10-21', 'close': 100}, {'date': '2026-10-21', 'close': 50},
        {'date': '2026-10-23', 'close': 200}])
    monkeypatch.setattr(snapshots.DividendService, 'read', lambda *a: [
        {'date': '2026-01-01', 'per_share': 2}, {'date': '2026-10-22', 'per_share': 2},
        {'date': '2099-01-01', 'per_share': 99}])
    historical = get(local, group='valuation', as_of='2026-10-21', years=3)
    values = {r['id']: r for r in historical['items']}
    assert values['pe']['value'] == '20.00倍'
    assert values['pe_percentile']['value'] == '50.00%'
    assert values['dividend_yield']['value'] == '4.00%'
    assert values['dividend_yield_percentile']['value'] == '50.00%'
    assert values['price']['value'] == '50.00元/股'
    assert all(item['date'] == '2026-10-21' for item in historical['items'])
    assert '2099-01-01' not in historical['available_dates']
    latest = get(local, group='valuation', years=3)
    assert latest['as_of'] == '2026-10-23'
    assert {r['id']: r for r in latest['items']}['pe_percentile']['value'] == '33.30%'
    missing = get(local, group='valuation', as_of='2026-10-22')
    assert missing['as_of'] == '2026-10-22' and not missing['has_date_data']
    assert all(not item['available'] for item in missing['items'])


def test_valuation_legacy_dates_zero_yield_and_invalid_date(local, monkeypatch):
    monkeypatch.setattr(snapshots.ValuationService, 'read', lambda *a: {'rows': [
        {'date': '2026-10-23', 'pe': 10, 'pe_date': '2026-10-21', 'pb': 2, 'pb_date': '2026-10-20'}]})
    monkeypatch.setattr(snapshots.PriceService, 'read', lambda *a: [{'date': '2026-10-21', 'close': 50}])
    values = {r['id']: r for r in get(local, group='valuation', as_of='2026-10-21')['items']}
    assert values['pe']['value'] == '10.00倍'
    assert values['dividend_yield']['value'] == '0.00%'
    assert not values['pb']['available']
    for day in ('2026-02-30', '20261021', '2099-01-01', '', 123):
        with pytest.raises(ValueError, match='数据日期'):
            get(local, group='valuation', as_of=day)


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
