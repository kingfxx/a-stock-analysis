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

from quarterly_dashboard import server


def payload(page):
    return json.loads(re.search(r'<script id="payload" type="application/json">(.*?)</script>',
                                page, re.S).group(1))


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CACHE", tmp_path / "fundamentals")
    monkeypatch.setattr(server, "VALUATION_CACHE", tmp_path / "valuation")
    server.CACHE.mkdir()
    server.VALUATION_CACHE.mkdir()
    return tmp_path


def stock():
    return {"code": "601600", "name": "中国铝业", "reports": [
        {"period": "2025-12-31", "revenue_ytd": 100, "profit_ytd": 10}],
        "prices": {"raw": [], "qfq": []}, "updated_at": "2026-09-27",
        "price_basis": "disclosure", "report_date_basis": server.REPORT_DATE_BASIS,
        "price_reference_basis": server.PRICE_REFERENCE_BASIS,
        "cash_flow_basis": server.CASH_FLOW_BASIS,
        "financial_fields_basis": server.FINANCIAL_FIELDS_BASIS,
        "dividend_basis": server.DIVIDEND_BASIS,
        "dividend_events": [{"report_period": "2025-12-31", "date": "2026-06-01",
                             "per_share": 1, "total_shares": 100}]}


def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Initial page must never request external data")
    monkeypatch.setattr(server.requests.Session, "request", forbidden)


def test_first_visit_returns_shell_without_fetching_data(cache, monkeypatch):
    forbid_network(monkeypatch)
    data = payload(server.render_page("601600", False))
    assert data["code"] == "601600"
    assert data["views"] == {}
    assert data["loading"] == {"financial": True, "dividends": True, "valuation": True}
    assert not list(cache.rglob("*.json"))


def test_initial_page_shows_stale_cache_and_schedules_only_missing_updates(cache, monkeypatch):
    forbid_network(monkeypatch)
    (server.CACHE / "601600.json").write_text(json.dumps(stock()), encoding="utf-8")
    (server.VALUATION_CACHE / "601600.json").write_text(json.dumps({
        "rows": [{"date": "2026-08-31", "pe": 9}], "industry": {"pe": 12},
        "basis": server.VALUATION_BASIS, "updated_on": "2000-01-01"}), encoding="utf-8")
    before = {path: path.read_bytes() for path in cache.rglob("*.json")}
    data = payload(server.render_page("601600", False))
    assert data["views"]["year"][0]["profit"] == 10
    assert data["views"]["year"][0]["cash_dividend"] == 100
    assert data["valuation"]["views"]["10"]["pe"]["current"] == 9
    assert data["loading"] == {"financial": False, "dividends": False, "valuation": True}
    assert all(path.read_bytes() == contents for path, contents in before.items())


def test_stock_list_does_not_fetch_missing_names(cache, monkeypatch):
    forbid_network(monkeypatch)
    path = server.CACHE / "601600.json"
    path.write_text(json.dumps({"code": "601600"}), encoding="utf-8")
    assert server.cached_stocks() == [{"code": "601600", "name": None}]
    assert "name" not in json.loads(path.read_text(encoding="utf-8"))


def test_financial_endpoint_does_not_wait_for_dividend_or_valuation_source(cache, monkeypatch):
    monkeypatch.setattr(server, "load_stock", lambda code, refresh=False: stock())
    def forbidden(*args, **kwargs):
        raise AssertionError("Financial data must not wait for the other sources")
    monkeypatch.setattr(server, "load_dividends", forbidden)
    monkeypatch.setattr(server, "load_valuation", forbidden)
    data = server.load_chart_data("601600", "financial", False)
    assert data["name"] == "中国铝业"
    assert data["views"]["year"][0]["profit"] == 10


def test_valuation_endpoint_reads_reports_without_refetching_financials(cache, monkeypatch):
    (server.CACHE / "601600.json").write_text(json.dumps(stock()), encoding="utf-8")
    def load(code, reports, refresh=False):
        assert reports[0]["revenue_ytd"] == 100
        return {"rows": [{"date": "2026-08-31", "pe": 8}], "industry": {},
                "updated_on": "2026-09-28", "warnings": []}
    monkeypatch.setattr(server, "load_valuation", load)
    monkeypatch.setattr(server, "load_stock", lambda *args: pytest.fail("finance refetched"))
    data = server.load_chart_data("601600", "valuation", False)
    assert data["views"]["10"]["pe"]["current"] == 8


def test_dividend_endpoint_updates_financial_view_without_refetching_stock(cache, monkeypatch):
    (server.CACHE / "601600.json").write_text(json.dumps(stock()), encoding="utf-8")
    monkeypatch.setattr(server, "load_stock", lambda *args: pytest.fail("finance refetched"))
    monkeypatch.setattr(server, "load_dividends", lambda data, refresh=False: ([
        {"report_period": "2025-12-31", "date": "2026-06-01", "per_share": 2,
         "total_shares": 100}], []))
    data = server.load_chart_data("601600", "dividends", False)
    assert data["views"]["year"][0]["cash_dividend"] == 200


def test_valuation_normalizes_reports_even_when_financial_migration_could_not_be_saved(cache, monkeypatch):
    data = stock()
    data.pop("report_date_basis")
    data["reports"] = [
        {"period": "2020-12-31", "publish_date": "2022-04-22", "revenue_ytd": 100},
        {"period": "2021-12-31", "publish_date": "2023-03-10", "revenue_ytd": 200}]
    (server.CACHE / "601600.json").write_text(json.dumps(data), encoding="utf-8")
    def load(code, reports, refresh=False):
        assert reports[1]["publish_date"] == "2022-04-22"
        assert reports[1]["source_publish_date"] == "2023-03-10"
        return {"rows": [], "industry": {}, "warnings": []}
    monkeypatch.setattr(server, "load_valuation", load)
    server.load_chart_data("601600", "valuation")


@pytest.mark.parametrize("outcome", ["success", "failure", "empty", "partial"])
def test_slow_dividend_fetch_does_not_block_financial_and_merges_latest_cache(cache, monkeypatch, outcome):
    path = server.CACHE / "601600.json"
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
        server._save_cache(path, data)
        return data
    monkeypatch.setattr(server, "fetch_dividend_events", fetch)
    monkeypatch.setattr(server, "load_stock", fresh_stock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        dividends = pool.submit(server.load_chart_data, "601600", "dividends", True)
        try:
            assert started.wait(2)
            financial = pool.submit(server.load_chart_data, "601600", "financial")
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
    (server.CACHE / "601600.json").write_text(json.dumps(stock()), encoding="utf-8")
    data = payload(server.render_page("601600", True))
    assert data["views"]["year"][0]["profit"] == 10
    assert data["refresh_requested"] is True


@pytest.mark.parametrize("code,section", [("../outside", "financial"), ("601600", "unknown")])
def test_data_endpoint_rejects_invalid_code_or_section(cache, code, section):
    with pytest.raises(ValueError):
        server.load_chart_data(code, section, False)


def test_background_browser_loading_preserves_and_updates_charts_independently():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed to exercise background loading")
    result = subprocess.run([node, str(Path(__file__).with_name("background_loading.cjs"))],
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
    (server.CACHE / "601600.json").write_text(json.dumps(stock()), encoding="utf-8")
    (server.VALUATION_CACHE / "601600.json").write_text(json.dumps({
        "rows": [{"date": "2026-08-31", "pe": 9}], "industry": {},
        "basis": server.VALUATION_BASIS, "updated_on": server.date.today().isoformat()}), encoding="utf-8")
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
    monkeypatch.setattr(server, "fetch_financial_reports", offline)
    with pytest.raises(HTTPError) as failure:
        urlopen(http_server + "/api/financial?code=601600", timeout=5)
    assert failure.value.code == 503
    assert "source unavailable" in json.load(failure.value)["error"]
    with urlopen(http_server + "/?code=601600", timeout=5) as response:
        assert payload(response.read().decode("utf-8"))["views"] == {}
