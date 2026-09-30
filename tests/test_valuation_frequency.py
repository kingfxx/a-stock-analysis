"""Display aggregation must retain real observation dates and stable percentiles."""

from quarterly_dashboard.valuation import (aggregate_valuation_rows,
                                           valuation_observation_rows,
                                           valuation_summary_observations)


def test_weekly_aggregation_keeps_each_metric_actual_date_and_open_period():
    rows = [
        {"date": "2026-09-28", "pe": 10, "pe_date": "2026-09-28", "pb": 2, "pb_date": "2026-09-28"},
        {"date": "2026-09-29", "pe": 11, "pe_date": "2026-09-29", "pb": None},
        {"date": "2026-09-30", "pe": None, "pb": 3, "pb_date": "2026-09-30"},
    ]
    weekly = aggregate_valuation_rows(rows, "week", as_of="2026-09-30")
    assert len(weekly) == 1
    assert weekly[0]["date"] == "2026-09-30"
    assert weekly[0]["pe"] == 11 and weekly[0]["pe_date"] == "2026-09-29"
    assert weekly[0]["pb"] == 3 and weekly[0]["pb_date"] == "2026-09-30"
    assert weekly[0]["period_complete"] is False


def test_sparse_history_uses_same_monthly_percentiles_for_all_display_frequencies():
    rows = [
        {"date": "2022-01-05", "pe": 10, "pe_date": "2022-01-05"},
        {"date": "2022-02-05", "pe": 20, "pe_date": "2022-02-05"},
        {"date": "2026-09-29", "pe": 30, "pe_date": "2026-09-29"},
        {"date": "2026-09-30", "pe": 40, "pe_date": "2026-09-30"},
    ]
    summary = valuation_summary_observations(rows, "pe", 5, {}, as_of="2026-09-30")
    assert summary["sample_frequency"] == "month"
    assert summary["count"] == 3
    assert summary["frequency"] == "week"
    assert len(summary["rows_by_frequency"]["day"]) == 4
    assert len(summary["rows_by_frequency"]["month"]) == 3
    assert summary["percentile"] == 66.7
    assert summary["current_date"] == "2026-09-30"


def test_ps_uses_revenue_disclosed_on_each_cap_observation_date():
    reports = [
        {"period": "2024-09-30", "publish_date": "2024-10-30", "revenue_ytd": 75e8},
        {"period": "2024-12-31", "publish_date": "2025-03-20", "revenue_ytd": 100e8},
        {"period": "2025-03-31", "publish_date": "2025-04-25", "revenue_ytd": 30e8},
        {"period": "2025-06-30", "publish_date": "2025-08-30", "revenue_ytd": 70e8},
        {"period": "2025-09-30", "publish_date": "2025-10-30", "revenue_ytd": 105e8},
        {"period": "2025-12-31", "publish_date": "2026-03-25", "revenue_ytd": 140e8},
    ]
    series = {"pe": [], "pb": [], "market_cap": [
        {"date": "2026-02-25", "value": 130},
        {"date": "2026-04-30", "value": 210}]}
    rows = valuation_observation_rows(series, reports)
    assert [(row["date"], row["ps"], row["ps_date"]) for row in rows] == [
        ("2026-02-25", 1.0, "2026-02-25"),
        ("2026-04-30", 1.5, "2026-04-30")]


def test_daily_view_uses_known_trading_dates_but_preserves_weekend_fact():
    rows = [{"date": "2026-09-26", "pe": 10},
            {"date": "2026-09-28", "pe": 11},
            {"date": "2026-09-29", "pe": 12}]
    summary = valuation_summary_observations(rows, "pe", 3, {}, as_of="2026-09-30",
                                             trading_dates=["2026-09-28", "2026-09-29"])
    assert [row["date"] for row in summary["rows_by_frequency"]["day"]] == [
        "2026-09-28", "2026-09-29"]
    assert summary["sample_frequency"] == "trading_day"
    assert summary["count"] == 2
