import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from stock_analysis.domains.indices.index_read_service import IndexReadService
from stock_analysis.domains.indices.index_repository import publish, read_indices
from test_index_collection import db, source


def seeded(db):
    _,_,row = source(db)
    with db.connection(write=True) as conn:
        publish(conn,[dict(row,observation_date='2026-10-07',pe_rolling=-2),
                      dict(row,observation_date='2026-10-08',pe_rolling=0),row])
    return row


def test_chart_matches_existing_calculation_and_revision_invalidates(db):
    row = seeded(db)
    service = IndexReadService(db)
    args = dict(code='cn:000300',provider='csindex',series='history.rolling',metric='pe_rolling')
    try:
        body = service.chart(**args)
        result = json.loads(body)
        original = read_indices(db,code=args['code'],provider=args['provider'],series=args['series'])
        for years, view in result['views']['pe_rolling'].items():
            expected = original['views']['pe_rolling'][years]
            for field in ('current','current_date','count','percentile','mean','stddev','high','median','low','negative_pe_ranges'):
                assert view[field] == expected[field]
            assert 'rows' not in view
            for frequency, points in view['rows_by_frequency'].items():
                assert points == [{k:p[k] for k in ('date','pe','pe_date') if k in p} for p in expected['rows_by_frequency'][frequency]]
        assert service.chart(**args) is body
        # Same date, different value: latest observation date cannot detect this.
        with db.connection(write=True) as conn:
            publish(conn,[dict(row,pe_rolling=22)])
        revised = json.loads(service.chart(**args))
        assert revised['revision'] != result['revision']
        assert revised['views']['pe_rolling']['10']['current'] == 22
        catalog = json.loads(service.catalog())
        assert catalog['revision'] == revised['revision']
        assert 'source_id' not in catalog['catalog'][0]
        assert 'rows' not in catalog and 'batches' not in catalog
    finally:
        service.close()


def test_cache_limits_and_simultaneous_miss(db,monkeypatch):
    seeded(db)
    service = IndexReadService(db,max_entries=2,max_bytes=1024*1024)
    args = dict(code='cn:000300',provider='csindex',series='history.rolling',metric='pe_rolling')
    import stock_analysis.domains.indices.index_read_service as module
    original = module.index_valuation_views
    calls=[]
    def count(*args,**kwargs):
        calls.append(1)
        return original(*args,**kwargs)
    monkeypatch.setattr(module,'index_valuation_views',count)
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            bodies=list(pool.map(lambda _:service.chart(**args),range(4)))
        assert len(calls)==1 and all(body is bodies[0] for body in bodies)
        service.chart(**dict(args,metric='pb'))
        service.chart(**dict(args,metric='dividend_yield'))
        assert len(service.cache)==2
        assert service.cache_bytes==sum(map(len,service.cache.values()))
        with pytest.raises(ValueError):service.chart(**dict(args,metric='wrong'))
    finally:
        service.close()
    bounded=IndexReadService(db,max_bytes=len(bodies[0]))
    oversized=IndexReadService(db,max_bytes=1)
    try:
        bounded.chart(**args)
        bounded.chart(**dict(args,metric='pb'))
        assert bounded.cache_bytes<=bounded.max_bytes and len(bounded.cache)==1
        oversized.chart(**args)
        assert not oversized.cache and oversized.cache_bytes==0
    finally:
        bounded.close();oversized.close()


def test_midnight_invalidates_date_dependent_views(db,monkeypatch):
    seeded(db)
    from datetime import date
    import stock_analysis.domains.indices.index_read_service as module
    service=IndexReadService(db)
    try:
        first=service.catalog()
        class NextDay(date):
            @classmethod
            def today(cls):return date(2026,10,12)
        monkeypatch.setattr(module,'date',NextDay)
        assert json.loads(service.catalog())['revision']!=json.loads(first)['revision']
    finally:service.close()


def test_ordinary_writes_keep_cached_catalog_and_chart(db):
    row=seeded(db)
    service=IndexReadService(db)
    args=dict(code='cn:000300',provider='csindex',series='history.rolling',metric='pe_rolling')
    try:
        catalog=service.catalog()
        chart=service.chart(**args)
        revision=json.loads(service.status())['revision']
        instrument=db.ensure_instrument('sh601919','中远海控')
        with db.connection(write=True) as conn:
            conn.execute("INSERT INTO stock_recent_views VALUES(?,?)",(instrument,'2026-10-11T12:00:00Z'))
            conn.execute("UPDATE stock_picker_preferences SET selected_group='all' WHERE id=1")
            conn.execute("UPDATE collection_batches SET result_json='{}'")
            assert publish(conn,[row])['unchanged']==1
        assert json.loads(service.status())['revision']==revision
        assert service.catalog() is catalog and service.chart(**args) is chart
        # Neither uncommitted changes nor a rollback may invalidate good caches.
        with pytest.raises(RuntimeError):
            with db.connection(write=True) as conn:
                conn.execute("UPDATE index_catalog SET name='rolled back'")
                assert json.loads(service.status())['revision']==revision
                raise RuntimeError('rollback')
        assert service.catalog() is catalog and service.chart(**args) is chart
    finally:service.close()


@pytest.mark.parametrize('change',[
    "UPDATE index_valuation_observations SET pe_rolling=19 WHERE is_latest=1",
    "DELETE FROM index_valuation_observations WHERE observation_date='2026-10-07'",
    "UPDATE index_catalog SET name='沪深300新名称'",
    "UPDATE index_metric_definitions SET definition_json=json_set(definition_json,'$.evidence','new evidence')",
    "INSERT INTO sw_industries VALUES('420000','交通运输',1,NULL,'SW2021')",
    "UPDATE sw_industries SET name='新行业名称' WHERE code='420000'",
    "DELETE FROM sw_industries WHERE code='420000'",
])
def test_relevant_writes_invalidate_even_without_new_observation_id(db,change):
    seeded(db)
    with db.connection(write=True) as conn:
        conn.execute("INSERT INTO sw_industries VALUES('420000','交通运输',1,NULL,'SW2021')")
    service=IndexReadService(db)
    args=dict(code='cn:000300',provider='csindex',series='history.rolling',metric='pe_rolling')
    try:
        before=service.catalog()
        service.chart(**args)
        revision=json.loads(before)['revision']
        with db.connection(write=True) as conn:
            if change.startswith('INSERT INTO sw_industries'):
                conn.execute("DELETE FROM sw_industries WHERE code='420000'")
            conn.execute(change)
        assert json.loads(service.status())['revision']!=revision
        assert not service.cache and service.cache_bytes==0
        after=service.catalog()
        assert after is not before
        assert json.loads(service.chart(**args))['revision']==json.loads(after)['revision']
    finally:service.close()


def test_monthly_zero_dividend_and_lightweight_status(db,monkeypatch):
    _,_,row=source(db,'sws','analysis.daily')
    row['pe_rolling']=None
    with db.connection(write=True) as conn:
        publish(conn,[dict(row,frequency='monthly',dividend_yield=5,observation_date='2026-10-01'),
                      dict(row,frequency='monthly',dividend_yield=0,observation_date='2026-10-09')])
    service=IndexReadService(db)
    try:
        chart=json.loads(service.chart(code='cn:000300',provider='sws',series='analysis.daily',metric='dividend_yield'))
        view=chart['views']['dividend_yield']['10']
        assert view['current']==0 and view['count']==1
        assert view['rows_by_frequency']['day']==view['rows_by_frequency']['week']==[]
        assert view['rows_by_frequency']['month'][0]['dividend_yield_date']=='2026-10-09'
        monkeypatch.setattr('stock_analysis.domains.indices.index_read_service.read_indices',lambda *_:pytest.fail('status must not read coverage/history'))
        assert 'operations' in json.loads(service.status())
    finally:service.close()
