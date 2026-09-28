import importlib

import pytest


def calculator():
    return importlib.import_module("quarterly_dashboard.roic")


def period_values(**overrides):
    return {"profit_before_tax": 100, "income_tax_expense": 20,
            "interest_expense": 10, "non_operating_interest_income": 2,
            **overrides}


def balance(**overrides):
    return {"total_equity": 500, "interest_bearing_debt": 100,
            "monetary_funds": 100, **overrides}


def test_default_roic_matches_agreed_formula_and_averages_period_endpoints():
    beginning = balance()
    ending = balance(total_equity=580)
    assert calculator().calculate_roic(period_values(), beginning, ending) == pytest.approx(16)
    assert beginning == {"total_equity": 500, "interest_bearing_debt": 100, "monetary_funds": 100}


def test_excess_cash_overrides_all_cash_independently_at_each_endpoint():
    beginning = balance(excess_cash=50)
    ending = balance(total_equity=580, excess_cash=70)
    # Average capital is (550 + 610) / 2 = 580, NOPAT is 86.4.
    assert calculator().calculate_roic(period_values(), beginning, ending) == pytest.approx(14.8965517241)


def test_zero_excess_cash_means_no_cash_deduction():
    assert calculator().invested_capital(balance(excess_cash=0)) == 600


def test_excess_cash_can_be_supplied_without_total_monetary_funds():
    assert calculator().invested_capital(balance(monetary_funds=None, excess_cash=40)) == 560


def test_explicit_unknown_excess_cash_does_not_silently_fall_back_to_all_cash():
    assert calculator().invested_capital(balance(excess_cash=None)) is None


@pytest.mark.parametrize("adjustment,expected", [(20, 13.0370370370), (-20, 18.9629629630), (0, 16)])
def test_non_operating_adjustments_remove_gains_and_add_back_losses_before_tax(adjustment, expected):
    values = period_values(non_operating_adjustments=adjustment)
    assert calculator().calculate_roic(values, balance(), balance(total_equity=580)) == pytest.approx(expected)


def test_both_extension_hooks_can_be_used_together():
    values = period_values(non_operating_adjustments=20)
    assert calculator().calculate_roic(values, balance(excess_cash=50),
                                       balance(total_equity=580, excess_cash=70)) == pytest.approx(12.1379310345)


@pytest.mark.parametrize("field,value", [
    ("profit_before_tax", None), ("profit_before_tax", 0), ("profit_before_tax", -10),
    ("income_tax_expense", None), ("income_tax_expense", -1), ("income_tax_expense", 101),
    ("interest_expense", None), ("interest_expense", -10),
    ("non_operating_interest_income", None), ("non_operating_interest_income", -2),
    ("non_operating_adjustments", None), ("non_operating_adjustments", float("nan")),
    ("income_tax_expense", float("inf")),
])
def test_missing_or_invalid_profit_inputs_do_not_produce_roic(field, value):
    assert calculator().calculate_roic(period_values(**{field:value}), balance(), balance()) is None


@pytest.mark.parametrize("field,value", [
    ("total_equity", None), ("interest_bearing_debt", None), ("interest_bearing_debt", -1),
    ("monetary_funds", None), ("monetary_funds", -1), ("excess_cash", -1),
    ("excess_cash", float("inf")),
])
def test_missing_or_invalid_capital_inputs_do_not_produce_roic(field, value):
    assert calculator().calculate_roic(period_values(), balance(**{field:value}), balance()) is None


@pytest.mark.parametrize("cash", [600, 700])
def test_non_positive_average_invested_capital_does_not_produce_roic(cash):
    assert calculator().calculate_roic(period_values(), balance(monetary_funds=cash),
                                       balance(monetary_funds=cash)) is None


def test_negative_adjusted_operating_profit_remains_a_negative_return():
    values = period_values(non_operating_adjustments=120)
    assert calculator().calculate_roic(values, balance(), balance()) == pytest.approx(-1.92)


def reports_for_pipeline():
    from quarterly_dashboard.core import DEBT_COMPONENTS
    reports = []
    for year in (2024, 2025):
        for q, ending in enumerate(('03-31', '06-30', '09-30', '12-31'), 1):
            reports.append({'period': f'{year}-{ending}', 'total_equity': 500,
                            'monetary_funds': 100,
                            **{field: (100 if i == 0 else 0) for i, field in enumerate(DEBT_COMPONENTS)},
                            'profit_before_tax_ytd': 25*q, 'income_tax_expense_ytd': 5*q,
                            'interest_expense_ytd': 2.5*q, 'non_operating_interest_income_ytd': .5*q})
    return reports


@pytest.mark.parametrize('period', ['quarter', 'ttm', 'year'])
def test_roic_is_available_in_every_chart_period_without_hover_growth(period):
    from quarterly_dashboard.core import build_period_rows, view_rows
    rows = view_rows(build_period_rows(reports_for_pipeline(), [], []), period)
    assert rows[-1]['roic'] == pytest.approx(17.28)
    assert 'roic' not in rows[-1]['yoy']


def test_ttm_uses_window_tax_totals_and_same_quarter_capital_endpoints():
    from quarterly_dashboard.core import build_period_rows, view_rows
    reports = reports_for_pipeline()
    reports[5]['profit_before_tax_ytd'] = 100
    reports[5]['income_tax_expense_ytd'] = 35
    reports[1]['total_equity'] = 300
    reports[5]['total_equity'] = 700
    result = view_rows(build_period_rows(reports, [], []), 'quarter')[5]
    assert result['roic'] == pytest.approx(158 * .7 / 500 * 100)


def test_annual_does_not_require_interior_quarters_but_ttm_does():
    from quarterly_dashboard.core import build_period_rows, view_rows
    reports = [r for r in reports_for_pipeline() if r['period'].endswith('12-31')]
    rows = build_period_rows(reports, [], [])
    assert view_rows(rows, 'year')[-1]['roic'] == pytest.approx(17.28)
    assert view_rows(rows, 'ttm')[-1]['roic'] is None


@pytest.mark.parametrize('field', ['interest_expense_ytd', 'non_operating_interest_income_ytd', 'total_equity'])
def test_pipeline_does_not_replace_missing_roic_inputs_with_zero(field):
    from quarterly_dashboard.core import build_period_rows, view_rows
    reports = reports_for_pipeline()
    reports[-1][field] = None
    assert view_rows(build_period_rows(reports, [], []), 'year')[-1]['roic'] is None


@pytest.mark.parametrize('period', ['quarter', 'ttm', 'year'])
def test_pipeline_preserves_both_extension_hooks(period):
    from quarterly_dashboard.core import build_period_rows, view_rows
    reports = reports_for_pipeline()
    for r in reports:
        q = int(r['period'][5:7]) // 3
        r['excess_cash'] = 0
        r['non_operating_adjustments_ytd'] = 5*q
    result = view_rows(build_period_rows(reports, [], []), period)[-1]
    assert result['roic'] == pytest.approx((108-20)*.8/600*100)
    reports[-1]['non_operating_adjustments_ytd'] = None
    assert view_rows(build_period_rows(reports, [], []), period)[-1]['roic'] is None
