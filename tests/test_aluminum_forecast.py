from copy import deepcopy
from datetime import date, datetime
import json
import sqlite3
import subprocess
from threading import Thread

import pytest
import requests

from stock_analysis import application as app
from stock_analysis.domains.research.sectors.aluminum.forecast import AluminumForecast, calculate
from stock_analysis.domains.research.sectors.aluminum.market_reference_parser import parse_monthly, parse_spot
from stock_analysis.domains.research.sectors.aluminum.market_references import MarketReferences, MONTHLY_PARAMS
from stock_analysis.domains.research.sectors.aluminum.model import SCENARIOS
from stock_analysis.domains.research.sectors.aluminum.quote_parser import settled_quotes
from stock_analysis.domains.research.sectors.aluminum.quotes import AluminumQuotes, BEIJING, candidates
from stock_analysis.domains.research.sectors.aluminum.sensitivity import actual_price, validate_transmission
from stock_analysis.collection.collection import ProviderBlocked
from stock_analysis.http.server import Handler, ThreadingHTTPServer
from stock_analysis.infrastructure.storage import Database
from stock_analysis.maintenance.maintenance import MaintenanceService


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / 'aluminum.sqlite3')
    database.initialize()
    return database


def complete(service):
    data = service.read()
    draft = deepcopy(data['defaults'])
    draft['base'].update(profit=12e9, al_price=20000, ao_price=3000, share_price=8)
    for rows in draft['scenarios'].values():
        for index, row in enumerate(rows):
            row.update(al_price=21000 + index * 100, ao_price=3500, al_sensitivity=2e8,
                       ao_sensitivity=-1e8, volume=3e8, cost=-1e8, other=0, shares=16e9)
    return dict(name='方案', note='离线测试', parent_version_id=None, **draft,
                evidence=data['evidence'], proof=data['proof'], quotes={},
                adopted={key: [{}, {}, {}] for key in SCENARIOS})


def test_price_effects_and_dividends_match_browser(db):
    case = complete(AluminumForecast(db, 'session'))
    args = [case[key] for key in ('base', 'start', 'scenarios')]
    result = calculate(*args)
    row = result['base']['rows'][0]
    assert row['parts']['al'] == 2e9
    assert row['parts']['ao'] == -5e8
    assert row['profit'] == 13.7e9
    assert row['cash'] == 4.11e9
    assert row['dps'] == pytest.approx(4.11 / 16)
    assert row['yield_rate'] == pytest.approx(4.11 / 16 / 8)
    assert result['base']['rows'][1]['profit'] == 13.9e9
    assert result['base']['cumulative_cash'] == pytest.approx(sum(r['cash'] for r in result['base']['rows']))
    case['scenarios']['bear'][0]['cost'] = -30e9
    case['scenarios']['bear'][1]['al_price'] = None
    case['scenarios']['bull'][0]['payout'] = 0
    case['scenarios']['bull'][1]['shares'] = None
    reduced = calculate(*args)
    assert reduced['bear']['rows'][0]['profit'] < 0
    assert reduced['bear']['rows'][0]['cash'] is None
    assert reduced['bear']['rows'][1]['profit'] is None
    assert reduced['bear']['cumulative_cash'] is None
    assert reduced['bull']['rows'][0]['cash'] == 0
    assert reduced['bull']['rows'][1]['cash'] > 0
    assert reduced['bull']['rows'][1]['dps'] is None
    js = r"""
const fs = require('node:fs');
const {calculate} = require('./web/features/research/sectors/aluminum/aluminum-forecast.js');
const {calculate_scenario} = require('./web/features/valuation/dividend-forecast.js');
const args = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(calculate(...args, calculate_scenario)));
"""
    browser = subprocess.run(['node', '-e', js], input=json.dumps(args), encoding='utf-8',
                             capture_output=True, check=True)
    assert json.loads(browser.stdout) == reduced


def test_version_immutability_restore_and_isolation(db, tmp_path):
    service = AluminumForecast(db, 'session')
    case = complete(service)
    company_version = service.save(case)
    original = service.read_version(company_version['id'])
    assert original['snapshot']['note'] == '离线测试'
    assert original['snapshot']['results'] == company_version['results']
    case['name'] = '更新参数'
    case['parent_version_id'] = company_version['id']
    case['scenarios']['base'][0]['al_price'] += 1000
    child = service.save(case)
    assert service.read_version(company_version['id']) == original
    assert child['results']['base']['rows'][0]['profit'] > company_version['results']['base']['rows'][0]['profit']
    with db.connection(write=True) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE research_aluminum_forecast_versions SET name='changed'")
        assert conn.execute('SELECT count(*) FROM research_shipping_forecast_versions').fetchone()[0] == 0
        conn.execute("INSERT INTO research_aluminum_forecast_versions "
                     "(name,forecast_start_year,model_version,schema_version,input_hash,snapshot_json,created_at) "
                     "VALUES('legacy',2027,'older-model',2,'hash','{\"results\":{\"base\":1}}','2026-11-25')")
        legacy_id = conn.execute('SELECT max(id) FROM research_aluminum_forecast_versions').fetchone()[0]
    legacy = service.read_version(legacy_id)
    assert not legacy['compatible'] and legacy['snapshot']['results'] == {'base': 1}
    report = MaintenanceService(db).read()
    summary = next(row for row in report['tables'] if row['name'] == 'research_aluminum_forecast_versions')
    assert summary['category'] == '行业研究'
    assert summary['rows'] == 3 and summary['updated_at'] and summary['time_basis'] == 'created_at'
    dest = db.backup(tmp_path / 'restored.sqlite3')
    assert AluminumForecast(Database(dest), 'new process').read_version(child['id'])['snapshot']['results'] == child['results']
    service.delete(company_version['id'])
    assert service.read_version(child['id'])['parent_version_id'] == company_version['id']
    with pytest.raises(LookupError):
        service.read_version(company_version['id'])


def test_quote_proof_and_tampered_facts_rejected(db):
    service = AluminumForecast(db, 'session')
    case = complete(service)
    value = {'source_id': 3, 'contract': 'AL2701', 'price': 21000.0, 'trade_date': '2026-11-24'}
    entry = service._quote_state({'quotes': {'al': value}, 'errors': {}})['quotes']['al']
    case['quotes']['al'] = entry
    case['adopted']['bear'][0]['al'] = entry
    # Browser JSON reserializes an integral float as an integer.
    value['price'] = 21000
    saved = service.save(case)
    context = service.read_version(saved['id'])['snapshot']['context']
    assert context['adopted']['bear'][0]['al'] == '3:AL2701'
    assert len(context['quote_registry']) == 1
    case['scenarios']['bear'][0]['al_price'] = None
    with pytest.raises(ValueError, match='不一致'):
        service.save(case)
    case['scenarios']['bear'][0]['al_price'] = 21000
    case['evidence']['as_of'] = '2026-01-01'
    with pytest.raises(ValueError, match='无法核验'):
        service.save(case)


def test_default_parameters_are_filled_and_scenarios_differ(db):
    service = AluminumForecast(db, 'session')
    facts = service.read()
    draft = facts['defaults']
    assert draft['start'] == max(2026, int(facts['evidence']['as_of'][:4]))
    assert draft['base']['al_price'] == 20750 and draft['base']['ao_price'] == 3221
    cases = draft['scenarios']
    for index in range(3):
        conservative, central, optimistic = (cases[key][index] for key in SCENARIOS)
        assert conservative['al_price'] < central['al_price'] < optimistic['al_price']
        assert conservative['ao_price'] < central['ao_price'] < optimistic['ao_price']
        assert central['al_sensitivity'] / 1e8 == pytest.approx(3.503, abs=.002)
        assert central['ao_sensitivity'] / 1e8 == pytest.approx(.517, abs=.002)


def test_current_year_realized_profit_and_remainder_match_browser(db):
    service = AluminumForecast(db, 'session')
    case = complete(service)
    case['base']['year'] = 2025
    start = 2026
    current = dict(year=start, actual=dict(period='2026-09-30', profit=17.9e9),
                   prior={'2025': dict(period='2025-09-30', profit=10e9)})
    args = [case['base'], start, case['scenarios']]
    results = calculate(*args, current)
    row = results['base']['rows'][0]
    assert row['progress']['actual_profit'] == 17.9e9
    assert row['parts']['base'] == 2e9
    assert row['parts']['al'] == .5e9
    assert row['parts']['ao'] == -.125e9
    # Operating adjustments apply to the period still being forecast.
    assert row['parts']['volume'] == .3e9
    assert row['progress']['forecast_profit'] == 2.575e9
    assert row['profit'] == 20.475e9
    assert results['base']['rows'][1] == calculate(*args)['base']['rows'][1] | {
        'growth_rate': results['base']['rows'][1]['growth_rate']}
    missing = calculate(*args, dict(current, prior={}))['base']['rows'][0]
    assert missing['profit'] is None and missing['progress']['actual_profit'] == 17.9e9
    full = calculate(*args, dict(current, actual=dict(period='2026-12-31', profit=21e9)))['base']['rows'][0]
    assert full['profit'] == 21e9 and full['progress']['remaining_quarters'] == 0
    case['start'] = start
    case['evidence']['current_year'] = current
    case['proof'] = service._sign('facts', case['evidence'])
    saved = service.save(case)
    loaded = service.read_version(saved['id'])
    assert loaded['evidence']['current_year'] == current
    assert loaded['snapshot']['results'] == results
    js = r"""
const fs = require('node:fs');
const {calculate} = require('./web/features/research/sectors/aluminum/aluminum-forecast.js');
const {calculate_scenario} = require('./web/features/valuation/dividend-forecast.js');
const [base, start, scenarios, current] = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(calculate(base, start, scenarios, calculate_scenario, current)));
"""
    browser = subprocess.run(['node', '-e', js], input=json.dumps([*args, current]), encoding='utf-8',
                             capture_output=True, check=True)
    assert json.loads(browser.stdout) == results


def test_existing_schema_upgrades_with_pre_migration_backup(tmp_path, monkeypatch):
    from stock_analysis.infrastructure import storage
    current = storage.MIGRATIONS
    database = Database(tmp_path / 'upgrade.sqlite3')
    monkeypatch.setattr(storage, 'MIGRATIONS', current[:-1])
    assert database.initialize()['schema_version'] == 49
    with database.connection(write=True) as conn:
        conn.execute("INSERT INTO research_shipping_forecast_versions "
                     "VALUES(1,'2026Q3','旧航运','forecast','shipping-v1','hash','{}','2026-11-25')")
    monkeypatch.setattr(storage, 'MIGRATIONS', current)
    assert database.initialize()['schema_version'] == 50
    with database.connection() as conn:
        assert conn.execute('SELECT name FROM research_shipping_forecast_versions').fetchone()[0] == '旧航运'
    row = next(item for item in MaintenanceService(database).read()['tables']
               if item['name'] == 'research_aluminum_forecast_versions')
    assert row['rows'] == 0 and row['updated_at'] is None and row['time_basis'] == 'created_at'
    assert list((tmp_path / 'backups').glob('pre-migration-*.sqlite3'))


def example_payload():
    def contract(product, delivery, price, interest, volume):
        return dict(PRODUCTID=product + '_f', DELIVERYMONTH=delivery, SETTLEMENTPRICE=price,
                    OPENINTEREST=interest, VOLUME=volume)
    return dict(report_date='20261124', update_date='20261124 15:55:49', o_code='0000', o_year_num='215',
                o_curinstrument=[contract('al', '2702', 20950, 100, 80),
                                 contract('al', '2701', 21560, 100, 80),
                                 contract('al', '2612', 22000, 100, 79),
                                 contract('ao', '2701', 2551, 50, 30)])


def test_quote_parser_settlement_selection_and_partial():
    payload = example_payload()
    day = date(2026, 11, 24)
    quotes, errors = settled_quotes(payload, day)
    assert not errors and quotes['al']['contract'] == 'AL2701' and quotes['ao']['price'] == 2551
    payload['o_curinstrument'][-1]['SETTLEMENTPRICE'] = ''
    assert set(settled_quotes(payload, day)[0]) == {'al'}
    payload['o_year_num'] = ''
    assert settled_quotes(payload, day)[0] == {}
    assert candidates(datetime(2026, 11, 23, 12, tzinfo=BEIJING))[0] == date(2026, 11, 20)
    with pytest.raises(ValueError, match='交易日'):
        settled_quotes(payload, date(2026, 11, 25))


def test_market_reference_source_and_window(db):
    html = """<h2>长江综合 ● 基础金属</h2><table><tr>
    <td class="product">A00铝锭(华东）</td><td class="range">22,300 - 22,340</td>
    <td class="mid">22320</td><td class="unitc">元/吨</td><td class="date">25/11/2026</td></tr></table>"""
    payload = [
        dict(d='2025-12-31', o='21615', c='22925', h='23030', l='21575'),
        dict(d='2026-11-25', o='21200', c='20600', h='21445', l='20580'),
        dict(d='2026-11-24', o='21400', c='21390', h='21990', l='21200'),
    ]
    today = date(2026, 11, 25)
    assert parse_spot(html, today)['price'] == 22320
    with pytest.raises(ValueError, match='单位或日期'):
        parse_spot(html, date(2026, 11, 24))
    with pytest.raises(ValueError, match='唯一'):
        parse_spot(html.replace('华东', '华南'), today)
    monthly = parse_monthly(payload, today)['points']
    assert len(monthly) == 12 and monthly[0]['month'] == '2025-12'
    assert monthly[1]['close'] is None and monthly[-1]['close'] == 20600
    invalid = deepcopy(payload)
    invalid.append(invalid[0])
    with pytest.raises(ValueError, match='交易日'):
        parse_monthly(invalid, today)

    calls = []

    class FixtureCollector:
        def __init__(self, database):
            self.database = database

        def get(self, batch, provider, dataset, url, **kwargs):
            from stock_analysis.collection.collection import archive_source
            calls.append(provider)
            content = html.encode() if provider == 'ccmn' else json.dumps(payload).encode()
            if provider == 'sina':
                assert kwargs['params'] == MONTHLY_PARAMS
            source = archive_source(self.database, batch, provider, dataset, {'url': url}, 1, raw=content)
            return type('Response', (), {'content': content})(), source

    references = MarketReferences(db, FixtureCollector)
    assert references.latest(today) == {'errors': {}} and not calls
    assert references.refresh(today)['spot']['price'] == 22320
    assert references.refresh(today)['monthly']['points'] == monthly
    assert calls == ['ccmn', 'sina']
    recovered = MarketReferences(db, FixtureCollector).latest(today)
    assert recovered['spot']['archived'] and recovered['monthly']['archived']
    assert calls == ['ccmn', 'sina']


def test_quote_refresh_holiday_refusal_and_cache(db):
    calls = []

    class FixtureCollector:
        def __init__(self, database):
            self.database = database

        def get(self, batch, provider, dataset, url, **kwargs):
            calls.append(url)
            if url.endswith('20261125.dat'):
                raise ValueError('shfe HTTP 404')
            from stock_analysis.collection.collection import archive_source
            payload = example_payload()
            source = archive_source(self.database, batch, provider, dataset, {'url': url}, 1,
                                    raw=json.dumps(payload).encode())
            return type('Response', (), {'json': lambda self: payload})(), source

    service = AluminumQuotes(db, FixtureCollector)
    now = datetime(2026, 11, 25, 17, tzinfo=BEIJING)
    assert service.latest()['quotes'] == {} and not calls
    assert service.refresh(now)['quotes']['al']['trade_date'] == '2026-11-24'
    assert len(calls) == 2
    assert service.refresh(now)['reused'] and len(calls) == 2
    with db.connection() as conn:
        assert conn.execute('SELECT coverage_status FROM collection_batches').fetchone()[0] == 'complete'
    recovered = AluminumQuotes(db, FixtureCollector).latest()
    assert recovered['quotes']['al']['archived'] and len(calls) == 2
    generated = AluminumForecast(db, 'session', AluminumQuotes(db, FixtureCollector)).read()['defaults']
    assert generated['scenarios']['base'][0]['al_price'] == 21560

    class BlockedCollector(FixtureCollector):
        def get(self, *args, **kwargs):
            calls.append('blocked')
            raise ProviderBlocked('SHFE 冷却')

    service._last_good = None
    service.collector_factory = BlockedCollector
    refreshed = service.refresh(now)
    assert refreshed['quotes']['al']['contract'] == 'AL2701'
    assert '冷却' in refreshed['errors']['al']
    assert calls.count('blocked') == 1


def test_rolling_fit_respects_disclosure_and_baseline():
    records = [dict(start=f'{year}-01-01', end=f'{year}-12-31', published_on=f'{year + 1}-03-28',
                    frequency='annual', basis='same scope', market_price=20000 + index * 500,
                    actual_price=18000 + index * 400)
               for index, year in enumerate(range(2018, 2026))]
    assert actual_price(18000000, 1000) == 18000
    assert actual_price(None, 1000) is None
    assert not validate_transmission(records[:7])['passed']
    checked = validate_transmission(records)
    assert checked['passed'] and checked['slope'] == pytest.approx(.8)
    assert checked['tests'][0]['train_end'] == '2022-12-31'
    assert checked['tests'][0]['period'] == '2024-12-31'
    assert checked['mae'] == 0 < checked['baseline_mae']
    with pytest.raises(ValueError, match='重叠'):
        validate_transmission([records[0], records[0]])


def test_http_routes_local_session_and_no_get_collection(db, monkeypatch):
    service = AluminumForecast(db, app.LOCAL_SESSION_TOKEN)
    monkeypatch.setattr(app, 'aluminum_forecast', lambda: service)
    fired = []
    monkeypatch.setattr(service.quotes, 'refresh', lambda: fired.append(True) or {'quotes': {}, 'errors': {}})
    monkeypatch.setattr(service.references, 'refresh', lambda: {'errors': {}})
    http = None
    for port in (8768, 8766, *range(18860, 18870)):
        try:
            http = ThreadingHTTPServer(('127.0.0.1', port), Handler)
            break
        except OSError:
            continue
    assert http is not None
    run = Thread(target=http.serve_forever, daemon=True)
    run.start()
    base = f'http://127.0.0.1:{http.server_port}'
    session = requests.Session()
    session.trust_env = False
    try:
        assert session.get(base + '/?code=601600&tab=aluminum-forecast', timeout=5).status_code == 200
        assert session.get(base + '/aluminum-forecast.js', timeout=5).status_code == 200
        result = session.get(base + '/api/aluminum-forecast', timeout=5)
        assert result.status_code == 200 and not fired
        assert session.get(base + '/api/aluminum-forecast?code=601919', timeout=5).status_code == 400
        headers = {'X-Local-Session': result.json()['session_token'], 'Origin': base}
        url = base + '/api/aluminum-forecast'
        assert session.post(url + '/refresh-quotes', json={}, timeout=5).status_code == 403
        assert not fired
        assert session.post(url + '/refresh-quotes', json={}, headers=headers, timeout=5).status_code == 200
        assert fired == [True]
        saved = session.post(url + '/save', json=complete(service), headers=headers, timeout=5)
        assert saved.status_code == 200, saved.text
        version = saved.json()['id']
        assert session.get(f'{url}?version={version}', timeout=5).json()['id'] == version
        assert session.post(url + '/delete', json={'id': version}, headers=headers, timeout=5).status_code == 200
        assert session.get(f'{url}?version={version}', timeout=5).status_code == 404
    finally:
        session.close()
        http.shutdown()
        http.server_close()
        run.join(timeout=5)
