import pytest

from quarterly_dashboard import server


@pytest.fixture(autouse=True)
def isolated_dashboard_database(tmp_path, monkeypatch):
    """Tests must never initialize, import into, or refresh the user's live store."""
    monkeypatch.setattr(server, "DATABASE_PATH", tmp_path / "dashboard.sqlite3")
    monkeypatch.setattr(server, "CHIP_CACHE", tmp_path / "chips")
