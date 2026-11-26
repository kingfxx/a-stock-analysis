"""Isolated checks for portfolio imports, history and the private HTTP boundary."""

import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from hashlib import sha256
from threading import Thread
from types import SimpleNamespace

import pytest
import requests

from stock_analysis import application
from stock_analysis.domains.portfolio import cmb_xls
from stock_analysis.domains.portfolio.calculations import daily_change, fill_snapshot_daily_pnl
from stock_analysis.domains.portfolio.cmb_transfers import HEADERS
from stock_analysis.domains.portfolio.performance import reconstruct
from stock_analysis.domains.portfolio.performance_events import reconcile_etf_dividends
from stock_analysis.domains.portfolio.service import ImportConflict, Portfolio
from stock_analysis.http.server import Handler, ThreadingHTTPServer
from stock_analysis.infrastructure.storage import Database, restore_backup
from stock_analysis.maintenance.maintenance import DESCRIPTIONS, table_time


SNAPSHOT = cmb_xls.BIFF_SIGNATURE + b'snapshot'
HISTORY = cmb_xls.BIFF_SIGNATURE + b'history'
OVERLAP = cmb_xls.BIFF_SIGNATURE + b'overlap'
DAY = '2025-11-24'


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    snapshot_headers = ['证券代码', '证券名称', '证券数量', '价格币种', '成本价', '成本金额',
                        '最新市值', '当前价', '持仓盈亏', '参考汇率中间价']
    trade_headers = ['证券代码', '证券名称', '成交日期', '成交时间', '买卖标志',
                     '成交价格', '成交数量', '成交金额', '成交编号', '委托编号']
    sell = ['000001', '示例银行', '20251121', '09:40:30', '卖出', 10, -100, 1000, '1', 'X']
    correction = ['000001', '示例银行', '20251121', '09:40:30', '交收资金修正取消', 0, 0, 0, '', 'X']
    examples = {
        SNAPSHOT: [
            ['币种', '余额', '可用', '可取', '参考市值', '资产'],
            ['人民币', 700, 900, 900, 1600, 2500],
            [],
            snapshot_headers,
            ['000001', '示例银行', 100, '人民币', 8, 800, 1000, 10, 200, ''],
            ['159915', '示例ETF', 200, '人民币', 2, 400, 500, 2.5, 100, ''],
            ['00009', '示例港股', 10, '港币', 8, 80, 100, '11.0港元', 20, '0.91 (HKD/CNY)'],
        ],
        HISTORY: [trade_headers, sell, correction],
        OVERLAP: [trade_headers, sell, sell, correction],
    }

    def workbook(*, file_contents, on_demand):
        rows = examples[file_contents]
        width = max(map(len, rows))
        sheet = SimpleNamespace(nrows=len(rows), ncols=width,
                                row_values=lambda row: rows[row] + [''] * (width - len(rows[row])))
        return SimpleNamespace(biff_version=80, nsheets=1, datemode=0, sheet_by_index=lambda _: sheet,
                               release_resources=lambda: None)

    monkeypatch.setattr(cmb_xls.xlrd, 'open_workbook', workbook)
    db = Database(tmp_path / 'portfolio.sqlite3')
    db.initialize()
    store = Portfolio(db, tmp_path / 'sources', lambda *_: {})
    return store


def import_snapshot(store, **extra):
    args = {'day': DAY, 'session': 'close', **extra}
    preview = store.ingest(SNAPSHOT, 'positions.xls', **args)
    return store.ingest(SNAPSHOT, 'positions.xls', confirmation=preview['preview'], **args)


def test_daily_change_respects_snapshot_asset_scope_and_broker_rate():
    gain = daily_change('1025', Decimal('25'))
    assert gain['daily_pnl_rate'] == '2.50' and gain['daily_rate_basis'] == 'estimated_opening_assets'
    assert daily_change('975', Decimal('-25'))['daily_pnl_rate'] == '-2.50'
    assert daily_change('975', Decimal('-25'), '-3')['daily_rate_basis'] == 'broker'
    assert daily_change(None, Decimal('-25'))['daily_pnl_rate'] is None
    assert daily_change('25', Decimal('25'))['daily_pnl_rate'] is None
    assert daily_change('100', Decimal('0'))['daily_pnl_rate'] == '0.00'


def test_daily_quote_fallback_requires_correct_dates_and_broker_reconciliation():
    snapshot = {'snapshot_date': DAY, 'session': 'close', 'broker_daily_pnl': '17'}
    previous = '2025-11-21'
    row = {'daily_pnl': None, 'quantity': 100, 'price': '7.61', 'asset_type': 'stock',
           'reference': {'close': 7.61, 'close_date': DAY, 'previous_close': 7.44, 'previous_close_date': previous}}
    fill_snapshot_daily_pnl(snapshot, [row], '2025-11-20')
    assert row['daily_pnl'] is None
    fill_snapshot_daily_pnl({**snapshot, 'broker_daily_pnl': '20'}, [row], previous)
    assert row['daily_pnl'] is None
    fill_snapshot_daily_pnl(snapshot, [row], previous)
    assert row['daily_pnl'] == '17.00' and row['daily_pnl_rate'] == '2.28'
    assert row['daily_pnl_basis'] == 'local_close_estimate'
    broker = {**row, 'daily_pnl': '18', 'daily_pnl_basis': None}
    fill_snapshot_daily_pnl(snapshot, [broker], previous)
    assert broker['daily_pnl'] == '18'


def test_performance_reconstructs_trades_dividends_and_gaps():
    key = ('sh', '600000')
    days = ['2025-12-31', '2026-01-05', '2026-01-06', '2026-01-07']
    prices = {key: {date: Decimal(value) for date, value in zip(days, ('10', '11', '12', '12'))}}

    def event(day, kind, gross, quantity=None, fees=None, net_cash=None):
        return {'security': key, 'event_date': day, 'event_type': kind, 'gross_amount': gross,
                'quantity': quantity, 'fees': fees, 'net_cash': net_cash}

    events = [event(days[1], 'buy', '105', quantity=10, fees='1'),
              event(days[2], 'sell', '60', quantity=-5, fees='2', net_cash='58'),
              event(days[2], 'dividend', '5'), event(days[2], 'dividend_tax', '2')]
    result = reconstruct(days, {key: 105}, events, prices)
    assert [row['pnl'] for row in result['records']] == ['104.00', '111.00', '0.00']
    assert [row['cumulative'] for row in result['records']] == ['104.00', '215.00', '215.00']
    assert result['records'][1]['dividend_net'] == '3.00'
    assert [row['market_value'] for row in result['records']] == ['1210.00', '1260.00', '1260.00']
    assert result['records'][0]['return_base'] == '1106.00'
    assert Decimal(result['records'][0]['return_rate']) == Decimal('104') / Decimal('1106')
    assert Decimal(result['records'][2]['return_rate']) == 0
    del prices[key][days[1]]
    estimated = reconstruct(days, {key: 105}, events, prices)['records']
    assert [row['pnl'] for row in estimated] == ['-6.00', '221.00', '0.00']
    assert [row['market_value'] for row in estimated] == ['1100.00', '1260.00', '1260.00']
    assert Decimal(estimated[1]['return_rate']) == Decimal('221') / Decimal('1100')
    assert estimated[1]['issues'] == []
    assert estimated[0]['estimated_closes'] == estimated[1]['estimated_closes'] == [
        {'code': '600000', 'date': days[1], 'price_date': days[0]}]
    assert estimated[2]['estimated_closes'] == []
    assert reconstruct(days, {key: 105}, [event(days[1], 'unknown', '0')], prices)['blocked_dates'] == [days[1]]
    assert reconstruct(days, {key: 0}, events, prices)['blocked_dates'] == [days[1]]


def test_performance_carries_one_prior_close_across_consecutive_gaps():
    a, b = ('sh', '600110'), ('sh', '600000')
    days = ['2022-10-19', '2022-10-20', '2022-10-21', '2022-10-24']
    before = '2022-10-18'
    prices = {a: {before: Decimal('10'), days[3]: Decimal('12')},
              b: {day: Decimal(value) for day, value in zip(days, ('20', '21', '22', '23'))}}
    records = reconstruct(days, {a: 100, b: 100}, [], prices)['records']
    assert [row['pnl'] for row in records] == ['100.00', '100.00', '300.00']
    assert [entry['price_date'] for entry in records[1]['estimated_closes']] == [before, before]
    assert days[1] not in prices[a]
    del prices[b][days[2]]
    records = reconstruct(days, {a: 100, b: 100}, [], prices)['records']
    assert [row['pnl'] for row in records] == ['100.00', None, None]
    assert any('600000' in issue for issue in records[1]['issues'])
    del prices[a][before]
    missing = reconstruct(days, {a: 100}, [], prices)['records'][0]
    assert missing['pnl'] is None and missing['estimated_closes'] == []


@pytest.mark.parametrize('posted,expected,estimated', [(True, '11.00', '0.00'), (False, '10.00', '10.00')])
def test_etf_dividend_uses_registration_holdings_and_counts_booked_cash_once(posted, expected, estimated):
    key = ('sh', '510300')
    days = ['2026-01-15', '2026-01-16', '2026-01-19', '2026-01-20', '2026-01-21']
    prices = {key: dict.fromkeys(days, Decimal('4'))}
    events = [
        {'id': 1, 'security': key, 'asset_type': 'etf', 'event_date': days[2], 'event_type': 'sell',
         'quantity': -50, 'gross_amount': '200', 'fees': '0', 'net_cash': None},
    ]
    if posted:
        events.extend([
            {'id': 2, 'security': key, 'asset_type': 'etf', 'event_date': days[1],
             'event_type': 'settlement_adjustment', 'quantity': 0, 'gross_amount': '11', 'net_cash': None},
            {'id': 3, 'security': key, 'asset_type': 'etf', 'event_date': days[3],
             'event_type': 'dividend', 'quantity': 0, 'gross_amount': '11', 'net_cash': None},
        ])
    dividends = [{'security': key, 'registration_date': days[1], 'ex_date': days[2],
                  'payment_date': days[4], 'cash_per_unit': Decimal('0.10')}]
    result = reconstruct(days, {key: 50}, reconcile_etf_dividends(events, dividends), prices, dividends)
    assert [row['pnl'] for row in result['records']] == ['0.00', expected, '0.00', '0.00']
    assert result['records'][1]['estimated_dividend'] == estimated


def test_snapshot_reconciliation_missing_cash_and_revision(isolated, tmp_path):
    store = isolated
    preview = store.ingest(SNAPSHOT, 'positions.xls')
    assert preview['reconciliation'] == {'market_difference': '0', 'assets_less_market_and_available': '0',
                                         'assets_less_market_and_balance': '200'}
    assert not store.source_root.exists()
    assert store.snapshot()['snapshot'] is None
    with pytest.raises(ValueError):
        store.ingest(SNAPSHOT, 'positions.xls', confirmation=preview['preview'])
    original = import_snapshot(store)['snapshot_id']
    data = store.snapshot()
    assert data['snapshot']['id'] == original
    assert data['totals']['market_value'] == '1500'
    assert data['totals']['weight_basis'] == 'securities'
    assert [row['weight'] for row in data['positions']] == ['66.67', '33.33', None]
    assert data['positions'][0]['code'] == '000001'
    assert data['positions'][-1]['exchange_rate'] == '0.91'
    assert import_snapshot(store)['already_imported']
    with pytest.raises(ImportConflict):
        import_snapshot(store, cash='900')
    changed = import_snapshot(store, cash='900', revise=True)['snapshot_id']
    assert changed != original
    assert store.snapshot(original)['totals']['cash'] is None
    assert store.snapshot()['totals']['assets'] == '2400'
    assert store.snapshot()['totals']['cash_weight'] == '37.50'
    assert len(list(store.source_root.glob('*.xls'))) == 1

    with store.db.connection() as conn:
        times = table_time(conn, 'portfolio_snapshot_positions', {'snapshot_id', 'source_row'})
    assert times[0] and times[1].startswith('portfolio_snapshots.imported_at')
    assert DESCRIPTIONS['portfolio_ledger_entries'][0] == '持仓分析'
    backup = store.db.backup(tmp_path / 'backup.sqlite3')
    restored = Database(restore_backup(backup, tmp_path / 'restored.sqlite3'))
    assert Portfolio(restored, store.source_root, store.reference_reader).snapshot()['snapshot']['id'] == changed


def test_history_first_and_cross_file_overlap_keep_real_split(isolated):
    store = isolated
    preview = store.ingest(HISTORY, 'history.xls')
    assert preview['entry_count'] == 2
    assert store.history()['total_count'] == 0
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: store.ingest(HISTORY, 'history.xls', confirmation=preview['preview']), range(2)))
    assert sum(result.get('already_imported', False) for result in results) == 1
    before = store.history()
    assert before['total_count'] == 2
    assert before['entries'][0]['net_cash'] is None
    assert Counter(entry['event_type'] for entry in before['entries']) == {'sell': 1, 'settlement_cancellation': 1}
    assert next(row for row in before['entries'] if row['event_type'] == 'sell')['quantity'] == -100
    candidate = store.ingest(OVERLAP, 'more.xls')
    assert candidate['overlap_count'] == 2
    with pytest.raises(ImportConflict):
        store.ingest(OVERLAP, 'more.xls', confirmation=candidate['preview'])
    saved = store.ingest(OVERLAP, 'more.xls', confirmation=candidate['preview'], overlap='mark')
    assert saved['duplicate_count'] == 2 and store.history()['total_count'] == 3
    result = import_snapshot(store)
    assert store.snapshot(result['snapshot_id'])['positions'][0]['quantity'] == 100
    assert store.history('2025-11-22')['total_count'] == 0


def test_huatai_transcription_and_same_day_account_rollup(isolated):
    store = isolated
    first = import_snapshot(store)['snapshot_id']
    image = b'\xff\xd8\xff fixture'
    draft = {
        'account': '华泰证券', 'currency': 'CNY', 'snapshot_date': DAY, 'snapshot_session': 'close',
        'source': {'type': 'screenshot_manual_transcription', 'sha256': sha256(image).hexdigest()},
        'broker_summary': {'total_assets': '1520', 'securities_market_value': '500', 'holding_pnl': '10'},
        'cash': {'opening_balance': '1020', 'opening_balance_basis': 'user_confirmed_assets_less_securities_value'},
        'positions': [{
            'code': '000001', 'exchange': 'sz', 'name': '示例银行', 'quantity': 50, 'available_quantity': 50,
            'displayed_price': '10', 'displayed_cost_price': '9.8', 'cost_amount': None,
            'market_value': '500', 'broker_holding_pnl': '10',
        }],
    }
    with pytest.raises(ValueError, match='来源不符'):
        store.ingest(image + b'x', 'screen.jpg', draft=draft)
    preview = store.ingest(image, 'screen.jpg', draft=draft)
    saved = store.ingest(image, 'screen.jpg', draft=draft, confirmation=preview['preview'])['snapshot_id']
    assert store.ingest(image, 'screen.jpg', draft=draft, confirmation=preview['preview'])['already_imported']
    assert store.snapshot(saved)['totals']['cash'] == '1020'
    assert store.snapshot(first)['totals']['cash'] is None
    combined = store.snapshot()
    assert combined['scope'] == 'combined'
    assert combined['totals']['weight_basis'] == 'securities'
    row = next(row for row in combined['positions'] if row['code'] == '000001')
    assert (row['quantity'], row['market_value'], len(row['accounts'])) == (150, '1500', 2)
    assert row['cost_price'] is None
    assert store.history()['total_count'] == 0

    next_day = '2025-11-25'
    preview = store.ingest(SNAPSHOT, 'positions.xls', day=next_day, session='close')
    store.ingest(SNAPSHOT, 'positions.xls', day=next_day, session='close', confirmation=preview['preview'])
    assert store.snapshot()['missing_accounts'] == ['华泰证券']
    assert store.snapshot(day=DAY)['missing_accounts'] == []


def test_bank_transfers_ignore_balance_checks_and_separate_cash_from_trade_history(isolated):
    rows = [
        HEADERS,
        ('2025-11-21', '09:00:00', '交易成功', '转入', '100.00', '001', '成功'),
        ('2025-11-21', '09:01:00', '交易成功', '转出', '20.00', '002', '成功'),
        ('2025-11-21', '09:02:00', '交易作废', '转出', '30.00', '003', '作废'),
        ('2025-11-21', '09:03:00', '查询成功', '查银行余额', '90.00', '004', '成功'),
    ]
    plain = '\r\n'.join('\t'.join(row) for row in rows).encode('gb18030')
    wrapped = ('\r\n' + '\r\n'.join('\t'.join(f'="{cell}"' for cell in row) + '\t' for row in rows)).encode('gb18030')
    import_snapshot(isolated, cash='900')
    preview = isolated.ingest(plain, 'bank.xls')
    assert preview['kind'] == 'transfers' and preview['ignored_count'] == 1
    assert all('execution_reference' not in entry for entry in preview['entries'])
    assert isolated.history(transfers=True)['total_count'] == 0
    result = isolated.ingest(plain, 'bank.xls', confirmation=preview['preview'])
    assert result['saved_count'] == 3 and result['ignored_count'] == 1
    assert isolated.ingest(plain, 'bank.xls', confirmation=preview['preview'])['already_imported']
    assert isolated.history()['total_count'] == 0
    records = isolated.history(transfers=True)['entries']
    assert [(r['event_type'], r['net_cash']) for r in records] == [
        ('bank_transfer_unposted', None), ('bank_transfer_out', '-20'), ('bank_transfer_in', '100')
    ]
    wrapped_preview = isolated.ingest(wrapped, 'other-bank.xls')
    assert wrapped_preview['overlap_count'] == 3
    result = isolated.ingest(wrapped, 'other-bank.xls', confirmation=wrapped_preview['preview'], overlap='mark')
    assert result['duplicate_count'] == 3 and isolated.history(transfers=True)['total_count'] == 3
    assert isolated.snapshot()['totals']['cash'] == '900'


def test_portfolio_http_gate_and_import(isolated, monkeypatch):
    monkeypatch.setattr(application, 'portfolio_service', lambda: isolated)
    http = None
    for port in (8766, 8768, *range(18860, 18870)):
        try:
            http = ThreadingHTTPServer(('127.0.0.1', port), Handler)
            break
        except OSError:
            continue
    assert http is not None
    worker = Thread(target=http.serve_forever, daemon=True)
    worker.start()
    base = f'http://127.0.0.1:{http.server_port}'
    with requests.Session() as session:
        session.trust_env = False
        try:
            page = session.get(base + '/portfolio', timeout=5)
            assert page.ok and '<title>持仓分析</title>' in page.text
            response = session.get(base + '/api/portfolio/snapshots', timeout=5)
            assert response.headers['Cache-Control'] == 'no-store'
            assert response.json()['snapshot'] is None
            token = response.json()['session_token']
            assert session.get(base + '/api/portfolio/history', headers={'Host': 'invalid.test'}, timeout=5).status_code == 400
            body = {'file': base64.b64encode(HISTORY).decode(), 'name': 'history.xls'}
            path = base + '/api/portfolio/import/'
            assert session.post(path + 'preview', json=body, timeout=5).status_code == 403
            headers = {'Origin': base, 'X-Local-Session': token}
            preview = session.post(path + 'preview', json=body, headers=headers, timeout=5).json()
            assert preview['entry_count'] == 2
            assert isolated.history()['total_count'] == 0
            posted = session.post(path + 'confirm', json=dict(body, preview=preview['preview']), headers=headers, timeout=5)
            assert posted.ok and posted.json()['saved_count'] == 2
            assert session.get(base + '/api/portfolio/transfers', timeout=5).json()['total_count'] == 0
            report = session.get(base + '/api/portfolio/performance', timeout=5)
            assert report.ok and report.json()['error'] == '缺少招商收盘持仓快照'
            http.request_gate = SimpleNamespace(enter=lambda: False)
            assert session.get(base + '/api/portfolio/history', timeout=5).status_code == 503
        finally:
            http.shutdown()
            http.server_close()
            worker.join(timeout=5)
