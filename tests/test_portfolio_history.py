"""History-specific publication and recovery against isolated databases and responses."""

import json
import sqlite3
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest
from requests import Response

from stock_analysis.collection.collection import RequestCollector, read_source
from stock_analysis.collection.collection_management import CollectionManagement, validate_params
from stock_analysis.domains.portfolio import history_collection as history
from stock_analysis.domains.portfolio.history_prices import read_closes
from stock_analysis.infrastructure.storage import Database, SyncKey, SyncResult, utc_now
from stock_analysis.maintenance.maintenance import MaintenanceService


CODE = '300622'


class Source:
    def __init__(self, dates):
        self.dates = dates
        self.calls = []
        self.close_price = '5.123456'
        self.responses = []
        self.transform = lambda rows: rows
        self.after_request = lambda: None

    def get(self, url, *, params, headers, timeout):
        symbol, frequency, start, end, count, adjust = params['param'].split(',')
        assert symbol == 'sz' + CODE and frequency == 'day' and not start and not adjust
        self.calls.append((end, int(count)))
        rows = [[day, '5', self.close_price, '6', '4', '100'] for day in self.dates if day <= end][-int(count):]
        response = Response()
        response.status_code = self.responses.pop(0) if self.responses else 200
        response.headers['Retry-After'] = '7200'
        response._content = json.dumps({'code': 0, 'data': {symbol: {'day': self.transform(rows)}}}).encode()
        self.after_request()
        return response

    def close(self):
        pass


@pytest.fixture
def isolated_db():
    # Keep the gzip archive's atomic temp file within Windows path limits.
    with TemporaryDirectory(prefix='portfolio-', dir=Path(__file__).resolve().parents[1]/'data'/'verification') as directory:
        db = Database(Path(directory)/'isolated.sqlite3')
        assert db.initialize()['schema_version'] >= 53
        yield db


@pytest.fixture
def setup(isolated_db, monkeypatch):
    db = isolated_db
    manager = CollectionManagement(db, None)
    now = SimpleNamespace(value=1_790_000_000.)

    def scope(dates):
        monkeypatch.setattr(history, 'WINDOWS', (('sz', CODE, dates[0], dates[-1], len(dates)),))
        monkeypatch.setattr(history, 'local_market_days', lambda *args: dates)
        source = Source(dates)

        def reserve():
            with db.connection(write=True) as conn:
                return manager.reserve(conn, history.TASK_KEY, {'mode': 'incremental'})

        def collect(ref):
            def sleep(seconds):
                now.value += seconds

            return history.run(db, ref, collector_factory=lambda db, **kwargs: RequestCollector(
                db, session=source, clock=lambda: now.value, sleep=sleep, uniform=lambda lo, hi: lo, **kwargs))

        return source, reserve, collect

    return db, manager, now, scope


def test_price_precision_provenance_revisions_and_stock_initialisation(setup, monkeypatch):
    db, manager, _, scope = setup
    days = ['2024-08-13', '2024-08-14', '2024-08-15']
    source, reserve, collect = scope(days)
    source.transform = lambda rows: rows[:1] + rows[2:]
    assert not next(t for t in manager.read()['tasks'] if t['task_key'] == history.TASK_KEY)['schedule']['enabled']
    with pytest.raises(ValueError):
        validate_params(history.TASK_KEY, {}, scheduled=True)
    with pytest.raises(ValueError):
        validate_params(history.TASK_KEY, {'start': days[0]})

    original = reserve()
    result = collect(original)
    assert result['network_requests'] == 1 and not result['errors'], result['errors']
    with db.shared_reader() as reader:
        first = read_closes(reader, [('sz', CODE), ('sz', CODE), ('sh', CODE)], days[0], days[-1])
        close, origin = first['sz', CODE][days[0]]
        assert close == Decimal('5.123456') and origin[0] == 'portfolio_historical_closes'
        assert days[1] not in first['sz', CODE] and first['sh', CODE] == {}
        assert not reader.conn.execute('SELECT 1 FROM instruments').fetchall()
    assert manager.stocks.page_status(CODE)['needs_initial']

    repeated = reserve()
    collect(repeated)
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM collection_sources').fetchone()[0] == 2
        assert conn.execute('SELECT count(*) FROM portfolio_historical_closes').fetchone()[0] == 2
        assert conn.execute('SELECT source_id FROM portfolio_historical_closes ORDER BY revision LIMIT 1').fetchone()[0] == origin[2]

    source.close_price = '5.123457'
    source.transform = lambda rows: rows
    changed = reserve()
    collect(changed)
    for ref in (original, repeated, changed):
        assert manager.detail(history.TASK_KEY, ref)['status'] == 'complete'
    reused = reserve()
    assert collect(reused)['network_requests'] == 0
    with db.connection() as conn:
        assert conn.execute('SELECT coverage_status FROM collection_batches WHERE operation_id=?', (original,)).fetchone()[0] == 'partial'
        old, new = conn.execute('SELECT * FROM portfolio_historical_closes WHERE trade_date=? ORDER BY revision', (days[0],)).fetchall()
        assert (old['close'], new['close']) == ('5.123456', '5.123457')
        assert conn.execute('SELECT status FROM collection_batches WHERE operation_id=?', (reused,)).fetchone()[0] == 'reused'
    for fact in (old, new):
        raw, meta = read_source(db, fact['source_id'])
        assert meta['batch_id'] == fact['batch_id'] and fact['close'].encode() in raw
    with pytest.raises(sqlite3.IntegrityError), db.connection(write=True) as conn:
        conn.execute('UPDATE portfolio_historical_closes SET batch_id=? WHERE source_id=?', (new['batch_id'], old['source_id']))

    # New stock initialization still reserves the normal complete work graph.
    monkeypatch.setattr('stock_analysis.domains.stocks.collection_operations.Thread', lambda **_: SimpleNamespace(start=lambda: None))
    ref = manager.stocks.start({'codes': [CODE]}, initial=True)['operation_ref']
    assert manager.stocks.detail(ref)['mode'] == 'full'
    identity = db.ensure_instrument(CODE)
    run_id = db.start_sync(SyncKey(identity, 'prices_raw', 'tencent'), parser_version='test', methodology_version='test')
    db.complete_sync(run_id, SyncResult(1, days[0], days[0], days[0]), lambda conn: conn.execute(
        "INSERT INTO raw_daily_prices VALUES(?,'tencent',?,7,7,7,7,100,'[]','hash',?,?)",
        (identity, days[0], utc_now(), run_id)))
    with db.shared_reader() as reader:
        prices = read_closes(reader, [('sz', CODE), ('sh', CODE)], days[0], days[-1])
        assert prices['sz', CODE][days[0]][0] == Decimal('7')
        assert prices['sz', CODE][days[1]][0] == Decimal('5.123457')
        assert prices['sh', CODE] == {}
    inventory = {row['name']: row for row in MaintenanceService(db).read()['tables']}
    table = inventory['portfolio_historical_closes']
    assert (table['category'], table['rows'], table['time_basis'], table['updated_at']) == ('持仓分析', 5, 'written_at', new['written_at'])


def test_cross_page_truncation_conflict_and_resume(setup):
    db, manager, _, scope = setup
    days = [(date(2022, 1, 1) + timedelta(days=i)).isoformat() for i in range(650)]
    source, reserve, collect = scope(days)
    source.transform = lambda rows: rows[50:]
    truncated = reserve()
    assert collect(truncated)['network_requests'] == 1
    assert manager.detail(history.TASK_KEY, truncated)['status'] == 'failed'

    def conflict(rows):
        if len(rows) < 640:
            rows[-1][2] = '5.123457'
        return rows

    source.transform = conflict
    mismatched = reserve()
    result = collect(mismatched)
    assert result['network_requests'] == 2, result['errors']
    with db.connection() as conn:
        assert '跨页收盘价不一致' in conn.execute('SELECT error FROM collection_batches WHERE operation_id=?', (mismatched,)).fetchone()[0]
        assert not conn.execute('SELECT 1 FROM portfolio_historical_closes').fetchone()

    source.transform = lambda rows: rows
    repaired = reserve()
    assert collect(repaired)['network_requests'] == 2
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM portfolio_historical_closes').fetchone()[0] == 650
        assert conn.execute('SELECT count(DISTINCT source_id) FROM portfolio_historical_closes').fetchone()[0] == 2


def test_retry_cooldown_and_cancellation(setup):
    db, manager, now, scope = setup
    source, reserve, collect = scope(['2024-08-15'])
    source.responses = [503, 429]
    denied = reserve()
    result = collect(denied)
    assert result['retries'] == 1 and result['network_requests'] == 2, result['errors']
    assert collect(reserve())['network_requests'] == 0
    with db.connection() as conn:
        assert [r[0] for r in conn.execute('SELECT http_status FROM collection_sources ORDER BY id')] == [503, 429]
        assert not conn.execute('SELECT 1 FROM portfolio_historical_closes').fetchone()
    now.value += 7201
    source.after_request = lambda: manager.cancel(history.TASK_KEY)
    stopped = reserve()
    assert collect(stopped)['network_requests'] == 1
    assert manager.detail(history.TASK_KEY, stopped)['status'] == 'cancelled'
    with db.connection() as conn:
        assert not conn.execute('SELECT 1 FROM portfolio_historical_closes').fetchone()
    source.after_request = lambda: None
    assert collect(reserve())['network_requests'] == 1
