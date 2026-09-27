from quarterly_dashboard.core import build_period_rows, disclosure_reference_snapshots, disclosure_snapshots, view_rows


def test_quarterly_differences_and_ttm_require_contiguous_reports():
    reports = [
        {"period": "2025-03-31", "publish_date": "2025-04-25", "revenue_ytd": 10, "profit_ytd": 2, "shares": 100, "equity": 50},
        {"period": "2025-06-30", "publish_date": "2025-08-15", "revenue_ytd": 25, "profit_ytd": 5, "shares": 100, "equity": 55},
        {"period": "2025-09-30", "publish_date": "2025-10-25", "revenue_ytd": 45, "profit_ytd": 9, "shares": 100, "equity": 60},
        {"period": "2025-12-31", "publish_date": "2026-04-01", "revenue_ytd": 70, "profit_ytd": 14, "shares": 100, "equity": 70},
    ]
    raw = [{"publish_date": "2025-04-25", "date": "2025-04-25", "close": 3},
           {"publish_date": "2026-04-01", "date": "2026-04-01", "close": 4}]
    adjusted = [{"publish_date": "2026-04-01", "date": "2026-04-01", "close": 3.8}]
    rows = build_period_rows(reports, raw, adjusted)
    assert [r["revenue_quarter"] for r in rows] == [10, 15, 20, 25]
    assert [r["profit_quarter"] for r in rows] == [2, 3, 4, 5]
    assert rows[-1]["revenue_ttm"] == 70
    assert rows[-1]["profit_ttm"] == 14
    assert rows[-1]["market_cap"] == 400
    assert rows[-1]["qfq_price"] == 3.8
    assert rows[1]["market_cap"] is None
    assert view_rows(rows, "year") == [{**rows[-1], "revenue": 70, "profit": 14}]


def test_missing_previous_cumulative_report_does_not_invent_quarter():
    reports = [
        {"period": "2025-03-31", "publish_date": "2025-04-25", "revenue_ytd": 10, "profit_ytd": 2, "shares": None, "equity": None},
        {"period": "2025-09-30", "publish_date": "2025-10-25", "revenue_ytd": 45, "profit_ytd": 9, "shares": None, "equity": None},
    ]
    rows = build_period_rows(reports, [], [])
    assert rows[1]["revenue_quarter"] is None
    assert rows[1]["profit_ttm"] is None
    assert view_rows(rows, "quarter")[1]["revenue"] is None


def test_weekend_disclosure_uses_last_trading_close_and_never_quarter_end():
    reports = [{"period": "2026-06-30", "publish_date": "2026-08-29",
                "revenue_ytd": 100, "profit_ytd": 10, "shares": 100, "equity": 50}]
    raw = disclosure_snapshots(reports, [
        {"date": "2026-06-30", "close": 12},
        {"date": "2026-08-28", "close": 16.88},
        {"date": "2026-08-31", "close": 17},
    ])
    qfq = disclosure_snapshots(reports, [{"date": "2026-08-28", "close": 15.2}])
    row = build_period_rows(reports, raw, qfq)[0]
    assert row["price_date"] == "2026-08-28"
    assert row["qfq_price_date"] == "2026-08-28"
    assert row["market_cap"] == 1688
    assert row["qfq_price"] == 15.2


def test_disclosure_without_recent_trading_price_is_missing():
    reports = [{"publish_date": "2026-08-29"}]
    assert disclosure_snapshots(reports, [{"date": "2026-06-30", "close": 12}]) == []


def test_negative_adjusted_close_is_not_treated_as_missing():
    reports = [{"period": "2013-09-30", "publish_date": "2013-10-31",
                "revenue_ytd": 10, "profit_ytd": 1, "shares": 100, "equity": 50}]
    raw = [{"publish_date": "2013-10-31", "date": "2013-10-31", "close": 3.13}]
    adjusted = [{"publish_date": "2013-10-31", "date": "2013-10-31", "close": -5.152}]
    row = build_period_rows(reports, raw, adjusted)[0]
    assert row["qfq_price"] == -5.152
    assert row["qfq_price_date"] == "2013-10-31"


def test_long_trading_gap_keeps_last_close_as_separate_reference():
    reports = [
        {"period": "2015-06-30", "publish_date": "2015-08-28", "shares": 100},
        {"period": "2015-09-30", "publish_date": "2015-10-29", "shares": 100},
    ]
    raw_daily = [{"date": "2015-08-07", "close": 11.63},
                 {"date": "2015-12-25", "close": 11.07}]
    qfq_daily = [{"date": "2015-08-07", "close": 1.386},
                 {"date": "2015-12-25", "close": 0.9}]
    assert disclosure_snapshots(reports, raw_daily) == []
    raw_refs = disclosure_reference_snapshots(reports, raw_daily)
    qfq_refs = disclosure_reference_snapshots(reports, qfq_daily)
    assert [(p["publish_date"], p["date"], p["lag_days"]) for p in raw_refs] == [
        ("2015-08-28", "2015-08-07", 21),
        ("2015-10-29", "2015-08-07", 83),
    ]
    rows = build_period_rows(reports, [], [], raw_refs, qfq_refs)
    assert [r["qfq_price"] for r in rows] == [None, None]
    assert [r["qfq_price_reference"] for r in rows] == [1.386, 1.386]
    assert [r["market_cap_reference"] for r in rows] == [1163, 1163]
    assert disclosure_reference_snapshots(reports, raw_daily[:1]) == []
