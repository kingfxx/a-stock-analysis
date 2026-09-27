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
    page = server.render_page("601919", False)
    assert '"name": "中远海控"' in page
    assert '"cached_stocks": [{"code": "601919", "name": "中远海控"}]' in page
