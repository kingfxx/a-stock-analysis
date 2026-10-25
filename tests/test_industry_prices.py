import json
from types import SimpleNamespace

import pytest

from stock_analysis.domains.industries import industry_prices as prices; from stock_analysis.domains.market import market_price_backfill as backfill
from stock_analysis.domains.industries.industry_service import IndustryService
from stock_analysis.infrastructure.storage import Database


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(backfill, 'ROOT', tmp_path)
    db = Database(tmp_path/'test.sqlite3'); db.initialize()
    with db.connection(write=True) as conn:
        member = conn.execute("INSERT INTO sw_imports(obtained_at,content_hash,source_manifest_json,status) "
            "VALUES('2026-10-10','members','{}','complete')").lastrowid
        conn.execute('UPDATE sw_imports SET member_import_id=? WHERE id=?', (member, member))
    foundation = {'members':[{'stock_code':'600000','listing_date':'2000-01-01'}]}
    return SimpleNamespace(db=db), foundation


def fake_sdk(folder):
    source = backfill.archive(folder, 'prices-2026',
        [{'date':'2026-09-30','instrument':'600000.SH','close':12.3}],
        backfill.SOURCE, 'cn_stock_real_bar1d', {'filters':{'date':['2026-09-30','2026-09-30']}})
    backfill.save(folder/'prices-2026.json', {'source':source})
    backfill.prepare(folder)


def sync(setup, recheck=False):
    service, foundation = setup
    return prices.sync_quarter(service, foundation, '2026-09-30', '2026-09-30', ['600000'], recheck, lambda _:None)


def test_sync_publish_skip_and_recheck(setup, monkeypatch):
    calls = []
    def sdk(folder):
        calls.append(folder); fake_sdk(folder)
    monkeypatch.setattr(prices, 'run_sdk', sdk)
    assert sync(setup)['inserted']==1
    assert sync(setup)['reused'] and len(calls)==1
    assert sync(setup, True)['skipped']==1 and len(calls)==2
    assert not list(backfill.ROOT.rglob('candidate.json'))
    assert len(list(backfill.ROOT.rglob('*.json.gz')))==2
    with setup[0].db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM instruments').fetchone()[0]==0


def test_permission_failure_is_durable_and_retryable(setup, monkeypatch):
    def denied(folder):
        raise ValueError(prices.sdk_error('ArrowFlight Unauthorized 403 permission denied'))
    monkeypatch.setattr(prices, 'run_sdk', denied)
    result = sync(setup)
    assert result['status']=='failed' and '权限' in result['error']
    assert '已保存市值保留' in prices.price_status({'quarter_prices':result})
    db = setup[0].db
    with db.connection(write=True) as conn:
        conn.execute("INSERT INTO sw_update_runs(action,target,recheck,started_at,status,result_json) "
            "VALUES('cap_quarter','2026Q3',0,'2026-10-10','complete',?)",
            (json.dumps({'quarter_prices':result}),))
        assert conn.execute('SELECT count(*) FROM market_quarterly_prices').fetchone()[0]==0
    assert '权限' in IndustryService(db).status()['message']
    monkeypatch.setattr(prices, 'run_sdk', fake_sdk)
    assert sync(setup)['inserted']==1


def test_sdk_error_messages_and_timeout(setup, monkeypatch):
    assert '额度' in prices.sdk_error('quota exceeded')
    assert '过期' in prices.sdk_error('authentication expired')
    assert '权限' in prices.sdk_error('请先申请SDK使用权限')
    assert '凭据' in prices.sdk_error('FileNotFoundError')
    runtime = backfill.ROOT/'data/verification/tools/bigquant-runtime/Scripts/python.exe'
    runtime.parent.mkdir(parents=True); runtime.touch()
    monkeypatch.setattr(prices.subprocess, 'run', lambda *a,**k:SimpleNamespace(
        returncode=1, stdout='SDK crash including secret token', stderr='secret token'))
    with pytest.raises(ValueError, match='有效结果'):
        prices.run_sdk(backfill.ROOT)
    def timed_out(*a, **k):
        raise prices.subprocess.TimeoutExpired('sdk', 120)
    monkeypatch.setattr(prices.subprocess, 'run', timed_out)
    with pytest.raises(ValueError, match='超时'):
        prices.run_sdk(backfill.ROOT)


def test_cap_task_reuses_saved_snapshot_with_prices_without_separate_sdk(setup, monkeypatch):
    from stock_analysis.domains.industries import industry_updates as updates
    from stock_analysis.domains.market import market_trade_snapshot as snapshot
    from market_snapshot_fixture import collector, cap_record
    from test_industries import fixture_bundle
    db=setup[0].db
    service=IndustryService(db)
    service.import_bundle(fixture_bundle(),capture_local=False)
    with db.connection(write=True) as conn:
        member=service.foundation()['member_import_id']
        conn.execute("INSERT INTO sw_cap_quarter_rosters VALUES('2026Q3','2026-09-30','current_constituents_backfill',?)",(member,))
        conn.execute("INSERT INTO sw_cap_quarter_members SELECT '2026Q3',stock_code,industry_code FROM sw_memberships WHERE import_id=?",(member,))
    records=[cap_record(r['stock_code'],'2026-09-30',r['total_cap']) for r in fixture_bundle()['caps']]
    client,session=collector(db,records)
    snapshot.collect_day(db,'initial','2026-09-30',quarter='2026Q3',http=client,progress=lambda _:None)
    before=len(session.calls)
    monkeypatch.setattr(snapshot,'RequestCollector',lambda *a,**k:client)
    monkeypatch.setattr('stock_analysis.domains.industries.industry_memberships.ensure_daily_memberships',lambda *a,**k:{})
    monkeypatch.setattr(prices,'sync_quarter',lambda *a,**k:pytest.fail('snapshot must include raw prices without a separate SDK'))
    result=updates.perform(service,'cap_quarter','2026Q3',False,lambda _:None)
    assert result['snapshot']['skipped'] and len(session.calls)==before
    assert result['quarter_prices']['status']=='complete'
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM market_trade_snapshot WHERE close_price=10').fetchone()[0]==3
