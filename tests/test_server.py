import json

from quarterly_dashboard import server


def test_existing_cache_gets_name_without_refetching_financial_data(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CACHE", tmp_path)
    path = tmp_path / "601919.json"
    path.write_text(json.dumps({"code": "601919", "reports": [], "prices": {"raw": [], "qfq": []},
                                "updated_at": "2026-09-26T00:00:00+00:00"}), encoding="utf-8")
    monkeypatch.setattr(server, "fetch_stock_name", lambda code, session: "中远海控")
    monkeypatch.setattr(server, "fetch_financial_reports", lambda *args: (_ for _ in ()).throw(AssertionError("finance refetched")))

    data = server.load_stock("601919")
    assert data["name"] == "中远海控"
    assert json.loads(path.read_text(encoding="utf-8"))["name"] == "中远海控"
    assert server.cached_stocks() == [{"code": "601919", "name": "中远海控"}]


def test_render_page_includes_cached_stock_options(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CACHE", tmp_path)
    (tmp_path / "601919.json").write_text(json.dumps({"code": "601919", "name": "中远海控"}), encoding="utf-8")
    monkeypatch.setattr(server, "load_stock", lambda code, refresh=False: {
        "code": "601919", "name": "中远海控", "reports": [],
        "prices": {"raw": [], "qfq": []}, "updated_at": "2026-09-26T00:00:00+00:00"})
    page = server.render_page("601919", False)
    assert '"name": "中远海控"' in page
    assert '"cached_stocks": [{"code": "601919", "name": "中远海控"}]' in page
