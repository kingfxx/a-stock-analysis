from stock_analysis import application as app
from stock_analysis.domains.stocks import data_service as data_module
from stock_analysis.http import rendering
from stock_analysis.domains.stocks import data_contracts as contracts
import json
import re
import shutil
import subprocess
from threading import Thread
from threading import Event
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import urlopen
from pathlib import Path

import pytest

from stock_analysis.http import server


@pytest.fixture(autouse=True)
def no_unrelated_mainop_requests(monkeypatch):
    monkeypatch.setattr(data_module.BusinessSegmentService, 'update',
                        lambda *args, **kwargs: {'sync': {}, 'warnings': []})


def payload(page):
    return json.loads(re.search(r'<script id="payload" type="application/json">(.*?)</script>',
                                page, re.S).group(1))


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(app.settings, "CACHE", tmp_path / "fundamentals")
    monkeypatch.setattr(app.settings, "VALUATION_CACHE", tmp_path / "valuation")
    monkeypatch.setattr(app.settings, "CHIP_CACHE", tmp_path / "chips")
    app.settings.CACHE.mkdir()
    app.settings.VALUATION_CACHE.mkdir()
    return tmp_path


def stock():
    return {"code": "601600", "name": "中国铝业", "reports": [
        {"period": "2025-12-31", "revenue_ytd": 100, "profit_ytd": 10}],
        "prices": {"raw": [], "qfq": []}, "updated_at": "2026-09-27",
        "price_basis": "disclosure", "report_date_basis": contracts.REPORT_DATE_BASIS,
        "price_reference_basis": contracts.PRICE_REFERENCE_BASIS,
        "cash_flow_basis": contracts.CASH_FLOW_BASIS,
        "financial_fields_basis": contracts.FINANCIAL_FIELDS_BASIS,
        "dividend_basis": contracts.DIVIDEND_BASIS,
        "dividend_events": [{"report_period": "2025-12-31", "date": "2026-06-01",
                             "per_share": 1, "total_shares": 100}]}


def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Initial page must never request external data")
    monkeypatch.setattr(data_module.requests.Session, "request", forbidden)


def test_first_visit_returns_shell_without_fetching_data(cache, monkeypatch):
    forbid_network(monkeypatch)
    data = payload(rendering.render_page("601600", False))
    assert data["code"] == "601600"
    assert data["views"] == {}
    assert not any(data["loading"].values())
    assert data["collection"]["needs_initial"] is True
    assert not list(cache.rglob("*.json"))


def test_initial_page_shows_stale_cache_without_scheduling_updates(cache, monkeypatch):
    forbid_network(monkeypatch)
    (app.settings.CACHE / "601600.json").write_text(json.dumps(stock()), encoding="utf-8")
    (app.settings.VALUATION_CACHE / "601600.json").write_text(json.dumps({
        "rows": [{"date": "2026-08-31", "pe": 9}], "industry": {"pe": 12},
        "basis": contracts.VALUATION_BASIS, "updated_on": "2000-01-01"}), encoding="utf-8")
    before = {path: path.read_bytes() for path in cache.rglob("*.json")}
    data = payload(rendering.render_page("601600", False))
    assert data["views"]["year"][0]["profit"] == 10
    assert data["views"]["year"][0]["cash_dividend"] == 100
    assert data["valuation"]["views"]["10"]["pe"]["current"] == 9
    assert not any(data["loading"].values())
    assert all(path.read_bytes() == contents for path, contents in before.items())


def test_stock_list_does_not_fetch_missing_names(cache, monkeypatch):
    forbid_network(monkeypatch)
    path = app.settings.CACHE / "601600.json"
    path.write_text(json.dumps({"code": "601600"}), encoding="utf-8")
    assert app.data.cached_stocks() == [{"code": "601600", "name": None}]
    assert "name" not in json.loads(path.read_text(encoding="utf-8"))


def test_financial_endpoint_does_not_wait_for_dividend_or_valuation_source(cache, monkeypatch):
    monkeypatch.setattr(app.data, "load_stock", lambda code, refresh=False: stock())
    def forbidden(*args, **kwargs):
        raise AssertionError("Financial data must not wait for the other sources")
    monkeypatch.setattr(app.data, "load_dividends", forbidden)
    monkeypatch.setattr(app.data, "load_valuation", forbidden)
    data = app.data.load_chart_data("601600", "financial", False)
    assert data["name"] == "中国铝业"
    assert data["views"]["year"][0]["profit"] == 10


def test_valuation_endpoint_reads_reports_without_refetching_financials(cache, monkeypatch):
    (app.settings.CACHE / "601600.json").write_text(json.dumps(stock()), encoding="utf-8")
    def load(code, reports, refresh=False):
        assert reports[0]["revenue_ytd"] == 100
        return {"rows": [{"date": "2026-08-31", "pe": 8}], "industry": {},
                "updated_on": "2026-09-28", "warnings": []}
    monkeypatch.setattr(app.data, "load_valuation", load)
    monkeypatch.setattr(app.data, "load_stock", lambda *args: pytest.fail("finance refetched"))
    data = app.data.load_chart_data("601600", "valuation", False)
    assert data["views"]["10"]["pe"]["current"] == 8


def test_dividend_endpoint_updates_financial_view_without_refetching_stock(cache, monkeypatch):
    (app.settings.CACHE / "601600.json").write_text(json.dumps(stock()), encoding="utf-8")
    monkeypatch.setattr(app.data, "load_stock", lambda *args: pytest.fail("finance refetched"))
    monkeypatch.setattr(app.data, "load_dividends", lambda data, refresh=False: ([
        {"report_period": "2025-12-31", "date": "2026-06-01", "per_share": 2,
         "total_shares": 100}], []))
    data = app.data.load_chart_data("601600", "dividends", False)
    assert data["views"]["year"][0]["cash_dividend"] == 200


def test_valuation_normalizes_reports_even_when_financial_migration_could_not_be_saved(cache, monkeypatch):
    data = stock()
    data.pop("report_date_basis")
    data["reports"] = [
        {"period": "2020-12-31", "publish_date": "2022-04-22", "revenue_ytd": 100},
        {"period": "2021-12-31", "publish_date": "2023-03-10", "revenue_ytd": 200}]
    (app.settings.CACHE / "601600.json").write_text(json.dumps(data), encoding="utf-8")
    def load(code, reports, refresh=False):
        assert reports[1]["publish_date"] == "2022-04-22"
        assert reports[1]["source_publish_date"] == "2023-03-10"
        return {"rows": [], "industry": {}, "warnings": []}
    monkeypatch.setattr(app.data, "load_valuation", load)
    app.data.load_chart_data("601600", "valuation")


@pytest.mark.parametrize("outcome", ["success", "failure", "empty", "partial"])
def test_slow_dividend_fetch_does_not_block_financial_and_merges_latest_cache(cache, monkeypatch, outcome):
    path = app.settings.CACHE / "601600.json"
    path.write_text(json.dumps(stock()), encoding="utf-8")
    started, release = Event(), Event()
    def fetch(*args):
        started.set()
        assert release.wait(5)
        if outcome == "failure":
            raise ValueError("dividends unavailable")
        if outcome == "empty":
            return []
        if outcome == "partial":
            return [{"report_period": "2024-12-31", "date": "2025-06-01",
                     "per_share": 1, "total_shares": 100}]
        return [{"report_period": "2025-12-31", "date": "2026-06-01",
                 "per_share": 2, "total_shares": 100}]
    def fresh_stock(*args):
        data = stock()
        data["reports"][0]["profit_ytd"] = 20
        app.data._save_cache(path, data)
        return data
    monkeypatch.setattr(data_module, "fetch_dividend_events", fetch)
    monkeypatch.setattr(app.data, "load_stock", fresh_stock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        dividends = pool.submit(app.data.load_chart_data, "601600", "dividends", True)
        try:
            assert started.wait(2)
            financial = pool.submit(app.data.load_chart_data, "601600", "financial")
            assert financial.result(timeout=1)["views"]["year"][0]["profit"] == 20
        finally:
            release.set()
        dividend_data = dividends.result(timeout=2)
    assert dividend_data["views"]["year"][0]["profit"] == 20
    assert dividend_data["views"]["year"][0]["cash_dividend"] == (200 if outcome == "success" else 100)
    if outcome != "success":
        assert dividend_data["warnings"]
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["reports"][0]["profit_ytd"] == 20
    assert saved["dividend_events"][0]["per_share"] == (2 if outcome == "success" else 1)


def test_refresh_url_still_shows_cache_before_network_work(cache, monkeypatch):
    forbid_network(monkeypatch)
    (app.settings.CACHE / "601600.json").write_text(json.dumps(stock()), encoding="utf-8")
    data = payload(rendering.render_page("601600", True))
    assert data["views"]["year"][0]["profit"] == 10
    assert data["refresh_requested"] is False


@pytest.mark.parametrize("code,section", [("../outside", "financial"), ("601600", "unknown")])
def test_data_endpoint_rejects_invalid_code_or_section(cache, code, section):
    with pytest.raises(ValueError):
        app.data.load_chart_data(code, section, False)


def test_background_browser_loading_preserves_and_updates_charts_independently():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed to exercise background loading")
    result = subprocess.run([node, str(Path(__file__).with_name("background_loading.cjs"))],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr


def test_valuation_frequency_browser_controls_use_saved_rows_and_ignore_stale_responses():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed to exercise valuation controls")
    result = subprocess.run([node, str(Path(__file__).with_name("valuation_ui.cjs"))],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def http_server(cache):
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_live_routes_return_cache_page_and_independent_json(http_server, monkeypatch):
    forbid_network(monkeypatch)
    (app.settings.CACHE / "601600.json").write_text(json.dumps(stock()), encoding="utf-8")
    (app.settings.VALUATION_CACHE / "601600.json").write_text(json.dumps({
        "rows": [{"date": "2026-08-31", "pe": 9}], "industry": {},
        "basis": contracts.VALUATION_BASIS, "updated_on": data_module.date.today().isoformat()}), encoding="utf-8")
    with urlopen(http_server + "/?code=601600", timeout=5) as response:
        assert response.headers["Cache-Control"] == "no-store"
        assert payload(response.read().decode("utf-8"))["views"]["year"][0]["profit"] == 10
    for section in ("financial", "valuation"):
        with urlopen(http_server + f"/api/{section}?code=601600", timeout=5) as response:
            assert response.headers["Content-Type"].startswith("application/json")
            data = json.load(response)
            if section == "financial":
                assert data["views"]["year"][0]["profit"] == 10
            else:
                assert data["views"]["10"]["pe"]["current"] == 9


def test_live_data_failure_returns_json_error_without_affecting_page(http_server, monkeypatch):
    def offline(*args):
        raise ValueError("source unavailable")
    monkeypatch.setattr(data_module, "fetch_financial_reports", offline)
    # Offline sources cannot affect a cache-only GET.
    with urlopen(http_server + "/api/financial?code=601600", timeout=5) as response:
        assert json.load(response)["views"] == {}
    with urlopen(http_server + "/?code=601600", timeout=5) as response:
        assert payload(response.read().decode("utf-8"))["views"] == {}


def test_live_shared_price_context_pins_all_consumers_and_rejects_missing_version(http_server, monkeypatch):
    chips, prices = app.data.services()
    days = ["2026-01-05", "2026-06-30", "2026-09-29"]
    rows = [dict(date=day, open=20, close=21, high=22, low=19, volume=100,
                 raw=[day,"20","21","22","19","100"]) for day in days]
    prices.fetcher = lambda code, adjustment, start, end: [r for r in rows if start <= r["date"] <= end]
    prices.ensure("601600", "raw")
    version = prices.ensure("601600")["version"]
    stock_data = stock()
    stock_data["reports"][0]["publish_date"] = days[0]
    (app.settings.CACHE / "601600.json").write_text(json.dumps(stock_data), encoding="utf-8")
    (app.settings.VALUATION_CACHE / "601600.json").write_text(json.dumps({
        "rows":[{"date":"2026-06-30","pe":9}],"basis":contracts.VALUATION_BASIS,
        "updated_on":data_module.date.today().isoformat()}), encoding="utf-8")
    data_module.FundamentalService(chips.db, app.settings.CACHE).import_legacy("601600")
    data_module.ValuationService(chips.db, app.settings.VALUATION_CACHE).import_legacy("601600")
    monkeypatch.setattr(data_module.FundamentalService, "_fetch_page", staticmethod(
        lambda *args: (_ for _ in ()).throw(ValueError("offline"))))
    monkeypatch.setattr(data_module.ValuationService, "_fetch_indicator", staticmethod(
        lambda *args: (_ for _ in ()).throw(ValueError("offline"))))
    chips.fetcher = lambda code, section, source, **kw: (
        [dict(SCODE=code,DATE=days[-1],RZYE=100,SPJ=21)] if section == "financing" else
        [dict(SECURITY_CODE=code,END_DATE=days[1],HOLDER_TOTAL_NUM=100)])
    for section in ("financing", "shareholders"):
        chips.update("601600", section)
    # Publish a different complete generation, then request the earlier pinned one.
    prices.fetcher = lambda *a: [dict(r,open=-10,close=-9,high=-8,low=-11,
                                    raw=[r["date"],"-10","-9","-8","-11","100"]) for r in rows]
    current = prices.ensure("601600", full=True)["version"]
    assert current != version
    with urlopen(http_server + f"/api/prices?code=601600&version={version}") as response:
        result = json.load(response)
    assert result["price_version"] == version
    assert result["financial"]["views"]["year"][0]["qfq_price"] == 21
    valuation = result['valuation']
    index = valuation['views']['10']['pe']['row_indices'][0]
    assert valuation['row_pool'][index]['qfq_close'] == 21
    assert result["shareholders"]["price_version"] == version
    assert result["financing"]["rows"][0]["qfq_close"] == 21
    for section in ("financial", "valuation", "shareholders", "financing"):
        with urlopen(http_server + f"/api/{section}?code=601600&price_version={version}") as response:
            assert json.load(response)["price_version"] == version
    with pytest.raises(HTTPError) as failure:
        urlopen(http_server + "/api/prices?code=601600&version=999999")
    assert failure.value.code == 409
    assert json.load(failure.value)["price_version_expired"] is True


def test_bad_financial_json_does_not_hide_sqlite_chip_facts(cache):
    chips, _ = app.data.services()
    chips.fetcher = lambda code, section, source, **kw: [dict(SCODE=code,DATE="2026-09-29",RZYE=100,SPJ=20)]
    chips.update("601600", "financing")
    path = app.settings.CACHE / "601600.json"
    path.write_text("broken JSON", encoding="utf-8")
    page = rendering.render_page("601600", False)
    assert payload(page)["financing"]["stored_count"] == 1
    assert "fundamentals 缓存不可用" not in page
    bundle = app.data.price_bundle("601600", version=0)
    assert bundle["financing"]["stored_count"] == 1
    assert "fundamentals 缓存不可用" not in " ".join(bundle["warnings"])
    assert path.read_text(encoding="utf-8") == "broken JSON"
