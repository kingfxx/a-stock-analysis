"""Offline missing-only collection: no live providers or ordinary database writes."""
import json
import re
import sqlite3

import pytest
import requests

from stock_analysis.collection.collection import RequestCollector
from stock_analysis.collection.collection_management import CollectionManagement, validate_params
from stock_analysis.domains.industries.industry_service import IndustryService
from stock_analysis.domains.market import market_financial as mf
from stock_analysis.domains.market import market_financial_missing as missing
from stock_analysis.infrastructure.storage import Database
from industry_financial_fixture import publish_financials
from test_industries import fixture_bundle

PERIOD = '2025-06-30'


class Clock:
    now = 1793400000
    def time(self): return self.now
    def sleep(self, seconds): self.now += seconds


def raw_row(kind, code, exchange='sz'):
    spec = mf.FIELDS[kind]
    row = {key: None for key in spec['fields']}
    row.update(SECURITY_CODE=code, SECUCODE=code+'.'+exchange.upper(), SECURITY_TYPE_CODE='058001001')
    row[spec.get('date_field', 'REPORT_DATE')] = PERIOD+' 00:00:00'
    if kind == 'income': row.update(TOTAL_OPERATE_INCOME=123, PARENT_NETPROFIT=0)
    return row


class Session:
    def __init__(self):
        self.calls = []
        self.available = {}
        self.bad = None
    def get(self, url, *, params, **kwargs):
        kind = next(k for k, spec in mf.FIELDS.items() if spec['report'] == params['reportName'])
        codes = re.findall(r'"(\d{6})"', params['filter'])
        self.calls.append((kind, codes, params.copy()))
        rows = [self.available[(kind, code)] for code in codes if (kind, code) in self.available]
        if self.bad:
            rows = self.bad(kind, rows)
        payload = {'success': True, 'code': 0, 'result': {'count': len(rows), 'pages': 1 if rows else 0, 'data': rows}}
        response = requests.Response(); response.status_code = 200
        response.headers['Content-Type'] = 'application/json'
        response._content = json.dumps(payload).encode()
        return response
    def close(self): pass


@pytest.fixture
def setup(tmp_path):
    db = Database(tmp_path/'missing.sqlite3'); db.initialize()
    bundle = fixture_bundle()
    IndustryService(db).import_bundle({**bundle, 'financials': []}, capture_local=False)
    publish_financials(db, bundle['financials'])
    session, clock = Session(), Clock()
    def client():
        return RequestCollector(db, session=session, clock=clock.time, sleep=clock.sleep, uniform=lambda a,b:a)
    return db, session, client


def delete(db, kind, code):
    with db.connection(write=True) as conn:
        conn.execute(f'DELETE FROM market_financial_{kind} WHERE security_code=? AND report_date=?', (code, PERIOD))


def facts(db):
    with db.connection() as conn:
        return {kind: {r['id']: dict(r) for r in conn.execute(f'SELECT * FROM market_financial_{kind} ORDER BY id')}
                for kind in mf.FIELDS}


def test_only_missing_interfaces_are_requested_and_existing_rows_unchanged(setup):
    db, session, client = setup
    delete(db, 'income', '300750'); delete(db, 'performance', '000001')
    session.available = {('income','300750'):raw_row('income','300750'),
                         ('performance','000001'):raw_row('performance','000001')}
    before = facts(db)
    result = missing.run_missing(db, 'test:missing', PERIOD, client())
    assert [(k,c) for k,c,_ in session.calls] == [('income',['300750']),('performance',['000001'])]
    assert all(params['columns'] == 'ALL' for _,_,params in session.calls)
    after = facts(db)
    assert all(after[k][identity] == row for k, items in before.items() for identity,row in items.items())
    assert result['remaining_count'] == 0 and sum(s['new'] for s in result['statements'].values()) == 2
    assert result['statements']['balance']['request_count'] == 0  # All its fields may be empty: row already exists.
    with db.connection() as conn:
        assert all(r[0] == 'unknown' for r in conn.execute('SELECT coverage_status FROM collection_batches WHERE parser_version=?', (missing.PARSER,)))


def test_no_gaps_means_no_source_calls_and_no_initial_period_is_rejected(setup):
    db, session, client = setup
    result = missing.run_missing(db, 'test:none', PERIOD, client())
    assert not session.calls and result['network_requests'] == 0 and result['remaining_count'] == 0
    with pytest.raises(ValueError, match='先普通采集'):
        missing.run_missing(db, 'test:empty', '2026-09-30', client())
    assert not session.calls


def test_empty_and_partial_responses_are_rechecked_without_redownloading_filled_rows(setup):
    db, session, client = setup
    delete(db, 'income', '300750'); delete(db, 'income', '000001')
    first = missing.run_missing(db, 'test:empty', PERIOD, client())
    assert first['remaining_count'] == 2
    session.available[('income','300750')] = raw_row('income','300750')
    second = missing.run_missing(db, 'test:partial', PERIOD, client())
    assert second['remaining_count'] == 1 and len(session.calls) == 2
    session.available[('income','000001')] = raw_row('income','000001')
    third = missing.run_missing(db, 'test:filled', PERIOD, client())
    assert third['remaining_count'] == 0 and session.calls[-1][1] == ['000001']


def test_failure_rolls_back_all_facts_and_retry_reuses_fully_validated_chunks(setup):
    db, session, client = setup
    delete(db, 'income', '300750'); delete(db, 'performance', '000001')
    session.available = {('income','300750'):raw_row('income','300750'),
                         ('performance','000001'):raw_row('performance','000001')}
    session.bad = lambda kind, rows: [{**row, 'UNKNOWN_FIELD': 1} for row in rows] if kind == 'performance' else rows
    before = facts(db)
    with pytest.raises(ValueError, match='字段'):
        missing.run_missing(db, 'test:failed', PERIOD, client())
    assert facts(db) == before
    session.bad = None
    result = missing.run_missing(db, 'test:retry', PERIOD, client())
    assert [k for k,_,_ in session.calls] == ['income', 'performance', 'performance']
    assert result['network_requests'] == 1 and result['remaining_count'] == 0
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM collection_sources WHERE request_status='reused'").fetchone()[0] == 1


@pytest.mark.parametrize('fault', ['extra','duplicate','period','fields'])
def test_invalid_response_never_publishes(setup, fault):
    db, session, client = setup
    delete(db, 'income', '300750')
    session.available[('income','300750')] = raw_row('income','300750')
    def corrupt(kind, rows):
        if fault == 'extra': return [raw_row(kind,'000002')]
        if fault == 'duplicate': return rows + rows
        if fault == 'period': return [{**rows[0], 'REPORT_DATE':'2025-03-31'}]
        row = rows[0].copy(); row.pop('TOTAL_OPERATE_INCOME'); return [row]
    session.bad = corrupt
    before = facts(db)
    with pytest.raises(ValueError): missing.run_missing(db, 'test:bad', PERIOD, client())
    assert facts(db) == before


def test_existing_row_appearing_during_download_is_not_overwritten(setup, monkeypatch):
    db, session, client = setup
    with db.connection() as conn:
        old = dict(conn.execute("SELECT * FROM market_financial_income WHERE security_code='300750' AND report_date=?", (PERIOD,)).fetchone())
    delete(db, 'income', '300750')
    session.available[('income','300750')] = raw_row('income','300750')
    publish = missing.publish_missing
    def concurrently_filled(conn, *args):
        conn.execute('INSERT INTO market_financial_income ('+','.join(old)+') VALUES ('+','.join('?' for _ in old)+')', tuple(old.values()))
        return publish(conn, *args)
    monkeypatch.setattr(missing, 'publish_missing', concurrently_filled)
    result = missing.run_missing(db, 'test:concurrent', PERIOD, client())
    assert result['statements']['income']['new'] == 0 and result['statements']['income']['unchanged'] == 1
    assert facts(db)['income'][old['id']] == old


def test_registry_reservation_and_dispatch_keep_missing_manual_only(setup, monkeypatch):
    db, session, client = setup
    params = validate_params('market_financial', {'mode':'missing','target':PERIOD})
    with pytest.raises(ValueError): validate_params('market_financial', params, scheduled=True)
    with pytest.raises(ValueError): validate_params('trade_snapshot', {'mode':'missing','target':'2025Q2'})
    service = IndustryService(db); management = CollectionManagement(db, service)
    with db.connection(write=True) as conn:
        ref = management.reserve(conn, 'market_financial', params)
        assert conn.execute('SELECT mode FROM collection_operations WHERE id=?',(int(ref.split(':')[1]),)).fetchone()[0] == 'missing'
    calls = []
    monkeypatch.setattr(service, '_refresh', lambda *a,**k:calls.append((a,k)))
    management.execute('market_financial', params, ref)
    assert calls[0][1] == {'missing_only':True}


def test_chunking_uses_bounded_stock_filters(setup):
    db, session, client = setup
    with db.connection(write=True) as conn:
        member = conn.execute('SELECT member_import_id FROM sw_imports ORDER BY id DESC LIMIT 1').fetchone()[0]
        for index in range(201):
            code = f'301{index:03d}'
            conn.execute('INSERT INTO sw_memberships(import_id,stock_code,name) VALUES(?,?,?)', (member,code,code))
            for kind in mf.FIELDS:
                session.available[(kind,code)] = raw_row(kind,code)
    result = missing.run_missing(db, 'test:chunks', PERIOD, client())
    assert len(session.calls) == 8 and max(len(codes) for _, codes, _ in session.calls) == 200
    assert sum(s['new'] for s in result['statements'].values()) == 804
    assert result['remaining_count'] == 0


def test_publish_error_rolls_back_and_fulfilled_scopes_survive_retry(setup):
    db, session, client = setup
    delete(db,'income','300750'); delete(db,'performance','000001')
    session.available = {('income','300750'):raw_row('income','300750'),
                         ('performance','000001'):raw_row('performance','000001')}
    before = facts(db)
    with db.connection(write=True) as conn:
        conn.execute("CREATE TRIGGER test_reject_patch BEFORE INSERT ON market_financial_performance BEGIN SELECT RAISE(ABORT,'test publication failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        missing.run_missing(db, 'test:publish-failure', PERIOD, client())
    assert facts(db) == before
    with db.connection(write=True) as conn: conn.execute('DROP TRIGGER test_reject_patch')
    result = missing.run_missing(db, 'test:publish-retry', PERIOD, client())
    assert len(session.calls) == 2 and result['network_requests'] == 0 and result['remaining_count'] == 0


def test_same_quarter_snapshot_extends_known_scope_to_beijing(setup):
    from stock_analysis.collection.collection import begin_batch, archive_source, finish_batch
    from stock_analysis.domains.market import market_trade_snapshot as snapshots
    db, session, client = setup
    raw = {field: None for field in snapshots.FIELDS}
    raw.update(SECURITY_CODE='830001', SECUCODE='830001.BJ', TRADE_DATE=PERIOD,
               CLOSE_PRICE=10, TOTAL_SHARES=100, TOTAL_MARKET_CAP=1000)
    batch = begin_batch(db,'test:roster',snapshots.DATASET,'eastmoney',start=PERIOD)
    source = archive_source(db,batch,'eastmoney',snapshots.DATASET,{'url':'https://example.test'},1,raw=json.dumps(raw).encode())
    with db.connection(write=True) as conn:
        snapshots.publish(conn,batch,[(snapshots.normalized(raw,PERIOD),source,'2026-10-31')])
    finish_batch(db,batch,'complete')
    for kind in mf.FIELDS:session.available[(kind,'830001')] = raw_row(kind,'830001','bj')
    result = missing.run_missing(db, 'test:beijing', PERIOD, client())
    assert result['requested_count'] == 1 and result['statements']['income']['new'] == 1
    assert all(codes == ['830001'] for _,codes,_ in session.calls)


def test_real_management_to_industry_workflow_uses_only_missing_collector(setup, monkeypatch):
    from stock_analysis.collection import collection
    from stock_analysis.domains.industries import industry_memberships
    db, session, client = setup
    delete(db,'income','300750')
    session.available[('income','300750')] = raw_row('income','300750')
    monkeypatch.setattr(collection,'RequestCollector',lambda *a,**k:client())
    monkeypatch.setattr(industry_memberships,'ensure_daily_memberships',lambda *a,**k:pytest.fail('补缺不刷新另一份名单'))
    service = IndustryService(db); management = CollectionManagement(db,service)
    params = {'mode':'missing','target':PERIOD}
    with db.connection(write=True) as conn: ref = management.reserve(conn,'market_financial',params)
    management.execute('market_financial',params,ref)
    with db.connection() as conn:
        row = conn.execute('SELECT status,result_json FROM collection_operations WHERE id=?',(int(ref.split(':')[1]),)).fetchone()
    assert row['status'] == 'complete', row['result_json']
    assert json.loads(row['result_json'])['statements']['income']['new'] == 1
    assert session.calls[0][1] == ['300750'] and len(session.calls) == 1
