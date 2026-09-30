"""Financial facts are refreshed by report type and recomputed from saved history."""

import json
from datetime import datetime, timedelta, timezone

from quarterly_dashboard.fundamental_service import FundamentalService
from quarterly_dashboard.storage import Database


def test_cache_timestamp_tracks_successful_financial_commits(tmp_path, monkeypatch):
    db = Database(tmp_path / "facts.sqlite3")
    db.initialize()
    now = [datetime.now(timezone.utc).isoformat()]
    monkeypatch.setattr("quarterly_dashboard.storage.utc_now", lambda: now[0])
    failed_types = set()

    def fetch(code, kind, num, page):
        if kind in failed_types:
            raise ValueError("source unavailable")
        row = _record("20260630", 100, "2026-08-30") if kind == "lrb" else {
            "publish_date": "20260830", "data": []}
        return {"records": {"20260630": row}, "total": 1}

    service = FundamentalService(db, tmp_path / "legacy", fetch_page=fetch)
    assert service.update("600519")["updated_at"] == now[0]
    first_update = now[0]
    now[0] = (datetime.fromisoformat(now[0]) + timedelta(hours=1)).isoformat()
    assert service.read("600519")["updated_at"] == first_update
    assert service.update("600519")["updated_at"] == first_update
    assert service.update("600519", refresh=True)["updated_at"] == now[0]

    last_success = now[0]
    now[0] = (datetime.fromisoformat(now[0]) + timedelta(hours=1)).isoformat()
    failed_types.update(("lrb", "fzb", "llb"))
    failed = service.update("600519", refresh=True)
    assert failed["warnings"]
    assert failed["updated_at"] == last_success
    assert failed["reports"][0]["revenue_ytd"] == 100

    failed_types.remove("llb")
    partial = service.update("600519", refresh=True)
    assert partial["warnings"]
    assert partial["updated_at"] == now[0]
    assert FundamentalService(db, tmp_path / "legacy").read("600519")["updated_at"] == now[0]


def _record(period, revenue, published):
    return {"publish_date": published.replace("-", ""), "update_time": 1,
            "data": [{"item_title": "营业收入", "item_value": str(revenue)},
                     {"item_title": "归属于母公司所有者的净利润", "item_value": "10"}]}


def test_refresh_uses_eight_period_window_and_keeps_old_report(tmp_path):
    db = Database(tmp_path / "facts.sqlite3")
    db.initialize()
    periods = {"20250331": _record("20250331", 100, "2025-04-30"),
               "20241231": _record("20241231", 80, "2025-03-30")}
    requested = []

    def fetch(code, kind, num, page):
        requested.append((kind, num, page))
        rows = periods if kind == "lrb" else {
            key: {"publish_date": row["publish_date"], "update_time": 1, "data": []}
            for key, row in periods.items()}
        return {"records": rows, "total": len(rows),
                "oldest_period": "2024-12-31", "newest_period": "2025-03-31"}

    service = FundamentalService(db, tmp_path / "legacy", fetch_page=fetch)
    first = service.update("601919")
    assert first["reports"][-1]["revenue_ytd"] == 100
    assert list((tmp_path / "backups").glob("facts-daily-*.sqlite3"))
    assert {num for _, num, _ in requested} == {200}

    requested.clear()
    periods["20250331"] = _record("20250331", 120, "2025-04-30")
    second = service.update("601919", refresh=True)
    assert {num for _, num, _ in requested} == {8}
    assert [row["revenue_ytd"] for row in second["reports"]] == [80, 120]
    assert second["reports"][0]["period"] == "2024-12-31"


def test_failed_report_type_preserves_committed_history(tmp_path):
    db = Database(tmp_path / "facts.sqlite3")
    db.initialize()
    fail = False

    def fetch(code, kind, num, page):
        if fail and kind == "lrb":
            raise ValueError("source unavailable")
        row = _record("20250331", 100, "2025-04-30") if kind == "lrb" else {
            "publish_date": "20250430", "data": []}
        return {"records": {"20250331": row}, "total": 1,
                "oldest_period": "2025-03-31", "newest_period": "2025-03-31"}

    service = FundamentalService(db, tmp_path / "legacy", fetch_page=fetch)
    assert service.update("601919")["reports"][0]["revenue_ytd"] == 100
    fail = True
    result = service.update("601919", refresh=True)
    assert result["reports"][0]["revenue_ytd"] == 100
    assert any("lrb" in warning for warning in result["warnings"])


def test_legacy_import_is_idempotent_and_preserves_explicit_zero_and_null(tmp_path):
    db = Database(tmp_path / "facts.sqlite3")
    db.initialize()
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "601919.json").write_text(json.dumps({"code": "601919", "name": "中远海控",
        "reports": [{"period": "2025-03-31", "publish_date": "2025-04-30",
                     "revenue_ytd": 100, "excess_cash": 0,
                     "non_operating_adjustments_ytd": None}]}), encoding="utf-8")
    service = FundamentalService(db, legacy)
    service.import_legacy("601919")
    service.import_legacy("601919")
    data = service.read("601919")
    assert data["name"] == "中远海控"
    assert data["reports"][0]["excess_cash"] == 0
    assert "non_operating_adjustments_ytd" in data["reports"][0]
    assert data["reports"][0]["non_operating_adjustments_ytd"] is None
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM legacy_imports WHERE dataset='financial:merged'").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM report_overrides").fetchone()[0] == 2


def test_full_audit_rejects_missing_old_period_outside_recent_eight(tmp_path):
    db = Database(tmp_path / "facts.sqlite3")
    db.initialize()
    periods = [f"{year}{quarter}" for year in (2023, 2024, 2025)
               for quarter in ("0331", "0630", "0930", "1231")][:10]
    current = {period: _record(period, 100, "2026-01-01") for period in reversed(periods)}

    def fetch(code, kind, num, page):
        rows = dict(list(current.items())[(page - 1) * num:page * num])
        return {"records": rows, "total": len(current),
                "oldest_period": min(current), "newest_period": max(current)}

    service = FundamentalService(db, tmp_path / "legacy", fetch_page=fetch)
    assert len(service.update("601919")["reports"]) == 10
    current.pop(periods[0])
    result = service.update("601919", full=True)
    assert len(result["reports"]) == 10
    assert any("lrb" in warning for warning in result["warnings"])


def test_missing_previously_present_financial_field_keeps_old_fact(tmp_path):
    db = Database(tmp_path / "facts.sqlite3")
    db.initialize()
    current = _record("20250331", 100, "2025-04-30")

    def fetch(code, kind, num, page):
        row = current if kind == "lrb" else {"publish_date": "20250430", "data": []}
        return {"records": {"20250331": row}, "total": 1,
                "oldest_period": "2025-03-31", "newest_period": "2025-03-31"}

    service = FundamentalService(db, tmp_path / "legacy", fetch_page=fetch)
    assert service.update("601919")["reports"][0]["revenue_ytd"] == 100
    current = {**current, "data": [item for item in current["data"]
                                  if item["item_title"] != "营业收入"]}
    result = service.update("601919", refresh=True)
    assert result["reports"][0]["revenue_ytd"] == 100
    assert any("lrb" in warning for warning in result["warnings"])


def test_first_income_batch_with_missing_revenue_is_not_marked_complete(tmp_path):
    db = Database(tmp_path / "facts.sqlite3")
    db.initialize()

    def fetch(code, kind, num, page):
        row = {"publish_date": "20250430", "data": [
            {"item_title": "归属于母公司所有者的净利润", "item_value": "10"}]}
        return {"records": {"20250331": row}, "total": 1,
                "oldest_period": "2025-03-31", "newest_period": "2025-03-31"}

    service = FundamentalService(db, tmp_path / "legacy", fetch_page=fetch)
    result = service.update("601919")
    assert result["reports"] == []
    with db.connection() as conn:
        state = conn.execute("SELECT data_status FROM sync_state WHERE dataset='financial:lrb'").fetchone()
    assert state[0] == "uninitialized"


def test_full_history_keeps_sparse_ancient_income_without_blocking_recent_reports(tmp_path):
    db = Database(tmp_path / "facts.sqlite3")
    db.initialize()
    recent = {f"{year}{quarter}": _record(f"{year}{quarter}", 100, "2025-04-30")
              for year in (2023, 2024, 2025) for quarter in ("0331", "0630", "0930", "1231")}
    old = {"publish_date": "19910501", "data": [
        {"item_title": "归属于母公司的净利润", "item_value": "10"}]}
    current = {**recent, "19901231": old}

    def fetch(code, kind, num, page):
        rows = current if kind == "lrb" else {
            period: {"publish_date": raw["publish_date"], "data": []}
            for period, raw in current.items()}
        items = sorted(rows.items(), reverse=True)
        return {"records": dict(items[(page - 1) * num:page * num]), "total": len(items),
                "oldest_period": min(rows), "newest_period": max(rows)}

    service = FundamentalService(db, tmp_path / "legacy", fetch_page=fetch)
    result = service.update("000001")
    assert len(result["reports"]) == 13
    assert result["reports"][0]["revenue_ytd"] is None
    assert any("1990-12-31" in warning for warning in result["warnings"])
    assert not any("更新失败" in warning for warning in result["warnings"])
    assert not any("更新失败" in warning for warning in service.update("000001", refresh=True)["warnings"])
