"""Display aggregation must retain real observation dates and stable percentiles."""

import pytest

from quarterly_dashboard import server
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
    summary = valuation_summary_observations(rows, "pe", 10, {}, as_of="2026-09-30")
    assert summary["sample_frequency"] == "month"
    assert summary["count"] == 3
    assert summary["frequency"] == "month"
    assert len(summary["rows_by_frequency"]["day"]) == 4
    assert len(summary["rows_by_frequency"]["month"]) == 3
    assert summary["percentile"] == 66.7
    assert summary["current_date"] == "2026-09-30"


@pytest.mark.parametrize("metric", ["pe", "pb", "ps"])
@pytest.mark.parametrize("dense", [False, True])
def test_five_year_percentiles_always_use_last_valid_observation_each_week(metric, dense):
    rows = [{"date": day, metric: value} for day, value in (
        ("2026-09-07", 10), ("2026-09-08", 40),
        ("2026-09-14", 20), ("2026-09-15", 30))]
    trades = [row["date"] for row in rows]
    if not dense:
        trades.append("2026-09-16")
    summary = valuation_summary_observations(rows, metric, 5, {}, as_of="2026-09-30",
                                             trading_dates=trades)
    assert summary["sample_frequency"] == summary["percentile_frequency"] == "week"
    assert summary["count"] == summary["sample_count"] == 2
    assert summary["percentile"] == 0
    assert summary["high"] == 38
    assert summary["median"] == 35
    assert summary["low"] == 32
    assert summary["current_date"] == "2026-09-15"


def test_weekly_percentiles_skip_invalid_and_nontrading_observations_without_filling_gaps():
    rows = [{"date": day, "pe": value} for day, value in (
        ("2026-09-07", 10), ("2026-09-08", -1),
        ("2026-09-20", 999), ("2026-09-21", 20), ("2026-09-22", None))]
    summary = valuation_summary_observations(rows, "pe", 5, {}, as_of="2026-09-30",
        trading_dates=["2026-09-07", "2026-09-08", "2026-09-14", "2026-09-21", "2026-09-22"])
    assert summary["count"] == 2
    assert summary["percentile"] == 50
    assert summary["median"] == 15


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


def test_price_overlay_distinguishes_whole_week_closure_from_missing_trading_day_price():
    data = {"observation_rows": [{"date": day, "pe": 6} for day in (
        "2026-02-13", "2026-02-15", "2026-02-22", "2026-02-24", "2026-02-27", "2026-03-01")]}
    raw = [{"date": day, "close": 15} for day in (
        "2026-02-13", "2026-02-24", "2026-02-27")]
    qfq = [{"date": "2026-02-13", "close": 13.87},
           {"date": "2026-02-27", "close": 14.57}]
    response = server._p4_valuation_payload(data, raw=raw, qfq=qfq)
    views = response["views"]["3"]["pe"]["rows_by_frequency"]
    weekly = {row["date"]: row for row in views["week"]}
    assert weekly["2026-02-15"]["qfq_close"] == 13.87
    assert weekly["2026-02-22"]["qfq_close"] is None
    assert weekly["2026-02-22"]["qfq_no_trades"] is True
    assert weekly["2026-03-01"]["qfq_close"] == 14.57
    daily = {row["date"]: row for row in views["day"]}
    assert daily["2026-02-24"]["qfq_close"] is None
    assert daily["2026-02-24"]["qfq_no_trades"] is False
    unknown_trades = server._p4_valuation_payload(data, qfq=qfq)
    assert all("qfq_no_trades" not in row for row in
               unknown_trades["views"]["3"]["pe"]["rows_by_frequency"]["week"])


@pytest.mark.parametrize("metric", ["pe", "pb", "ps"])
def test_sparse_valuation_dates_use_independent_period_end_prices(metric):
    data = {"observation_rows": [{"date": "2026-09-06", metric: 10},
                                 {"date": "2026-09-13", metric: 20}]}
    raw = [{"date": day, "close": 10} for day in (
        "2026-09-04", "2026-09-11", "2026-09-30")]
    qfq = [{"date": "2026-09-04", "close": -2},
           {"date": "2026-09-11", "close": 0},
           {"date": "2026-09-30", "close": 3}]
    response = server._p4_valuation_payload(data, raw=raw, qfq=qfq)
    summary = response["views"]["5"][metric]
    weekly = summary["rows_by_frequency"]["week"]
    assert [(row["qfq_close"], row["qfq_close_date"]) for row in weekly] == [
        (-2, "2026-09-04"), (0, "2026-09-11")]
    monthly = summary["rows_by_frequency"]["month"]
    assert monthly[0]["qfq_close"] == 3
    assert monthly[0]["qfq_close_date"] == "2026-09-30"
    reference = valuation_summary_observations(data["observation_rows"], metric, 5, {},
                                               trading_dates=[row["date"] for row in raw])
    for key in ("current", "current_date", "count", "percentile", "high", "median", "low"):
        assert summary[key] == reference[key]
    assert all("qfq_close" not in row for row in data["observation_rows"])


def test_period_end_price_missing_on_known_trade_does_not_use_earlier_close():
    data = {"observation_rows": [{"date": "2026-09-30", "pe": 10}]}
    raw = [{"date": "2026-09-29", "close": 10}, {"date": "2026-09-30", "close": 11}]
    qfq = [{"date": "2026-09-29", "close": 9}]
    response = server._p4_valuation_payload(data, raw=raw, qfq=qfq)
    for frequency in ("day", "week", "month"):
        row = response["views"]["3"]["pe"]["rows_by_frequency"][frequency][0]
        assert row["qfq_close"] is None
        assert row["qfq_close_date"] is None
        assert row["qfq_no_trades"] is False


def test_negative_pe_ranges_keep_raw_values_and_do_not_change_chart_or_percentiles():
    series = {"pe": [{"date": day, "value": value} for day, value in (
        ("2025-07-31", 225.51), ("2025-08-21", 254.93), ("2025-08-22", -144.76),
        ("2025-09-30", -299.55), ("2026-02-27", -591.08), ("2026-02-28", 84.35))]}
    rows = valuation_observation_rows(series, [])
    assert rows[2]["pe"] is None and rows[2]["pe_raw"] == -144.76
    baseline = [{key: value for key, value in row.items() if key != "pe_raw"} for row in rows]
    payload = server._p4_valuation_payload({"observation_rows": rows})
    for years in (3, 5, 10):
        summary = payload["views"][str(years)]["pe"]
        assert summary["negative_pe_ranges"] == [{"start": "2025-08-22", "end": "2026-02-27"}]
        reference = valuation_summary_observations(baseline, "pe", years, {})
        for key in ("count", "percentile", "high", "median", "low", "current", "current_date"):
            assert summary[key] == reference[key]
        for frequency in ("day", "week", "month"):
            assert summary["rows_by_frequency"][frequency] == reference["rows_by_frequency"][frequency]
        assert payload["views"][str(years)]["pb"]["negative_pe_ranges"] == []


def test_negative_pe_ranges_clip_to_selected_window_and_distinguish_missing_and_zero():
    series = {"pe": [{"date": day, "value": value} for day, value in (
        ("2022-01-01", -10), ("2025-01-01", -20), ("2025-02-01", None),
        ("2025-03-01", -30), ("2025-04-01", 0), ("2025-05-01", -40),
        ("2025-06-01", 10), ("2027-01-01", -50))],
        "pb": [{"date": "2025-01-15", "value": 2}]}
    rows = valuation_observation_rows(series, [])
    summary = valuation_summary_observations(rows, "pe", 3, {}, as_of="2026-10-01")
    assert summary["negative_pe_ranges"] == [
        {"start": day, "end": day} for day in ("2025-01-01", "2025-03-01", "2025-05-01")]
    summary = valuation_summary_observations(rows, "pe", 10, {}, as_of="2026-10-01")
    assert summary["negative_pe_ranges"][0] == {"start": "2022-01-01", "end": "2025-01-01"}
