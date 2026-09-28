import json

from quarterly_dashboard import server


def test_existing_cache_gets_name_without_refetching_financial_data(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CACHE", tmp_path)
    path = tmp_path / "601919.json"
    path.write_text(json.dumps({"code": "601919", "reports": [], "prices": {"raw": [], "qfq": []},
                                "price_basis": "disclosure",
                                "updated_at": "2026-09-26T00:00:00+00:00"}), encoding="utf-8")
    monkeypatch.setattr(server, "fetch_stock_name", lambda code, session: "中远海控")
    monkeypatch.setattr(server, "fetch_financial_reports", lambda *args: (_ for _ in ()).throw(AssertionError("finance refetched")))
    monkeypatch.setattr(server, "fetch_cash_flow_reports", lambda *args: {})

    data = server.load_stock("601919")
    assert data["name"] == "中远海控"
    assert json.loads(path.read_text(encoding="utf-8"))["name"] == "中远海控"
    assert server.cached_stocks() == [{"code": "601919", "name": "中远海控"}]


def test_existing_cache_adds_cash_flow_without_refetching_prices(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CACHE", tmp_path)
    path = tmp_path / "600887.json"
    path.write_text(json.dumps({"code": "600887", "name": "伊利股份", "reports": [
        {"period": "2025-12-31", "publish_date": "2026-04-30", "revenue_ytd": 100}],
        "prices": {"raw": [], "qfq": []}, "price_basis": "disclosure",
        "price_reference_basis": server.PRICE_REFERENCE_BASIS,
        "report_date_basis": server.REPORT_DATE_BASIS,
        "financial_fields_basis": server.FINANCIAL_FIELDS_BASIS}), encoding="utf-8")
    monkeypatch.setattr(server, "fetch_financial_reports", lambda *args: (_ for _ in ()).throw(AssertionError("finance refetched")))
    monkeypatch.setattr(server, "fetch_daily_prices", lambda *args: (_ for _ in ()).throw(AssertionError("prices refetched")))
    monkeypatch.setattr(server, "fetch_cash_flow_reports", lambda *args: {
        "2025-12-31": {"operating_cash_flow_ytd": 30, "capex_ytd": 8}})

    data = server.load_stock("600887")
    assert data["reports"][0]["operating_cash_flow_ytd"] == 30
    assert data["reports"][0]["capex_ytd"] == 8
    assert json.loads(path.read_text(encoding="utf-8"))["cash_flow_basis"] == server.CASH_FLOW_BASIS


def test_existing_cache_backfills_new_report_fields_without_refetching_prices(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CACHE", tmp_path)
    path = tmp_path / "300750.json"
    path.write_text(json.dumps({"code": "300750", "name": "宁德时代", "reports": [
        {"period": "2025-12-31", "revenue_ytd": 100, "capex_ytd": 8}],
        "prices": {"raw": [], "qfq": []}, "price_basis": "disclosure",
        "price_reference_basis": server.PRICE_REFERENCE_BASIS,
        "report_date_basis": server.REPORT_DATE_BASIS,
        "cash_flow_basis": server.CASH_FLOW_BASIS,
        "financial_fields_basis": "sina_margin_debt_inputs_v1"}), encoding="utf-8")
    monkeypatch.setattr(server, "fetch_financial_reports", lambda *args: [{
        "period": "2025-12-31", "operating_cost_ytd": 60, "net_profit_ytd": 20,
        "monetary_funds": 30, "short_term_borrowings": 10,
        "short_term_bonds": None, "current_noncurrent_liabilities": 0,
        "long_term_borrowings": 5, "bonds_payable": None, "lease_liabilities": 2,
        "profit_before_tax_ytd": 100, "income_tax_expense_ytd": 20,
        "interest_expense_ytd": 10, "non_operating_interest_income_ytd": 2,
        "total_equity": 500}])
    monkeypatch.setattr(server, "fetch_daily_prices", lambda *args: (_ for _ in ()).throw(AssertionError("prices refetched")))
    monkeypatch.setattr(server, "fetch_cash_flow_reports", lambda *args: (_ for _ in ()).throw(AssertionError("cash refetched")))
    data = server.load_stock("300750")
    assert data["reports"][0]["capex_ytd"] == 8
    assert data["reports"][0]["operating_cost_ytd"] == 60
    assert data["reports"][0]["net_profit_ytd"] == 20
    assert data["reports"][0]["monetary_funds"] == 30
    assert data["reports"][0]["total_equity"] == 500
    assert data["reports"][0]["interest_expense_ytd"] == 10
    assert data["reports"][0]["non_operating_interest_income_ytd"] == 2
    assert data["financial_fields_basis"] == server.FINANCIAL_FIELDS_BASIS


def test_roic_migration_retains_user_hooks_and_rejects_losing_known_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CACHE", tmp_path)
    report = {"period": "2025-12-31", "total_equity": 500,
              "interest_expense_ytd": 10, "excess_cash": 0,
              "non_operating_adjustments_ytd": -20}
    old = {"code": "300750", "name": "宁德时代", "reports": [report],
           "prices": {"raw": [], "qfq": []}, "price_basis": "disclosure",
           "price_reference_basis": server.PRICE_REFERENCE_BASIS,
           "report_date_basis": server.REPORT_DATE_BASIS,
           "cash_flow_basis": server.CASH_FLOW_BASIS,
           "financial_fields_basis": "sina_margin_debt_inputs_v1"}
    path = tmp_path / '300750.json'
    path.write_text(json.dumps(old), encoding='utf-8')
    monkeypatch.setattr(server, 'fetch_financial_reports', lambda *args: [
        {'period': '2025-12-31', 'total_equity': 500, 'interest_expense_ytd': None}])
    result = server.load_stock('300750')
    assert result['reports'] == [report]
    assert '保留原缓存' in result['warnings'][0]
    assert json.loads(path.read_text(encoding='utf-8')) == old
    monkeypatch.setattr(server, 'fetch_financial_reports', lambda *args: [
        {'period': '2025-12-31', 'total_equity': 600, 'interest_expense_ytd': 10}])
    result = server.load_stock('300750')
    assert result['reports'][0]['total_equity'] == 600
    assert result['reports'][0]['excess_cash'] == 0
    assert result['reports'][0]['non_operating_adjustments_ytd'] == -20
    assert json.loads(path.with_suffix('.json.bak').read_text(encoding='utf-8')) == old


def test_roic_is_selectable_in_all_three_series_menus():
    from html.parser import HTMLParser

    class Menus(HTMLParser):
        def __init__(self):
            super().__init__()
            self.current = None
            self.options = {}

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == 'select':
                self.current = attrs.get('id')
            if tag == 'option' and self.current:
                self.options.setdefault(self.current, []).append(attrs.get('value'))

        def handle_endtag(self, tag):
            if tag == 'select':
                self.current = None

    menus = Menus()
    menus.feed(server.TEMPLATE)
    for control in ('bar-metric', 'bar-metric-2', 'line-metric'):
        assert 'roic' in menus.options[control]


def test_cash_flow_refresh_failure_retains_previous_cash_values(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CACHE", tmp_path)
    path = tmp_path / "600887.json"
    path.write_text(json.dumps({"code": "600887", "name": "伊利股份", "reports": [
        {"period": "2025-12-31", "operating_cash_flow_ytd": 30, "capex_ytd": 8}],
        "prices": {"raw": [], "qfq": []}, "cash_flow_basis": server.CASH_FLOW_BASIS}), encoding="utf-8")
    monkeypatch.setattr(server, "fetch_financial_reports", lambda *args: [{"period": "2025-12-31"}])
    monkeypatch.setattr(server, "fetch_cash_flow_reports", lambda *args: (_ for _ in ()).throw(ValueError("offline")))
    monkeypatch.setattr(server, "_disclosure_prices", lambda *args: ({"raw": [], "qfq": []}, [], True))
    monkeypatch.setattr(server, "fetch_stock_name", lambda *args: "伊利股份")

    data = server.load_stock("600887", refresh=True)
    assert data["reports"][0]["operating_cash_flow_ytd"] == 30
    assert data["reports"][0]["capex_ytd"] == 8
    assert "现金流量表获取失败" in data["warnings"][0]


def test_month_end_cache_migrates_to_disclosure_snapshots(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CACHE", tmp_path)
    monkeypatch.setattr(server, "fetch_cash_flow_reports", lambda *args: (_ for _ in ()).throw(ValueError("offline")))
    path = tmp_path / "601919.json"
    report = {"period": "2026-06-30", "publish_date": "2026-08-29"}
    path.write_text(json.dumps({"code": "601919", "name": "中远海控", "reports": [report],
                                "financial_fields_basis": server.FINANCIAL_FIELDS_BASIS,
                                "prices": {"raw": [{"date": "2026-06-30", "close": 12}], "qfq": []}}),
                    encoding="utf-8")
    monkeypatch.setattr(server, "fetch_financial_reports", lambda *args: (_ for _ in ()).throw(AssertionError("finance refetched")))
    monkeypatch.setattr(server, "fetch_daily_prices", lambda code, session, adjust, start, end: [
        {"date": "2026-08-28", "close": 16.88 if adjust == "" else 15.2}])

    data = server.load_stock("601919")
    assert data["prices"]["raw"] == [{"publish_date": "2026-08-29", "date": "2026-08-28", "close": 16.88}]
    assert data["price_basis"] == "disclosure"
    assert "现金流量表获取失败" in data["warnings"][0]
    assert json.loads(path.read_text(encoding="utf-8"))["price_basis"] == "disclosure"


def test_cache_migrates_shifted_report_dates_and_reprices(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CACHE", tmp_path)
    monkeypatch.setattr(server, "fetch_cash_flow_reports", lambda *args: {})
    path = tmp_path / "300750.json"
    path.write_text(json.dumps({"code": "300750", "name": "宁德时代", "price_basis": "disclosure",
                                "financial_fields_basis": server.FINANCIAL_FIELDS_BASIS,
                                "reports": [
                                    {"period": "2020-12-31", "publish_date": "2022-04-22"},
                                    {"period": "2021-12-31", "publish_date": "2023-03-10"}],
                                "prices": {"raw": [{"publish_date": "2023-03-10", "date": "2023-03-09", "close": 10}],
                                           "qfq": []}}), encoding="utf-8")
    monkeypatch.setattr(server, "fetch_financial_reports", lambda *args: (_ for _ in ()).throw(AssertionError("finance refetched")))
    monkeypatch.setattr(server, "fetch_daily_prices", lambda *args: [
        {"date": "2022-04-21", "close": 20}])

    data = server.load_stock("300750")
    assert data["reports"][1]["publish_date"] == "2022-04-22"
    assert data["prices"]["raw"] == [{"publish_date": "2022-04-22", "date": "2022-04-21", "close": 20}]
    assert data["report_date_basis"] == "sina_same_period_shift_v1"


def test_render_page_includes_cached_stock_options(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CACHE", tmp_path)
    monkeypatch.setattr(server, "VALUATION_CACHE", tmp_path / "valuation")
    (tmp_path / "601919.json").write_text(json.dumps({"code": "601919", "name": "中远海控"}), encoding="utf-8")
    page = server.render_page("601919", False)
    assert '"name": "中远海控"' in page
    assert '"cached_stocks": [{"code": "601919", "name": "中远海控"}]' in page


def test_dividend_events_are_cached_and_partial_refresh_keeps_history(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CACHE", tmp_path)
    stock = {"code": "601919", "reports": []}
    path = tmp_path / "601919.json"
    path.write_text(json.dumps(stock), encoding="utf-8")
    event = {"date": "2026-06-26", "report_period": "2025-12-31",
             "per_share": .44, "total_shares": 1000000000}
    monkeypatch.setattr(server, "fetch_dividend_events", lambda *args: [event])
    events, warnings = server.load_dividends(stock)
    assert events == [event] and not warnings
    assert json.loads(path.read_text(encoding="utf-8"))["dividend_events"] == [event]
    monkeypatch.setattr(server, "fetch_dividend_events", lambda *args: [])
    assert server.load_dividends(stock)[0] == [event]
    events, warnings = server.load_dividends(stock, refresh=True)
    assert events == [event]
    assert "保留原缓存" in warnings[0]


def test_empty_dividend_cache_retries_on_normal_open(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CACHE", tmp_path)
    stock = {"code": "601600", "reports": [], "dividend_events": [],
             "dividend_basis": server.DIVIDEND_BASIS}
    path = tmp_path / "601600.json"
    path.write_text(json.dumps(stock), encoding="utf-8")
    event = {"date": "2026-08-14", "report_period": "2025-12-31",
             "per_share": .147, "total_shares": 17154971327}
    monkeypatch.setattr(server, "fetch_dividend_events", lambda *args: [event])
    events, warnings = server.load_dividends(stock)
    assert events == [event] and not warnings
    assert json.loads(path.read_text(encoding="utf-8"))["dividend_events"] == [event]


def test_empty_dividend_response_does_not_mark_cache_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CACHE", tmp_path)
    stock = {"code": "601600", "reports": []}
    path = tmp_path / "601600.json"
    path.write_text(json.dumps(stock), encoding="utf-8")
    monkeypatch.setattr(server, "fetch_dividend_events", lambda *args: [])
    events, warnings = server.load_dividends(stock)
    assert events is None
    assert "空" in warnings[0]
    assert "dividend_basis" not in json.loads(path.read_text(encoding="utf-8"))


def test_render_page_includes_report_period_cash_dividend(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CACHE", tmp_path)
    monkeypatch.setattr(server, "VALUATION_CACHE", tmp_path / "valuation")
    monkeypatch.setattr(server, "cached_stocks", lambda: [])
    (tmp_path / "601919.json").write_text(json.dumps({
        "code": "601919", "name": "中远海控", "reports": [{"period": "2025-12-31"}],
        "prices": {"raw": [], "qfq": []}, "updated_at": "2026-09-27T00:00:00+00:00",
        "dividend_events": [{"date": "2026-06-26", "report_period": "2025-12-31",
                             "per_share": .44, "total_shares": 1000000000}]}), encoding="utf-8")
    page = server.render_page("601919", False)
    assert '"cash_dividend": 440000000.0' in page
    assert '2025-12-31 → 2026-06-26' in page


def test_valuation_cache_is_reused_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VALUATION_CACHE", tmp_path / "valuation")
    path = tmp_path / "valuation" / "601919.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"rows": [{"date": "2026-09-25", "pe": 9}],
                                "industry": {"pe": 12},
                                "updated_on": server.date.today().isoformat(),
                                "basis": server.VALUATION_BASIS, "warnings": []}),
                    encoding="utf-8")
    monkeypatch.setattr(server, "fetch_valuation_series", lambda *args: (_ for _ in ()).throw(AssertionError("network called")))
    assert server.load_valuation("601919", [])["rows"][0]["pe"] == 9


def test_dividend_source_failure_keeps_other_valuation_metrics(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VALUATION_CACHE", tmp_path / "valuation")
    monkeypatch.setattr(server.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(server, "fetch_valuation_series", lambda *args: {
        "pe": [{"date": "2026-09-24", "value": 9}], "pb": [], "market_cap": []})
    monkeypatch.setattr(server, "fetch_dividend_yields", lambda *args: (_ for _ in ()).throw(ValueError("unavailable")))
    monkeypatch.setattr(server, "fetch_monthly_prices", lambda *args: [{"date": "2026-09-25", "close": 8.5}])
    monkeypatch.setattr(server, "fetch_industry_snapshot", lambda *args: {"pe": 12})

    data = server.load_valuation("601919", [])
    assert data["rows"][0]["pe"] == 9
    assert data["rows"][0]["qfq_close"] == 8.5
    assert data["rows"][0].get("dividend_yield") is None
    assert "历史股息率获取失败" in data["warnings"][0]


def test_adjusted_price_failure_preserves_cached_monthly_prices(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VALUATION_CACHE", tmp_path / "valuation")
    monkeypatch.setattr(server.time, "sleep", lambda seconds: None)
    path = tmp_path / "valuation" / "601919.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"rows": [{"date": "2026-09-25", "qfq_close": 8.5,
                                         "qfq_close_date": "2026-09-24"}]}), encoding="utf-8")
    monkeypatch.setattr(server, "fetch_valuation_series", lambda *args: {"pe": [], "pb": [], "market_cap": []})
    monkeypatch.setattr(server, "fetch_dividend_yields", lambda *args: [])
    monkeypatch.setattr(server, "fetch_monthly_prices", lambda *args: (_ for _ in ()).throw(ValueError("unavailable")))
    monkeypatch.setattr(server, "fetch_industry_snapshot", lambda *args: {})

    data = server.load_valuation("601919", [])
    assert data["rows"][0]["qfq_close"] == 8.5
    assert "前复权月度股价获取失败" in data["warnings"][0]


def test_serve_opens_browser_after_binding_when_requested(monkeypatch):
    events = []

    class FakeServer:
        def __init__(self, address, handler):
            events.append(("bound", address))

        def serve_forever(self):
            events.append("serving")

        def server_close(self):
            events.append("closed")

    monkeypatch.setattr(server, "ThreadingHTTPServer", FakeServer)
    monkeypatch.setattr(server.webbrowser, "open", lambda url: events.append(("opened", url)))

    server.serve(8766, open_browser=True)
    assert events == [
        ("bound", ("127.0.0.1", 8766)),
        ("opened", "http://127.0.0.1:8766/"),
        "serving",
        "closed",
    ]


def test_existing_disclosure_cache_adds_long_gap_references(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CACHE", tmp_path)
    monkeypatch.setattr(server, "fetch_cash_flow_reports", lambda *args: {})
    path = tmp_path / "601919.json"
    reports = [{"period": "2015-06-30", "publish_date": "2015-08-28"},
               {"period": "2015-09-30", "publish_date": "2015-10-29"}]
    path.write_text(json.dumps({"code": "601919", "name": "中远海控", "reports": reports,
                                "financial_fields_basis": server.FINANCIAL_FIELDS_BASIS,
                                "price_basis": "disclosure", "report_date_basis": server.REPORT_DATE_BASIS,
                                "prices": {"raw": [], "qfq": []}}), encoding="utf-8")
    monkeypatch.setattr(server, "fetch_financial_reports", lambda *args: (_ for _ in ()).throw(AssertionError("finance refetched")))
    monkeypatch.setattr(server, "fetch_daily_prices", lambda code, session, adjust, start, end: [
        {"date": "2015-08-07", "close": 11.63 if adjust == "" else 1.386},
        {"date": "2015-12-25", "close": 11.07 if adjust == "" else 0.9},
    ])

    data = server.load_stock("601919")
    assert data["prices"]["raw"] == []
    assert [p["date"] for p in data["prices"]["raw_reference"]] == ["2015-08-07"] * 2
    assert [p["close"] for p in data["prices"]["qfq_reference"]] == [1.386] * 2
    assert json.loads(path.read_text(encoding="utf-8"))["price_reference_basis"] == server.PRICE_REFERENCE_BASIS


def test_partial_financial_refresh_keeps_last_good_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CACHE", tmp_path)
    path = tmp_path / "601919.json"
    previous = {"code": "601919", "name": "中远海控", "reports": [
        {"period": "2025-03-31", "revenue_ytd": 100},
        {"period": "2025-06-30", "revenue_ytd": 220}],
        "prices": {"raw": [], "qfq": []}, "updated_at": "old"}
    path.write_text(json.dumps(previous), encoding="utf-8")
    monkeypatch.setattr(server, "fetch_financial_reports", lambda *args: [
        {"period": "2025-06-30", "revenue_ytd": 230}])
    data = server.load_stock("601919", refresh=True)
    assert data["reports"] == previous["reports"]
    assert "未覆盖已有报告期" in data["warnings"][0]
    assert json.loads(path.read_text(encoding="utf-8")) == previous


def test_partial_cash_flow_refresh_keeps_last_good_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CACHE", tmp_path)
    path = tmp_path / "601919.json"
    previous = {"code": "601919", "name": "中远海控", "reports": [
        {"period": "2025-03-31", "revenue_ytd": 100,
         "operating_cash_flow_ytd": 30, "capex_ytd": 4},
        {"period": "2025-06-30", "revenue_ytd": 220,
         "operating_cash_flow_ytd": 60, "capex_ytd": 9}],
        "prices": {"raw": [], "qfq": []}, "updated_at": "old"}
    path.write_text(json.dumps(previous), encoding="utf-8")
    monkeypatch.setattr(server, "fetch_financial_reports", lambda *args: [
        {"period": p["period"], "revenue_ytd": p["revenue_ytd"]} for p in previous["reports"]])
    monkeypatch.setattr(server, "fetch_cash_flow_reports", lambda *args: {
        "2025-06-30": {"operating_cash_flow_ytd": 61, "capex_ytd": 10}})
    data = server.load_stock("601919", refresh=True)
    assert data["reports"] == previous["reports"]
    assert "现金流量表" in data["warnings"][0]
    assert json.loads(path.read_text(encoding="utf-8")) == previous


def test_partial_price_refresh_keeps_last_good_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CACHE", tmp_path)
    path = tmp_path / "601919.json"
    previous = {"code": "601919", "name": "中远海控", "reports": [
        {"period": "2025-03-31", "publish_date": "2025-04-25", "revenue_ytd": 100}],
        "prices": {"raw": [{"publish_date": "2025-04-25", "date": "2025-04-25", "close": 10}],
                   "qfq": []}, "price_basis": "disclosure", "updated_at": "old"}
    path.write_text(json.dumps(previous), encoding="utf-8")
    monkeypatch.setattr(server, "fetch_financial_reports", lambda *args: previous["reports"])
    monkeypatch.setattr(server, "fetch_cash_flow_reports", lambda *args: {})
    monkeypatch.setattr(server, "fetch_daily_prices", lambda *args: [])
    data = server.load_stock("601919", refresh=True)
    assert data["prices"] == previous["prices"]
    assert "价格快照" in data["warnings"][0]
    assert json.loads(path.read_text(encoding="utf-8")) == previous


def test_successful_refresh_saves_previous_cache_as_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CACHE", tmp_path)
    path = tmp_path / "601919.json"
    previous = {"code": "601919", "name": "中远海控", "reports": [],
                "prices": {"raw": [], "qfq": []}}
    path.write_text(json.dumps(previous), encoding="utf-8")
    monkeypatch.setattr(server, "fetch_financial_reports", lambda *args: [
        {"period": "2025-03-31", "revenue_ytd": 100}])
    monkeypatch.setattr(server, "fetch_cash_flow_reports", lambda *args: {
        "2025-03-31": {"operating_cash_flow_ytd": 10, "capex_ytd": 2}})
    monkeypatch.setattr(server, "_disclosure_prices", lambda *args: (
        {"raw": [], "qfq": [], "raw_reference": [], "qfq_reference": []}, [], True))
    monkeypatch.setattr(server, "fetch_stock_name", lambda *args: "中远海控")
    data = server.load_stock("601919", refresh=True)
    assert data["reports"][0]["revenue_ytd"] == 100
    assert json.loads(path.with_suffix(".json.bak").read_text(encoding="utf-8")) == previous


def test_full_refresh_keeps_explicit_roic_extension_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'CACHE', tmp_path)
    old_reports = [
        {'period': '2024-12-31', 'excess_cash': 0, 'non_operating_adjustments_ytd': -20},
        {'period': '2025-12-31', 'excess_cash': None, 'non_operating_adjustments_ytd': None}]
    path = tmp_path / '601919.json'
    path.write_text(json.dumps({'code': '601919', 'name': '中远海控',
                               'reports': old_reports, 'prices': {'raw': [], 'qfq': []}}), encoding='utf-8')
    monkeypatch.setattr(server, 'fetch_financial_reports', lambda *args: [
        {'period': '2024-12-31', 'total_equity': 500},
        {'period': '2025-12-31', 'total_equity': 600},
        {'period': '2026-03-31', 'total_equity': 650}])
    monkeypatch.setattr(server, 'fetch_cash_flow_reports', lambda *args: {})
    monkeypatch.setattr(server, '_disclosure_prices', lambda *args: ({'raw': [], 'qfq': []}, [], True))
    monkeypatch.setattr(server, 'fetch_stock_name', lambda *args: '中远海控')
    result = server.load_stock('601919', refresh=True)
    for old, fresh in zip(old_reports, result['reports']):
        for field in ('excess_cash', 'non_operating_adjustments_ytd'):
            assert field in fresh and fresh[field] == old[field]
    assert 'excess_cash' not in result['reports'][-1]
    assert json.loads(path.read_text(encoding='utf-8'))['reports'] == result['reports']


def test_partial_valuation_refresh_keeps_previous_months_and_backup_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VALUATION_CACHE", tmp_path / "valuation")
    monkeypatch.setattr(server.time, "sleep", lambda seconds: None)
    path = tmp_path / "valuation" / "601919.json"
    path.parent.mkdir()
    previous = {"rows": [
        {"date": "2026-07-31", "pe": 8, "pb": 1.0, "qfq_close": 9, "qfq_close_date": "2026-07-31"},
        {"date": "2026-08-31", "pe": 9, "pb": 1.1, "qfq_close": 10, "qfq_close_date": "2026-08-31"}],
        "industry": {"pe": 12}, "updated_on": "2026-09-01", "basis": server.VALUATION_BASIS}
    path.write_text(json.dumps(previous), encoding="utf-8")
    monkeypatch.setattr(server, "fetch_valuation_series", lambda *args: {
        "pe": [{"date": "2026-08-31", "value": 9}],
        "pb": [{"date": "2026-08-31", "value": 1.1}], "market_cap": []})
    monkeypatch.setattr(server, "fetch_dividend_yields", lambda *args: [])
    monkeypatch.setattr(server, "fetch_monthly_prices", lambda *args: [
        {"date": "2026-08-31", "close": 10}])
    monkeypatch.setattr(server, "fetch_industry_snapshot", lambda *args: {"pe": 12})
    data = server.load_valuation("601919", [], refresh=True)
    assert data["rows"] == previous["rows"]
    assert "历史覆盖不足" in data["warnings"][0]
    assert json.loads(path.read_text(encoding="utf-8")) == previous
