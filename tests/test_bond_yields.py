import json
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import requests

from stock_analysis.domains.market import bond_yields as by
from stock_analysis.collection.collection import Cancelled, ProviderBlocked, RequestCollector, encoded
from stock_analysis.domains.indices import index_collection as ic
from stock_analysis.domains.indices.index_collection import start_operation
from stock_analysis.domains.indices import index_price_derived; from stock_analysis.domains.market import market_congestion
from stock_analysis.domains.market.bond_yield_audit import audit as audit_bond_yields
from stock_analysis.maintenance.maintenance import MaintenanceService
from stock_analysis.infrastructure.storage import Database


@pytest.fixture
def db(tmp_path):
    result = Database(tmp_path / 'bond.sqlite3')
    result.initialize()
    return result


class Clock:
    def __init__(self):
        self.now = 10_000

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def response(payload=None, status=200, headers=None, body=None):
    result = requests.Response()
    result.status_code = status
    result._content = body if body is not None else json.dumps(payload or {}, ensure_ascii=False, default=str).encode()
    result.headers.update(headers or {'Content-Type': 'application/json'})
    result.encoding = 'utf-8'
    return result


class Session:
    def __init__(self, clock, results):
        self.clock = clock
        self.results = list(results)
        self.calls = []

    def mount(self, *args):
        pass

    def get(self, url, **kwargs):
        self.calls.append((self.clock.time(), url, copy.deepcopy(kwargs)))
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        self.clock.sleep(1)
        return item


def http(db, results, clock=None):
    clock = clock or Clock()
    session = Session(clock, results)
    client = RequestCollector(db, session=session, clock=clock.time, sleep=clock.sleep,
                              uniform=lambda low, high: low)
    return client, session, clock


def raw_row(day='2026-10-19', **overrides):
    row = {'SOLAR_DATE': day}
    for field in by.FIELDS:
        row[field] = None
    row.update({'EMM00166466': Decimal('3.125000000000000001'),
                'EMM00166462': Decimal('0'), 'EMM00166469': Decimal('-0.25')})
    row.update(overrides)
    return row


def payload(rows, *, pages=1, count=None):
    return {'result': {'pages': pages, 'count': len(rows) if count is None else count, 'data': rows}}


def raw_json(value):
    # Decimal source values are emitted as numeric-compatible strings; parse_page
    # applies Decimal to both JSON numbers and strings without binary float loss.
    return json.dumps(value, ensure_ascii=False, default=str).encode()


def operation(db):
    return start_operation(db, mode='incremental', providers=[], datasets=[by.DATASET])


def collect_year(db, results, *, year=2026, start='2026-10-01', end='2026-10-31',
                 operation_id=None, cancelled=lambda: False, progress=lambda _message: None):
    operation_id = operation_id or operation(db)
    client, session, clock = http(db, results)
    try:
        result = by._fetch_year(db, client, operation_id, year, start, end, 'manual', None,
                                cancelled, progress)
    except BaseException:
        with db.connection(write=True) as conn:
            conn.execute("UPDATE index_collection_operations SET status='failed' WHERE operation_id=?", (operation_id,))
        raise
    with db.connection(write=True) as conn:
        conn.execute("UPDATE index_collection_operations SET status='complete' WHERE operation_id=?", (operation_id,))
    return result, session, clock, operation_id


def test_decimal_parser_preserves_precision_zero_negative_and_missing():
    parsed = by.parse_page(raw_json(payload([raw_row()])))[2][0]
    assert parsed['china_10y'] == '3.125000000000000001'
    assert parsed['china_5y'] == '0'
    assert parsed['china_30y'] == '-0.25'
    assert parsed['china_2y'] is None
    assert by.parse_page(raw_json(payload([raw_row(EMM00166466=Decimal('0.000000000000000001'))])))[2][0]['china_10y'] == '0.000000000000000001'


def test_fixed_annual_filter_paginates_and_rejects_truncation_without_publication(db, monkeypatch):
    monkeypatch.setitem(by.PARAMS, 'ps', '2')
    page1 = [raw_row('2026-10-03'), raw_row('2026-10-02')]
    page2 = [raw_row('2026-10-01')]
    op = operation(db)
    client, session, _ = http(db, [response(payload(page1, pages=2, count=3)),
                                    response(payload(page2, pages=2, count=3))])
    result = by._fetch_year(db, client, op, 2026, '2026-10-01', '2026-10-03', 'manual', None,
                            lambda: False, lambda _message: None)
    assert result['total'] == 3
    assert [call[2]['params']['filter'] for call in session.calls] == [
        "(SOLAR_DATE>='2026-10-01')(SOLAR_DATE<='2026-10-03')"] * 2
    assert [call[2]['params']['p'] for call in session.calls] == [1, 2]
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM sovereign_yield_observations').fetchone()[0] == 3
    with db.connection(write=True) as conn:
        conn.execute("UPDATE index_collection_operations SET status='complete' WHERE operation_id=?", (op,))

    bad_op = operation(db)
    truncated, failed_session, _ = http(db, [response(payload(page1, pages=2, count=3)),
                                              response(payload([], pages=2, count=3))])
    with pytest.raises(ValueError):
        by._fetch_year(db, truncated, bad_op, 2026, '2026-10-01', '2026-10-03', 'manual', None,
                       lambda: False, lambda _message: None)
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM sovereign_yield_observations').fetchone()[0] == 3
        assert [r[0] for r in conn.execute('SELECT parse_status FROM collection_sources ORDER BY id DESC LIMIT 2')][::-1] == ['failed', 'failed']


@pytest.mark.parametrize('bad_rows', [
    [raw_row('2026-10-02'), raw_row('2026-10-03')],
    [raw_row('2026-10-03'), raw_row('2026-10-03')],
    [raw_row('2026-11-01')],
])
def test_annual_pages_reject_wrong_order_duplicates_and_out_of_range(db, bad_rows):
    client, _, _ = http(db, [response(payload(bad_rows))])
    with pytest.raises(ValueError):
        by._fetch_year(db, client, operation(db), 2026, '2026-10-01', '2026-10-31', 'manual', None,
                       lambda: False, lambda _message: None)
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM sovereign_yield_observations').fetchone()[0] == 0


def test_cancel_after_last_page_keeps_sources_and_does_not_publish(db, monkeypatch):
    monkeypatch.setitem(by.PARAMS, 'ps', '1')
    cancelled = {'value': False}
    def progress(message):
        if '第 2/2 页' in message:
            cancelled['value'] = True
    client, _, _ = http(db, [response(payload([raw_row('2026-10-02')], pages=2, count=2)),
                              response(payload([raw_row('2026-10-01')], pages=2, count=2))])
    with pytest.raises(Cancelled):
        by._fetch_year(db, client, operation(db), 2026, '2026-10-01', '2026-10-02', 'manual', None,
                       lambda: cancelled['value'], progress)
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM sovereign_yield_observations').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM collection_sources').fetchone()[0] == 2
        assert conn.execute('SELECT parse_status FROM collection_sources ORDER BY id').fetchall()[0][0] == 'failed'


def test_eastmoney_request_uses_shared_provider_state_and_healthy_probe_clears_cooldown(db):
    clock = Clock()
    denied, _, _ = http(db, [response(status=403)], clock)
    batch_op = operation(db)
    from stock_analysis.collection.collection import begin_batch
    batch = begin_batch(db, batch_op, by.DATASET, 'eastmoney')
    with pytest.raises(ProviderBlocked):
        denied.get(batch, 'eastmoney', by.DATASET, by.URL, by.PARAMS, expect='json',
                   healthy_response=lambda item: by._is_healthy(item))
    clock.now += 3601
    healthy, session, _ = http(db, [response(payload([]))], clock)
    healthy.get(batch, 'eastmoney', by.DATASET, by.URL, by.PARAMS, expect='json',
                healthy_response=lambda item: by._is_healthy(item))
    assert healthy.state('eastmoney')['cooldown_until'] is None
    assert len(session.calls) == 1
    with db.connection() as conn:
        columns = [row['name'] for row in conn.execute('PRAGMA table_info(collection_provider_state)')]
        assert 'source_system' in columns
        assert conn.execute("SELECT 1 FROM collection_provider_state WHERE source_system='eastmoney'").fetchone()


def test_recent_expiry_rereads_same_range_and_publishes_late_us_revision(db, monkeypatch):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 10, 20, 12, tzinfo=timezone.utc)
            return value if tz else value.replace(tzinfo=None)

    # Freeze the operation's local planning date so the same bounded window is
    # deterministic and remains independent of the machine running the test.
    monkeypatch.setattr(by, 'datetime', FrozenDatetime)
    op = operation(db)
    # Seed the recent observation through the same checked publishing path.
    _, _, _, _ = collect_year(db, [response(payload([raw_row(EMG00001310=None)]))],
                              start='2026-10-19', end='2026-10-19', operation_id=op)
    with db.connection(write=True) as conn:
        row = conn.execute("SELECT id,result_json FROM collection_batches WHERE dataset=? ORDER BY id DESC LIMIT 1", (by.DATASET,)).fetchone()
        stored = json.loads(row['result_json'])
        for item in stored['freshness'].values():
            item['last_checked_at'] = '2020-01-01T00:00:00+00:00'
        conn.execute('UPDATE collection_batches SET result_json=? WHERE id=?', (encoded(stored), row['id']))
    refresh_op = operation(db)
    client, session, _ = http(db, [response(payload([raw_row(EMG00001310=Decimal('4.125'))]))])
    refreshed = by.collect(db, refresh_op, collector=client, cancelled=lambda: False, progress=lambda _message: None)
    assert session.calls[0][2]['params']['filter'] == "(SOLAR_DATE>='2026-10-06')(SOLAR_DATE<='2026-10-20')"
    assert refreshed['annual_batches'][0]['revised'] == 1
    with db.connection() as conn:
        rows = conn.execute("SELECT version,is_latest,china_10y,us_10y,source_id FROM sovereign_yield_observations WHERE observation_date='2026-10-19' ORDER BY version").fetchall()
        assert [(r['version'], r['is_latest'], r['us_10y']) for r in rows] == [(1, 0, None), (2, 1, '4.125')]
        assert rows[0]['source_id'] != rows[1]['source_id']


def test_cross_year_empty_tail_stops_at_same_range_and_recovers_after_expiry(db, monkeypatch):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 1, 10, 12, tzinfo=timezone.utc)
            return value if tz else value.replace(tzinfo=None)

    monkeypatch.setattr(by, 'datetime', FrozenDatetime)
    seed_op = operation(db)
    collect_year(db, [response(payload([raw_row('2025-12-31')]))], year=2025,
                 start='2025-12-31', end='2025-12-31', operation_id=seed_op)

    def expire_latest_check():
        with db.connection(write=True) as conn:
            row = conn.execute("SELECT id,result_json FROM collection_batches WHERE dataset=? ORDER BY id DESC LIMIT 1", (by.DATASET,)).fetchone()
            saved = json.loads(row['result_json'])
            for item in saved.get('freshness', {}).values():
                item['last_checked_at'] = '2020-01-01T00:00:00+00:00'
            conn.execute('UPDATE collection_batches SET result_json=? WHERE id=?', (encoded(saved), row['id']))

    expire_latest_check()
    first_op = operation(db)
    first, first_session, _ = http(db, [response(payload([]))])
    stopped = by.collect(db, first_op, collector=first, cancelled=lambda: False, progress=lambda _message: None)
    assert stopped['status'] == 'empty'
    assert stopped['start'] == '2025-12-27' and stopped['end'] == '2026-01-10'
    assert [call[2]['params']['filter'] for call in first_session.calls] == [
        "(SOLAR_DATE>='2025-12-27')(SOLAR_DATE<='2025-12-31')"]
    with db.connection() as conn:
        last = conn.execute("SELECT scope_start,scope_end,status FROM collection_batches WHERE dataset=? ORDER BY id DESC LIMIT 1", (by.DATASET,)).fetchone()
        assert (last['scope_start'], last['scope_end'], last['status']) == ('2025-12-27', '2025-12-31', 'failed')
    with db.connection(write=True) as conn:
        conn.execute("UPDATE index_collection_operations SET status='failed' WHERE operation_id=?", (first_op,))

    # Force the one-hour empty-response TTL to expire; the next click must retry
    # the same older year before advancing to the newer year.
    expire_latest_check()
    retry_op = operation(db)
    retry, retry_session, _ = http(db, [response(payload([raw_row('2025-12-31')])),
                                        response(payload([raw_row('2026-01-10')]))])
    recovered = by.collect(db, retry_op, collector=retry, cancelled=lambda: False, progress=lambda _message: None)
    assert recovered['status'] == 'complete'
    assert [call[2]['params']['filter'] for call in retry_session.calls] == [
        "(SOLAR_DATE>='2025-12-27')(SOLAR_DATE<='2025-12-31')",
        "(SOLAR_DATE>='2026-01-01')(SOLAR_DATE<='2026-01-10')"]


def test_withdrawal_candidate_keeps_old_value_and_later_recovery_is_partial_then_revision(db):
    op = operation(db)
    collect_year(db, [response(payload([raw_row(EMM00166466=Decimal('3.2'))]))],
                 start='2026-10-19', end='2026-10-19', operation_id=op)
    candidate_op = operation(db)
    candidate, _, _, _ = collect_year(db, [response(payload([raw_row(EMM00166466=None)]))],
                                       start='2026-10-19', end='2026-10-19', operation_id=candidate_op)
    assert candidate['pending_withdrawals']
    with db.connection() as conn:
        fact = conn.execute("SELECT china_10y,version FROM sovereign_yield_observations WHERE observation_date='2026-10-19' AND is_latest=1").fetchone()
        assert (fact['china_10y'], fact['version']) == ('3.2', 1)
        batch = conn.execute('SELECT status,result_json FROM collection_batches WHERE id=(SELECT max(id) FROM collection_batches WHERE dataset=?)', (by.DATASET,)).fetchone()
        assert batch['status'] == 'complete'  # execution succeeded, coverage is partial/unknown
        assert json.loads(batch['result_json'])['pending_withdrawals']
    recover_op = operation(db)
    recovered, _, _, _ = collect_year(db, [response(payload([raw_row(EMM00166466=Decimal('3.3'))]))],
                                      start='2026-10-19', end='2026-10-19', operation_id=recover_op)
    assert recovered['revised'] == 1
    with db.connection() as conn:
        assert conn.execute("SELECT china_10y FROM sovereign_yield_observations WHERE observation_date='2026-10-19' AND is_latest=1").fetchone()[0] == '3.3'


def test_month_old_withdrawal_candidate_is_resolved_before_recent_ttl_skip(db, monkeypatch):
    frozen = {'now': datetime(2026, 9, 10, 12, tzinfo=timezone.utc)}
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls.fromtimestamp(frozen['now'].timestamp(), tz=timezone.utc)
            return value if tz else value.replace(tzinfo=None)

    monkeypatch.setattr(by, 'datetime', FrozenDatetime)
    day = '2026-09-01'
    first_op = operation(db)
    collect_year(db, [response(payload([raw_row(day, EMM00166466=Decimal('3.2'))]))],
                 start=day, end=day, operation_id=first_op)
    candidate_op = operation(db)
    candidate, _, _, _ = collect_year(db, [response(payload([raw_row(day, EMM00166466=None)]))],
                                          start=day, end=day, operation_id=candidate_op)
    assert candidate['pending_withdrawals']
    with db.connection() as conn:
        candidate_batch = conn.execute("SELECT id,result_json FROM collection_batches WHERE dataset=? ORDER BY id DESC LIMIT 1", (by.DATASET,)).fetchone()
        candidate_id = candidate_batch['id']
        assert json.loads(candidate_batch['result_json'])['pending_withdrawals'][0]['date'] == day

    # A later successful observation advances the ordinary water mark beyond the
    # unresolved date and keeps recent freshness separate from that old candidate.
    newer_op = operation(db)
    collect_year(db, [response(payload([raw_row('2026-10-19')]))], start='2026-10-19', end='2026-10-19',
                 operation_id=newer_op)
    frozen['now'] = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)

    resumed_op = operation(db)
    recovery, session, _ = http(db, [
        response(payload([raw_row(day, EMM00166466=Decimal('3.2'))])),
        response(payload([raw_row('2026-10-20')]))])
    result = by.collect(db, resumed_op, collector=recovery, cancelled=lambda: False, progress=lambda _message: None)
    assert session.calls[0][2]['params']['filter'] == f"(SOLAR_DATE>='{day}')(SOLAR_DATE<='{day}')"
    assert session.calls[1][2]['params']['filter'] == "(SOLAR_DATE>='2026-10-06')(SOLAR_DATE<='2026-10-20')"
    assert result['status'] == 'complete'

    with db.connection() as conn:
        original = json.loads(conn.execute('SELECT result_json FROM collection_batches WHERE id=?', (candidate_id,)).fetchone()[0])
        resolved = original['pending_withdrawals'][0]
        assert resolved['resolved'] is True
        recovery_batch = conn.execute('SELECT id FROM collection_batches WHERE operation_id=? AND dataset=? AND scope_start=? AND scope_end=? ORDER BY id DESC LIMIT 1',
                                      (resumed_op, by.DATASET, day, day)).fetchone()
        recovery_source = conn.execute('SELECT id FROM collection_sources WHERE batch_id=? ORDER BY id DESC LIMIT 1',
                                       (recovery_batch['id'],)).fetchone()
        assert resolved['resolved_by_batch_id'] == recovery_batch['id']
        assert resolved['resolved_by_source_id'] == recovery_source['id']
        fact = conn.execute('SELECT version,is_latest,china_10y FROM sovereign_yield_observations WHERE observation_date=? ORDER BY version DESC LIMIT 1', (day,)).fetchone()
        assert (fact['version'], fact['is_latest'], fact['china_10y']) == (1, 1, '3.2')


def test_initialization_publishes_latest_year_then_resumes_older_year(db, monkeypatch):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2003, 2, 1, 12, tzinfo=timezone.utc)
            return value if tz else value.replace(tzinfo=None)

    monkeypatch.setattr(by, 'datetime', FrozenDatetime)
    first_op = operation(db)
    manifest = by._manifest_batch(db, first_op, 'manual')
    scope = by._get_scope(db, manifest)
    scope.update(target_end='2003-02-01', target_year=2003)
    by._save_scope(db, manifest, scope)

    cancel_after_latest = {'value': False}
    def latest_progress(message):
        if '已完成2003年' in message:
            cancel_after_latest['value'] = True

    latest, first_session, _ = http(db, [response(payload([raw_row('2003-02-01')]))])
    with pytest.raises(Cancelled):
        by.collect(db, first_op, collector=latest, cancelled=lambda: cancel_after_latest['value'],
                   progress=latest_progress)
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM sovereign_yield_observations WHERE observation_date LIKE '2003-%' AND is_latest=1").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM sovereign_yield_observations WHERE observation_date LIKE '2002-%'").fetchone()[0] == 0
        saved = json.loads(conn.execute('SELECT scope_json FROM collection_batches WHERE id=?', (manifest,)).fetchone()[0])
        assert saved['target_end'] == '2003-02-01'
        assert [item['year'] for item in saved['annual_segments']] == [2003]
    with db.connection(write=True) as conn:
        conn.execute("UPDATE index_collection_operations SET status='cancelled' WHERE operation_id=?", (first_op,))

    second_op = operation(db)
    old, second_session, _ = http(db, [response(payload([raw_row('2002-01-04', EMM00166466=Decimal('4.1'))])),
                                        response(payload([]))])
    by.collect(db, second_op, collector=old, cancelled=lambda: False, progress=lambda _message: None)
    assert [call[2]['params']['filter'] for call in first_session.calls] == [
        "(SOLAR_DATE>='2003-01-01')(SOLAR_DATE<='2003-02-01')"]
    assert [call[2]['params']['filter'] for call in second_session.calls] == [
        "(SOLAR_DATE>='2002-01-04')(SOLAR_DATE<='2002-12-31')",
        "(SOLAR_DATE>='2003-01-18')(SOLAR_DATE<='2003-02-01')"]
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM sovereign_yield_observations WHERE observation_date='2002-01-04' AND is_latest=1").fetchone()[0] == 1
        saved = json.loads(conn.execute('SELECT scope_json FROM collection_batches WHERE id=?', (manifest,)).fetchone()[0])
        assert {item['year'] for item in saved['annual_segments']} == {2003, 2002}


def test_maintenance_lists_macro_classification_and_source_time_even_for_empty_table(db):
    data = MaintenanceService(db).read(refresh=True)
    row = next(item for item in data['tables'] if item['name'] == 'sovereign_yield_observations')
    assert row['category'] == '宏观数据'
    assert row['description'] != '尚未登记用途'
    assert row['rows'] == 0
    assert row['updated_at'] is None
    assert 'collection_sources.obtained_at' in row['time_basis']

    collect_year(db, [response(payload([raw_row()]))])
    data = MaintenanceService(db).read(refresh=True)
    row = next(item for item in data['tables'] if item['name'] == 'sovereign_yield_observations')
    assert row['rows'] == 1
    assert row['updated_at']
    assert 'collection_sources.obtained_at' in row['time_basis']


@pytest.mark.parametrize('mode,start,end', [
    ('full', None, None), ('audit', None, None), ('incremental', '2026-10-01', None),
    ('incremental', None, '2026-10-31'),
])
def test_bond_dataset_rejects_non_incremental_or_explicit_range(mode, start, end, db):
    with pytest.raises(ValueError, match='国债收益率附加任务'):
        start_operation(db, mode=mode, providers=[], datasets=[by.DATASET], start=start, end=end)


def test_empty_first_initialization_year_remains_resumable(db, monkeypatch):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2002, 1, 20, 12, tzinfo=timezone.utc)
            return value if tz else value.replace(tzinfo=None)

    monkeypatch.setattr(by, 'datetime', FrozenDatetime)
    op = operation(db)
    # Empty source windows are valid for recent freshness checks, but cannot certify
    # the mandatory first history segment beginning 2002-01-04.
    manifest = by._manifest_batch(db, op, 'manual')
    by._save_scope(db, manifest, {**by._get_scope(db, manifest), 'target_end': '2002-01-04', 'target_year': 2002})
    with pytest.raises(ValueError, match='2002-01-04'):
        collector, _, _ = http(db, [response(payload([]))])
        by.collect(db, op, collector=collector, cancelled=lambda: False, progress=lambda _message: None)
    with db.connection() as conn:
        # This is deliberately the acceptance guard for the initialization caller:
        # the empty annual segment must not be treated as covered/completed.
        assert conn.execute('SELECT count(*) FROM sovereign_yield_observations').fetchone()[0] == 0
        assert conn.execute("SELECT status FROM collection_batches WHERE dataset=? ORDER BY id DESC LIMIT 1", (by.DATASET,)).fetchone()[0] == 'failed'
        manifest_scope = json.loads(conn.execute('SELECT scope_json FROM collection_batches WHERE id=?', (manifest,)).fetchone()[0])
        assert manifest_scope['annual_segments'] == []
        assert conn.execute('SELECT status FROM collection_batches WHERE id=?', (manifest,)).fetchone()[0] == 'running'
    with db.connection(write=True) as conn:
        conn.execute("UPDATE index_collection_operations SET status='failed' WHERE operation_id=?", (op,))

    resumed = operation(db)
    collector, session, _ = http(db, [response(payload([raw_row('2002-01-04', EMM00166466=Decimal('4.1'))])),
                                       response(payload([]))])
    by.collect(db, resumed, collector=collector, cancelled=lambda: False, progress=lambda _message: None)
    assert [call[2]['params']['filter'] for call in session.calls] == [
        "(SOLAR_DATE>='2002-01-04')(SOLAR_DATE<='2002-01-04')",
        "(SOLAR_DATE>='2002-01-05')(SOLAR_DATE<='2002-01-20')"]
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM sovereign_yield_observations WHERE observation_date='2002-01-04' AND is_latest=1").fetchone()[0] == 1
        assert conn.execute('SELECT status FROM collection_batches WHERE id=?', (manifest,)).fetchone()[0] == 'complete'


def test_dataset_survives_index_failure_and_operation_keeps_failed_status(db, monkeypatch, tmp_path):
    calls = []
    class BrokenIndexCollector:
        def __init__(self):
            self.http = RequestCollector(db, session=Session(Clock(), []), clock=lambda: 10_000,
                                         sleep=lambda _seconds: None, uniform=lambda low, high: low)
        def load_calendar(self, cached=False):
            raise RuntimeError('calendar unavailable')

    def fake_bond_collect(database, op, *, collector, cancelled, progress):
        calls.append((database, op, collector))
        return {'status': 'complete', 'requests': 0}

    monkeypatch.setattr(by, 'collect', fake_bond_collect)
    op = start_operation(db, mode='incremental', providers=['csindex'], datasets=[by.DATASET])
    result = ic.run_operation(db, op, collector=BrokenIndexCollector())
    assert len(calls) == 1
    assert calls[0][0] is db and calls[0][1] == op
    assert result['status'] == 'failed'
    saved = json.loads(result['result_json'])
    assert saved['bond_yields']['status'] == 'complete'
    assert 'calendar unavailable' in saved['errors']['operation']


def test_bond_only_operation_saves_result_and_cancellation_state(db, monkeypatch):
    seen = []
    def successful(database, op, *, collector, cancelled, progress):
        seen.append((op, cancelled(), collector))
        return {'status': 'complete', 'requests': 3}

    monkeypatch.setattr(by, 'collect', successful)
    op = start_operation(db, mode='incremental', providers=[], datasets=[by.DATASET])
    result = ic.run_operation(db, op)
    assert result['status'] == 'complete'
    assert json.loads(result['result_json'])['bond_yields'] == {'status': 'complete', 'requests': 3}
    assert seen[0][0] == op and not seen[0][1]
    with db.connection() as conn:
        saved = conn.execute('SELECT status,result_json FROM index_collection_operations WHERE operation_id=?', (op,)).fetchone()
        assert saved['status'] == 'complete'
        assert json.loads(saved['result_json'])['bond_yields']['requests'] == 3

    def cancelled(database, op, *, collector, cancelled, progress):
        raise Cancelled('user cancelled')

    monkeypatch.setattr(by, 'collect', cancelled)
    cancelled_op = start_operation(db, mode='incremental', providers=[], datasets=[by.DATASET])
    stopped = ic.run_operation(db, cancelled_op)
    assert stopped['status'] == 'cancelled'
    assert json.loads(stopped['result_json'])['errors']['bond_zh_us_rate'] == 'user cancelled'
    with db.connection() as conn:
        saved = conn.execute('SELECT status,result_json FROM index_collection_operations WHERE operation_id=?', (cancelled_op,)).fetchone()
        assert saved['status'] == 'cancelled'
        assert json.loads(saved['result_json'])['errors']['bond_zh_us_rate'] == 'user cancelled'


def test_combined_operation_holds_os_lock_through_bond_phase(db, monkeypatch):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 10, 20, 12, tzinfo=timezone.utc)
            return value if tz else value.replace(tzinfo=None)

    monkeypatch.setattr(ic, 'datetime', FrozenDatetime)
    lock = {'active': 0}
    @contextmanager
    def tracked_lock(_path):
        lock['active'] += 1
        try:
            yield
        finally:
            lock['active'] -= 1

    monkeypatch.setattr(ic, 'instance_lock', tracked_lock)
    monkeypatch.setattr(market_congestion, 'prepare_index_dependency', lambda *_args: {'status': 'ready', 'network_requests': 0})
    monkeypatch.setattr(index_price_derived, 'recompute', lambda *_args, **_kwargs: {'status': 'current', 'facts': 0, 'network_requests': 0})
    lock_states = []

    class CombinedCollector:
        def __init__(self):
            self.http = RequestCollector(db, session=Session(Clock(), []), clock=lambda: 10_000,
                                         sleep=lambda _seconds: None, uniform=lambda low, high: low)
            self.trading_days = []
        def load_calendar(self, cached=False):
            self.trading_days = [f'2026-10-{day:02d}' for day in range(1, 20)]
        def collect_cs(self, *_args):
            raise RuntimeError('controlled index source failure')
        def plan_operation_work_units(self, *_args):
            pass

    def bond_collect(database, op, *, collector, cancelled, progress):
        lock_states.append(lock['active'])
        return {'status': 'complete', 'requests': 0}

    monkeypatch.setattr(by, 'collect', bond_collect)
    op = start_operation(db, mode='incremental', providers=['csindex'], datasets=[by.DATASET])
    result = ic.run_operation(db, op, collector=CombinedCollector())
    assert result['status'] == 'failed'
    assert lock_states == [1]


def test_target_discovery_parse_failure_closes_source_as_failed(db):
    op = operation(db)
    manifest = by._manifest_batch(db, op, 'manual')
    collector, _, _ = http(db, [response({'result': {'data': []}})])
    with pytest.raises(ValueError, match='总页数'):
        by._discover_target_end(db, collector, op, manifest, lambda: False)
    with db.connection() as conn:
        source = conn.execute('SELECT parse_status,error FROM collection_sources ORDER BY id DESC LIMIT 1').fetchone()
        batch = conn.execute("SELECT status FROM collection_batches WHERE dataset=? ORDER BY id DESC LIMIT 1", (by.DATASET,)).fetchone()
        assert source['parse_status'] == 'failed'
        assert source['error']
        assert batch['status'] == 'failed'


def test_audit_aggregates_facts_across_batches_and_all_revisions(db):
    # Three one-row source pages span three independent operation/batch IDs;
    # the final page size is 1 while the history has 3 revisions and 2 latest rows.
    collect_year(db, [response(payload([raw_row('2026-10-01', EMM00166466=Decimal('3.1'))]))],
                 start='2026-10-01', end='2026-10-01')
    collect_year(db, [response(payload([raw_row('2026-10-02', EMM00166466=Decimal('3.2'))]))],
                 start='2026-10-02', end='2026-10-02')
    collect_year(db, [response(payload([raw_row('2026-10-01', EMM00166466=Decimal('3.3'))]))],
                 start='2026-10-01', end='2026-10-01')

    with db.connection() as conn:
        expected_latest = conn.execute('SELECT count(*) FROM sovereign_yield_observations WHERE is_latest=1').fetchone()[0]
        expected_revisions = conn.execute('SELECT count(*) FROM sovereign_yield_observations').fetchone()[0]
        assert expected_latest == 2 and expected_revisions == 3
        assert conn.execute('SELECT count(DISTINCT batch_id) FROM sovereign_yield_observations').fetchone()[0] == 3

    report = audit_bond_yields(db.path, verify_files=True)
    assert report['facts']['latest_rows'] == expected_latest
    assert report['facts']['all_revisions'] == expected_revisions
    assert report['storage']['total_bytes'] > 0
    assert report['performance_ms_median']['total_bytes'] == report['storage']['total_bytes']
    assert report['performance_ms_median']['bytes_per_stored_revision'] == pytest.approx(
        report['storage']['total_bytes'] / expected_revisions)
    assert report['sources']['all_hashes_verified'] is True
    assert report['sources']['stored_fact_hash_mismatches'] == 0
    assert report['sources']['source_to_fact'] == {
        'raw_rows': 3, 'exact_value_in_history': 3, 'current_source_reference': 0,
        'pending_withdrawal': 0, 'unmatched': 0}
    assert report['sources']['fact_to_source'] == {
        'published_facts': 3, 'source_row_exact_match': 3, 'unmatched': 0}


@pytest.mark.parametrize('bond_outcome,index_error,expected',[
    ('complete',False,'complete'),('partial',False,'failed'),('empty',False,'failed'),
    ('error',False,'failed'),('cancelled',False,'cancelled'),('complete',True,'failed'),
])
def test_combined_bond_and_three_providers_overlap_and_merge(db,monkeypatch,bond_outcome,index_error,expected):
    import threading
    from unittest.mock import Mock
    seen={};snapshots=[]
    def snapshot():
        with db.connection() as conn:
            result=json.loads(conn.execute('SELECT result_json FROM index_collection_operations WHERE operation_id=?',(op,)).fetchone()[0])
            snapshots.append(result['work_progress'])
    gate=threading.Barrier(4,action=snapshot,timeout=5)
    def calendar(worker,**kwargs):
        worker.trading_days=[f'2026-10-{day:02d}' for day in range(1,20)]
        seen['parent']=worker.http
    def collect_index(provider):
        def collect(worker,*args):
            task=worker.task_progress.register(provider,'test','test')
            worker.task_progress.transition(task,'running')
            seen[provider]=worker.http
            gate.wait()
            worker.task_progress.transition(task,'success')
            if index_error and provider=='legu':raise RuntimeError('index failure')
        return collect
    def collect_bond(database,operation,*,collector,cancelled,progress):
        seen['eastmoney']=collector
        collector.requests=3;collector.retries=1
        close=Mock(wraps=collector.session.close)
        collector.session.close=close;seen['close']=close
        assert collector.policy_for('eastmoney').max_concurrency==1
        assert collector.policy_for('eastmoney').interval==10
        gate.wait()
        progress('bond independent progress')
        if bond_outcome=='error':raise RuntimeError('bond failure')
        if bond_outcome=='cancelled':raise Cancelled('user cancelled')
        return {'status':bond_outcome,'requests':3}
    monkeypatch.setattr(ic.IndexCollector,'load_calendar',calendar)
    monkeypatch.setattr(ic.IndexCollector,'plan_operation_work_units',lambda *args:None)
    for provider,method in [('csindex','collect_cs'),('legu','collect_legu'),('sws','collect_sw')]:
        monkeypatch.setattr(ic.IndexCollector,method,collect_index(provider))
    monkeypatch.setattr(market_congestion,'prepare_index_dependency',lambda *args:{'status':'ready','network_requests':0})
    monkeypatch.setattr(index_price_derived,'recompute',lambda *args,**kwargs:{'status':'current','facts':0,'network_requests':0})
    monkeypatch.setattr(by,'collect',collect_bond)
    op=start_operation(db,mode='incremental',datasets=[by.DATASET])
    result=ic.run_operation(db,op)
    assert result['status']==expected
    saved=json.loads(result['result_json'])
    assert set(snapshots[0]['providers'])=={'eastmoney','csindex','legu','sws'}
    assert all(counts['running']==1 for counts in snapshots[0]['providers'].values())
    assert len({id(seen[name].session) for name in ['parent','eastmoney','csindex','legu','sws']})==5
    seen['close'].assert_called_once()
    assert saved['network_requests']==3 and saved['retries']==1
    assert saved['work_progress']['phase']=='finished'
    if bond_outcome in ('error','cancelled','partial','empty'):
        assert 'bond_zh_us_rate' in saved['errors']
    else:assert saved['bond_yields']['status']=='complete'
    assert saved['work_progress']['providers']['csindex']['success']==1


def test_parallel_bond_continues_when_calendar_fails(db,monkeypatch):
    import threading
    started=threading.Event();calendar_failed=threading.Event()
    def calendar(worker,**kwargs):
        assert started.wait(5)
        calendar_failed.set()
        raise RuntimeError('calendar unavailable')
    def collect_bond(database,operation,**kwargs):
        started.set()
        assert calendar_failed.wait(5)
        return {'status':'complete','requests':0}
    monkeypatch.setattr(ic.IndexCollector,'load_calendar',calendar)
    monkeypatch.setattr(by,'collect',collect_bond)
    op=start_operation(db,mode='incremental',datasets=[by.DATASET])
    result=ic.run_operation(db,op)
    saved=json.loads(result['result_json'])
    assert result['status']=='failed'
    assert saved['bond_yields']['status']=='complete'
    assert saved['work_progress']['providers']['eastmoney']['success']==1
    assert 'calendar unavailable' in saved['errors']['operation']


def test_parallel_progress_updates_provider_when_another_provider_is_current(db):
    op=operation(db);tracker=ic.OperationProgress(db,op)
    bond=tracker.register('eastmoney','bond','bond')
    other=tracker.register('legu','pe','pe')
    tracker.transition(bond,'running');tracker.transition(other,'running')
    tracker.update_message('eastmoney','checking recent yields')
    with db.connection() as conn:
        progress=json.loads(conn.execute('SELECT result_json FROM index_collection_operations WHERE operation_id=?',(op,)).fetchone()[0])['work_progress']
    assert progress['providers']['eastmoney']['current']=='checking recent yields'
    assert progress['current']['provider']=='legu'
