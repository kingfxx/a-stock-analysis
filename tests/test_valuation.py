import pytest

from quarterly_dashboard.valuation import (
    fetch_dividend_events, fetch_industry_snapshot, fetch_valuation_series,
    merge_dividend_yields, monthly_dividend_yields, monthly_valuation, valuation_summary,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def test_baidu_history_keeps_latest_negative_pe_for_monthly_gap():
    class Session:
        def get(self, url, params, **kwargs):
            values = [["2026-01-10", "10"], ["2026-01-31", "-2"]]
            return FakeResponse({"Result": [{"DisplayData": {"resultData": {"tplData": {
                "result": {"chartInfo": [{"body": values}]}}}}}]})

    series = fetch_valuation_series("601919", Session())
    assert monthly_valuation(series, [])[0]["pe"] is None


def test_industry_snapshot_selects_average_not_median():
    class Session:
        def get(self, url, params, **kwargs):
            return FakeResponse({"result": {"data": [
                {"CORRE_SECURITY_CODE": "行业中值", "PE_TTM": 12},
                {"CORRE_SECURITY_CODE": "行业平均", "PE_TTM": 19.2, "PB_MRQ": 1.7,
                 "PS_TTM": 3.1, "TOTAL_COUNT": 37, "REPORT_DATE": "2025-12-31 00:00:00"},
            ]}})

    industry = fetch_industry_snapshot("601919", Session())
    assert industry == {"pe": 19.2, "pb": 1.7, "ps": 3.1,
                        "peer_count": 37, "report_period": "2025-12-31"}


def test_dividend_events_convert_per_ten_shares_to_per_share():
    class Session:
        def get(self, url, params, **kwargs):
            return FakeResponse({"result": {"count": 2, "data": [
                {"EX_DIVIDEND_DATE": "2026-06-26 00:00:00", "PRETAX_BONUS_RMB": 4.4,
                 "ASSIGN_PROGRESS": "实施分配"},
                {"EX_DIVIDEND_DATE": None, "PRETAX_BONUS_RMB": 10,
                 "ASSIGN_PROGRESS": "预案"},
            ]}})

    events = fetch_dividend_events("601919", Session())
    assert events[0]["date"] == "2026-06-26"
    assert events[0]["per_share"] == pytest.approx(.44)
    assert len(events) == 1


def test_dividend_yield_uses_paid_cash_in_trailing_year_and_raw_month_close():
    prices = [{"date": "2026-01-30", "close": 10},
              {"date": "2026-02-27", "close": 20},
              {"date": "2026-03-31", "close": 20}]
    events = [{"date": "2025-02-01", "per_share": .5},
              {"date": "2026-02-15", "per_share": 1.0}]
    yields = monthly_dividend_yields(prices, events)
    assert yields == [{"date": "2026-01-30", "value": 5},
                      {"date": "2026-02-27", "value": 5},
                      {"date": "2026-03-31", "value": 5}]
    merged = merge_dividend_yields([{"date": "2026-02-28", "pe": 10}], yields)
    assert merged[1]["dividend_yield"] == 5
    assert merged[1]["dividend_yield_date"] == "2026-02-27"


def test_zero_dividend_yield_is_valid_but_has_no_industry_comparison():
    rows = [{"date": "2025-09-30", "dividend_yield": 0},
            {"date": "2026-09-24", "dividend_yield": 0,
             "dividend_yield_date": "2026-09-24"}]
    summary = valuation_summary(rows, "dividend_yield", 3, {}, as_of="2026-09-27")
    assert summary["count"] == 2
    assert summary["current"] == 0
    assert summary["percentile"] == 0
    assert summary["industry"] is None


def test_monthly_valuation_uses_latest_observation_and_only_published_revenue():
    reports = [
        {"period": "2024-09-30", "publish_date": "2024-10-30", "revenue_ytd": 75e8},
        {"period": "2024-12-31", "publish_date": "2025-03-20", "revenue_ytd": 100e8},
        {"period": "2025-03-31", "publish_date": "2025-04-25", "revenue_ytd": 30e8},
        {"period": "2025-06-30", "publish_date": "2025-08-30", "revenue_ytd": 70e8},
        {"period": "2025-09-30", "publish_date": "2025-10-30", "revenue_ytd": 105e8},
        {"period": "2025-12-31", "publish_date": "2026-03-25", "revenue_ytd": 140e8},
    ]
    series = {
        "pe": [{"date": "2026-02-10", "value": 8}, {"date": "2026-02-25", "value": 9},
               {"date": "2026-04-30", "value": 10}],
        "pb": [{"date": "2026-02-25", "value": 1.1}, {"date": "2026-04-30", "value": 1.2}],
        "market_cap": [{"date": "2026-02-25", "value": 130},
                       {"date": "2026-04-30", "value": 210}],
    }
    result = monthly_valuation(series, reports)
    assert result == [
        {"date": "2026-02-25", "pe": 9, "pb": 1.1, "ps": 1.0},
        {"date": "2026-04-30", "pe": 10, "pb": 1.2, "ps": 1.5},
    ]


def test_summary_uses_positive_multiples_and_selected_window():
    rows = [
        {"date": "2022-01-31", "pe": 1, "pb": 1, "ps": 1},
        {"date": "2024-01-31", "pe": -2, "pb": 2, "ps": 2},
        {"date": "2025-01-31", "pe": 10, "pb": 3, "ps": 3},
        {"date": "2026-01-31", "pe": 20, "pb": 4, "ps": 4},
    ]
    result = valuation_summary(rows, "pe", 3, {"pe": 15}, as_of="2026-09-27")
    assert result["count"] == 2
    assert result["current"] == 20
    assert result["high"] == 18
    assert result["median"] == 15
    assert result["low"] == 12
    assert result["percentile"] == 50
    assert result["industry"] == 15
    assert result["industry_relation"] == "higher"
