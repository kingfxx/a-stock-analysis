"""Semantic and lifecycle regressions for the two removed resident dependencies."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
from threading import Barrier
from types import SimpleNamespace

import pytest

from stock_analysis.domains.stocks import stock_search
from stock_analysis.domains.stocks.stock_library import StockLibrary
from stock_analysis.infrastructure import storage
from stock_analysis.infrastructure.storage import Database
from stock_analysis.maintenance.database_restore import RestoreManager


def test_old_database_and_restore_prepare_search_without_new_table(tmp_path, monkeypatch):
    old = Database(tmp_path / 'old.sqlite3')
    with monkeypatch.context() as patch:
        patch.setattr(storage, 'MIGRATIONS', storage.MIGRATIONS[:48])
        old.initialize()
        old.ensure_instrument('601919', '中远海控')
        with old.connection() as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            columns = {r['name'] for r in conn.execute('PRAGMA table_info(instruments)')}
        backup = old.backup(tmp_path / 'old-copy.sqlite3')
    old.initialize()
    with old.connection() as conn:
        assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} == tables
        assert {r['name'] for r in conn.execute('PRAGMA table_info(instruments)')} - columns == {
            'search_pinyin', 'search_initials'}
    assert 'zhongyuanhaikong zyhk' in StockLibrary(old).read()['stocks'][0]['search']
    current = Database(tmp_path / 'current.sqlite3')
    current.initialize()
    target = current.path.parent / 'backups' / 'old.sqlite3'
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(backup.read_bytes())
    manager = RestoreManager(current)
    try:
        preview = manager.prepare({'backup': target.name})
        assert preview['upgraded'] and preview['added_tables'] == []
        # Locate only the manager's disposable prepared candidate, not the source backup.
        candidates = [p for p in manager.root.rglob('*.sqlite3') if p != target]
        assert any('zhongyuanhaikong' in StockLibrary(Database(p)).read()['stocks'][0]['search']
                   for p in candidates)
    finally:
        manager.cancel()


def test_search_reads_and_same_name_writes_never_spawn(tmp_path, monkeypatch):
    db = Database(tmp_path / 'db.sqlite3'); db.initialize()
    identity = db.ensure_instrument('601919', '中远海控')
    def unexpected(*args, **kwargs):
        raise AssertionError('ordinary reads must not start a worker')
    monkeypatch.setattr(stock_search, 'generate_keys', unexpected)
    assert db.ensure_instrument('601919', '中远海控') == identity
    assert db.ensure_instrument('601919') == identity
    db.initialize()
    assert StockLibrary(db).read()['stocks'] == [dict(code='601919', name='中远海控',
        search='601919 中远海控 zhongyuanhaikong zyhk')]


def test_name_and_keys_publish_together_and_failure_keeps_old(tmp_path, monkeypatch):
    db = Database(tmp_path / 'db.sqlite3'); db.initialize()
    db.ensure_instrument('601919', '中远海控')
    with monkeypatch.context() as patch:
        patch.setattr(stock_search, 'generate_keys', lambda names: (_ for _ in ()).throw(OSError('worker failed')))
        with pytest.raises(OSError):
            db.ensure_instrument('601919', '重庆银行')
        assert StockLibrary(db).read()['stocks'][0]['name'] == '中远海控'
    db.ensure_instrument('601919', '重庆银行')
    assert StockLibrary(db).read()['stocks'][0]['search'] == '601919 重庆银行 chongqingyinhang cqyh'
    db.ensure_instrument('601919', '')
    assert StockLibrary(db).read()['stocks'][0]['search'] == '601919   '


def test_initialization_failure_can_retry_and_partial_keys_are_rebuilt(tmp_path, monkeypatch):
    db = Database(tmp_path / 'db.sqlite3'); db.initialize()
    with db.connection(write=True) as conn:
        conn.execute("INSERT INTO instruments(exchange,code,name,created_at,search_initials) "
                     "VALUES ('sh','601919','中远海控',?,'stale')", (storage.utc_now(),))
    with monkeypatch.context() as patch:
        patch.setattr(stock_search, 'generate_keys', lambda names: (_ for _ in ()).throw(OSError('failed')))
        with pytest.raises(storage.StorageError): StockLibrary(db).read()
        with pytest.raises(OSError): db.initialize()
    db.initialize()
    assert StockLibrary(db).read()['stocks'][0]['search'].endswith('zhongyuanhaikong zyhk')


def test_concurrent_renames_keep_matching_keys(tmp_path, monkeypatch):
    db = Database(tmp_path / 'db.sqlite3'); db.initialize()
    db.ensure_instrument('601919')
    barrier = Barrier(2)
    def generate(names):
        barrier.wait(timeout=5)
        return [(names[0] + '-pinyin', names[0] + '-initials')]
    monkeypatch.setattr(stock_search, 'generate_keys', generate)
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(db.ensure_instrument, '601919', name) for name in ('First', 'Second')]
        for future in futures: future.result(timeout=10)
    with db.connection() as conn:
        row = conn.execute('SELECT * FROM instruments').fetchone()
        assert row['search_pinyin'] == row['name'] + '-pinyin'
        assert row['search_initials'] == row['name'] + '-initials'


@pytest.mark.parametrize('output', [b'null', b'[]', b'[[1,2]]', b'[["one"]]', b'not-json'])
def test_worker_protocol_rejects_invalid_results(monkeypatch, output):
    monkeypatch.setattr(stock_search.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout=output))
    with pytest.raises(OSError): stock_search.generate_keys(['中远海控'])


def test_worker_timeout_is_reported(monkeypatch):
    def fail(*args, **kwargs): raise subprocess.TimeoutExpired('worker', 30)
    monkeypatch.setattr(stock_search.subprocess, 'run', fail)
    with pytest.raises(OSError): stock_search.generate_keys(['中远海控'])


def test_main_process_paths_and_pure_parser_have_no_heavy_imports(tmp_path):
    script = '''
import sys
from stock_analysis.domains.valuation.institution_forecast_parser import parse_forecast
assert not {'sqlite3','requests','pandas','pypinyin'} & sys.modules.keys()
from stock_analysis.http import server
from stock_analysis.infrastructure.storage import Database
from stock_analysis.domains.stocks.stock_library import StockLibrary
db=Database(sys.argv[1]);db.initialize();db.ensure_instrument('601919','中远海控')
assert 'zyhk' in StockLibrary(db).read()['stocks'][0]['search']
assert parse_forecast('<title>601919</title>本年度暂无机构做出业绩预测','601919',[2026])[0]['profit'] is None
assert not any(n=='pandas' or n.startswith('pandas.') or n=='pypinyin' or n.startswith('pypinyin.') for n in sys.modules)
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path / 'probe.sqlite3')],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stdout + result.stderr
