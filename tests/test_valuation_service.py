"""Valuation observations keep their source density across updates."""

import json

from quarterly_dashboard.storage import Database
from quarterly_dashboard.valuation_service import ValuationService


def test_initial_three_windows_then_one_year_update_preserves_dense_history(tmp_path):
    db = Database(tmp_path / "facts.sqlite3")
    db.initialize()
    calls = []
    points = {"近十年": [{"date": "2020-01-01", "value": 10},
                       {"date": "2024-01-01", "value": 20}],
              "近五年": [{"date": "2024-01-01", "value": 20}],
              "近三年": [{"date": "2024-01-01", "value": 20},
                       {"date": "2026-01-01", "value": 30}],
              "近一年": [{"date": "2026-01-01", "value": 31},
                       {"date": "2026-05-01", "value": 40}]}

    def fetch(code, metric, window):
        calls.append((metric, window))
        return points[window]

    service = ValuationService(db, tmp_path / "legacy", fetch_indicator=fetch)
    initial = service.update("601919", [], refresh=True)
    assert len(calls) == 9
    assert list((tmp_path / "backups").glob("facts-daily-*.sqlite3"))
    assert {window for _, window in calls} == {"近十年", "近五年", "近三年"}
    assert [row["date"] for row in initial["observation_rows"]] == [
        "2020-01-01", "2024-01-01", "2026-01-01"]
    assert initial["coverage"]["pe"]["count"] == 3
    assert initial["coverage"]["pe"]["max_gap_days"] == 1461
    with db.connection(write=True) as conn:
        conn.execute("UPDATE sync_state SET checked_at='2026-05-01T00:00:00+00:00' "
                     "WHERE dataset='valuation:pe'")
    assert service.read("601919", [])["updated_on"] == "2026-05-01"
    calls.clear()
    service.update("601919", [], refresh=True)
    assert len(calls) == 3
    assert {window for _, window in calls} == {"近一年"}
    saved = service.observations("601919", "pe")
    assert [(row["observed_on"], row["value"]) for row in saved] == [
        ("2020-01-01", 10), ("2024-01-01", 20),
        ("2026-01-01", 31), ("2026-05-01", 40)]


def test_failed_initial_window_does_not_mark_metric_initialized(tmp_path):
    db = Database(tmp_path / "facts.sqlite3")
    db.initialize()
    broken = True

    def fetch(code, metric, window):
        if broken and metric == "pe" and window == "近五年":
            raise ValueError("missing window")
        return [{"date": "2025-01-01", "value": 10}]

    service = ValuationService(db, tmp_path / "legacy", fetch_indicator=fetch)
    first = service.update("601919", [], refresh=True)
    assert any("pe" in warning for warning in first["warnings"])
    assert service.observations("601919", "pe") == []
    broken = False
    service.update("601919", [], refresh=True)
    assert len(service.observations("601919", "pe")) == 1


def test_legacy_monthly_snapshot_is_preserved_without_fake_daily_observations(tmp_path):
    db = Database(tmp_path / "facts.sqlite3")
    db.initialize()
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "601919.json").write_text(json.dumps({"basis": "monthly_qfq_overlay_v1",
        "rows": [{"date": "2025-01-31", "pe": 12, "pb": 2, "ps": 1}],
        "industry": {"pe": 15}}), encoding="utf-8")
    service = ValuationService(db, legacy)
    service.import_legacy("601919")
    service.import_legacy("601919")
    assert service.observations("601919", "pe") == []
    result = service.read("601919", [])
    assert result["rows"] == [{"date": "2025-01-31", "pe": 12, "pb": 2, "ps": 1}]
    assert result["basis"] == "monthly_qfq_overlay_v1"
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM legacy_valuation_snapshots").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM legacy_imports WHERE dataset='valuation_legacy'").fetchone()[0] == 1


def test_industry_snapshot_is_reused_when_source_fails(tmp_path):
    db = Database(tmp_path / "facts.sqlite3")
    db.initialize()
    failing = False

    def industry(code):
        if failing:
            raise ValueError("offline")
        return {"snapshot": {"pe": 15, "pb": 2, "ps": 1.5, "peer_count": 8},
                "raw": {"PE_TTM": 15, "PB_MRQ": 2, "PS_TTM": 1.5, "TOTAL_COUNT": 8},
                "industry_name": "航运", "industry_code": "B1"}

    service = ValuationService(db, tmp_path / "legacy", fetch_indicator=lambda *args: [
        {"date": "2026-01-01", "value": 10}], fetch_industry=industry)
    first = service.update("601919", [], refresh=True)
    assert first["industry"]["pe"] == 15
    failing = True
    second = service.update("601919", [], refresh=True)
    assert second["industry"]["pe"] == 15
    assert any("行业" in warning for warning in second["warnings"])


def test_long_offline_gap_expands_beyond_three_year_window(tmp_path):
    db = Database(tmp_path / "facts.sqlite3")
    db.initialize()
    calls = []

    def fetch(code, metric, window):
        calls.append(window)
        if window == "近十年":
            return [{"date": "2020-01-01", "value": 10},
                    {"date": "2026-01-01", "value": 20}]
        if window == "近五年":
            return [{"date": "2026-01-01", "value": 20}]
        if window == "近三年":
            return [{"date": "2026-01-01", "value": 20}]
        return [{"date": "2026-01-01", "value": 20}]

    service = ValuationService(db, tmp_path / "legacy", fetch_indicator=fetch)
    service.update("601919", [])
    # Simulate a long-offline database by removing observations after 2020.
    with db.connection(write=True) as conn:
        conn.execute("DELETE FROM valuation_observations WHERE observed_on > '2020-01-01'")
        conn.execute("UPDATE sync_state SET coverage_end='2020-01-01',data_watermark='2020-01-01' "
                     "WHERE dataset LIKE 'valuation:%'")
    calls.clear()
    service.update("601919", [], refresh=True)
    assert "近十年" in calls
