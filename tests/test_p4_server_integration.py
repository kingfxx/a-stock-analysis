from stock_analysis import application as app
from stock_analysis.domains.stocks import data_service as data_module
from stock_analysis.http import rendering
"""Normal dashboard reads survive removal of migrated JSON caches."""

import json
import pytest

from stock_analysis.http import server
from stock_analysis.domains.financials.fundamental_service import FundamentalService
from stock_analysis.infrastructure.storage import Database


@pytest.fixture(autouse=True)
def no_unrelated_mainop_requests(monkeypatch):
    monkeypatch.setattr(data_module.BusinessSegmentService, 'update',
                        lambda *args, **kwargs: {'sync': {}, 'warnings': []})


def test_render_page_reads_imported_financial_fact_without_json(tmp_path, monkeypatch):
    root = tmp_path / "fundamentals"
    root.mkdir()
    cache = root / "601919.json"
    cache.write_text(json.dumps({"code": "601919", "name": "中远海控", "reports": [
        {"period": "2025-03-31", "publish_date": "2025-04-30",
         "revenue_ytd": 987654321, "profit_ytd": 100000000}]}), encoding="utf-8")
    monkeypatch.setattr(app.settings, "CACHE", root)
    monkeypatch.setattr(app.settings, "VALUATION_CACHE", tmp_path / "valuation")
    db = Database(app.settings.DATABASE_PATH)
    db.initialize()
    FundamentalService(db, root).import_legacy("601919")
    updated_at = FundamentalService(db, root).read("601919")["updated_at"]
    assert updated_at
    cache.unlink()

    page = rendering.render_page("601919", False)
    assert '"name": "中远海控"' in page
    assert "987654321" in page
    assert '"code": "601919"' in page
    assert f'"updated_at": "{updated_at}"' in page
    assert app.data.price_bundle("601919", version=0)["financial"]["updated_at"] == updated_at


def test_financial_api_keeps_sqlite_history_when_source_fails(tmp_path, monkeypatch):
    root = tmp_path / "fundamentals"
    root.mkdir()
    cache = root / "601919.json"
    cache.write_text(json.dumps({"code": "601919", "reports": [
        {"period": "2025-03-31", "publish_date": "2025-04-30",
         "revenue_ytd": 987654321, "profit_ytd": 100000000}]}), encoding="utf-8")
    monkeypatch.setattr(app.settings, "CACHE", root)
    db = Database(app.settings.DATABASE_PATH)
    db.initialize()
    FundamentalService(db, root).import_legacy("601919")
    updated_at = FundamentalService(db, root).read("601919")["updated_at"]
    cache.unlink()
    monkeypatch.setattr(FundamentalService, "_fetch_page", staticmethod(
        lambda *args: (_ for _ in ()).throw(ValueError("offline"))))
    monkeypatch.setattr(data_module, "fetch_stock_name", lambda *args: (_ for _ in ()).throw(ValueError("offline")))
    monkeypatch.setattr(app.data, "load_stock", lambda *args: (_ for _ in ()).throw(
        AssertionError("legacy JSON path called")))

    response = app.data.load_chart_data("601919", "financial")
    assert response["views"]["quarter"][0]["revenue"] == 987654321
    assert response["updated_at"] == updated_at
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
    monkeypatch.setattr(app.settings, "CACHE", root)
    monkeypatch.setattr(app.settings, "VALUATION_CACHE", values)
    db = Database(app.settings.DATABASE_PATH)
    db.initialize()
    app.data.import_p4_legacy(db)
    (root / "601919.json").unlink()
    (values / "601919.json").unlink()
    assert data_module.FundamentalService(db, root).read("601919")["reports"]
    assert data_module.DividendService(db, root).read("601919")
    assert data_module.ValuationService(db, values).read("601919", [])["rows"]
