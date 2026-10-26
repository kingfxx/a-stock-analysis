from stock_analysis.domains.stocks import data_service as data_module
from stock_analysis import application as app
"""Isolated MAINOP sync tests; no real source requests or daily database writes."""
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import requests

from stock_analysis.domains.financials import business_segment_service as module
from stock_analysis.domains.financials.business_segment_service import BusinessSegmentService, ten_year_floor, parse_rows
from stock_analysis.collection.collection import RequestCollector
from stock_analysis.maintenance.maintenance import MaintenanceService
from stock_analysis.infrastructure.storage import Database


def item(period='2025-12-31', name='饮料', value=100, **extra):
    return {'SECUCODE': '605499.SH', 'REPORT_DATE': period + ' 00:00:00', 'REPORT_NAME': period,
            'MAINOP_TYPE': '2', 'ITEM_NAME': name, 'ITEM_CODE': None,
            'MAIN_BUSINESS_INCOME': value, 'RANK': 1, **extra}


class Session:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []
        self.transform = lambda payload, page: payload

    def get(self, url, params, **kwargs):
        self.calls.append(dict(params))
        page, size = params['pageNumber'], params['pageSize']
        payload = {'success': True, 'result': {'count': len(self.rows), 'pages': (len(self.rows) + size - 1) // size,
                   'data': self.rows[(page - 1) * size:page * size]}}
        payload = self.transform(payload, page)
        response = requests.Response()
        response.status_code = 200
        response.headers['Content-Type'] = 'application/json'
        response._content = json.dumps(payload).encode()
        return response


def setup(tmp_path, monkeypatch, rows=None):
    db = Database(tmp_path / 'segments.sqlite3')
    db.initialize()
    session = Session(rows if rows is not None else [item()])
    collector = RequestCollector(db, session=session, uniform=lambda *_: 0)
    svc = BusinessSegmentService(db, collector=collector)
    monkeypatch.setattr(module, 'today_shanghai', lambda: date(2026, 10, 16))
    return db, svc, session


def counts(db):
    with db.connection() as conn:
        return tuple(conn.execute('SELECT count(*) FROM ' + table).fetchone()[0] for table in
                     ('business_segment_versions', 'business_segment_items', 'sync_runs', 'collection_sources'))


def test_history_idempotency_precision_revision_and_sources(tmp_path, monkeypatch):
    db, svc, session = setup(tmp_path, monkeypatch, [item(value='1234567890123456789.1234567890123456789')])
    result = svc.update('605499')
    assert result['sync']['status'] == 'success'
    assert '2016-10-16' in session.calls[0]['filter']
    assert counts(db) == (1, 1, 1, 1)
    with db.connection() as conn:
        assert conn.execute('SELECT trigger_type FROM collection_batches').fetchone()[0] == 'automatic'
    svc.update('605499')
    assert len(session.calls) == 1
    svc.update('605499', refresh=True)
    assert counts(db) == (1, 1, 2, 2)
    session.rows[0]['MAIN_BUSINESS_INCOME'] = 200
    svc.update('605499', refresh=True)
    session.rows[0]['MAIN_BUSINESS_INCOME'] = '1234567890123456789.1234567890123456789'
    svc.update('605499', refresh=True)
    with db.connection() as conn:
        versions = conn.execute('SELECT * FROM business_segment_versions ORDER BY id').fetchall()
        assert [v['revision'] for v in versions] == [1, 2, 3]
        assert versions[0]['content_hash'] == versions[2]['content_hash']
        row = conn.execute('SELECT i.*,s.obtained_at FROM business_segment_items i JOIN collection_sources s ON s.id=i.source_id LIMIT 1').fetchone()
        assert row['revenue'] == '1234567890123456789.1234567890123456789'
        assert versions[0]['obtained_at'] == row['obtained_at']
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
        assert conn.execute('SELECT count(*) FROM collection_sources WHERE file_path IS NOT NULL OR raw_sha256 IS NOT NULL').fetchone()[0] == 0
    assert not list((tmp_path / 'collection_sources').rglob('*.raw.gz'))


def test_incremental_two_actual_periods_late_quarter_and_audit(tmp_path, monkeypatch):
    db, svc, session = setup(tmp_path, monkeypatch, [item('2024-12-31'), item('2025-06-30'), item('2025-12-31')])
    svc.update('605499')
    # Stub respects the server-side range for incremental responses.
    session.rows = [item('2025-06-30'), item('2025-12-31'), item('2026-03-31')]
    svc.update('605499', refresh=True)
    assert "REPORT_DATE>='2025-06-30'" in session.calls[-1]['filter']
    assert svc.read('605499')['sync']['data_watermark'] == '2026-03-31'
    with db.connection(write=True) as conn:
        conn.execute("UPDATE sync_state SET next_full_audit_at='2000-01-01T00:00:00+00:00'")
    session.rows = [item('2024-12-31'), *session.rows]
    svc.update('605499')
    assert "REPORT_DATE>='2016-10-16'" in session.calls[-1]['filter']
    with db.connection() as conn:
        assert conn.execute('SELECT trigger_type FROM collection_batches ORDER BY id DESC LIMIT 1').fetchone()[0] == 'automatic'


def test_empty_cache_9201_and_failure_retry(tmp_path, monkeypatch):
    db, svc, session = setup(tmp_path, monkeypatch, [])
    session.transform = lambda *_: {'code': 9201, 'result': None}
    assert svc.update('605499')['sync']['status'] == 'no_data'
    svc.update('605499')
    assert len(session.calls) == 1
    session.transform = lambda payload, page: payload
    session.rows = [item(SECUCODE='601919.SH')]
    assert svc.update('605499', refresh=True)['sync']['status'] == 'failed'
    count = len(session.calls)
    svc.update('605499')
    assert len(session.calls) == count
    session.rows = [item()]
    assert svc.update('605499', refresh=True)['sync']['status'] == 'success'


@pytest.mark.parametrize('change', ['missing_item', 'missing_value', 'duplicate', 'wrong_stock', 'invalid_date', 'nan'])
def test_failed_candidate_preserves_facts(tmp_path, monkeypatch, change):
    db, svc, session = setup(tmp_path, monkeypatch)
    svc.update('605499')
    if change == 'missing_item': session.rows = []
    if change == 'missing_value': session.rows[0]['MAIN_BUSINESS_INCOME'] = None
    if change == 'duplicate': session.rows.append(dict(session.rows[0]))
    if change == 'wrong_stock': session.rows[0]['SECUCODE'] = '601919.SH'
    if change == 'invalid_date': session.rows[0]['REPORT_DATE'] = '1900-01-01'
    if change == 'nan': session.rows[0]['MAIN_BUSINESS_INCOME'] = 'NaN'
    assert svc.update('605499', refresh=True)['sync']['status'] == 'failed'
    assert counts(db)[:2] == (1, 1)


@pytest.mark.parametrize('change', ['none', 'count', 'first', 'truncate'])
def test_paging_and_stability(tmp_path, monkeypatch, change):
    db, svc, session = setup(tmp_path, monkeypatch, [item(name=f'项目{i:04}') for i in range(501)])
    def transform(payload, page):
        if change == 'count' and page == 2: payload['result']['count'] = 502
        if change == 'first' and len(session.calls) == 3: payload['result']['data'][0] = item(name='变化')
        if change == 'truncate' and page == 2: payload['result']['data'] = []
        return payload
    session.transform = transform
    result = svc.update('605499')
    assert result['sync']['status'] == ('success' if change == 'none' else 'failed')
    assert counts(db)[:2] == ((1, 501) if change == 'none' else (0, 0))
    if change == 'none': assert len(session.calls) == 3


def test_read_only_maintenance_and_leap_day(tmp_path, monkeypatch):
    db, svc, session = setup(tmp_path, monkeypatch)
    assert ten_year_floor(date(2024, 2, 29)) == date(2014, 2, 28)
    svc.read('605499')
    assert session.calls == []
    before = MaintenanceService(db).read()
    tables = {r['name']: r for r in before['tables']}
    assert tables['business_segment_versions']['category'] == '个股数据'
    assert tables['business_segment_items']['updated_at'] is None
    svc.update('605499')
    tables = {r['name']: r for r in MaintenanceService(db).read()['tables']}
    assert tables['business_segment_items']['updated_at'] == tables['business_segment_versions']['updated_at']


def test_numeric_zero_negative_null_and_float_decimal():
    rows = parse_rows([item(value=Decimal('1.1234567890123456789'), MAIN_BUSINESS_COST=-1,
                            MBI_RATIO=0, MBR_RATIO=None)], '605499.SH', '2020-01-01', '2026-10-16')
    assert rows[0]['revenue'] == '1.1234567890123456789'
    assert rows[0]['cost'] == '-1' and rows[0]['revenue_share'] == '0' and rows[0]['profit_share'] is None


def test_stale_check_and_expired_failure_retry(tmp_path, monkeypatch):
    db, svc, session = setup(tmp_path, monkeypatch)
    svc.update('605499')
    with db.connection(write=True) as conn:
        conn.execute('UPDATE sync_state SET checked_at=?', ((datetime.now(timezone.utc)-timedelta(hours=25)).isoformat(),))
    svc.update('605499')
    assert len(session.calls) == 2
    session.rows = []
    svc.update('605499', refresh=True)
    with db.connection(write=True) as conn:
        conn.execute("UPDATE sync_runs SET finished_at='2000-01-01T00:00:00+00:00' WHERE status='failed'")
        conn.execute("UPDATE sync_state SET checked_at='2000-01-01T00:00:00+00:00'")
    session.rows = [item()]
    assert svc.update('605499')['sync']['status'] == 'success'


def test_transaction_rollback_and_source_foreign_key(tmp_path, monkeypatch):
    db, svc, session = setup(tmp_path, monkeypatch)
    svc.update('605499')
    with db.connection(write=True) as conn:
        conn.execute("CREATE TRIGGER reject_segment BEFORE INSERT ON business_segment_items BEGIN SELECT RAISE(ABORT,'test rollback'); END")
    session.rows[0]['MAIN_BUSINESS_INCOME'] = 123
    assert svc.update('605499', refresh=True)['sync']['status'] == 'failed'
    assert counts(db)[:2] == (1, 1)
    assert svc.read('605499')['periods'][0]['revision'] == 1


def test_cleanup_all_revisions(tmp_path, monkeypatch):
    from stock_analysis.domains.stocks.stock_library import StockLibrary
    from stock_analysis.maintenance.stock_cleanup import StockCleanupService
    from stock_analysis.maintenance.database_restore import RequestGate
    db, svc, session = setup(tmp_path, monkeypatch)
    for value in (100, 200, 300):
        session.rows[0]['MAIN_BUSINESS_INCOME'] = value
        svc.update('605499', refresh=True)
    assert counts(db)[0] == 3
    StockLibrary(db).change({'action': 'unfollow', 'codes': ['605499']})
    cleanup = StockCleanupService(db)
    preview = cleanup.preview({'codes': ['605499'], 'categories': ['cache', 'financial']})
    gate = RequestGate()
    gate.enter()
    try:
        cleanup.execute({'token': preview['token'], 'confirm': '清理'}, gate)
    finally:
        gate.leave()
    assert counts(db)[:2] == (0, 0)
    assert db.check()['integrity'] == 'ok'


@pytest.mark.parametrize('mode', ['normal', 'segment_failure', 'financial_failure'])
def test_financial_integration_is_independent(tmp_path, monkeypatch, mode):
    from stock_analysis.http import server
    from stock_analysis.domains.financials.fundamental_service import FundamentalService
    from stock_analysis.domains.stocks.announcement_service import AnnouncementService
    from test_announcement_server_integration import _seed_legacy
    db = Database(tmp_path / 'stock.sqlite3')
    root = tmp_path / 'fundamentals'
    _seed_legacy(db, root)
    monkeypatch.setattr(app.settings, 'DATABASE_PATH', db.path)
    monkeypatch.setattr(app.settings, 'CACHE', root)
    monkeypatch.setattr(app.settings, 'VALUATION_CACHE', tmp_path / 'valuation')
    def financial(self, code, **kwargs):
        if mode == 'financial_failure': raise ValueError('financial offline')
        return self.read(code)
    monkeypatch.setattr(FundamentalService, 'update', financial)
    monkeypatch.setattr(AnnouncementService, 'update', lambda *a, **k: {'sync': {}, 'warnings': []})
    calls = []
    def segment(self, code, **kwargs):
        calls.append(kwargs)
        if mode == 'segment_failure': raise ValueError('segment offline')
        return {'sync': {'status': 'success'}, 'warnings': []}
    monkeypatch.setattr(BusinessSegmentService, 'update', segment)
    for refresh, full in ((False, False), (True, False), (True, True)):
        result = app.data.load_chart_data('601919', 'financial', refresh, full=full)
        assert result['views']['quarter'][0]['revenue'] == 100000000
        assert result['business_segment_sync']['status'] == ('failed' if mode == 'segment_failure' else 'success')
    assert calls == [{'refresh': False, 'full': False}, {'refresh': True, 'full': False}, {'refresh': True, 'full': True}]


def test_read_api_never_updates(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from stock_analysis.http import server
    db, svc, session = setup(tmp_path, monkeypatch)
    svc.update('605499')
    monkeypatch.setattr(app.data, 'services', lambda: (SimpleNamespace(db=db),))
    monkeypatch.setattr(BusinessSegmentService, 'update', lambda *a, **k: pytest.fail('read triggered update'))
    output = {}
    handler = server.Handler.__new__(server.Handler)
    handler.path = '/api/business-segments?code=605499'
    handler._ai_reply = lambda body: output.update(body)
    server.Handler._do_GET(handler)
    assert len(output['periods']) == 1
    assert len(session.calls) == 1


def test_provider_denial_stops_and_shared_cooldown(tmp_path, monkeypatch):
    db, svc, session = setup(tmp_path, monkeypatch)
    def denied(*args, **kwargs):
        session.calls.append(kwargs)
        response = requests.Response()
        response.status_code = 429
        response._content = b'too many requests'
        response.headers['Content-Type'] = 'text/plain'
        return response
    session.get = denied
    assert svc.update('605499')['sync']['status'] == 'failed'
    assert len(session.calls) == 1
    assert svc.update('605499', refresh=True)['sync']['status'] == 'failed'
    assert len(session.calls) == 1
    with db.connection() as conn:
        assert conn.execute("SELECT cooldown_until FROM collection_provider_state WHERE source_system='eastmoney'").fetchone()[0]
    assert counts(db)[:2] == (0, 0)


def test_rank_change_is_not_business_revision_and_sources_immutable(tmp_path, monkeypatch):
    import sqlite3
    db, svc, session = setup(tmp_path, monkeypatch)
    svc.update('605499')
    session.rows[0]['RANK'] = 100
    svc.update('605499', refresh=True)
    assert counts(db)[0] == 1
    with pytest.raises(sqlite3.IntegrityError), db.connection(write=True) as conn:
        conn.execute('UPDATE business_segment_items SET batch_id=batch_id+1')
    with pytest.raises(sqlite3.IntegrityError), db.connection(write=True) as conn:
        conn.execute("UPDATE business_segment_versions SET source='other'")
    assert db.check()['integrity'] == 'ok'


def test_loading_considers_mainop_failure_quiet_period(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from stock_analysis.http import server
    db, svc, session = setup(tmp_path, monkeypatch)
    db.ensure_instrument('605499', '东鹏饮料')
    monkeypatch.setattr(app.data, 'services', lambda: (SimpleNamespace(db=db),))
    now = datetime.now(timezone.utc)
    def state(database, key):
        if key.dataset == 'business_segments': return {}
        return {'checked_at': now.isoformat(), 'next_full_audit_at': (now+timedelta(days=180)).isoformat()}
    monkeypatch.setattr(data_module, 'sync_state', state)
    monkeypatch.setattr(data_module, 'recent_sync_failure', lambda db, key: None)
    assert app.data.p4_loading('605499')['financial'] is True
    monkeypatch.setattr(data_module, 'recent_sync_failure', lambda db, key: {'error': 'failed'})
    assert app.data.p4_loading('605499')['financial'] is False
