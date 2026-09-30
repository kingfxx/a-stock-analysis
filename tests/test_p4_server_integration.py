"""Normal dashboard reads survive removal of migrated JSON caches."""

import json

from quarterly_dashboard import server
from quarterly_dashboard.fundamental_service import FundamentalService
from quarterly_dashboard.storage import Database


def test_render_page_reads_imported_financial_fact_without_json(tmp_path, monkeypatch):
    root = tmp_path / "fundamentals"
    root.mkdir()
    cache = root / "601919.json"
    cache.write_text(json.dumps({"code": "601919", "name": "中远海控", "reports": [
        {"period": "2025-03-31", "publish_date": "2025-04-30",
         "revenue_ytd": 987654321, "profit_ytd": 100000000}]}), encoding="utf-8")
    monkeypatch.setattr(server, "CACHE", root)
    monkeypatch.setattr(server, "VALUATION_CACHE", tmp_path / "valuation")
    db = Database(server.DATABASE_PATH)
    db.initialize()
    FundamentalService(db, root).import_legacy("601919")
    cache.unlink()

    page = server.render_page("601919", False)
    assert '"name": "中远海控"' in page
    assert "987654321" in page
    assert '"code": "601919"' in page


def test_financial_api_keeps_sqlite_history_when_source_fails(tmp_path, monkeypatch):
    root = tmp_path / "fundamentals"
    root.mkdir()
    cache = root / "601919.json"
    cache.write_text(json.dumps({"code": "601919", "reports": [
        {"period": "2025-03-31", "publish_date": "2025-04-30",
         "revenue_ytd": 987654321, "profit_ytd": 100000000}]}), encoding="utf-8")
    monkeypatch.setattr(server, "CACHE", root)
    db = Database(server.DATABASE_PATH)
    db.initialize()
    FundamentalService(db, root).import_legacy("601919")
    cache.unlink()
    monkeypatch.setattr(FundamentalService, "_fetch_page", staticmethod(
        lambda *args: (_ for _ in ()).throw(ValueError("offline"))))
    monkeypatch.setattr(server, "load_stock", lambda *args: (_ for _ in ()).throw(
        AssertionError("legacy JSON path called")))

    response = server.load_chart_data("601919", "financial")
    assert response["views"]["quarter"][0]["revenue"] == 987654321
    assert any("保留已存事实" in warning for warning in response["warnings"])


def test_startup_imports_all_three_legacy_datasets_without_json_runtime_read(tmp_path, monkeypatch):
    root = tmp_path / "fundamentals"
    values = tmp_path / "valuation"
    root.mkdir()
    values.mkdir()
    (root / "601919.json").write_text(json.dumps({"code": "601919", "name": "中远海控",
        "reports": [{"period": "2025-03-31", "publish_date": "2025-04-30", "revenue_ytd": 100}],
        "dividend_events": [{"date": "2026-06-26", "report_period": "2025-03-31",
                             "per_share": .44, "total_shares": 1000000000}]}), encoding="utf-8")
    (values / "601919.json").write_text(json.dumps({"basis": "monthly_qfq_overlay_v1",
        "rows": [{"date": "2025-01-31", "pe": 12}]}), encoding="utf-8")
    monkeypatch.setattr(server, "CACHE", root)
    monkeypatch.setattr(server, "VALUATION_CACHE", values)
    db = Database(server.DATABASE_PATH)
    db.initialize()
    server.import_p4_legacy(db)
    (root / "601919.json").unlink()
    (values / "601919.json").unlink()
    assert server.FundamentalService(db, root).read("601919")["reports"]
    assert server.DividendService(db, root).read("601919")
    assert server.ValuationService(db, values).read("601919", [])["rows"]
