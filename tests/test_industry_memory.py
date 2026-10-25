from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock

from test_industries import fixture_bundle, service
from stock_analysis.domains.industries import industry_service


def test_provenance_is_shared_and_read_does_not_mutate_it(service):
    service.import_bundle(fixture_bundle(), capture_local=False)
    with service.db.connection() as conn:
        rows=conn.execute('SELECT provenance_id,provenance_json FROM sw_financial_facts').fetchall()
        assert len({r['provenance_id'] for r in rows})==1
        assert all(r['provenance_json']=='{}' for r in rows)
        before=[tuple(r) for r in conn.execute('SELECT * FROM sw_financial_provenance')]
    data=service._load()
    service.read(code='300750', mode='ytd')
    with service.db.connection() as conn:
        assert [tuple(r) for r in conn.execute('SELECT * FROM sw_financial_provenance')]==before
    assert service._load() is data


def test_legacy_sources_are_shared_without_merging_different_sources(service):
    service.import_bundle(fixture_bundle(), capture_local=False)
    with service.db.connection(write=True) as conn:
        conn.execute("UPDATE sw_financial_facts SET provenance_id=NULL,provenance_json=?",
                     ('{"source":"legacy","field":"TOTAL_OPERATE_INCOME"}',))
        conn.execute("UPDATE sw_financial_facts SET provenance_json=? WHERE stock_code='000002'",
                     ('{"source":"different","field":"TOTAL_OPERATE_INCOME"}',))
    # The offline resolver retains distinct legacy evidence; live reads use the new projection.
    from stock_analysis.domains.industries.industry_storage import latest_financial_rows
    with service.db.connection() as conn:
        sources={(r['stock_code'],r['period']):r['provenance_json'] for r in latest_financial_rows(conn)}
    assert sources[('300750','2024-06-30')]==sources[('000001','2025-06-30')]
    assert sources[('000002','2024-06-30')]!=sources[('300750','2024-06-30')]
    assert industry_service.json.loads(sources[('000002','2024-06-30')])['source']=='different'
    assert service.read(code='300750',mode='ytd')['stock_provenance']['revenue']['source']=='eastmoney:RPT_DMSK_FN_INCOME'


def test_concurrent_cold_loads_build_once_and_refresh_rebuilds(service, monkeypatch):
    service.import_bundle(fixture_bundle(), capture_local=False)
    original = service._financial_reader.facts
    gate, entered, release, guard = Barrier(6), Event(), Event(), Lock()
    calls = []

    def load_rows(*args,**kwargs):
        with guard:
            calls.append(1)
        entered.set()
        assert release.wait(5)
        return original(*args,**kwargs)

    monkeypatch.setattr(service._financial_reader, 'facts', load_rows)
    def window():
        data=service._load()
        return service._financial_reader.window(data['token'],data['import']['member_import_id'],
                                                data['nonfinancial'],'2025-06-30','ttm')

    def load():
        gate.wait(timeout=5)
        return window()

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
    refreshed = window()
    assert len(calls) == 1
    assert refreshed is not results[0]
    assert refreshed[('300750', '2024-06-30')]['revenue'] == 42
