import shutil
import subprocess
from pathlib import Path

import pytest

from quarterly_dashboard.core import build_period_rows, view_rows


def test_chart_renderer_appends_yoy_only_to_supported_metrics():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed to exercise the chart renderer")
    result = subprocess.run([node, str(Path(__file__).with_name("chart_hover.cjs"))],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr


def test_hover_yoy_compares_same_quarter_annual_and_ttm_without_mutating_source():
    reports = []
    for year, values in ((2024, (100, 300, 600, 1000)),
                         (2025, (120, 370, 790, 1340))):
        for month, revenue in zip(("03-31", "06-30", "09-30", "12-31"), values):
            reports.append({"period": f"{year}-{month}", "revenue_ytd": revenue,
                            "profit_ytd": revenue / 10,
                            "operating_cash_flow_ytd": revenue / 5,
                            "capex_ytd": revenue / 20})
    rows = build_period_rows(reports, [], [])
    quarters = view_rows(rows, "quarter")
    assert quarters[5]["yoy"]["revenue"] == "同比 +25.00%"
    assert quarters[5]["yoy"]["profit"] == "同比 +25.00%"
    assert quarters[5]["yoy"]["operating_cash_flow"] == "同比 +25.00%"
    assert quarters[5]["yoy"]["free_cash_flow"] == "同比 +25.00%"
    assert quarters[5]["yoy"]["capex"] == "同比 +25.00%"
    assert quarters[-1]["yoy"]["revenue"] == "同比 +37.50%"
    assert view_rows(rows, "year")[-1]["yoy"]["revenue"] == "同比 +34.00%"
    assert view_rows(rows, "ttm")[-1]["yoy"]["revenue"] == "同比 +34.00%"
    assert view_rows(rows, "ttm")[5]["yoy"]["revenue"] == "同比 —（缺少上年同期）"
    assert not {"market_cap", "qfq_price", "revenue_growth", "profit_growth",
                "roe", "gross_margin", "net_margin"}.intersection(quarters[5]["yoy"])
    assert all("yoy" not in row for row in rows + reports)


@pytest.mark.parametrize("previous,current,expected", [
    (100, 80, "同比 -20.00%"),
    (100, 100, "同比 0.00%"),
    (100, 0, "同比 -100.00%"),
    (0, 100, "同比 —（上年同期为零）"),
    (None, 100, "同比 —（缺少上年同期）"),
    (-10, 5, "同比 扭亏为盈"),
    (10, -5, "同比 转亏"),
    (-10, -5, "同比 亏损收窄"),
    (-10, -20, "同比 亏损扩大"),
    (-10, -10, "同比 0.00%"),
    (-10, 0, "同比 亏损归零"),
    (100, 99.999, "同比 0.00%"),
])
def test_profit_hover_yoy_handles_sign_zero_and_missing_base(previous, current, expected):
    reports = [{"period": "2024-03-31", "profit_ytd": previous},
               {"period": "2025-03-31", "profit_ytd": current}]
    row = view_rows(build_period_rows(reports, [], []), "quarter")[-1]
    assert row["yoy"]["profit"] == expected


def test_hover_yoy_does_not_use_adjacent_report_when_same_quarter_is_missing():
    reports = [{"period": "2024-03-31", "profit_ytd": 10},
               {"period": "2025-06-30", "profit_ytd": 20}]
    row = view_rows(build_period_rows(reports, [], []), "quarter")[-1]
    assert row["yoy"]["profit"] == "同比 —（本期数据缺失）"


def test_dividend_hover_yoy_uses_report_year_not_payment_year():
    reports = [{"period": "2024-12-31"}, {"period": "2025-12-31"}]
    events = [{"report_period": "2024-12-31", "date": "2025-06-01",
               "per_share": 1, "total_shares": 100},
              {"report_period": "2025-12-31", "date": "2026-06-01",
               "per_share": 1.2, "total_shares": 100}]
    row = view_rows(build_period_rows(reports, [], [], dividend_events=events), "year")[-1]
    assert row["yoy"]["cash_dividend"] == "同比 +20.00%（仅已实施口径）"


def test_signed_balance_and_cash_flow_hover_does_not_call_negative_values_losses():
    reports = [{"period": "2024-03-31", "operating_cash_flow_ytd": -10,
                "monetary_funds": 5},
               {"period": "2025-03-31", "operating_cash_flow_ytd": -5,
                "monetary_funds": 10}]
    for report in reports:
        report.update(short_term_borrowings=20, short_term_bonds=0,
                      current_noncurrent_liabilities=0, long_term_borrowings=0,
                      bonds_payable=0, lease_liabilities=0)
    row = view_rows(build_period_rows(reports, [], []), "quarter")[-1]
    assert row["yoy"]["operating_cash_flow"] == "同比 负值收窄"
    assert row["yoy"]["net_cash"] == "同比 负值收窄"
    assert row["yoy"]["interest_bearing_debt"] == "同比 0.00%"


@pytest.mark.parametrize("previous,current,expected", [
    (-10, 5, "同比 由负转正"),
    (10, -5, "同比 由正转负"),
    (-10, -20, "同比 负值扩大"),
    (-10, 0, "同比 负值归零"),
])
def test_cash_flow_sign_changes_are_not_profit_labels(previous, current, expected):
    reports = [{"period": "2024-03-31", "operating_cash_flow_ytd": previous},
               {"period": "2025-03-31", "operating_cash_flow_ytd": current}]
    row = view_rows(build_period_rows(reports, [], []), "quarter")[-1]
    assert row["yoy"]["operating_cash_flow"] == expected


def test_hover_yoy_does_not_substitute_an_older_year_for_missing_same_period():
    reports = [{"period": "2023-03-31", "profit_ytd": 10},
               {"period": "2025-03-31", "profit_ytd": 20}]
    row = view_rows(build_period_rows(reports, [], []), "quarter")[-1]
    assert row["yoy"]["profit"] == "同比 —（缺少上年同期）"
