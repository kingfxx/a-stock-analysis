from quarterly_dashboard.sources import parse_financial_reports, parse_monthly_prices


def test_financial_parser_uses_report_period_and_disclosure_date():
    income = {"result": {"data": {"report_list": {"20250630": {
        "publish_date": "20250815", "update_time": 123,
        "data": [{"item_title": "营业收入", "item_value": "250.5"},
                 {"item_title": "归属于母公司所有者的净利润", "item_value": "45.2"}],
    }}}}}
    balance = {"result": {"data": {"report_list": {"20250630": {
        "data": [{"item_title": "实收资本(或股本)", "item_value": "100.0"},
                 {"item_title": "归属于母公司股东权益合计", "item_value": "500.0"}],
    }}}}}
    assert parse_financial_reports(income, balance) == [{
        "period": "2025-06-30", "publish_date": "2025-08-15",
        "revenue_ytd": 250.5, "profit_ytd": 45.2,
        "shares": 100.0, "equity": 500.0, "source_update_time": 123,
    }]


def test_monthly_parser_treats_empty_or_missing_data_as_failure():
    valid = {"code": 0, "data": {"sz300750": {"month": [
        ["2025-03-31", "1", "2", "3", "1", "100"]]}}}
    assert parse_monthly_prices(valid, "sz300750", "") == [{"date": "2025-03-31", "close": 2.0}]
    try:
        parse_monthly_prices({"code": 0, "data": {"sz300750": {"month": []}}}, "sz300750", "")
    except ValueError:
        pass
    else:
        raise AssertionError("empty monthly K-line must fail")
