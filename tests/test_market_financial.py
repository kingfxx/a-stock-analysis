import copy
import gzip
import json
from pathlib import Path

import pytest

from quarterly_dashboard import market_financial as mf
from quarterly_dashboard.industry_service import IndustryService
from quarterly_dashboard.maintenance import MaintenanceService
from quarterly_dashboard.storage import Database
from test_industries import fixture_bundle

PERIOD = '2025-06-30'


@pytest.fixture
def db(tmp_path):
    result = Database(tmp_path/'sample.sqlite3')
    result.initialize()
    return result


def row(dataset, value=0, code='600000', exchange='SH'):
    values = {key: None for key in mf.FIELDS[dataset]['fields']}
    values.update(SECUCODE=code+'.'+exchange, SECURITY_CODE=code,
                  SECURITY_TYPE_CODE='058001001', NOTICE_DATE='2025-08-01 00:00:00')
    values[mf.FIELDS[dataset].get('date_field','REPORT_DATE')]=PERIOD+' 00:00:00'
    metric = next(k for k, v in mf.FIELDS[dataset]['fields'].items() if v['type'] == 'REAL')
    values[metric] = value
    return values


def source(page=1):
    return {'page_number': page, 'url': 'https://example.test', 'params': {'pageNumber': page},
            'file_path': 'sample.json.gz', 'sha256': 'a'*64, 'raw_bytes': 1000, 'stored_bytes': 200,
            'obtained_at': '2026-10-08T00:00:00+00:00'}


def datasets():
    return [{'dataset': name, 'rows': [row(name)], 'sources': [source()], 'checkpoint': 'unused'}
            for name in mf.FIELDS]


def save(db, data):
    batch = mf.begin_batch(db, PERIOD)
    with db.connection(write=True) as conn:
        summary = mf.publish(conn, batch, PERIOD, data)
    return batch, summary


def test_migration_inventory_empty_and_populated(db):
    inventory = MaintenanceService(db).read()
    tables = [t for t in inventory['tables'] if t['name'].startswith('market_financial_')]
    assert len(tables) == 6
    assert all(t['rows'] == 0 and t['updated_at'] is None and t['total_bytes'] > 0 for t in tables)
    assert all(t['category'] != '其他' and '尚未登记' not in t['description'] for t in tables)
    save(db, datasets())
    populated = MaintenanceService(db).read()['tables']
    tables = [t for t in populated if t['name'].startswith('market_financial_')]
    assert all(t['rows'] > 0 and t['updated_at'] and 'at' in t['time_basis'] for t in tables)


def test_repeat_zero_negative_enrichment_revision_and_reversion(db):
    data = datasets()
    metric = next(k for k, v in mf.FIELDS['income']['fields'].items() if v['type'] == 'REAL')
    _, result = save(db, data)
    assert result['income']['new'] == 1
    _, result = save(db, data)
    assert result['income']['unchanged'] == 1
    # A valid zero is a real observation, not a missing field.
    data[0]['rows'][0][metric] = -2
    _, result = save(db, data)
    assert result['income']['revised'] == 1
    data[0]['rows'][0][metric] = 0
    save(db, data)
    # Previously null numeric fields may be completed without a stale predecessor.
    data[0]['rows'][0]['OPERATE_COST'] = 0
    _, result = save(db, data)
    assert result['income']['enriched'] == 1
    data[0]['rows'][0]['SECURITY_NAME_ABBR'] = '新名称'
    _, result = save(db, data)
    assert result['income']['unchanged'] == 1
    with db.connection() as conn:
        rows = conn.execute('SELECT * FROM market_financial_income ORDER BY version').fetchall()
        assert len(rows) == 3 and [r[metric.lower()] for r in rows] == [0, -2, 0]
        assert rows[-1]['operate_cost'] == 0 and rows[-1]['is_latest'] == 1
        assert sum(r['is_latest'] for r in rows) == 1
        assert rows[-1]['obtained_at'] == source()['obtained_at']
    data[0]['rows'][0]['OPERATE_COST'] = None
    _, result = save(db, data)
    assert result['income']['revised'] == 1


def test_announcement_change_and_definition_change_keep_versions(db, monkeypatch):
    data = datasets()
    save(db, data)
    data[0]['rows'][0]['NOTICE_DATE'] = '2025-08-02 00:00:00'
    _, result = save(db, data)
    assert result['income']['revised'] == 1
    monkeypatch.setitem(mf.FIELDS['income'], 'definition_version', 'summary-v2')
    _, result = save(db, data)
    assert result['income']['revised'] == 1


def test_unknown_nonfinite_or_wrong_scope_fail(db):
    for change in [{'UNKNOWN_FIELD': 1}, {'PARENT_NETPROFIT': float('inf')},
                   {'SECURITY_TYPE_CODE': 'B'}, {'REPORT_DATE': '2024-06-30'},
                   {'SECUCODE': '000001.SZ'}, {'PARENT_NETPROFIT': 'bad'}]:
        bad = row('income') | change
        with pytest.raises(ValueError):
            mf.normalized('income', bad, PERIOD)


def test_atomic_publication_rolls_back_legacy_and_all_new_tables(db):
    service = IndustryService(db)
    original = fixture_bundle()
    service.import_bundle(copy.deepcopy(original), capture_local=False)
    with db.connection() as conn:
        before = conn.execute('SELECT count(*) FROM sw_imports').fetchone()[0]
    changed = copy.deepcopy(original)
    changed['financials'][0]['revenue'] = 456
    batch = mf.begin_batch(db, PERIOD)
    data = datasets()
    data[1]['rows'][0]['UNKNOWN_FIELD'] = 1
    with pytest.raises(ValueError, match='未登记'):
        service.import_bundle(changed, capture_local=False,
                              publish=lambda conn: mf.publish(conn, batch, PERIOD, data))
    mf.fail_batch(db, batch, 'failure')
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM sw_imports').fetchone()[0] == before
        assert conn.execute('SELECT count(*) FROM market_financial_sources').fetchone()[0] == 0
        assert all(conn.execute(f'SELECT count(*) FROM market_financial_{name}').fetchone()[0] == 0
                   for name in mf.FIELDS)
        assert conn.execute('SELECT status FROM market_financial_batches').fetchone()[0] == 'failed'
    # Even an unchanged legacy import must invoke the shared publication hook.
    batch = mf.begin_batch(db, PERIOD)
    service.import_bundle(copy.deepcopy(original), capture_local=False,
                          publish=lambda conn: mf.publish(conn, batch, PERIOD, datasets()))
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM market_financial_income').fetchone()[0] == 1


def test_abrupt_coverage_drop_cannot_replace_previous_data(db):
    save(db, datasets())
    bad = datasets()
    bad[1]['rows'] = []
    with pytest.raises(ValueError, match='覆盖骤减'):
        save(db, bad)
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM market_financial_sources').fetchone()[0] == 4


class Pacer:
    def before_request(self):
        pass


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(kwargs['params'])
        payload = next(self.responses)
        class Response:
            content = json.dumps(payload, ensure_ascii=False).encode()
            def raise_for_status(self):
                pass
        return Response()


def payload(records, count=None, pages=1):
    return {'success': True, 'result': {'count': len(records) if count is None else count,
                                       'pages': pages, 'data': records}}


def test_compressed_response_hash_checkpoint_and_success_refresh(tmp_path):
    raw = payload([row('income')])
    first = Session([raw])
    data = mf.collect_dataset(first, tmp_path, 'income', PERIOD, Pacer(), lambda _: None)
    assert first.calls[0]['columns'] == 'ALL'
    evidence = data['sources'][0]
    path = tmp_path/evidence['file_path']
    assert mf.digest(gzip.decompress(path.read_bytes())) == evidence['raw_sha256']
    assert mf.digest(path.read_bytes()) == evidence['sha256']
    cached = Session([])
    mf.collect_dataset(cached, tmp_path, 'income', PERIOD, Pacer(), lambda _: None)
    assert not cached.calls
    mf.clear_checkpoints([data])
    repeat = Session([raw])
    mf.collect_dataset(repeat, tmp_path, 'income', PERIOD, Pacer(), lambda _: None)
    assert len(repeat.calls) == 1
    assert len(list(tmp_path.rglob('*.json.gz'))) == 1


def test_pages_changed_and_duplicate_securities_rejected(tmp_path):
    records = [row('income', code=f'{i:06}') for i in range(500)]
    session = Session([payload(records, 501, 2), payload([row('income')], 502, 2)])
    with pytest.raises(ValueError, match='分页数量变化'):
        mf.collect_dataset(session, tmp_path, 'income', PERIOD, Pacer(), lambda _: None)
    assert not list(tmp_path.rglob('*-income.json'))
    session = Session([payload([row('balance'), row('balance')])])
    with pytest.raises(ValueError, match='股票重复'):
        mf.collect_dataset(session, tmp_path, 'balance', PERIOD, Pacer(), lambda _: None)


def test_recovery_fails_only_unfinished_batches(db):
    batch, _ = save(db, datasets())
    interrupted = mf.begin_batch(db, PERIOD)
    IndustryService(db).recover_interrupted_runs()
    with db.connection() as conn:
        states = dict(conn.execute('SELECT id,status FROM market_financial_batches'))
    assert states == {batch: 'complete', interrupted: 'failed'}


def test_daily_button_collects_once_without_legacy_reads_or_writes(db, tmp_path, monkeypatch):
    from quarterly_dashboard import industry_updates, industry_memberships
    service = IndustryService(db, tmp_path/'industry-sources')
    bundle = fixture_bundle()
    existing = {r['stock_code'] for r in bundle['members']}
    for i in range(600000, 610000):
        code = str(i)
        if code not in existing:
            bundle['members'].append({**bundle['members'][0], 'stock_code': code})
        if len(bundle['members']) == 4000:
            break
    service.import_bundle(bundle, capture_local=False)
    monkeypatch.setattr(industry_memberships, 'ensure_daily_memberships', lambda *a: {'reused': True})
    monkeypatch.setattr(service, 'local_period', lambda *a: pytest.fail('daily financial read individual Sina'))
    monkeypatch.setattr(service, 'import_bundle', lambda *a,**k: pytest.fail('daily financial used legacy publication'))
    with db.connection() as conn:
        legacy={table:[tuple(r) for r in conn.execute('SELECT * FROM '+table)] for table in
                ('sw_financial_facts','sw_financial_provenance','sw_imports','sw_cap_facts')}
    calls = []
    def collect(*args, **kwargs):
        calls.append(kwargs)
        data = datasets()
        data[0]['rows'][0]['TOTAL_OPERATE_INCOME'] = 50
        for item in data:
            item['checkpoint'] = tmp_path/'unused'
            for source in item['sources']:
                source['file'] = str(tmp_path/'source.json.gz')
        return data
    monkeypatch.setattr(mf, 'collect', collect)
    monkeypatch.setattr(industry_updates, 'fetch_cap_quarter', lambda *a, **k: pytest.fail('finance fetched cap'))
    result = industry_updates.perform(service, 'financial_period', PERIOD, False, lambda _: None)
    assert len(calls) == 1 and calls[0]['resume'] is False
    assert set(result['statements']) == {*mf.FIELDS, 'performance'}
    assert result['returned_count']==1 and result['legacy_updated'] is False
    with db.connection() as conn:
        assert conn.execute('SELECT total_operate_income FROM market_financial_income').fetchone()[0] == 50
        for table,rows in legacy.items():
            assert [tuple(r) for r in conn.execute('SELECT * FROM '+table)]==rows
        imports = conn.execute('SELECT count(*) FROM sw_imports').fetchone()[0]
    def failed(*args, **kwargs):
        raise ValueError('现金流量表失败')
    monkeypatch.setattr(mf, 'collect', failed)
    with pytest.raises(ValueError, match='现金流量表失败'):
        industry_updates.perform(service, 'financial_period', PERIOD, False, lambda _: None)
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM sw_imports').fetchone()[0] == imports
        assert conn.execute('SELECT count(*) FROM market_financial_income').fetchone()[0] == 1
        assert conn.execute('SELECT status FROM market_financial_batches ORDER BY id DESC').fetchone()[0] == 'failed'


def test_explicit_history_range():
    from quarterly_dashboard.market_financial_backfill import periods
    assert periods('2025-09-30', '2026-06-30') == ['2025-09-30', '2025-12-31', '2026-03-31', '2026-06-30']
    assert len(periods('2014-09-30','2016-06-30'))==8
    with pytest.raises(ValueError):
        periods('2025-09-30', '2025-06-30')


def test_performance_all_fields_zero_negative_and_announcement_revision(db):
    data=datasets()
    performance=next(d for d in data if d['dataset']=='performance')
    performance['rows'][0].update(BASIC_EPS=0,WEIGHTAVG_ROE=-2.68,ASSIGNDSCRPT='不分配不转增',
                                  UPDATE_DATE='2026-10-08',EITIME='2026-10-08 01:00:00')
    save(db,data)
    performance['rows'][0]['EITIME']='2026-10-08 02:00:00'
    _,summary=save(db,data)
    assert summary['performance']['unchanged']==1
    with db.connection() as conn:
        record=conn.execute('SELECT * FROM market_financial_performance').fetchone()
        assert record['report_date']==PERIOD and record['source_report_date']==PERIOD+' 00:00:00'
        assert record['basic_eps']==0 and record['weightavg_roe']==-2.68
        assert record['assigndscrpt']=='不分配不转增'
    performance['rows'][0]['NOTICE_DATE']='2025-08-02 00:00:00'
    _,summary=save(db,data)
    assert summary['performance']['revised']==1


def test_history_updates_only_new_four_tables_and_skip_requires_all_columns(db,tmp_path,monkeypatch):
    from quarterly_dashboard import market_financial_backfill as history
    IndustryService(db).import_bundle(fixture_bundle(),capture_local=False)
    before=history.legacy_fingerprint(db)
    data=datasets()
    # A previous partial CPD batch must not cause the full-field backfill to skip.
    for d in data:
        for source in d['sources']:
            source['params']['columns']='ALL' if d['dataset']!='performance' else 'BASIC_EPS'
        d['checkpoint']=tmp_path/'unused'
    save(db,data)
    with db.connection() as conn:
        assert history.completed_batch(conn,PERIOD) is None
    for d in data:
        d['sources'][0]['params']['columns']='ALL'
    calls=[]
    def collect(*args,**kwargs):
        calls.append(args[1]);return data
    monkeypatch.setattr(mf,'collect',collect)
    result=history.run(db,PERIOD,PERIOD,tmp_path/'report.json')
    assert result['status']=='complete' and calls==[PERIOD]
    assert history.legacy_fingerprint(db)==before
    assert result['legacy_updated'] is False
    repeat=history.run(db,PERIOD,PERIOD,tmp_path/'repeat.json')
    assert repeat['periods'][0]['skipped'] is True and calls==[PERIOD]
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM market_financial_performance').fetchone()[0]==1
