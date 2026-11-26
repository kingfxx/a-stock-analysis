"""Mixed stock collection and HK shared facts using offline provider responses."""

import json
import os
from pathlib import Path
import shutil
import subprocess
from threading import Event, Thread
import time
from urllib.parse import urlsplit

import pytest
import requests

from stock_analysis.collection.collection import RequestCollector
from stock_analysis.domains.financials.business_segment_service import BusinessSegmentService
from stock_analysis.domains.financials.hk_statement_parser import CATALOG, REPORTS
from stock_analysis.domains.financials.statements import read_statements
from stock_analysis.domains.stocks.collection_operations import StockCollections
from stock_analysis.domains.stocks.collection_schedules import configure
from stock_analysis.domains.stocks.data_service import StockDataService
from stock_analysis.domains.stocks.hk_dividend_parser import INTERFACE as DIVIDENDS
from stock_analysis.domains.stocks.stock_library import StockLibrary
from stock_analysis.infrastructure.app_settings import AppSettings
from stock_analysis.infrastructure import storage
from stock_analysis.infrastructure.storage import Database


class FixtureSession:
    dividend_amount = '0.50414'

    def get(self, url, params=None, **kwargs):
        params = params or {}
        host = urlsplit(url).hostname
        if host == 'stockanalysis.com':
            return reply('''<title>Cosco (HKG:1919)</title><p>Financials in millions CNY.</p>
                <table id="main-table-revenue-by segment">
                <tr><th>Fiscal Year</th><th>FY 2025</th><th>FY 2024</th></tr>
                <tr><th>Period Ending</th><td>Dec 31, 2025</td><td>Dec 31, 2024</td></tr>
                <tr><th>Container Shipping Business</th><td>210.73B</td><td>225.97B</td></tr>
                <tr><th>Terminal Business</th><td>12.04B</td><td>-</td></tr>
                <tr><th>Offset</th><td>-3.27B</td><td>-</td></tr>
                <tr><th>Total</th><td>219.50B</td><td>225.97B</td></tr></table>''')
        if host == 'www1.hkexnews.hk':
            if url.endswith('prefix.do'):
                return reply('callback({"stockInfo":[{"stockId":9217,"code":"01919"}]});')
            record = dict(NEWS_ID='12059822', STOCK_CODE='01919', DATE_TIME='20/03/2026 06:04',
                          FILE_LINK='/listedco/listconews/sehk/2026/0320/2026032000005.pdf',
                          TITLE='ANNUAL RESULTS FOR THE YEAR ENDED 31 DECEMBER 2025',
                          LONG_TEXT='Announcements and Notices - [Final Results]')
            return reply(dict(result=json.dumps([record]), recordCnt=1, loadedRecord=1, hasNextRow=False))
        if host.endswith('baidu.com'):
            chart = {'body': [['2026-05-28', '6.42']], 'header': [params['query']]}
            return reply({'Result': [{'DisplayData': {'resultData': {'tplData': {
                'result': {'chartInfo': [chart]}}}}}]})
        if host.endswith('gtimg.cn'):
            symbol, _, _, end, _, adjustment = params['param'].split(',')
            rows = [[day, '14.55', '14.06', '14.62', '14.00', '100']
                    for day in ('2025-12-31', '2026-03-20', '2026-05-28') if day <= end]
            assert ('hkfqkline/get' in url) == (symbol.startswith('hk') and adjustment == 'qfq')
            quote = ['100', '中远海控', symbol[2:]] + [''] * 72 + ['HKD']
            return reply(dict(code=0, data={symbol: {adjustment + 'day': rows, 'qt': {symbol: quote}}}))
        assert host.endswith('eastmoney.com')
        common = dict(SECUCODE='01919.HK', SECURITY_CODE='01919', REPORT_DATE='2025-12-31 00:00:00',
                      START_DATE='2025-01-01 00:00:00', DATE_TYPE_CODE='001')
        interface = params['reportName']
        if interface == CATALOG:
            return result([{'REPORT_LIST': [{**common, 'CURRENCY': '人民币', 'FISCAL_YEAR': '12-31',
                'ACCOUNT_STANDARD': '国际会计准则', 'REPORT_TYPE': '年报', 'SECURITY_NAME_ABBR': '中远海控'}]}])
        if interface == DIVIDENDS:
            return result([{**common, 'YEAR': '2006', 'REPORT_TYPE': '年度分配',
                'EX_DIVIDEND_DATE': '2007/04/12', 'DIVIDEND_DATE':'现金：2007/05/29\n送股：2007/05/29',
                'NOTICE_DATE': '2007-03-29 00:00:00',
                'PLAN_EXPLAIN':'每股派人民币0.09元(相当于港币0.090962元(计算值)),每100股派送15股'},
                {**common, 'YEAR': '2025', 'REPORT_TYPE': '年度分配',
                'EX_DIVIDEND_DATE': '2026/05/28', 'DIVIDEND_DATE':'2026/06/26',
                'NOTICE_DATE': '2026-03-20 00:00:00',
                'PLAN_EXPLAIN':f'每股派人民币0.44元(相当于港币{self.dividend_amount}元)'}])
        fields = {
            REPORTS['fzb']: [('004009999', '总资产', 483758023838.79)],
            REPORTS['lrb']: [('004001999', '营运收入', 219503805000), ('004025002', '股东应占溢利', 30859651000)],
            REPORTS['llb']: [('003999', '经营业务现金净额', 45545770000)],
        }
        assert params['pageSize'] == 500
        assert "(REPORT_DATE>='2025-12-31')(REPORT_DATE<='2025-12-31')" in params['filter']
        return result([{**common, 'STD_ITEM_CODE': field, 'STD_ITEM_NAME': label, 'AMOUNT': amount}
                       for field, label, amount in fields[interface]])

    def close(self):
        pass


def reply(payload):
    response = requests.Response()
    response.status_code = 200
    response.encoding = 'utf-8'
    response._content = (payload if isinstance(payload, str) else json.dumps(payload)).encode()
    response.headers['Content-Type'] = 'text/html' if isinstance(payload, str) else 'application/json'
    return response


def result(rows):
    return reply({'success': True, 'result': {'count': len(rows), 'pages': int(bool(rows)), 'data': rows}})


@pytest.fixture
def stock(tmp_path, monkeypatch):
    db = Database(tmp_path / 'test.sqlite3')
    db.initialize()
    data = StockDataService(AppSettings(DATABASE_PATH=db.path, CACHE=tmp_path/'cache',
        CHIP_CACHE=tmp_path/'chips', VALUATION_CACHE=tmp_path/'valuation'))
    service = StockCollections(db, data)
    moment = 1_800_000_000.0

    def sleep(seconds):
        nonlocal moment
        moment += seconds

    def collector(db, **kwargs):
        return RequestCollector(db, session=FixtureSession(), clock=lambda: moment, sleep=sleep,
                                uniform=lambda *_: 0, **kwargs)

    monkeypatch.setattr('stock_analysis.domains.stocks.collection_operations.RequestCollector', collector)
    return service


def run(stock, **params):
    with stock.db.connection(write=True) as conn:
        ref = stock.reserve(conn, params)
    stock.run(ref)
    detail = stock.detail(ref)
    assert detail['status'] == 'complete', [(item['dataset'], item['status'], item.get('error'))
                                             for item in detail['items']]
    return detail


def test_hk_data_publishes_in_shared_tables(stock):
    detail = run(stock, codes=['01919'], mode='full')
    assert {r['dataset'] for r in detail['items'] if r['status']=='not_applicable'} == {'shareholders','financing'}
    code, page, error = stock.data.page_payload('01919', False)
    assert code == '01919' and not error and page['name'] == '中远海控'
    assert page['views']['year'][-1]['profit'] == 30859651000
    assert page['views']['year'][-1]['qfq_price'] == 14.06
    assert page['views']['year'][-1]['period_end_qfq_price_date'] == '2025-12-31'
    assert page['hk_latest_prices']['raw'] == {'date': '2026-05-28', 'close': 14.06}
    assert page['hk_dividends'][-1]['payment_per_share'] == '0.50414'
    assert page['hk_dividends'][-1]['payment_date'] == '2026-06-26'
    assert page['hk_dividends'][0]['payment_date'] == '2007-05-29'
    assert page['hk_dividends'][0]['payment_per_share'] is None
    assert read_statements(stock.db, code)['values']['parent_profit']['source_field'] == '004025002'
    segments = BusinessSegmentService(stock.db).read(code)['periods']
    assert segments[-1]['report_currency'] == 'CNY'
    assert segments[-1]['dimensions']['1'][0]['revenue'] == 210730000000
    assert segments[0]['dimensions']['1'][0]['revenue_share'] is None
    with stock.db.connection() as conn:
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        sources = conn.execute('SELECT source_system,file_path FROM collection_sources').fetchall()
        catalogs = conn.execute("SELECT count(*) FROM collection_sources "
                                "WHERE json_extract(request_json,'$.params.reportName')=?", (CATALOG,)).fetchone()[0]
    assert catalogs == 1
    assert {r['source_system'] for r in sources} == {'tencent','eastmoney','hkexnews','baidu','stockanalysis'}
    assert all(r['file_path'] is None for r in sources)


def test_stock_group_mix_and_schedule_scope(stock):
    library = StockLibrary(stock.db)
    stock.db.ensure_instrument('601919', '中远海控')
    stock.db.ensure_instrument('01919.HK', '中远海控')
    created = library.change({'action': 'create', 'name': '持仓'})
    group = created['groups'][0]['id']
    updated = library.change({'action': 'membership', 'id': group, 'add': True, 'codes': ['601919', '01919']})
    assert updated['groups'][0]['codes'] == ['601919','01919']
    assert {r['exchange'] for r in updated['stocks']} == {'sh','hk'}
    detail = run(stock, group_id=group, datasets=['prices'])
    assert {x['code'] for x in detail['items']} == {'601919','01919'}
    assert {x['status'] for x in run(stock, codes=['601919','01919'], datasets=['prices'], mode='resume')['items']} == {'reused'}
    with stock.db.connection(write=True) as conn:
        ref = stock.reserve(conn, dict(group_id=group, datasets=['prices']), trigger='scheduled')
    assert stock.detail(ref)['scope']['codes'] == ['601919']
    stock.run(ref)
    configure(stock.db, {'params': {'group_id': group, 'datasets': ['prices'], 'include_hk': True},
        'enabled': True, 'local_time': '19:00', 'rule': {'frequency': 'daily'}})
    with stock.db.connection() as conn:
        saved = json.loads(conn.execute("SELECT params_json FROM collection_schedules WHERE task_key='stocks'").fetchone()[0])
    with stock.db.connection(write=True) as conn:
        ref = stock.reserve(conn, saved, trigger='scheduled')
    assert stock.detail(ref)['scope']['codes'] == ['601919','01919']
    with pytest.raises(ValueError, match='均不适用'):
        with stock.db.connection(write=True) as conn:
            stock.reserve(conn, dict(codes=['01919'], datasets=['shareholders']))


def test_revised_dividend_keeps_original_source_fact(stock, monkeypatch):
    run(stock, codes=['01919'], datasets=['dividends'])
    monkeypatch.setattr(FixtureSession, 'dividend_amount', '0.507')
    run(stock, codes=['01919'], datasets=['dividends'])
    with stock.db.connection() as conn:
        old = conn.execute('SELECT raw_json,run_id FROM stock_fact_revisions').fetchall()
        active = conn.execute("SELECT payment_per_share,run_id FROM dividend_events WHERE ex_dividend_date='2026-05-28'").fetchone()
    assert len(old) == 1
    assert '0.50414' in old[0]['raw_json']
    assert active['payment_per_share'] == '0.507'
    assert active['run_id'] != old[0]['run_id']


def test_single_hk_can_start_independent_sources_before_financial_finishes(stock, monkeypatch):
    from stock_analysis.collection.execution_context import current_execution

    financial_done = Event()
    valuation_started = Event()
    prices_started = Event()

    def collect(code, dataset, full, collector):
        assert current_execution.get()['batch_pacing'] is False
        if dataset == 'financial':
            assert valuation_started.wait(5)
            assert prices_started.wait(5)
            financial_done.set()
        elif dataset == 'valuation':
            valuation_started.set()
        elif dataset == 'prices':
            prices_started.set()
        else:
            assert financial_done.is_set()

    monkeypatch.setattr(stock, 'collect', collect)
    detail = run(stock, codes=['01919'], datasets=['financial', 'announcements', 'prices', 'valuation'], mode='full')
    assert {item['dataset'] for item in detail['items']} == {'financial', 'announcements', 'prices', 'valuation'}


def test_upgrade_preserves_existing_instrument_references(tmp_path, monkeypatch):
    db = Database(tmp_path / 'old.sqlite3')
    with monkeypatch.context() as patch:
        patch.setattr(storage, 'MIGRATIONS', tuple(row for row in storage.MIGRATIONS if row[0] <= 55))
        db.initialize()
        identity = db.ensure_instrument('601919', '中远海控')
        with db.connection(write=True) as conn:
            group = conn.execute("INSERT INTO stock_groups(name,position) VALUES('持仓',0)").lastrowid
            conn.execute('INSERT INTO stock_group_members(group_id,instrument_id,position) VALUES(?,?,0)', (group, identity))
    db.initialize()
    db.initialize()
    assert db.ensure_instrument('601919') == identity
    assert db.ensure_instrument('01919') != identity
    assert StockLibrary(db).read()['groups'][0]['codes'] == ['601919']
    assert db.check()['integrity'] == 'ok'


def test_hk_tabs_in_browser(stock, tmp_path, monkeypatch):
    from stock_analysis import application as app
    from stock_analysis.http import server

    edge = shutil.which('msedge') or r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
    node = shutil.which('node')
    if not Path(edge).is_file() or not node:
        pytest.skip('Edge and Node are required')
    run(stock, codes=['01919'], mode='full')
    stock.db.ensure_instrument('601919', '中远海控')
    monkeypatch.setattr(app, 'data', stock.data)
    monkeypatch.setattr(app.settings, 'DATABASE_PATH', stock.db.path)
    monkeypatch.setattr(app, 'stock_collections', lambda: stock)
    http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    worker = Thread(target=http.serve_forever, daemon=True)
    worker.start()
    profile = tmp_path / 'browser'
    browser = None
    try:
        with (tmp_path / 'edge.log').open('w', encoding='utf-8') as log:
            browser = subprocess.Popen([edge, '--headless=new', '--disable-gpu', '--no-first-run',
                '--remote-debugging-port=0', f'--user-data-dir={profile}', 'about:blank'],
                stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            endpoint = profile / 'DevToolsActivePort'
            deadline = time.monotonic() + 15
            while not endpoint.exists():
                if browser.poll() is not None or time.monotonic() > deadline:
                    pytest.fail('Headless browser did not start')
                time.sleep(0.05)
            check = subprocess.run([node, str(Path(__file__).with_name('hk_stocks_browser.cjs')),
                endpoint.read_text().splitlines()[0], f'http://127.0.0.1:{http.server_port}'],
                capture_output=True, text=True, encoding='utf-8', timeout=35)
            assert check.returncode == 0, check.stderr
    finally:
        if browser and browser.poll() is None:
            browser.terminate()
            try:
                browser.wait(timeout=5)
            except subprocess.TimeoutExpired:
                browser.kill()
                browser.wait(timeout=5)
        http.shutdown()
        http.server_close()
        worker.join(timeout=2)
