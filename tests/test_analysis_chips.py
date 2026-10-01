from datetime import date, timedelta

import pytest

from quarterly_dashboard.analysis_chips import joint_evidence, months_before

PERIODS = ['2026-03-31', '2026-06-30']


def holder(day, count):
    return dict(stat_date=day, holders=count, source='eastmoney:F10', holder_scope='total', announced_on=day)


def series():
    prices, financing = [], []
    day = date(2026, 3, 31)
    while day <= date(2026, 9, 30):
        if day.weekday() < 5:
            prices.append({'date': day.isoformat(), 'close': 10 + len(prices) / 100})
            financing.append({'trade_date': day.isoformat(), 'margin_balance': 1000 + len(prices), 'source': 'eastmoney'})
        day += timedelta(days=1)
    return prices, financing


def facts(holders=None, financing=None, prices=None, raw=None, periods=PERIODS, as_of='2026-10-01'):
    return {e['id']: e for e in joint_evidence(holders or [], financing or [], prices or [], raw or [],
                                           report_periods=periods, as_of=as_of)}


def test_calendar_months_and_requested_comparison_windows():
    assert months_before('2024-05-31', 3) == '2024-02-29'
    prices, financing = series()
    holders = [holder('2026-03-31', 100), holder('2026-06-30', 150), holder('2026-09-15', 999)]
    result = facts(holders, financing, prices, prices)
    assert len(result) == 2
    value = result['shareholders.price.latest_reports']['value']
    assert value['usable']
    assert value['start_on'] == value['price_start_on'] == '2026-03-31'
    assert value['end_on'] == value['price_end_on'] == '2026-06-30'
    assert value['start_value'] == 100 and value['end_value'] == 150
    assert value['change_pct'] == pytest.approx(50)
    value = result['financing.price.3m']['value']
    assert value['usable'] and value['missing_trading_days'] == 0
    assert value['start_on'] == value['price_start_on'] == '2026-06-30'
    assert value['end_on'] == value['price_end_on'] == '2026-09-30'


@pytest.mark.parametrize('change', ['source', 'scope', 'unknown'])
def test_never_mix_shareholder_sources_or_scopes(change):
    prices, _ = series()
    first, last = holder('2026-03-31', 100), holder('2026-06-30', 200)
    if change == 'source': first['source'] = 'other'
    elif change == 'scope': first['holder_scope'] = 'a_share'
    else: first['holder_scope'] = last['holder_scope'] = 'unknown'
    value = facts([first, last], prices=prices)['shareholders.price.latest_reports']['value']
    assert not value['usable'] and value['change_pct'] is None


def test_same_day_f10_preference_and_no_future_disclosure():
    prices, _ = series()
    first, last = holder('2026-03-31', 100), holder('2026-06-30', 200)
    unknown = {**last, 'source': 'other', 'holder_scope': 'unknown', 'holders': 400}
    future = holder('2026-09-30', 999)
    future['announced_on'] = '2026-10-20'
    value = facts([first, last, unknown, future], prices=prices)['shareholders.price.latest_reports']['value']
    assert value['usable'] and value['end_value'] == 200
    value = facts([first, last, future], prices=prices,
                  periods=['2026-06-30','2026-09-30'])['shareholders.price.latest_reports']['value']
    assert not value['usable'] and value['end_value'] is None


@pytest.mark.parametrize('missing', ['previous_report', 'latest_report', 'second_report'])
def test_no_substitution_of_interim_or_older_holder_dates(missing):
    prices, _ = series()
    records = [holder('2026-03-31', 100), holder('2026-06-30', 200),
               holder('2025-12-31', 50), holder('2026-05-31', 999)]
    periods = PERIODS
    if missing == 'previous_report': records.pop(0)
    elif missing == 'latest_report': records.pop(1)
    else: periods = ['2026-06-30']
    value = facts(records, prices=prices, periods=periods)['shareholders.price.latest_reports']['value']
    assert not value['usable']
    if missing != 'latest_report': assert value['start_value'] is None
    else: assert value['end_value'] is None


def test_short_financing_history_and_missing_prices_stay_missing():
    prices, financing = series()
    value = facts(financing=financing[-40:], prices=prices, raw=prices)['financing.price.3m']['value']
    assert not value['usable'] and value['start_on'] is None
    value = facts([holder('2026-03-31', 100), holder('2026-06-30', 200)],
                  prices=prices[-40:])['shareholders.price.latest_reports']['value']
    assert not value['usable'] and value['price_change_pct'] is None


@pytest.mark.parametrize('problem', ['missing_day', 'zero_base', 'negative_price', 'wrong_source', 'no_trading_calendar'])
def test_financing_requires_comparable_nonzero_base_and_coverage(problem):
    prices, financing = series()
    raw = prices[:]
    baseline = next(i for i, r in enumerate(financing) if r['trade_date'] == '2026-06-30')
    if problem == 'missing_day': financing.pop(baseline + 10)
    elif problem == 'zero_base': financing[baseline]['margin_balance'] = 0
    elif problem == 'negative_price': prices[baseline] = {**prices[baseline], 'close': -1}
    elif problem == 'wrong_source':
        for row in financing[:baseline + 1]: row['source'] = 'other'
    else: raw = []
    value = facts(financing=financing, prices=prices, raw=raw)['financing.price.3m']['value']
    assert not value['usable']
    if problem == 'missing_day': assert value['missing_trading_days'] == 1
    if problem == 'zero_base': assert value['change_pct'] is None
    if problem == 'negative_price': assert value['price_change_pct'] is None


def test_price_up_financing_down_retains_positive_and_negative_signs():
    prices, financing = series()
    for i, row in enumerate(financing): row['margin_balance'] = 2000 - i
    value = facts(financing=financing, prices=prices, raw=prices)['financing.price.3m']['value']
    assert value['usable']
    assert value['price_change_pct'] > 0 and value['change_pct'] < 0


def test_shareholder_price_matches_prior_trade_not_future_or_stale():
    records = [holder('2026-03-31', 100), holder('2026-06-30', 200)]
    prices = [{'date':'2026-03-30','close':10}, {'date':'2026-04-01','close':20},
              {'date':'2026-06-30','close':30}]
    value = facts(records, prices=prices)['shareholders.price.latest_reports']['value']
    assert value['usable'] and value['price_start_on'] == '2026-03-30'
    assert value['price_change_pct'] == 200
    prices[0]['date'] = '2026-03-01'
    value = facts(records, prices=prices)['shareholders.price.latest_reports']['value']
    assert not value['usable'] and value['price_start_on'] is None
