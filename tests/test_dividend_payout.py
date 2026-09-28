import pytest

from quarterly_dashboard.core import build_period_rows, view_rows


@pytest.mark.parametrize('profit, dividend, expected', [
    (100, 30, 30), (100, 0, 0), (100, 150, 150),
    (0, 30, None), (-100, 30, None), (None, 30, None), (100, None, None),
])
def test_annual_payout_ratio_uses_full_year_profit_and_report_year_dividends(profit, dividend, expected):
    reports = [{'period': '2025-06-30', 'profit_ytd': 20},
               {'period': '2025-12-31', 'profit_ytd': profit}]
    events = [{'report_period': '2025-06-30', 'date': '2025-08-20',
               'per_share': 1, 'total_shares': None if dividend is None else dividend / 3},
              {'report_period': '2025-12-31', 'date': '2026-06-05',
               'per_share': 2, 'total_shares': None if dividend is None else dividend / 3}]
    rows = build_period_rows(reports, [], [], dividend_events=events)
    annual = view_rows(rows, 'year')[0]
    assert annual['dividend_payout_ratio'] == expected
    for period in ('quarter', 'ttm'):
        assert all('dividend_payout_ratio' not in row for row in view_rows(rows, period))
    assert all('dividend_payout_ratio' not in row for row in rows)


def test_unloaded_dividends_do_not_produce_zero_payout_ratio():
    rows = build_period_rows([{'period': '2025-12-31', 'profit_ytd': 100}], [], [])
    assert view_rows(rows, 'year')[0]['dividend_payout_ratio'] is None
