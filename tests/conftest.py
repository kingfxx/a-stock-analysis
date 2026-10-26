from stock_analysis import application as app
import pytest

@pytest.fixture(autouse=True)
def isolated_dashboard_database(tmp_path, monkeypatch):
    """Tests must never initialize, import into, or refresh the user's live store."""
    monkeypatch.setattr(app.settings, "DATABASE_PATH", tmp_path / "dashboard.sqlite3")
    monkeypatch.setattr(app.settings, "CHIP_CACHE", tmp_path / "chips")
    monkeypatch.setattr(app.settings, "CACHE", tmp_path / "fundamentals")
    monkeypatch.setattr(app.settings, "VALUATION_CACHE", tmp_path / "valuation")
