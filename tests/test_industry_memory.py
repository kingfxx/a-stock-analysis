from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock

from test_industries import fixture_bundle, service
from quarterly_dashboard import industry_service


def test_provenance_is_shared_and_read_does_not_mutate_it(service):
    service.import_bundle(fixture_bundle(), capture_local=False)
    data = service._load()
    first = data['provenance'][('300750', '2024-06-30')]
    assert all(source is first for source in data['provenance'].values())
    before = industry_service.dumps(first)
    service.read(code='300750', mode='ytd')
    assert industry_service.dumps(first) == before
    assert service._load() is data


def test_legacy_sources_are_shared_without_merging_different_sources(service):
    service.import_bundle(fixture_bundle(), capture_local=False)
    with service.db.connection(write=True) as conn:
        conn.execute("UPDATE sw_financial_facts SET provenance_id=NULL,provenance_json=?",
                     ('{"source":"legacy","field":"TOTAL_OPERATE_INCOME"}',))
        conn.execute("UPDATE sw_financial_facts SET provenance_json=? WHERE stock_code='000002'",
                     ('{"source":"different","field":"TOTAL_OPERATE_INCOME"}',))
    sources = service._load()['provenance']
    assert sources[('300750', '2024-06-30')] is sources[('000001', '2025-06-30')]
    assert sources[('000002', '2024-06-30')] is not sources[('300750', '2024-06-30')]
    assert sources[('000002', '2024-06-30')]['source'] == 'different'


def test_concurrent_cold_loads_build_once_and_refresh_rebuilds(service, monkeypatch):
    service.import_bundle(fixture_bundle(), capture_local=False)
    original = industry_service.latest_financial_rows
    gate, entered, release, guard = Barrier(6), Event(), Event(), Lock()
    calls = []

    def load_rows(conn):
        with guard:
            calls.append(1)
        entered.set()
        assert release.wait(5)
        return original(conn)

    monkeypatch.setattr(industry_service, 'latest_financial_rows', load_rows)

    def load():
        gate.wait(timeout=5)
        return service._load()

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(load) for _ in range(6)]
        assert entered.wait(5)
        release.set()
        results = [future.result(timeout=5) for future in futures]
    assert len(calls) == 1
    assert all(result is results[0] for result in results)
    bundle = fixture_bundle()
    bundle['financials'][0]['revenue'] = 42
    service.import_bundle(bundle, capture_local=False)
    calls.clear()
    refreshed = service._load()
    assert len(calls) == 1
    assert refreshed is not results[0]
    assert refreshed['facts'][('300750', '2024-06-30')]['revenue'] == 42
