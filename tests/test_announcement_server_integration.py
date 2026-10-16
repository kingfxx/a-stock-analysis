"""Server-facing announcement integration stays metadata-only and read-only on page views."""

import json
import pytest
from types import SimpleNamespace

from quarterly_dashboard import server
from quarterly_dashboard.announcement_service import AnnouncementService
from quarterly_dashboard.fundamental_service import FundamentalService
from quarterly_dashboard.storage import Database


@pytest.fixture(autouse=True)
def no_unrelated_mainop_requests(monkeypatch):
    monkeypatch.setattr(server.BusinessSegmentService, 'update',
                        lambda *args, **kwargs: {'sync': {}, 'warnings': []})


def _seed_legacy(db, root):
    root.mkdir(parents=True, exist_ok=True)
    path = root / "601919.json"
    path.write_text(json.dumps({"code": "601919", "name": "中远海控", "reports": [
        {"period": "2025-03-31", "publish_date": "2025-04-30",
         "revenue_ytd": 100000000, "profit_ytd": 10000000,
         "source_publish_date": "2025-05-01", "publish_date_source": "cninfo_original",
         "publish_date_status": "matched_original", "latest_revision_on": "2025-05-08"}
    ]}), encoding="utf-8")
    db.initialize()
    FundamentalService(db, root).import_legacy("601919")
    return path


def test_page_read_never_starts_announcement_collection(tmp_path, monkeypatch):
    root = tmp_path / "fundamentals"
    monkeypatch.setattr(server, "DATABASE_PATH", tmp_path / "db.sqlite3")
    monkeypatch.setattr(server, "CACHE", root)
    monkeypatch.setattr(server, "VALUATION_CACHE", tmp_path / "valuation")
    _seed_legacy(Database(server.DATABASE_PATH), root)

    def forbidden(*args, **kwargs):
        raise AssertionError("page read must not collect announcement metadata")

    monkeypatch.setattr(AnnouncementService, "update", forbidden)
    page = server.render_page("601919", False)
    assert '公告日期：尚未建档' in page
    assert "sina_unverified" in page


def test_financial_update_allows_initial_sync_only_for_explicit_refresh(tmp_path, monkeypatch):
    root = tmp_path / "fundamentals"
    monkeypatch.setattr(server, "DATABASE_PATH", tmp_path / "db.sqlite3")
    monkeypatch.setattr(server, "CACHE", root)
    monkeypatch.setattr(server, "VALUATION_CACHE", tmp_path / "valuation")
    _seed_legacy(Database(server.DATABASE_PATH), root)
    monkeypatch.setattr(FundamentalService, "update", lambda self, code, **kwargs: self.read(code))
    calls = []

    def fake_update(self, code, reports, **kwargs):
        calls.append(kwargs)
        return {"dates": {}, "sync": {"status": "complete", "has_data": True}, "warnings": []}

    monkeypatch.setattr(AnnouncementService, "update", fake_update)
    server.load_chart_data("601919", "financial", refresh=False)
    server.load_chart_data("601919", "financial", refresh=True)
    assert [call["allow_initial"] for call in calls] == [False, True]


def test_announcement_failure_keeps_sina_reports_available(tmp_path, monkeypatch):
    root = tmp_path / "fundamentals"
    monkeypatch.setattr(server, "DATABASE_PATH", tmp_path / "db.sqlite3")
    monkeypatch.setattr(server, "CACHE", root)
    monkeypatch.setattr(server, "VALUATION_CACHE", tmp_path / "valuation")
    _seed_legacy(Database(server.DATABASE_PATH), root)
    monkeypatch.setattr(FundamentalService, "update", lambda self, code, **kwargs: self.read(code))
    monkeypatch.setattr(AnnouncementService, "update", lambda *args, **kwargs: (_ for _ in ()).throw(
        ValueError("offline")))

    result = server.load_chart_data("601919", "financial", refresh=True)
    assert result["views"]["quarter"][0]["revenue"] == 100000000
    assert any("已保留新浪财务事实" in warning for warning in result["warnings"])


def test_final_disclosure_date_drives_actual_financial_price_projection():
    report = {"period": "2025-03-31", "publish_date": "2025-04-30",
              "publish_date_source": "cninfo_original", "publish_date_status": "matched_original",
              "latest_revision_on": "2025-05-08", "revenue_ytd": 100000000,
              "profit_ytd": 10000000, "report_date_basis": server.REPORT_DATE_BASIS}
    projected = server.financial_prices(
        {"code": "601919", "reports": [report], "report_date_basis": server.REPORT_DATE_BASIS},
        [{"date": "2025-04-30", "close": 12.5}],
        [{"date": "2025-04-30", "close": 11.5}])
    payload = server._financial_payload(projected)
    row = payload["views"]["quarter"][0]
    assert row["publish_date"] == "2025-04-30"
    assert row["price_date"] == "2025-04-30"
    assert row["qfq_price_date"] == "2025-04-30"
    assert row["publish_date_source"] == "cninfo_original"
    assert row["latest_revision_on"] == "2025-05-08"


def test_announcement_list_api_returns_collection_categories_and_cninfo_links(monkeypatch):
    rows = [
        {"title": "年报", "published_on": "2026-03-01", "announcement_id": "a1", "org_id": "org",
         "title_category": "formal_report", "report_type": "annual"},
        {"title": "半年报", "published_on": "2026-08-01", "announcement_id": "a2", "org_id": "org",
         "title_category": "formal_report", "report_type": "interim"},
        {"title": "一季报", "published_on": "2026-04-01", "announcement_id": "a3", "org_id": "org",
         "title_category": "formal_report", "report_type": "q1"},
        {"title": "三季报", "published_on": "2026-10-01", "announcement_id": "a4", "org_id": "org",
         "title_category": "formal_report", "report_type": "q3"},
        {"title": "业绩预告", "published_on": "2026-01-01", "announcement_id": "a5", "org_id": "org",
         "title_category": "forecast", "report_type": "unknown"},
        {"title": "分红", "published_on": "2026-02-01", "announcement_id": "a6", "org_id": "org",
         "title_category": "dividend", "report_type": "unknown"},
        *[{"title": title, "published_on": day, "announcement_id": ident, "org_id": "org",
           "title_category": "unknown", "report_type": "unknown"} for title,day,ident in [
            ("中远海控2024年前三季度业绩预增公告", "2024-10-08", "1221336810"),
            ("中远海控2024年度业绩预增公告", "2025-01-11", "1222297684"),
            ("中远海控2025年第一季度业绩预增公告", "2025-04-08", "1223020724")]],
    ]

    class Cursor:
        def __init__(self, row=None, values=None): self.row, self.values = row, values
        def fetchone(self): return self.row
        def fetchall(self): return self.values

    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, params): return Cursor({"id": 1}) if "SELECT id FROM instruments" in sql else Cursor(values=rows)

    class DB:
        def connection(self): return Connection()

    monkeypatch.setattr(server, "services", lambda: (SimpleNamespace(db=DB()),))
    payload = {}
    handler = SimpleNamespace(path="/api/stock-announcements?code=601919",
                              _ai_reply=lambda body: payload.update(body))
    server.Handler._do_GET(handler)
    assert [item["category"] for item in payload["announcements"]] == [
        "annual", "interim", "q1", "q3", "forecast", "dividend", "forecast", "forecast", "forecast"]
    assert all("announcementId=" in item["url"] and "stockCode=601919" in item["url"]
               for item in payload["announcements"])
