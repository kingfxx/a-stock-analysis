"""Task rate configuration and real shared-budget application, with no source network."""
import json

import pytest

from stock_analysis.collection.collection import PacingPolicy, ProviderBlocked, RequestCollector, begin_batch
from stock_analysis.collection.collection_management import CollectionManagement
from stock_analysis.collection.collection_rate_limits import describe, fields, task_for_batch, validate
from stock_analysis.domains.industries.industry_service import IndustryService
from stock_analysis.infrastructure.storage import Database
from test_collection_provider_pacing import Clock, Session, Response


@pytest.fixture
def management(tmp_path):
    db = Database(tmp_path/'rates.sqlite3'); db.initialize()
    return CollectionManagement(db, IndustryService(db))


def settings(**kwargs):
    return dict(fields(PacingPolicy()), **kwargs)


def operation(db, ref='test', scope=None, status='complete'):
    with db.connection(write=True) as conn:
        conn.execute('INSERT INTO index_collection_operations(operation_id,mode,scope_json,status,started_at) VALUES(?,?,?,?,?)',
                     (ref, 'incremental', json.dumps(scope or {}), status, '2026-10-25'))


@pytest.mark.parametrize('override', [dict(interval=0), dict(jitter=float('nan')), dict(rest_every=True),
    dict(window_requests=1.5), dict(max_concurrency=3), dict(rest_min=200, rest_max=10), dict(window=1)])
def test_validation(override):
    with pytest.raises(ValueError): validate(settings(**override))


def test_read_and_save_preserve_existing_profiles_and_shared_state(management):
    db = management.db
    before = management.read()
    index = next(t for t in before['tasks'] if t['task_key'] == 'indices')
    assert [r['current']['interval'] for r in index['rate_limits']] == [2, 3, 2]
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM collection_rate_limits').fetchone()[0] == 0
    collector = RequestCollector(db); state = collector.state('legu')
    state.update(budget_json='{"starts":[1800000000],"count":1}', cooldown_until='2030-01-01T00:00:00+00:00')
    collector.save_state(state)
    state = collector.state('legu')
    management.configure_rate('indices', 'legu', settings(interval=8))
    assert collector.state('legu') == state
    with db.connection() as conn:
        rates = describe(conn, 'indices', ['legu'])
        assert rates[0]['current']['interval'] == 8 and rates[0]['updated_at']
        assert describe(conn, 'market_congestion', ['legu'])[0]['current']['interval'] == 10
        assert conn.execute('SELECT count(*) FROM collection_schedules').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM index_collection_operations').fetchone()[0] == 0


def test_running_guard_and_provider_caps(management):
    for task, provider in [('indices','legu'),('sovereign_yields','eastmoney'),('trade_snapshot','eastmoney')]:
        with pytest.raises(ValueError): management.configure_rate(task, provider, settings(max_concurrency=2))
    with pytest.raises(ValueError): management.configure_rate('indices', 'sina', settings())
    with pytest.raises(ValueError): management.configure_rate('shipping_image_review', 'mot', settings())
    operation(management.db, status='running')
    with pytest.raises(ValueError, match='运行'): management.configure_rate('indices', 'csindex', settings())


def test_actual_pacing_shared_across_collectors_and_cooldown(management):
    db = management.db
    management.configure_rate('indices', 'legu', settings(interval=2, jitter=0, window_requests=2,
                                rest_every=1000, rest_min=0, rest_max=0))
    operation(db)
    batch = begin_batch(db, 'test', 'index.legu.pe', 'legu')
    clock = Clock(); session = Session(clock, [Response(), Response(), Response(429)])
    collector = RequestCollector(db, session=session, clock=clock.time, sleep=clock.sleep, uniform=lambda *_: 0)
    for _ in range(2): collector.get(batch,'legu','index.legu.pe','https://example.test',index_parallel=True)
    assert session.calls[1]-session.calls[0] == 2
    # A new collector keeps the existing provider budget/window instead of resetting it.
    other = RequestCollector(db, session=session, clock=clock.time, sleep=clock.sleep, uniform=lambda *_: 0)
    with pytest.raises(ProviderBlocked): other.get(batch,'legu','index.legu.pe','https://example.test',index_parallel=True)
    assert session.calls[2]-session.calls[0] >= 60
    assert other.state('legu')['cooldown_until']
    with db.connection() as conn:
        audit = json.loads(conn.execute('SELECT scope_json FROM collection_batches WHERE id=?',(batch,)).fetchone()[0])
        assert audit['rate_limits']['legu']['interval'] == 2
        assert audit['rate_limits']['legu']['cooldown'] == 3600


@pytest.mark.parametrize('scope,dataset,provider,expected', [
    ({},'index.cs.history','csindex','indices'),
    ({'datasets':['bond_zh_us_rate']},'index.legu.pe','legu','indices'),
    ({'datasets':['bond_zh_us_rate']},'bond_zh_us_rate','eastmoney','sovereign_yields'),
    ({'dataset':'market_congestion'},'market.legu.congestion.page','legu','market_congestion'),
    ({},'market.legu.congestion.page','legu','market_congestion'),
    ({'dataset':'shipping_monthly'},'shipping.mot_monthly','mot','shipping_monthly'),
    ({'dataset':'shipping_weekly'},'shipping.sse','sse','shipping_weekly'),
    ({},'index.calendar','sina',None),
])
def test_registered_batch_identity(management, scope, dataset, provider, expected):
    db = management.db; operation(db, scope=scope)
    batch = begin_batch(db, 'test', dataset, provider)
    with db.connection() as conn: assert task_for_batch(conn, batch, provider) == expected


def test_unregistered_backfill_and_stock_are_unchanged(management):
    db = management.db
    management.configure_rate('indices','legu',settings(interval=1))
    batch = begin_batch(db,'cli-backfill','index.legu.pe','legu')
    collector = RequestCollector(db)
    assert collector.batch_policy(batch,'legu',collector.policy) is collector.policy
    operation(db)
    stock_batch = begin_batch(db,'test','index.legu.pe','legu',domain='stock')
    assert collector.batch_policy(stock_batch,'legu',collector.policy) is collector.policy


@pytest.mark.parametrize('action,expected', [('financial_period','market_financial'),
    ('cap_quarter','trade_snapshot'), ('financial_history',None), ('trade_snapshot_history',None)])
def test_industry_tasks_and_cli_backfills_remain_distinct(management,action,expected):
    db=management.db
    with db.connection(write=True) as conn:
        run=conn.execute("INSERT INTO sw_update_runs(action,target,recheck,started_at,status) VALUES(?,'2026Q3',0,'2026-10-25','complete')",(action,)).lastrowid
    batch=begin_batch(db,f'sw_update_runs:{run}','market.financial.income','eastmoney')
    with db.connection() as conn: assert task_for_batch(conn,batch,'eastmoney') == expected


def test_configuration_inventory_and_real_timestamp(management):
    from stock_analysis.maintenance.maintenance import MaintenanceService, table_time
    db=management.db
    inventory=MaintenanceService(db).read()
    row=next(r for r in inventory['tables'] if r['name']=='collection_rate_limits')
    assert row['rows']==0 and row['category']=='运行与配置' and not row['updated_at']
    saved=management.configure_rate('market_financial','eastmoney',settings(interval=1.23456789,max_concurrency=2))
    with db.connection() as conn:
        row=conn.execute('SELECT * FROM collection_rate_limits').fetchone()
        assert json.loads(row['settings_json'])['interval']==1.23456789
        assert table_time(conn,'collection_rate_limits',row.keys())[0]==saved['updated_at']
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()
