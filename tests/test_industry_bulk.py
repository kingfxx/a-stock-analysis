import copy

import pytest

from quarterly_dashboard.industry_bulk import cap_rows, fetch_pages, publish, recent_quarters
from quarterly_dashboard.industry_service import IndustryService
from quarterly_dashboard.storage import Database
from datetime import date
from test_industries import fixture_bundle


def test_window_excludes_unfinished_quarter():
    assert recent_quarters(date(2026, 10, 3)) == ['2025Q4', '2026Q1', '2026Q2', '2026Q3']
    assert recent_quarters(date(2026, 9, 30)) == ['2025Q3', '2025Q4', '2026Q1', '2026Q2']
    history = recent_quarters(date(2026, 10, 3), 10)
    assert len(history) == 40 and history[0] == '2016Q4' and history[-1] == '2026Q3'


def test_explicit_empty_is_cached_but_failure_is_not_empty(tmp_path, monkeypatch):
    monkeypatch.setattr('quarterly_dashboard.industry_bulk.time.sleep', lambda n: None)
    class Empty:
        def get(self, *args, **kwargs):
            class Response:
                def raise_for_status(self): pass
                def json(self): return {'success': False, 'code': 9201, 'message': '返回数据为空', 'result': None}
            return Response()
    rows, files = fetch_pages(Empty(), tmp_path, 'cap', 'code', 'old', lambda m: None)
    assert rows == [] and len(files) == 1
    class Failed(Empty):
        def get(self, *args, **kwargs): raise OSError('timeout')
    assert fetch_pages(Failed(), tmp_path, 'cap', 'code', 'old', lambda m: None)[0] == []
    with pytest.raises(ValueError, match='timeout'):
        fetch_pages(Failed(), tmp_path, 'cap', 'code', 'other', lambda m: None)


def test_pacer_rests_after_twenty_requests(monkeypatch):
    from quarterly_dashboard.industry_bulk import RequestPacer
    sleeps = []
    monkeypatch.setattr('quarterly_dashboard.industry_bulk.time.sleep', sleeps.append)
    pacer = RequestPacer(lambda m: None)
    for _ in range(21): pacer.before_request()
    assert sleeps.count(30) == 1
    assert all(2.5 <= n <= 3.5 for n in sleeps if n != 30)


class Session:
    def __init__(self, inconsistent=False):
        self.calls = 0
        self.inconsistent = inconsistent

    def get(self, url, params, timeout):
        self.calls += 1
        page = params['pageNumber']
        start, end = (0, 500) if page == 1 else (500, 501)
        data = [{'SECURITY_CODE': str(n).zfill(6)} for n in range(start, end)]
        payload = {'success': True, 'result': {'data': data, 'count': 502 if self.inconsistent and page == 2 else 501, 'pages': 2}}
        class Response:
            def raise_for_status(self): pass
            def json(self): return payload
        return Response()


def test_paging_cache_and_reject_changed_total(tmp_path, monkeypatch):
    monkeypatch.setattr('quarterly_dashboard.industry_bulk.time.sleep', lambda n: None)
    session = Session()
    rows, files = fetch_pages(session, tmp_path, 'test', 'SECURITY_CODE', 'filter', lambda m: None)
    assert len(rows) == 501 and len(files) == session.calls == 2
    fetch_pages(session, tmp_path, 'test', 'SECURITY_CODE', 'filter', lambda m: None)
    assert session.calls == 2
    other = tmp_path / 'other'; other.mkdir()
    with pytest.raises(ValueError, match='数量变化'):
        fetch_pages(Session(True), other, 'test', 'SECURITY_CODE', 'filter', lambda m: None)


def test_cap_date_scope_units_and_share_validation():
    rows = [{'SECURITY_CODE': '000001', 'TRADE_DATE': '2026-09-30 00:00:00',
             'TOTAL_MARKET_CAP': 100, 'TOTAL_SHARES': 10, 'CLOSE_PRICE': 10},
            {'SECURITY_CODE': '920001', 'TRADE_DATE': '2026-09-30 00:00:00', 'TOTAL_MARKET_CAP': 999}]
    facts = cap_rows(rows, '2026-09-30', {'000001'}, {})
    assert len(facts) == 1 and facts[0]['total_cap'] == 100
    broken = copy.deepcopy(rows); broken[0]['TOTAL_MARKET_CAP'] = 102
    with pytest.raises(ValueError, match='不一致'):
        cap_rows(broken, '2026-09-30', {'000001'}, {})
    with pytest.raises(ValueError, match='日期'):
        cap_rows(rows, '2026-09-29', {'000001'}, {})


def test_publish_versions_quarter_and_no_individual_tables(tmp_path):
    db = Database(tmp_path / 'test.sqlite3'); db.initialize()
    service = IndustryService(db)
    initial = fixture_bundle(); service.import_bundle(initial)
    foundation = service.foundation()
    roster = {'quarter': '2026Q3', 'target_date': '2026-09-30',
              'composition': 'current_constituents_backfill',
              'member_import_id': foundation['member_import_id'],
              'members': [{k: m[k] for k in ('stock_code', 'industry_code')} for m in foundation['members']]}
    caps = copy.deepcopy(initial['caps']); caps[0]['total_cap'] = 101
    collection = {**initial, 'foundation': foundation, 'caps': [{'quarter': '2026Q3', 'target_date': '2026-09-30',
                  'rows': caps, 'roster': roster}], 'manifest': {'scope': 'all_market_one_year', 'pilot_industries': []}}
    publish(service, collection)
    assert service.read(industry='630701', mode='ytd', period='2025-06-30')['market_series'][0]['total_cap'] == 301
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM instruments').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM financial_reports').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM sw_memberships').fetchone()[0] == 3


def test_old_finance_visible_and_empty_cap_stays_null(tmp_path):
    db = Database(tmp_path / 'history.sqlite3'); db.initialize()
    service = IndustryService(db)
    initial = fixture_bundle(); service.import_bundle(initial)
    foundation = service.foundation()
    financials = copy.deepcopy([r for r in initial['financials'] if r['period'] == '2025-06-30'])
    for row in financials: row['period'] = '2016-09-30'
    roster = {'quarter': '2016Q4', 'target_date': '2016-12-30',
              'composition': 'current_constituents_backfill', 'member_import_id': foundation['member_import_id'],
              'members': [{k: m[k] for k in ('stock_code', 'industry_code')} for m in foundation['members']]}
    collection = {**initial, 'foundation': foundation, 'financials': financials,
                  'caps': [{'quarter': '2016Q4', 'rows': [], 'roster': roster}],
                  'manifest': {'scope': 'all_market_history', 'years': 10, 'pilot_industries': []}}
    publish(service, collection)
    assert '2016-09-30' in service.read(industry='630701', mode='ytd')['periods']
    with db.connection() as conn:
        rows = conn.execute("SELECT known_count,known_cap,total_cap FROM sw_industry_cap_quarters WHERE quarter='2016Q4'").fetchall()
        assert rows and all(r['known_count'] == 0 and r['known_cap'] is None and r['total_cap'] is None for r in rows)
