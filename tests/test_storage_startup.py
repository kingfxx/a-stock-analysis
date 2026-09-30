import pytest

from quarterly_dashboard import server
from quarterly_dashboard.storage import Database, StorageError, instance_lock


def test_startup_initializes_database_without_importing_or_rewriting_json(tmp_path, monkeypatch):
    database = tmp_path / "stock_analysis.sqlite3"
    legacy = tmp_path / "fundamentals" / "000001.json"
    legacy.parent.mkdir()
    legacy.write_bytes(b'{"code":"000001","reports":[]}')
    original = legacy.read_bytes()
    monkeypatch.setattr(server, "DATABASE_PATH", database)
    monkeypatch.setattr(server, "CACHE", legacy.parent)
    def no_network(*args, **kwargs):
        pytest.fail("Database startup must not request sources")
    monkeypatch.setattr(server.requests.Session, "request", no_network)
    events = []
    class FakeServer:
        def __init__(self, *args):
            events.append("bound")
        def serve_forever(self):
            assert Database(database).check()["instruments"] == 0
            assert legacy.read_bytes() == original
            assert list((database.parent / "backups").glob("*-daily-*.sqlite3"))
            events.append("serving")
        def server_close(self):
            events.append("closed")
    monkeypatch.setattr(server, "ThreadingHTTPServer", FakeServer)
    server.serve()
    assert events == ["bound", "serving", "closed"]
    with instance_lock(database):
        pass  # Shutdown releases the database's OS lock.


def test_startup_failure_closes_bound_port_and_releases_database_lock(tmp_path, monkeypatch):
    database = tmp_path / "stock_analysis.sqlite3"
    monkeypatch.setattr(server, "DATABASE_PATH", database)
    events = []
    class FakeServer:
        def __init__(self, *args):
            events.append("bound")
        def serve_forever(self):
            pytest.fail("Must not serve after initialization failed")
        def server_close(self):
            events.append("closed")
    monkeypatch.setattr(server, "ThreadingHTTPServer", FakeServer)
    monkeypatch.setattr(server.Database, "initialize", lambda self: (_ for _ in ()).throw(StorageError("disk unavailable")))
    with pytest.raises(StorageError, match="disk unavailable"):
        server.serve()
    assert events == ["bound", "closed"]
    with instance_lock(database):
        pass


def test_another_instance_is_rejected_before_binding_http(tmp_path, monkeypatch):
    database = tmp_path / "stock_analysis.sqlite3"
    monkeypatch.setattr(server, "DATABASE_PATH", database)
    monkeypatch.setattr(server, "ThreadingHTTPServer", lambda *args: pytest.fail("Duplicate instance must not bind"))
    with instance_lock(database):
        with pytest.raises(StorageError, match="Another application"):
            server.serve(8768)
