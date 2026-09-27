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

    data = server.load_stock("601919")
    assert data["name"] == "中远海控"
    assert json.loads(path.read_text(encoding="utf-8"))["name"] == "中远海控"
    assert server.cached_stocks() == [{"code": "601919", "name": "中远海控"}]


def test_month_end_cache_migrates_to_disclosure_snapshots(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CACHE", tmp_path)
    path = tmp_path / "601919.json"
    report = {"period": "2026-06-30", "publish_date": "2026-08-29"}
    path.write_text(json.dumps({"code": "601919", "name": "中远海控", "reports": [report],
                                "prices": {"raw": [{"date": "2026-06-30", "close": 12}], "qfq": []}}),
                    encoding="utf-8")
    monkeypatch.setattr(server, "fetch_financial_reports", lambda *args: (_ for _ in ()).throw(AssertionError("finance refetched")))
    monkeypatch.setattr(server, "fetch_daily_prices", lambda code, session, adjust, start, end: [
        {"date": "2026-08-28", "close": 16.88 if adjust == "" else 15.2}])

    data = server.load_stock("601919")
    assert data["prices"]["raw"] == [{"publish_date": "2026-08-29", "date": "2026-08-28", "close": 16.88}]
    assert data["price_basis"] == "disclosure"
    assert json.loads(path.read_text(encoding="utf-8"))["price_basis"] == "disclosure"


def test_cache_migrates_shifted_report_dates_and_reprices(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CACHE", tmp_path)
    path = tmp_path / "300750.json"
    path.write_text(json.dumps({"code": "300750", "name": "宁德时代", "price_basis": "disclosure",
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
    (tmp_path / "601919.json").write_text(json.dumps({"code": "601919", "name": "中远海控"}), encoding="utf-8")
    monkeypatch.setattr(server, "load_stock", lambda code, refresh=False: {
        "code": "601919", "name": "中远海控", "reports": [],
        "prices": {"raw": [], "qfq": []}, "updated_at": "2026-09-26T00:00:00+00:00"})
    monkeypatch.setattr(server, "load_valuation", lambda code, reports, refresh=False: {
        "rows": [], "industry": {}, "updated_on": "2026-09-27", "warnings": []})
    page = server.render_page("601919", False)
    assert '"name": "中远海控"' in page
    assert '"cached_stocks": [{"code": "601919", "name": "中远海控"}]' in page


def test_valuation_cache_is_reused_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CACHE", tmp_path)
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
    monkeypatch.setattr(server, "CACHE", tmp_path)
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
    monkeypatch.setattr(server, "CACHE", tmp_path)
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
    path = tmp_path / "601919.json"
    reports = [{"period": "2015-06-30", "publish_date": "2015-08-28"},
               {"period": "2015-09-30", "publish_date": "2015-10-29"}]
    path.write_text(json.dumps({"code": "601919", "name": "中远海控", "reports": reports,
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
