import json
from types import SimpleNamespace

import pytest

from quarterly_dashboard import industry_prices as prices, market_price_backfill as backfill
from quarterly_dashboard.industry_service import IndustryService
from quarterly_dashboard.storage import Database


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


def test_cap_task_calls_prices_after_saving_even_without_cap_gaps(setup, monkeypatch, tmp_path):
    from quarterly_dashboard import industry_updates as updates
    service, foundation = setup
    foundation['members'] += [{'stock_code':str(n).zfill(6),'listing_date':'2000-01-01'} for n in range(3999)]
    service.foundation = lambda:foundation
    service.directory = tmp_path/'industry'
    service.cap_known = lambda day:{'600000'}
    events = []
    service.import_bundle = lambda *a,**k:events.append('saved cap') or 1
    monkeypatch.setattr('quarterly_dashboard.industry_memberships.ensure_daily_memberships', lambda *a:{})
    monkeypatch.setattr(updates, 'target_trade_date', lambda *a:'2026-09-30')
    monkeypatch.setattr(updates, 'cap_roster', lambda *a:{'members':[{'stock_code':'600000'}], 'composition':'test'})
    def sync_prices(*args):
        events.append('prices'); return {'status':'failed','error':'BigQuant 权限不足'}
    monkeypatch.setattr(prices, 'sync_quarter', sync_prices)
    result = updates.perform(service, 'cap_quarter', '2026Q3', False, lambda _:None)
    assert events==['saved cap','prices']
    assert result['requested_count']==0 and result['quarter_prices']['status']=='failed'
