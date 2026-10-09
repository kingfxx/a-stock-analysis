"""Season-end market caps must use season-end prices and that report's shares."""

from quarterly_dashboard.core import period_end_snapshots
from quarterly_dashboard.price_projection import financial_prices
from quarterly_dashboard.server import _financial_payload, REPORT_DATE_BASIS


def test_period_end_cap_is_independent_of_disclosure_and_adjusted_prices():
    reports = [
        {"period": "2023-12-31", "publish_date": "2024-04-20", "shares": 100},
        {"period": "2024-03-31", "publish_date": "2024-04-25", "shares": 200},
    ]
    raw = [{"date": day, "close": close} for day, close in [
        ("2023-12-29", 10), ("2024-03-29", 12), ("2024-04-01", 50),
        ("2024-04-19", 20), ("2024-04-25", 30),
    ]]
    data = financial_prices({"code": "600000", "reports": reports, "report_date_basis": REPORT_DATE_BASIS}, raw,
                            [{"date": "2024-03-29", "close": 999}])
    payload = _financial_payload(data)
    rows = payload["views"]["quarter"]
    assert [r["period_end_market_cap"] for r in rows] == [1000, 2400]
    assert [r["period_end_price_date"] for r in rows] == ["2023-12-29", "2024-03-29"]
    assert [r["market_cap"] for r in rows] == [2000, 6000]
    assert payload["views"]["year"][0]["period_end_market_cap"] == 1000
    assert [r["period_end_market_cap"] for r in payload["views"]["ttm"]] == [1000, 2400]


def test_missing_shares_prices_and_long_gaps_stay_empty_but_zero_is_kept():
    reports = [
        {"period": "2024-03-31", "shares": 100},
        {"period": "2024-06-30", "shares": 100},
        {"period": "2024-09-30", "shares": None},
        {"period": "2024-12-31", "shares": 0},
    ]
    raw = [{"date": "2024-04-01", "close": 10},
           {"date": "2024-06-14", "close": 11},
           {"date": "2024-09-30", "close": 12},
           {"date": "2024-12-31", "close": 13}]
    data = financial_prices({"code": "600000", "reports": reports, "report_date_basis": REPORT_DATE_BASIS}, raw, [])
    rows = _financial_payload(data)["views"]["quarter"]
    assert [r["period_end_market_cap"] for r in rows] == [None, None, None, 0]
    assert rows[0]["period_end_price_date"] is None
    assert rows[1]["period_end_price_date"] is None
    assert period_end_snapshots(reports, []) == []
    assert period_end_snapshots([], raw) == []
    assert financial_prices(data, [], [])["prices"]["raw_period_end"] == []
