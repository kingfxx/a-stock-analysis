from types import SimpleNamespace

import pytest

from stock_analysis import application
from stock_analysis.infrastructure import memory_reclaim
from stock_analysis.infrastructure.memory_reclaim import IdleMemoryReclaimer
from stock_analysis.maintenance.database_restore import RequestGate
from stock_analysis.domains.indices import index_collection as ic
from stock_analysis.infrastructure.storage import Database


class Clock:
    now = 0
    def __call__(self): return self.now


def report():
    return dict(collected=1, heap_optimized=True, elapsed_ms=1,
                before=dict(working_bytes=200, private_bytes=300),
                after=dict(working_bytes=100, private_bytes=200))


def test_background_checks_dont_keep_process_active_and_requests_do():
    clock = Clock()
    gate = RequestGate(clock=clock)
    reclaim = IdleMemoryReclaimer(clock=clock, release=report)
    clock.now = 3599
    assert gate.enter(background=True)
    gate.leave(background=True)
    assert gate.last_request_at == 0 and not reclaim.due(gate.last_request_at)
    clock.now = 3600
    assert reclaim.due(gate.last_request_at)
    reclaim.reclaim()
    assert not reclaim.due(gate.last_request_at)
    clock.now = 7199
    gate.enter(); gate.leave()
    clock.now = 7200
    assert not reclaim.due(gate.last_request_at)
    clock.now = 10799
    assert reclaim.due(gate.last_request_at)


def test_empty_caches_still_reclaim_and_running_tasks_defer(monkeypatch):
    clock = Clock()
    calls = []
    gate = RequestGate(clock=clock)
    reclaim = IdleMemoryReclaimer(clock=clock, release=lambda: calls.append(1) or report())
    server = SimpleNamespace(request_gate=gate, memory_reclaimer=reclaim)
    monkeypatch.setattr(application, '_INDUSTRY_SERVICES', {})
    monkeypatch.setattr(application, '_MAINTENANCE_SERVICES', {})
    monkeypatch.setattr(application, '_restore_activity', lambda: True)
    clock.now = 3600
    gate.enter(background=True)
    assert application._expire_idle_caches(server) == 0 and not calls
    monkeypatch.setattr(application, '_restore_activity', lambda: False)
    assert application._expire_idle_caches(server) == 0 and calls == [1]
    application._expire_idle_caches(server)
    assert calls == [1]
    gate.leave(background=True)


def test_active_request_and_restore_gate_prevent_gc(monkeypatch):
    clock = Clock()
    calls = []
    gate = RequestGate(clock=clock)
    reclaim = IdleMemoryReclaimer(clock=clock, release=lambda: calls.append(1) or report())
    server = SimpleNamespace(request_gate=gate, memory_reclaimer=reclaim)
    monkeypatch.setattr(application, '_INDUSTRY_SERVICES', {})
    monkeypatch.setattr(application, '_MAINTENANCE_SERVICES', {})
    monkeypatch.setattr(application, '_restore_activity', lambda: False)
    gate.enter()  # A long-running request must remain protected even after an hour.
    gate.enter(background=True)
    clock.now = 3600
    application._expire_idle_caches(server)
    assert not calls
    gate.leave(); gate.leave(background=True)
    clock.now = 7200
    gate.enter(background=True); gate.blocked = True
    application._expire_idle_caches(server)
    assert not calls
    gate.blocked = False
    application._expire_idle_caches(server)
    assert calls == [1]
    gate.leave(background=True)


def test_heap_operation_failure_preserves_gc_and_measurement(monkeypatch):
    monkeypatch.setattr(memory_reclaim, 'memory_state', lambda: dict(working_bytes=100, private_bytes=200))
    monkeypatch.setattr(memory_reclaim.gc, 'collect', lambda: 7)
    def unsupported(): raise OSError('unsupported heap optimization')
    monkeypatch.setattr(memory_reclaim, '_optimize_windows_heap', unsupported)
    result = memory_reclaim.release_unused_memory()
    assert result['collected'] == 7 and result['heap_optimized'] is False
    assert result['before'] == result['after']
    assert 'unsupported' in result['heap_error']


@pytest.mark.parametrize('injected', [False, True])
@pytest.mark.parametrize('failure', [None, 'calendar', 'finish'])
def test_index_main_session_ownership_in_success_and_failures(tmp_path, monkeypatch, injected, failure):
    db = Database(tmp_path/'indices.sqlite3'); db.initialize()
    operation = ic.start_operation(db, 'incremental', ['sws'], start='2026-10-09', end='2026-10-09')
    instances = []
    class Collector:
        def __init__(self, *args, **kwargs):
            self.closed = 0
            self.http = SimpleNamespace(session=SimpleNamespace(close=self.close), requests=0, retries=0,
                                        halted=set(), report=lambda: dict(network_requests=0))
            self.trading_days = {'2026-10-09'}
            instances.append(self)
        def close(self): self.closed += 1
        def load_calendar(self, **kwargs):
            if failure == 'calendar': raise ValueError('calendar failed')
        def collect_sw(self, *args): pass
    monkeypatch.setattr(ic, 'IndexCollector', Collector)
    monkeypatch.setattr('stock_analysis.domains.market.market_congestion.prepare_index_dependency',
                        lambda *a, **k: dict(status='reused', network_requests=0))
    monkeypatch.setattr('stock_analysis.domains.indices.index_price_derived.recompute',
                        lambda *a, **k: dict(status='complete', facts=0, network_requests=0))
    if failure == 'finish':
        def fail(*args): raise RuntimeError('finish failed')
        monkeypatch.setattr(ic.OperationProgress, 'finish', fail)
    supplied = Collector() if injected else None
    if failure == 'finish':
        with pytest.raises(RuntimeError, match='finish failed'):
            ic.run_operation(db, operation, collector=supplied)
    else:
        result = ic.run_operation(db, operation, collector=supplied)
        assert result['status'] == ('failed' if failure else 'complete')
    assert instances[0].closed == (0 if injected else 1)
    assert all(worker.closed == 1 for worker in instances[1:])


@pytest.mark.parametrize('failure', [None, 'cancelled', 'finish'])
def test_bond_only_session_closes_after_success_cancel_or_finalization_error(tmp_path, monkeypatch, failure):
    db = Database(tmp_path/'bond.sqlite3'); db.initialize()
    closed = []
    client = SimpleNamespace(session=SimpleNamespace(close=lambda: closed.append(1)), report=lambda: {})
    monkeypatch.setattr('stock_analysis.collection.collection.RequestCollector', lambda *a, **k: client)
    def collect(*args, **kwargs):
        if failure == 'cancelled': raise ic.Cancelled('cancelled')
        return dict(status='complete')
    monkeypatch.setattr('stock_analysis.domains.market.bond_yields.collect', collect)
    if failure == 'finish':
        def fail(*args): raise RuntimeError('finish failed')
        monkeypatch.setattr(ic.OperationProgress, 'finish', fail)
    operation = ic.start_operation(db, 'incremental', [], datasets=['bond_zh_us_rate'])
    if failure == 'finish':
        with pytest.raises(RuntimeError, match='finish failed'): ic.run_operation(db, operation)
    else:
        result = ic.run_operation(db, operation)
        assert result['status'] == ('cancelled' if failure else 'complete')
    assert closed == [1]
