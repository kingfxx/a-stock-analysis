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
    annual = view_rows(rows, "year")[0]
    assert annual["revenue"] == 70
    assert annual["profit"] == 14
    assert annual["operating_cash_flow"] is None


def test_cash_dividends_follow_report_period_not_payment_year():
    reports = [{"period": f"2025-{month}", "profit_ytd": index + 1}
               for index, month in enumerate(("03-31", "06-30", "09-30", "12-31"))]
    dividends = [
        {"report_period": "2025-06-30", "date": "2025-08-20", "per_share": 1,
         "total_shares": 100000000},
        {"report_period": "2025-12-31", "date": "2026-04-22", "per_share": 2,
         "total_shares": 100000000},
    ]
    rows = build_period_rows(reports, [], [], dividend_events=dividends)
    assert [row["cash_dividend"] for row in view_rows(rows, "quarter")] == [0, 100000000, 0, 200000000]
    annual = view_rows(rows, "year")[0]
    assert annual["cash_dividend"] == 300000000
    assert "2026-04-22" in annual["cash_dividend_details"]
    assert view_rows(rows, "ttm")[-1]["cash_dividend"] == 300000000


def test_cash_dividend_missing_share_base_is_unknown_not_zero():
    reports = [{"period": "2025-12-31"}]
    dividends = [{"report_period": "2025-12-31", "date": "2026-04-22",
                  "per_share": 2, "total_shares": None}]
    row = view_rows(build_period_rows(reports, [], [], dividend_events=dividends), "year")[0]
    assert row["cash_dividend"] is None


def test_missing_previous_cumulative_report_does_not_invent_quarter():
    reports = [
        {"period": "2025-03-31", "publish_date": "2025-04-25", "revenue_ytd": 10, "profit_ytd": 2, "shares": None, "equity": None},
        {"period": "2025-09-30", "publish_date": "2025-10-25", "revenue_ytd": 45, "profit_ytd": 9, "shares": None, "equity": None},
    ]
    rows = build_period_rows(reports, [], [])
    assert rows[1]["revenue_quarter"] is None
    assert rows[1]["profit_ttm"] is None
    assert view_rows(rows, "quarter")[1]["revenue"] is None


def test_growth_roe_and_cash_flows_use_comparable_periods():
    reports = []
    for year, data in ((2024, [(100, 10, 12, 2, 80), (220, 24, 28, 5, 84),
                               (360, 39, 45, 8, 90), (520, 56, 64, 12, 100)]),
                       (2025, [(120, 15, 18, 3, 110), (270, 33, 38, 7, 120),
                               (450, 54, 60, 10, 130), (650, 78, 87, 16, 140)])):
        for month, (revenue, profit, cash, capex, equity) in zip((3, 6, 9, 12), data):
            reports.append({"period": f"{year}-{month:02d}-{31 if month in (3, 12) else 30}",
                            "revenue_ytd": revenue, "profit_ytd": profit,
                            "operating_cash_flow_ytd": cash, "capex_ytd": capex, "equity": equity})
    rows = build_period_rows(reports, [], [])
    q2 = view_rows(rows, "quarter")[5]
    assert q2["operating_cash_flow"] == 20
    assert q2["free_cash_flow"] == 16
    assert q2["revenue_growth"] == 25
    assert q2["profit_growth"] == (18 / 14 - 1) * 100
    assert q2["roe"] == 65 / ((84 + 120) / 2) * 100
    annual = view_rows(rows, "year")[-1]
    assert annual["operating_cash_flow"] == 87
    assert annual["free_cash_flow"] == 71
    assert annual["revenue_growth"] == 25
    assert annual["roe"] == 78 / 120 * 100
    ttm = view_rows(rows, "ttm")[5]
    assert ttm["operating_cash_flow"] == 64 - 28 + 38
    assert ttm["revenue_growth"] is None  # No earlier complete four-quarter window.


def test_negative_prior_profit_and_missing_cash_flow_do_not_make_ratios():
    reports = [{"period": "2024-03-31", "revenue_ytd": 10, "profit_ytd": -2, "equity": 10},
               {"period": "2025-03-31", "revenue_ytd": 15, "profit_ytd": 3, "equity": 12}]
    rows = view_rows(build_period_rows(reports, [], []), "quarter")
    assert rows[1]["revenue_growth"] == 50
    assert rows[1]["profit_growth"] is None
    assert rows[1]["operating_cash_flow"] is None
    assert rows[1]["free_cash_flow"] is None
    assert rows[1]["roe"] is None


def test_margins_capex_and_balance_sheet_debt_keep_period_basis():
    reports = [
        {"period": "2025-03-31", "revenue_ytd": 100, "operating_cost_ytd": 60,
         "net_profit_ytd": 20, "capex_ytd": 8, "monetary_funds": 80,
         "short_term_borrowings": 10, "short_term_bonds": None,
         "current_noncurrent_liabilities": 5, "long_term_borrowings": 20,
         "bonds_payable": None, "lease_liabilities": 5},
        {"period": "2025-06-30", "revenue_ytd": 250, "operating_cost_ytd": 170,
         "net_profit_ytd": 35, "capex_ytd": 20, "monetary_funds": 30,
         "short_term_borrowings": 15, "short_term_bonds": None,
         "current_noncurrent_liabilities": 10, "long_term_borrowings": 20,
         "bonds_payable": None, "lease_liabilities": 5},
    ]
    rows = build_period_rows(reports, [], [])
    q2 = view_rows(rows, "quarter")[1]
    assert q2["gross_margin"] == 100 * (150 - 110) / 150
    assert q2["net_margin"] == 10
    assert q2["capex"] == 12
    assert q2["interest_bearing_debt"] == 50
    assert q2["net_cash"] == -20
    assert view_rows(rows, "ttm")[1]["gross_margin"] is None
    assert view_rows(rows, "ttm")[1]["capex"] is None


def test_missing_debt_component_or_zero_revenue_does_not_invent_metric():
    reports = [{"period": "2025-03-31", "revenue_ytd": 0,
                "operating_cost_ytd": 0, "net_profit_ytd": 2,
                "monetary_funds": 10, "short_term_borrowings": 3}]
    row = view_rows(build_period_rows(reports, [], []), "quarter")[0]
    assert row["gross_margin"] is None
    assert row["net_margin"] is None
    assert row["interest_bearing_debt"] is None
    assert row["net_cash"] is None


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
