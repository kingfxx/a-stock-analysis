from quarterly_dashboard.sources import (normalize_report_dates, parse_cash_flow_reports,
                                         parse_financial_reports, parse_monthly_prices, parse_stock_name)


def test_cash_flow_parser_keeps_cumulative_cash_and_capex_by_period():
    payload = {"result": {"data": {"report_list": {"20250630": {"data": [
        {"item_title": "经营活动产生的现金流量净额", "item_value": "1,000.5"},
        {"item_title": "销售商品、提供劳务收到的现金", "item_value": "2,500"},
        {"item_title": "购建固定资产、无形资产和其他长期资产所支付的现金", "item_value": "200.25"},
    ]}}}}}
    assert parse_cash_flow_reports(payload) == {
        "2025-06-30": {"operating_cash_flow_ytd": 1000.5, "main_business_cash_flow_ytd": 2500, "capex_ytd": 200.25}}


def test_financial_parser_uses_report_period_and_disclosure_date():
    income = {"result": {"data": {"report_list": {"20250630": {
        "publish_date": "20250815", "update_time": 123,
        "data": [{"item_title": "营业收入", "item_value": "250.5"},
                 {"item_title": "营业成本", "item_value": "150.5"},
                 {"item_title": "净利润", "item_value": "50.0"},
                 {"item_title": "归属于母公司所有者的净利润", "item_value": "45.2"}],
    }}}}}
    balance = {"result": {"data": {"report_list": {"20250630": {
        "data": [{"item_title": "实收资本(或股本)", "item_value": "100.0"},
                 {"item_title": "归属于母公司股东权益合计", "item_value": "500.0"},
                 {"item_title": "货币资金", "item_value": "300.0"},
                 {"item_title": "短期借款", "item_value": "10.0"},
                 {"item_title": "应付短期债券", "item_value": None},
                 {"item_title": "一年内到期的非流动负债", "item_value": "5.0"},
                 {"item_title": "长期借款", "item_value": "20.0"},
                 {"item_title": "应付债券", "item_value": None},
                 {"item_title": "租赁负债", "item_value": "3.0"}],
    }}}}}
    assert parse_financial_reports(income, balance) == [{
        "period": "2025-06-30", "publish_date": "2025-08-15",
        "source_publish_date": "2025-08-15",
        "revenue_ytd": 250.5, "profit_ytd": 45.2,
        "operating_cost_ytd": 150.5, "net_profit_ytd": 50.0,
        "monetary_funds": 300.0,
        "profit_before_tax_ytd": None, "income_tax_expense_ytd": None,
        "interest_expense_ytd": None, "non_operating_interest_income_ytd": None,
        "total_equity": None,
        "short_term_borrowings": 10.0, "short_term_bonds": None,
        "current_noncurrent_liabilities": 5.0, "long_term_borrowings": 20.0,
        "bonds_payable": None, "lease_liabilities": 3.0,
        "shares": 100.0, "equity": 500.0, "source_update_time": 123,
    }]


def test_roic_parser_uses_consolidated_equity_and_financing_interest_not_operating_interest():
    income = {"result": {"data": {"report_list": {"20251231": {"data": [
        {"item_title": "利润总额", "item_value": "100"},
        {"item_title": "所得税费用", "item_value": "20"},
        {"item_title": "利息收入", "item_field": "INTEINCO", "item_value": "40"},
        {"item_title": "财务费用", "item_value": "-5"},
        {"item_title": "利息收入", "item_field": "INTEINCOOPCOST", "item_value": "2"},
        {"item_title": "利息费用", "item_field": "INTERESTEXPENSE", "item_value": "-10"},
        {"item_title": "利息支出", "item_value": "90"},
    ]}}}}}
    balance = {"result": {"data": {"report_list": {"20251231": {"data": [
        {"item_title": "归属于母公司股东权益合计", "item_value": "500"},
        {"item_title": "所有者权益(或股东权益)合计", "item_value": "600"},
    ]}}}}}
    row = parse_financial_reports(income, balance)[0]
    assert row['profit_before_tax_ytd'] == 100
    assert row['income_tax_expense_ytd'] == 20
    assert row['interest_expense_ytd'] == 10
    assert row['non_operating_interest_income_ytd'] == 2
    assert row['total_equity'] == 600
    income['result']['data']['report_list']['20251231']['data'][4]['item_value'] = None
    assert parse_financial_reports(income, balance)[0]['non_operating_interest_income_ytd'] is None


def test_shifted_sina_dates_use_previous_year_same_period_record():
    reports = [
        {"period": "2020-12-31", "publish_date": "2022-04-22"},
        {"period": "2021-12-31", "publish_date": "2023-03-10"},
        {"period": "2022-12-31", "publish_date": "2024-03-16"},
    ]
    corrected = normalize_report_dates(reports)
    assert corrected[0]["publish_date"] is None
    assert corrected[1]["publish_date"] == "2022-04-22"
    assert corrected[1]["source_publish_date"] == "2023-03-10"
    assert corrected[2]["publish_date"] == "2023-03-10"
    assert normalize_report_dates(corrected) == corrected


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


def test_stock_name_parser_checks_symbol_and_code():
    assert parse_stock_name('v_sh601919="1~中远海控~601919~12.00";', "sh601919") == "中远海控"
    try:
        parse_stock_name('v_sh601919="1~别的股票~600519~12.00";', "sh601919")
    except ValueError:
        pass
    else:
        raise AssertionError("mismatched quote code must fail")
