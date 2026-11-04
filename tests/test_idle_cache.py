from contextlib import nullcontext
from threading import Event, Thread
from types import SimpleNamespace

from stock_analysis.infrastructure.idle_cache import IdleCache
from stock_analysis.infrastructure.storage import Database
from stock_analysis.domains.industries.industry_service import IndustryService
from stock_analysis.domains.indices.index_read_service import IndexReadService
from stock_analysis.maintenance.maintenance import MaintenanceService
from stock_analysis.maintenance.database_restore import RequestGate
from stock_analysis.collection.collection_management import Scheduler
from stock_analysis import application, runtime
from test_industry_financial_reader import setup
from test_index_read_service import seeded


class Clock:
    now = 0

    def __call__(self):
        return self.now


def test_idle_entries_expire_individually_and_hits_extend_lifetime():
    clock = Clock()
    cache = IdleCache(clock=clock)
    cache['first'] = object()
    clock.now = 100
    cache['second'] = object()
    clock.now = 3599
    assert not cache.expire()
    first = cache['first']
    clock.now = 3700
    assert [key for key, _ in cache.expire()] == ['second']
    assert cache['first'] is first
    clock.now = 7300
    assert len(cache.expire()) == 1 and not cache and not cache._used
    cache['another'] = 1
    cache.popitem(last=False)
    assert not cache._used
    cache['another'] = 1
    cache.clear()
    assert not cache._used


def test_industry_expiration_rebuilds_same_results_and_poll_does_not_touch(setup):
    db, _, _ = setup
    clock = Clock()
    service = IndustryService(db, clock=clock)
    args = dict(code='300750', industry='630000', level=1, mode='ytd', period='2025-06-30')
    before = service.read(**args)
    clock.now = 3599
    service.status()
    assert service.expire_idle() == 0
    clock.now = 3600
    assert service.expire_idle() >= 3
    assert service._cache is None
    assert not service._financial_reader._facts and not service._financial_reader._series
    after = service.read(**args)
    for field in ('companies', 'ranking', 'series', 'stock_metrics', 'stock_provenance'):
        assert after[field] == before[field]
    clock.now = 7199
    warm = service._load()
    clock.now = 7200
    service.expire_idle()
    assert service._cache[1] is warm  # Metadata hit alone doesn't renew financial windows.
    assert not service._financial_reader._facts


def test_request_itself_discards_expired_financial_windows(setup):
    db, _, _ = setup
    clock = Clock()
    service = IndustryService(db, clock=clock)
    data = service._load()
    reader = service._financial_reader
    args = (data['token'], data['import']['member_import_id'], data['nonfinancial'], '2025-06-30', 'ytd')
    first = reader.window(*args)
    clock.now = 3599
    assert reader.window(*args) is first
    clock.now = 7199
    rebuilt = reader.window(*args)
    assert rebuilt is not first and rebuilt == first
    with reader._lock:
        clock.now += 3600
        assert reader.expire_idle() == 0
    assert reader.expire_idle() == 1


def test_index_bytes_metadata_expiration_and_active_entries(tmp_path):
    db = Database(tmp_path/'index.sqlite3'); db.initialize(); seeded(db)
    clock = Clock()
    service = IndexReadService(db, clock=clock)
    args = dict(code='cn:000300', provider='csindex', series='history.rolling', metric='pe_rolling')
    try:
        first = service.chart(**args)
        service.catalog()
        clock.now = 100
        second = service.chart(**dict(args, metric='pb'))
        clock.now = 2000
        assert service.chart(**args) is first
        clock.now = 3600
        service.status()
        assert service.metadata is None and len(service.cache) == 2
        clock.now = 3700
        assert service.expire_idle() == 1
        assert service.cache_bytes == len(first) and len(service.cache) == 1
        rebuilt = service.chart(**dict(args, metric='pb'))
        assert rebuilt == second and rebuilt is not second
        clock.now = 7300
        assert service.expire_idle() == 2
        assert not service.cache and service.cache_bytes == 0
    finally:
        service.close()


def test_busy_index_owner_is_skipped_without_blocking(tmp_path):
    db = Database(tmp_path/'index.sqlite3'); db.initialize()
    clock = Clock()
    service = IndexReadService(db, clock=clock)
    service.cache['key'] = b'data'; service.cache_bytes = 4
    clock.now = 3600
    entered, release = Event(), Event()
    def hold():
        with service.lock:
            entered.set(); assert release.wait(5)
    thread = Thread(target=hold); thread.start()
    try:
        assert entered.wait(5)
        assert service.expire_idle() == 0
    finally:
        release.set(); thread.join(5)
    assert service.expire_idle() == 1 and service.cache_bytes == 0
    service.close()


def test_maintenance_keeps_existing_freshness_and_releases_idle(monkeypatch):
    clock = Clock()
    service = MaintenanceService(None, clock=clock)
    calls = []
    def read():
        calls.append(1); return {'generation': len(calls)}
    monkeypatch.setattr(service, '_read', read)
    first = service.read()
    clock.now = 59
    assert service.read() is first
    clock.now = 60
    assert service.read() is not first  # Preserve the original 60-second freshness.
    clock.now = 3659
    assert service.expire_idle() == 0
    clock.now = 3660
    assert service.expire_idle() == 1 and service._snapshot is None
    assert service.read()['generation'] == 3


def test_assembly_sweeps_only_existing_owners(monkeypatch):
    calls = []
    owner = SimpleNamespace(expire_idle=lambda: calls.append('expired') or 1)
    monkeypatch.setattr(application, '_INDUSTRY_SERVICES', {'db': owner})
    monkeypatch.setattr(application, '_MAINTENANCE_SERVICES', {})
    monkeypatch.setattr(application, 'Database', lambda *a: (_ for _ in ()).throw(AssertionError('no database work')))
    assert application._expire_idle_caches(SimpleNamespace(index_reader=owner)) == 2
    assert len(calls) == 2


def test_scheduler_cleanup_respects_restore_gate_and_needs_no_new_thread():
    class Once:
        stopped = False
        def is_set(self): return self.stopped
        def wait(self, seconds):
            assert seconds == 30
            self.stopped = True
    for blocked in (False, True):
        calls = []
        gate = RequestGate(); gate.blocked = blocked
        scheduler = Scheduler(SimpleNamespace(tick=lambda **k: calls.append('tick')),
                              gate=gate, idle_cleanup=lambda: calls.append('cleanup'))
        scheduler.stop_event = Once()
        scheduler._loop()
        assert calls == ([] if blocked else ['cleanup', 'tick'])
        assert gate.active == 0


def test_runtime_wires_current_server_and_closes_its_single_scheduler(monkeypatch, tmp_path):
    calls = []
    server = SimpleNamespace(serve_forever=lambda: calls.append('serve'),
                             server_close=lambda: calls.append('server_close'))
    monkeypatch.setattr(runtime, 'Database', lambda path: object())
    monkeypatch.setattr(runtime, 'instance_lock', lambda path: nullcontext())
    monkeypatch.setattr(runtime, 'RestoreManager', lambda *a, **k: SimpleNamespace(pending=False))
    monkeypatch.setattr(runtime, 'SourceArchiveService', lambda *a: object())
    class FakeScheduler:
        def __init__(self, management, *, gate, archive, idle_cleanup): self.cleanup = idle_cleanup
        def start(self): self.cleanup()
        def close(self): calls.append('scheduler_close')
    monkeypatch.setattr(runtime, 'Scheduler', FakeScheduler)
    runtime.run_server(8766, False, database_path=tmp_path/'unused.sqlite3', handler=None,
        server_factory=lambda *a: server, initialize=lambda *a, **k: calls.append('initialize'),
        activity_check=lambda: False, management_factory=lambda db: object(), clear_state=lambda: None,
        open_url=lambda url: None, idle_cleanup=lambda current: calls.append(current))
    assert calls == ['initialize', server, 'serve', 'scheduler_close', 'server_close']
