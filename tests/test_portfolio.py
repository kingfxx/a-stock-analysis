"""Isolated checks for portfolio imports, history and the private HTTP boundary."""

import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from threading import Thread
from time import perf_counter
from types import SimpleNamespace
from uuid import uuid4

import pytest
import requests

from stock_analysis import application
from stock_analysis.domains.portfolio import cmb_xls, hk_performance, performance_read, revaluation
from stock_analysis.domains.portfolio.activity_read import activity_dates
from stock_analysis.domains.portfolio.calculations import daily_change, fill_snapshot_daily_pnl
from stock_analysis.domains.portfolio.cash_history import account_return_with_hk, attach_cash, reconstruct_cash
from stock_analysis.domains.portfolio.cmb_transfers import HEADERS
from stock_analysis.domains.portfolio.performance import (
    attach_monthly_security_pnl, combine_market_performance, merge_performance, reconstruct,
    sum_domestic_performance,
)
from stock_analysis.domains.portfolio.performance_events import reconcile_etf_dividends
from stock_analysis.domains.portfolio.service import ImportConflict, Portfolio
from stock_analysis.http.server import Handler, ThreadingHTTPServer
from stock_analysis.infrastructure import storage
from stock_analysis.domains.valuation import hk_valuation
from stock_analysis.infrastructure.storage import Database, SyncKey, SyncResult, restore_backup
from stock_analysis.maintenance.maintenance import DESCRIPTIONS, MaintenanceService, table_time


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


def test_confirmed_hk_snapshot_and_fixed_rate_history(isolated, monkeypatch):
    snapshot_id = import_snapshot(isolated)['snapshot_id']
    with isolated.db.connection(write=True) as conn:
        conn.execute("""
            UPDATE portfolio_snapshot_positions SET code='01919',quantity=1000,
                price='16.73',exchange_rate='0.8498',market_value='14217.15',
                cost_amount='12000',holding_pnl='2217.15'
            WHERE snapshot_id=? AND exchange='hk'
        """, (snapshot_id,))
        account = conn.execute('SELECT account_id FROM portfolio_snapshots WHERE id=?', (snapshot_id,)).fetchone()[0]
        conn.executemany("""
            INSERT INTO portfolio_ledger_entries(
                account_id,event_date,event_type,source_label,exchange,code,name,asset_type,
                quantity,gross_amount,source_sha256,source_name,source_row,fingerprint,imported_at
            ) VALUES (?,?,?,?,'hk','01919','港股样例','hong_kong',?,?,'fixture','fixture',?,'fixture',?)
        """, [(account, '2025-11-21', 'buy', '港股订单买申报', 1000, '12119.52', 1, DAY),
              (account, '2025-11-22', 'dividend', '股息入账', 0, '352', 2, DAY)])
    dates = ['2025-11-20', '2025-11-21', DAY, '2025-11-25']
    raw = dict(zip(dates, map(Decimal, ('14.90', '14.92', '16.73', '16.93'))))
    instrument = isolated.db.ensure_instrument('01919')
    valuation_days = ['2020-12-01', DAY, dates[-1]]
    for metric, values in [('pe', [12, 8, 9]), ('pb', [2, 1, 1.2])]:
        key = SyncKey(instrument, f'valuation:{metric}', hk_valuation.SOURCE)
        run = isolated.db.start_sync(key, parser_version=hk_valuation.VERSION, methodology_version=hk_valuation.VERSION)
        points = [dict(observed_on=day, value=value, sampling_version=hk_valuation.VERSION, raw_json={})
                  for day, value in zip(valuation_days, values)]
        isolated.db.complete_sync(run, SyncResult(len(points), valuation_days[0], dates[-1], dates[-1]),
                                  lambda conn: isolated.db.upsert_valuation_observations(conn, key, run, points))
    def closes(reader, keys, start, end):
        return {(market, code): {stamp: (quote, ()) for stamp, quote in raw.items() if start <= stamp <= end}
                for market, code in keys}
    monkeypatch.setattr(hk_performance, 'read_closes', closes)
    monkeypatch.setattr(revaluation, 'read_closes', closes)
    monkeypatch.setattr(revaluation, 'read_unadjusted_closes', lambda *_: raw)
    monkeypatch.setattr(revaluation, 'local_market_days', lambda *_: dates)
    payload = isolated.snapshot(snapshot_id)
    hk = next(row for row in payload['positions'] if row['exchange'] == 'hk')
    assert hk['included'] and hk['weight'] and payload['totals']['market_value'] == '15717.15'
    valuation = hk['reference']['valuation']
    assert (valuation['pe']['value'], valuation['pe']['years'], valuation['pe']['percentile']) == (8, 5, 0)
    assert valuation['pb']['value'] == 1 and 'dividend_yield' not in valuation

    report = isolated.performance()
    scope = report['hong_kong']
    assert report['combined']['hong_kong'] == scope
    assert scope['fee_gap_count'] == 1 and scope['fx_basis_date'] == DAY and scope['fixed_fx'] == '0.8498'
    assert [row['date'] for row in scope['records']] == ['2025-11-21', '2025-11-22', DAY]
    assert scope['records'][1]['pnl'] == scope['records'][1]['dividend_net'] == '352.00'
    assert scope['records'][-1]['cumulative'] == '2449.63'
    assert scope['monthly_security_pnl'] == {'2025-11': {'hk:01919': 244963}}

    value = revaluation.read_revaluation(isolated.db, payload, datetime.fromisoformat(dates[-1] + 'T16:30:00'))
    market = next(row for row in value['positions'] if row['asset_type'] == 'hong_kong')
    assert (market['close'], market['market_value'], market['daily_pnl']) == ('16.93', '14387.11', '169.96')
    saved = revaluation.save_revaluation(isolated.db, payload, value)
    current = isolated.revaluation_snapshot(saved['saved_id'])
    assert current['positions'][-1]['daily_pnl_basis'] == 'fixed_fx_estimate'
    assert current['positions'][-1]['reference']['valuation']['pe']['value'] == 9
    assert isolated.performance()['hong_kong']['records'][-1]['pnl'] == '169.96'

    legacy = {**value, 'positions': [row for row in value['positions'] if row['asset_type'] != 'hong_kong'],
              'totals': {'market_value': '2539.50'}}
    old = isolated.revaluation_snapshot(revaluation.save_revaluation(isolated.db, payload, legacy)['saved_id'])
    hk = next(row for row in old['positions'] if row['exchange'] == 'hk')
    assert not hk['included'] and hk['exclusion_reason']


def test_domestic_combination_sums_independently_rounded_pnl():
    dates = ['2025-11-20', '2025-11-21']
    stock, etf = ('sh', '601919'), ('sz', '159915')
    prices = {key: dict(zip(dates, map(Decimal, ('1.000', '1.006')))) for key in (stock, etf)}
    stocks = reconstruct(dates, {stock: 1}, [], prices)
    etfs = reconstruct(dates, {etf: 1}, [], prices)
    combined = sum_domestic_performance(stocks, etfs)['records'][0]
    assert stocks['records'][0]['pnl'] == etfs['records'][0]['pnl'] == '0.01'
    assert combined['pnl'] == combined['cumulative'] == '0.02'
    assert combined['security_pnl'] == {stock: 1, etf: 1}


def test_combined_markets_align_local_dates_and_keep_valuation_gaps():
    def daily(day, pnl, value, security, dividend='0.00'):
        return {'date': f'2025-11-{day}', 'pnl': pnl, 'market_value': value,
                'dividend_net': dividend, 'estimated_dividend': '0.00', 'trades': 0,
                'fee_gap_count': 0, 'issues': ['缺少当日估值'] if pnl is None else [],
                'estimated_closes': [],
                'security_pnl': {security: int(Decimal(pnl) * 100)} if pnl is not None else {}}
    a = ('sh', '601919')
    h = ('hk', '01919')
    domestic = {'start': '2025-11-20', 'as_of': '2025-11-26', 'blocked_dates': [],
                'records': [daily('20', '100.00', '1000.00', a),
                            daily('24', '-10.00', '990.00', a), daily('25', '25.00', '1015.00', a),
                            daily('26', '12.00', '1027.00', a)]}
    hk = {'start': '2025-11-21', 'as_of': '2025-11-25', 'blocked_dates': [],
          'fixed_fx': '0.8498', 'fx_basis_date': '2025-11-25',
          'records': [daily('21', '50.00', '500.00', h, '20.00'),
                      daily('25', None, None, h)]}
    mixed = combine_market_performance(domestic, hk)
    assert (mixed['start'], mixed['as_of'], mixed['fixed_fx']) == ('2025-11-20', '2025-11-25', '0.8498')
    first, hk_day, domestic_day, gap = mixed['records']
    assert first['market_value'] == '1000.00'
    assert (hk_day['pnl'], hk_day['dividend_net'], hk_day['market_value']) == ('50.00', '20.00', '1500.00')
    assert hk_day['carried_markets'] == [{'market': 'A 股及 ETF', 'valuation_date': '2025-11-20'}]
    assert (domestic_day['pnl'], domestic_day['market_value'], domestic_day['cumulative']) == (
        '-10.00', '1490.00', '140.00')
    assert gap['pnl'] is None and gap['market_value'] is None and gap['issues']
    attach_monthly_security_pnl(mixed)
    assert mixed['monthly_security_pnl'] == {'2025-11': {'sh:601919': 9000, 'hk:01919': 5000}}


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
    attach_monthly_security_pnl(result)
    assert result['monthly_security_pnl'] == {'2026-01': {'sh:600000': 21500}}
    assert all('security_pnl' not in row for row in result['records'])
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


def test_combined_performance_includes_history_before_later_account_starts():
    first, second, third = '2025-11-19', '2025-11-20', '2025-11-21'
    security = ('sh', '600000')

    def account(broker, days, qty, quotes):
        scope = reconstruct(days, {security: qty}, [], {security: quotes})
        return dict(broker=broker, start=days[1], as_of=days[-1], stocks=scope, transfers=[], **scope)

    left = account('甲', ['2025-11-18', first, second, third], 100,
                   {'2025-11-18': Decimal(8), first: Decimal(9), second: Decimal(10), third: Decimal(11)})
    right = account('乙', [first, second, third], 300,
                    {first: Decimal(20), second: Decimal(19), third: Decimal(20)})
    attach_cash(left['records'], {first: '10.00', second: '20.00', third: '30.00'})
    attach_cash(right['records'], {second: '40.00', third: '50.00'})
    merged = merge_performance([left, right])
    assert (merged['start'], merged['as_of']) == (first, third)
    assert [row['date'] for row in merged['records']] == [first, second, third]
    early = merged['records'][0]
    assert early['pnl'] == '100.00' and early['return_base'] == '800.00' and not early['issues']
    assert early['cash_balance'] == '10.00' and early['total_assets'] == '910.00'
    row = merged['records'][1]
    assert row['pnl'] == '-200.00' and row['return_base'] == '6900.00'
    assert Decimal(row['return_rate']) == Decimal(-200) / Decimal(6900)
    assert row['cash_balance'] == '60.00' and row['total_assets'] == '6760.00'
    assert merged['records'][2]['cumulative'] == '300.00'
    attach_monthly_security_pnl(merged)
    assert merged['monthly_security_pnl'] == {'2025-11': {'sh:600000': 30000}}
    right['records'].pop(0)
    with_gap = merge_performance([left, right])
    incomplete = with_gap['records'][1]
    assert incomplete['pnl'] is None and incomplete['issues'] == ['乙：缺少当日复算结果']
    assert incomplete['cash_balance'] is None and incomplete['total_assets'] is None
    attach_monthly_security_pnl(with_gap)
    assert with_gap['monthly_security_pnl'] == {'2025-11': {'sh:600000': 50000}}


def test_security_pnl_reconciles_when_fractional_cents_round():
    first, second = ('sh', '600000'), ('sz', '159857')
    days = ['2026-01-30', '2026-02-02', '2026-02-03']
    prices = {key: dict(zip(days, map(Decimal, quotes))) for key, quotes in (
        (first, ('1.000', '1.406', '1.006')), (second, ('1.000', '0.996', '1.296')))}
    result = reconstruct(days, {first: 1, second: 1}, [], prices)
    attach_monthly_security_pnl(result)
    assert [row['pnl'] for row in result['records']] == ['0.40', '-0.10']
    assert result['monthly_security_pnl'] == {'2026-02': {'sz:159857': 30}}


def test_hk_account_return_counts_internal_cash_once_and_keeps_bank_flows_separate():
    opening, buy, dividend, next_day = '2025-11-21', '2025-11-24', '2025-11-25', '2025-11-26'
    def entry(day, event_type, asset, net=None, gross=None):
        return dict(event_date=day, event_type=event_type, asset_type=asset, source_label=event_type,
                    exchange='hk', code='01919', gross_amount=gross, net_cash=net, fees=None)
    ledger = [entry(buy, 'bank_transfer_in', 'unknown', net='13000'),
              entry(buy, 'buy', 'hong_kong', gross='12000'),
              entry(dividend, 'dividend', 'hong_kong', gross='100'),
              entry(next_day, 'dividend', 'hong_kong', gross='20')]
    domestic = dict(start=buy, opening_date=buy, opening_assumed_zero=True, return_gaps=[],
                    transfers=[{'date': buy, 'in': '13000', 'out': '0'},
                               {'date': next_day, 'in': '200', 'out': '0'}])
    mixed = dict(start=buy, as_of=next_day, records=[
        {'date': buy, 'market_value': '12000.00'}, {'date': dividend, 'market_value': '11900.00'},
        {'date': next_day, 'market_value': '12400.00'},
    ])
    first = dict(start=buy, opening_day=opening, anchor_day=dividend, as_of=next_day,
                 confirmed_cash='1100', ledger=ledger, hk_security=('hk', '01919'),
                 opening_market_value='0.00', opening_assumed_zero=True)
    second = dict(start=next_day, opening_day=dividend, anchor_day=next_day, as_of=next_day,
                  confirmed_cash='200', ledger=[entry(next_day, 'bank_transfer_in', 'unknown', net='200')],
                  hk_security=None, opening_market_value='0.00', opening_assumed_zero=True)
    result = account_return_with_hk(domestic, mixed, (first, second))
    assert result['opening_assets'] == '0.00'
    assert result['transfers'] == domestic['transfers']
    assert [(row['cash_balance'], row['total_assets']) for row in result['records']] == [
        ('1000.00', '13000.00'), ('1100.00', '13000.00'), ('1320.00', '13720.00'),
    ]


def test_cash_backfill_uses_actual_paid_dates_and_leaves_missing_balances_blank():
    friday, monday, tuesday = '2026-11-20', '2026-11-23', '2026-11-24'
    days = [friday, monday, tuesday]
    def entry(day, kind, asset='etf', net=None, gross=None, fees=None, label=''):
        return dict(event_date=day, event_type=kind, asset_type=asset, net_cash=net,
                    gross_amount=gross, fees=fees, source_label=label)
    flows = [
        entry('2026-11-22', 'bank_transfer_in', asset='cash', net='100'),
        entry(monday, 'buy', net='-75.00', gross='70.00', fees='5.00'),
        entry(monday, 'dividend', gross='4.00'),
        entry(tuesday, 'dividend_tax', gross='1.00'),
        entry(tuesday, 'dividend', asset='hong_kong', gross='100.00'),
        entry(monday, 'bank_transfer_unposted', asset='cash', gross='200.00'),
        entry(tuesday, 'settlement_adjustment', gross='4.00'),
    ]
    assert reconstruct_cash(days, '20', flows) == {friday: '-8.00', monday: '21.00', tuesday: '20.00'}
    flows.append(entry(monday, 'sell'))
    assert reconstruct_cash(days, '20', flows) == {friday: None, monday: '21.00', tuesday: '20.00'}
    assert reconstruct_cash(days, None, flows) == dict.fromkeys(days)


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


def test_revaluation_uses_same_day_raw_prices_and_keeps_snapshot(isolated, monkeypatch, tmp_path):
    snapshot_id = import_snapshot(isolated)['snapshot_id']
    before = isolated.snapshot(snapshot_id)
    prior, following = '2025-11-21', '2025-11-25'
    stock = {prior: Decimal('9'), DAY: Decimal('10'), following: Decimal('11')}
    etf = {prior: 2.3, DAY: 2.5}
    monkeypatch.setattr(revaluation, 'read_closes', lambda reader, keys, start, end: {
        ('sz', '000001'): {day: (value, None) for day, value in stock.items() if day <= end}})
    monkeypatch.setattr(revaluation, 'read_unadjusted_closes', lambda conn, exchange, code, start, end: etf)
    monkeypatch.setattr(revaluation, 'local_market_days', lambda reader, start, end: [
        day for day in (prior, DAY, following) if day <= end])
    intraday = revaluation.read_revaluation(isolated.db, before, datetime.fromisoformat(following + 'T14:30:00'))
    assert intraday['quote_date'] == DAY
    assert intraday['totals'] == dict(market_value='1500.00', holding_pnl='300.00', daily_pnl='140.00')
    after_close = revaluation.read_revaluation(isolated.db, before, datetime.fromisoformat(following + 'T16:30:00'))
    assert after_close == intraday
    assert isolated.snapshot(snapshot_id) == before
    etf[following] = 2.6
    # The index calendar may publish later than the stock and ETF closes.
    monkeypatch.setattr(revaluation, 'local_market_days', lambda *_: [prior, DAY])
    estimate = revaluation.read_revaluation(isolated.db, before, datetime.fromisoformat(following + 'T16:30:00'))
    assert estimate['totals']['daily_pnl'] == '120.00'
    started = perf_counter()
    with ThreadPoolExecutor(max_workers=2) as pool:
        saved, retried = pool.map(lambda _: revaluation.save_revaluation(isolated.db, before, estimate), range(2))
    write_ms = (perf_counter() - started) * 1000
    assert retried == saved
    estimate_id = saved['saved_id']
    started = perf_counter()
    assert isolated.saved_revaluation(estimate_id) == saved
    read_ms = (perf_counter() - started) * 1000
    data = isolated.snapshot(snapshot_id)
    assert data['snapshot'] == before['snapshot'] and data['positions'] == before['positions']
    assert data['revaluations'] == [dict(id=estimate_id, quote_date=following)]
    display = isolated.revaluation_snapshot(estimate_id)
    assert (display['snapshot']['snapshot_date'], display['basis_date']) == (following, DAY)
    assert display['estimate_id'] == estimate_id
    assert display['totals']['market_value'] == '1620.00'
    assert display['totals']['daily_pnl'] == '120.00'
    assert display['positions'][0]['price'] == '11' and display['positions'][0]['cost_price'] == '8'
    assert display['positions'][1]['reference']['close_date'] == following
    stats = next(row for row in MaintenanceService(isolated.db).read()['tables']
                 if row['name'] == 'portfolio_revaluation_snapshots')
    assert stats['category'] == '持仓分析' and stats['rows'] == 1
    assert stats['time_basis'] == 'written_at' and stats['updated_at']
    print(f"revaluation rows={stats['rows']}, total_bytes={stats['total_bytes']}, concurrent writes={write_ms:.1f}ms, read={read_ms:.1f}ms")
    backup = isolated.db.backup(tmp_path / 'revaluation-backup.sqlite3')
    restored = Database(restore_backup(backup, tmp_path / 'revaluation-restored.sqlite3'))
    assert revaluation.saved_revaluation(restored, estimate_id) == saved

    legacy = dict(estimate, positions=[dict(row, daily_pnl=None) for row in estimate['positions']],
                  totals=dict(estimate['totals'], daily_pnl=None))
    legacy_id = revaluation.save_revaluation(isolated.db, before, legacy)['saved_id']
    filled = isolated.revaluation_snapshot(legacy_id)
    assert {row['code']: row['daily_pnl'] for row in filled['positions'] if row['daily_pnl'] is not None} == {
        '000001': '100.00', '159915': '20.00',
    }
    assert filled['totals']['daily_pnl'] == '120.00'
    assert isolated.saved_revaluation(legacy_id)['totals']['daily_pnl'] is None
    etf.pop(DAY)
    assert isolated.revaluation_snapshot(legacy_id)['totals']['daily_pnl'] is None


def test_revaluation_saves_updated_prior_close_as_new_version(isolated, monkeypatch):
    payload = isolated.snapshot(import_snapshot(isolated)['snapshot_id'])
    following = '2025-11-25'
    now = datetime.fromisoformat(following + 'T16:30:00')
    etf = {DAY: '2.5'}
    monkeypatch.setattr(revaluation, 'read_closes', lambda *_: {
        ('sz', '000001'): {DAY: (Decimal('10'), None), following: (Decimal('11'), None)}})
    monkeypatch.setattr(revaluation, 'read_unadjusted_closes', lambda *_: etf)
    monkeypatch.setattr(revaluation, 'local_market_days', lambda *_: [DAY, following])
    first = revaluation.read_revaluation(isolated.db, payload, now)
    assert first['quote_date'] == DAY and first['totals']['market_value'] == '1500.00'
    version = revaluation.save_revaluation(isolated.db, payload, first)
    assert revaluation.save_revaluation(isolated.db, payload, first) == version
    etf[DAY] = '2.55'
    corrected = revaluation.read_revaluation(isolated.db, payload, now)
    revised = revaluation.save_revaluation(isolated.db, payload, corrected)
    assert revised['saved_id'] > version['saved_id'] and revised['quote_date'] == DAY
    assert revised['totals']['market_value'] == '1510.00'
    assert isolated.saved_revaluation(version['saved_id']) == version
    etf.clear()
    incomplete = revaluation.read_revaluation(isolated.db, payload, now)
    assert incomplete['positions'][1]['close'] is None
    assert 'saved_id' not in revaluation.save_revaluation(isolated.db, payload, incomplete)


def test_performance_extends_saved_close_using_post_snapshot_ledger(isolated, monkeypatch):
    import_snapshot(isolated, cash='900')
    prior, following = '2025-11-21', '2025-11-25'
    stock = {prior: Decimal('9'), DAY: Decimal('10'), following: Decimal('11')}
    etf = {prior: Decimal('2.3'), DAY: Decimal('2.5'), following: Decimal('2.6')}

    def stock_closes(reader, keys, start, end, **kwargs):
        return {('sz', '000001'): {day: (price, None) for day, price in stock.items() if day <= end}}

    def etf_closes(conn, exchange, code, start, end, **kwargs):
        return {day: price for day, price in etf.items() if day <= end}

    for module in (performance_read, revaluation):
        monkeypatch.setattr(module, 'local_market_days', lambda *_: [prior, DAY])
        monkeypatch.setattr(module, 'read_closes', stock_closes)
        monkeypatch.setattr(module, 'read_unadjusted_closes', etf_closes)

    for day in (DAY, following):
        isolated.save_manual('transfers', dict(broker='招商', business='银行转证券', event_date=day,
                                              gross_amount='200', request_id=str(uuid4())))
    isolated.save_manual('history', dict(broker='招商', business='买入', event_date=following,
                                        code='000001', quantity='10', price='10.5', gross_amount='105',
                                        fees='1', request_id=str(uuid4())))

    official = isolated.performance()
    assert official['as_of'] == DAY
    payload = isolated.snapshot()
    estimate = revaluation.read_revaluation(isolated.db, payload, datetime.fromisoformat(following + 'T16:30:00'))
    saved_id = revaluation.save_revaluation(isolated.db, payload, estimate)['saved_id']
    updated = isolated.performance()
    assert updated['as_of'] == updated['combined']['as_of'] == following
    assert updated['local_close_from'] == updated['combined']['local_close_from'] == following
    assert updated['records'][:-1] == official['records']
    last = updated['records'][-1]
    assert (last['pnl'], last['market_value'], last['cash_balance'], last['total_assets']) == (
        '124.00', '1730.00', '994.00', '2724.00')
    assert updated['stocks']['records'][-1]['pnl'] == '104.00'
    assert updated['etfs']['records'][-1]['pnl'] == '20.00'
    assert updated['etfs']['records'][-1]['market_value'] == '520.00'
    assert updated['combined']['etfs']['records'][-1]['pnl'] == '20.00'
    assert set(updated['etfs']['monthly_security_pnl']['2025-11']) == {'sz:159915'}
    revaluation.delete_revaluation(isolated.db, saved_id)
    assert isolated.performance()['combined']['as_of'] == DAY


def test_revaluation_schema_upgrades_from_54(tmp_path, monkeypatch):
    db = Database(tmp_path / 'upgrade.sqlite3')
    migrations = storage.MIGRATIONS
    monkeypatch.setattr(storage, 'MIGRATIONS', tuple(row for row in migrations if row[0] <= 54))
    assert db.initialize()['schema_version'] == 54
    monkeypatch.setattr(storage, 'MIGRATIONS', tuple(row for row in migrations if row[0] <= 55))
    assert db.initialize()['schema_version'] == 55
    with db.connection() as conn:
        assert revaluation.revaluation_directory(conn, None) == []
        assert table_time(conn, 'portfolio_revaluation_snapshots', {'written_at'}) == (None, 'written_at')


def test_revaluation_upgrade_retains_versions_and_never_reuses_deleted_id(tmp_path, monkeypatch):
    db = Database(tmp_path / 'upgrade.sqlite3')
    with monkeypatch.context() as patch:
        patch.setattr(storage, 'MIGRATIONS', storage.MIGRATIONS[:-1])
        assert db.initialize()['schema_version'] == 59
        with db.connection(write=True) as conn:
            conn.execute("""
                INSERT INTO portfolio_revaluation_snapshots VALUES (42,NULL,?,'[]','fixture','{}',?)
            """, (DAY, DAY))
    assert db.initialize()['schema_version'] == 60
    assert revaluation.saved_revaluation(db, 42) == {'saved_id': 42}
    revaluation.delete_revaluation(db, 42)
    result = revaluation.save_revaluation(db, {'snapshot': {'id': 1}}, {
        'quote_date': DAY, 'today': DAY, 'totals': {'market_value': '1.00'},
    })
    assert result['saved_id'] > 42


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


def test_manual_entries_share_ledger_types_and_source_archive(isolated):
    import_snapshot(isolated)
    base = dict(broker='招商', event_date=DAY, code='000001')
    cash_cases = [('现金分红', 'dividend', '股息入账', '10.25'),
                  ('红利税补缴', 'dividend_tax', '股息红利税补缴', '-10.25')]
    for label, event, display, net in cash_cases:
        body = dict(base, business=label, gross_amount='10.25', request_id=str(uuid4()))
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.map(lambda _: isolated.save_manual('history', body), range(2))
        assert first['id'] == second['id'] and sum(row.get('already_saved', False) for row in (first, second)) == 1
        row = next(row for row in isolated.history()['entries'] if row['id'] == first['id'])
        assert (row['event_type'], row['source_label'], row['net_cash']) == (event, display, net)
        assert row['fees'] is row['event_time'] is row['quantity'] is row['price'] is None
        assert row['name'] == '示例银行'
    red = dict(base, business='红股入账', quantity='10', request_id=str(uuid4()))
    isolated.save_manual('history', red)
    ipo = dict(base, business='申购配号', code='787428', execution_reference='00001', request_id=str(uuid4()))
    isolated.save_manual('history', ipo)
    take = dict(broker='新券商', new_broker='新券商', event_date=DAY, business='证券转银行',
                gross_amount='100.50', request_id=str(uuid4()), execution_reference='00042')
    with pytest.raises(ValueError):
        isolated.save_manual('transfers', dict(take, new_broker=''))
    isolated.save_manual('transfers', take)
    assert isolated.history()['total_count'] == 4
    transfer = isolated.history(transfers=True)['entries'][0]
    assert (transfer['broker'], transfer['source_label'], transfer['gross_amount'], transfer['net_cash']) == (
        '新券商', '转出 · 成功', '100.5', '-100.5')
    assert {'新券商', '招商证券'} == {row['broker'] for row in isolated.manual_options()['accounts']}
    new_account = next(row['id'] for row in isolated.manual_options()['accounts'] if row['broker'] == '新券商')
    assert not isolated.manual_options(account_id=new_account)['securities']
    assert isolated.manual_options('159915', account_id=new_account)['securities'][0]['asset_type'] == 'etf'
    assert isolated.performance()['combined']['missing_accounts'] == ['新券商']
    with isolated.db.connection() as conn:
        for row in conn.execute("SELECT source_sha256 FROM portfolio_ledger_entries WHERE source_name LIKE 'manual/%'"):
            raw = (isolated.source_root / (row[0] + '.json')).read_bytes()
            assert sha256(raw).hexdigest() == row[0]


def test_manual_trade_checks_both_import_orders_and_preserves_real_splits(isolated):
    import_snapshot(isolated)
    body = dict(broker='招商证券', event_date='2025-11-21', business='证券卖出', code='000001',
                quantity='100', price='10', fees='0', event_time='09:40:30', request_id=str(uuid4()))
    first = isolated.save_manual('history', body)
    assert isolated.history()['entries'][0]['fees'] == '0'
    with pytest.raises(ValueError):
        isolated.save_manual('history', dict(body, price='11'))
    preview = isolated.ingest(HISTORY, 'history.xls')
    assert preview['overlap_count'] == 1
    result = isolated.ingest(HISTORY, 'history.xls', confirmation=preview['preview'], overlap='mark')
    assert result['duplicate_count'] == 1
    assert isolated.history()['total_count'] == 2
    again = dict(body, request_id=str(uuid4()), fees='')
    candidate = isolated.save_manual('history', again)
    assert len(candidate['candidates']) == 1 and isolated.history()['total_count'] == 2
    second = isolated.save_manual('history', dict(again, confirmation=candidate['confirmation']))
    assert second['id'] != first['id']
    assert [row['fees'] for row in isolated.history()['entries'] if row['event_type'] == 'sell'] == [None, '0']
    assert isolated.save_manual('history', dict(again, confirmation=candidate['confirmation']))['already_saved']


def test_ledger_filters_and_numeric_sort_precede_pagination(isolated):
    import_snapshot(isolated)
    with isolated.db.connection(write=True) as conn:
        account = conn.execute('SELECT id FROM portfolio_accounts').fetchone()[0]
        conn.executemany("""
            INSERT INTO portfolio_ledger_entries(
                account_id,event_date,event_type,source_label,code,name,asset_type,quantity,price,gross_amount,
                source_sha256,source_name,source_row,fingerprint,imported_at
            ) VALUES (?, '2025-11-24', 'buy', '买入', '000001', '示例银行', 'stock', 1, '1', ?,
                      'fixture', 'fixture', ?, 'fixture', '2025-11-24')
        """, [(account, str(amount), amount) for amount in range(1, 116)])
    picked = isolated.history(broker='招商证券', business='买入', security='示例', amount='9.00')
    assert picked['total_count'] == 1 and picked['entries'][0]['gross_amount'] == '9'
    assert picked['facets'] == {'broker': ['招商证券'], 'business': ['买入']}
    first = isolated.history(sort='amount', direction='asc')
    later = isolated.history(sort='amount', direction='asc', offset=100)
    assert [int(row['gross_amount']) for row in first['entries'] + later['entries']] == list(range(1, 116))
    assert later['total_count'] == 115
    assert isolated.history(sort='amount', direction='desc')['entries'][0]['gross_amount'] == '115'
    assert isolated.history('2025-11-25')['total_count'] == 0
    with pytest.raises(ValueError):
        isolated.history(sort='id DESC')
    with pytest.raises(ValueError):
        isolated.history(transfers=True, security='000001')


@pytest.mark.parametrize(('period','today','first'), [
    ('month', '2026-11-26', '2026-11-01'),
    ('quarter', '2026-11-26', '2026-10-01'),
    ('quarter', '2026-01-05', '2026-01-01'),
    ('year', '2026-11-26', '2026-01-01'),
    ('all', '2026-11-26', None),
])
def test_activity_date_presets(period, today, first):
    assert activity_dates(period, today) == (first, today)


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

    prices = {('sz', '000001', 'stock'): {DAY: Decimal('10'), next_day: Decimal('11')},
              ('sz', '159915', 'etf'): {DAY: Decimal('2.5'), next_day: Decimal('2.6')}}
    rows = revaluation.revalue_positions(
        [row for row in combined['positions'] if row['asset_type'] in {'stock', 'etf'}], prices, next_day, DAY)
    result = dict(quote_date=next_day, today=next_day, snapshot_date=DAY, positions=rows,
                  totals={'market_value': '2170.00'})
    estimate_id = revaluation.save_revaluation(store.db, combined, result)['saved_id']
    display = store.revaluation_snapshot(estimate_id)
    row = next(row for row in display['positions'] if row['code'] == '000001')
    assert display['scope'] == 'combined' and display['totals']['market_value'] == '2170.00'
    assert row['market_value'] == '1650.00'
    assert sorted(part['market_value'] for part in row['accounts']) == ['1100.00', '550.00']


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
            revalue = base + '/api/portfolio/revaluation'
            assert session.get(revalue, headers={'Host': 'invalid.test'}, timeout=5).status_code == 400
            assert session.get(revalue, timeout=5).status_code == 404
            body = {'file': base64.b64encode(HISTORY).decode(), 'name': 'history.xls'}
            path = base + '/api/portfolio/import/'
            assert session.post(path + 'preview', json=body, timeout=5).status_code == 403
            headers = {'Origin': base, 'X-Local-Session': token}
            preview = session.post(path + 'preview', json=body, headers=headers, timeout=5).json()
            assert preview['entry_count'] == 2
            assert isolated.history()['total_count'] == 0
            posted = session.post(path + 'confirm', json=dict(body, preview=preview['preview']), headers=headers, timeout=5)
            assert posted.ok and posted.json()['saved_count'] == 2
            with isolated.db.connection(write=True) as conn:
                account_id = conn.execute(
                    'INSERT INTO portfolio_accounts(broker,created_at) VALUES (?,?)', ('华泰证券', DAY)
                ).lastrowid
                conn.executemany("""
                    INSERT INTO portfolio_ledger_entries(
                        account_id,event_date,event_type,source_label,code,name,asset_type,
                        source_sha256,source_name,source_row,fingerprint,imported_at,net_cash
                    ) VALUES (?, ?, ?, ?, '', '', 'unknown', 'fixture', 'fixture', ?, 'fixture', ?, ?)
                """, [(account_id, DAY, 'buy', '买入', 1, DAY, None),
                      (account_id, DAY, 'bank_transfer_in', '银行转证券', 2, DAY, '100')])
            history_url = base + '/api/portfolio/history'
            history = session.get(history_url, timeout=5).json()
            assert history['total_count'] == 3
            assert [row['broker'] for row in history['entries']] == ['华泰证券', '招商证券', '招商证券']
            filtered = session.get(history_url, params={'start': DAY, 'end': DAY}, timeout=5).json()
            assert filtered['total_count'] == 1 and filtered['entries'][0]['broker'] == '华泰证券'
            later = session.get(history_url, params={'offset': 1}, timeout=5).json()
            assert later['total_count'] == 3 and len(later['entries']) == 2
            scoped = session.get(history_url, params={'broker':'华泰证券', 'sort':'date','direction':'asc'}, timeout=5)
            assert scoped.ok and scoped.json()['entries'][0]['broker'] == '华泰证券'
            assert session.get(history_url, params={'sort':'bad'}, timeout=5).status_code == 400
            transfers = session.get(base + '/api/portfolio/transfers', timeout=5).json()
            assert transfers['total_count'] == 1
            assert [(row['broker'], row['net_cash']) for row in transfers['entries']] == [('华泰证券', '100')]
            report = session.get(base + '/api/portfolio/performance', timeout=5)
            assert report.ok and report.json()['error'] == '缺少招商收盘持仓快照'
            huatai = next(account for account in report.json()['accounts'] if account['broker'] == '华泰证券')
            assert huatai == dict(broker='华泰证券', trade_count=1, transfer_count=1,
                                  first_trade=DAY, last_trade=DAY, snapshot_date=None)
            snapshot_id = import_snapshot(isolated)['snapshot_id']
            estimate = session.get(revalue, params={'snapshot': snapshot_id}, timeout=5)
            assert estimate.ok and estimate.json()['snapshot_date'] == DAY
            assert 'session_token' not in estimate.json()
            options = session.get(base + '/api/portfolio/manual-options', params={'q': '000001'}, timeout=5)
            assert options.ok and options.json()['securities'][0]['code'] == '000001'
            manual_url = base + '/api/portfolio/manual/trade'
            manual = dict(broker='招商', business='现金分红', event_date=DAY, code='000001',
                          gross_amount='5', request_id=str(uuid4()))
            assert session.post(manual_url, json=manual, timeout=5).status_code == 403
            manual_result = session.post(manual_url, json=manual, headers=headers, timeout=5)
            assert manual_result.ok and manual_result.json()['id']
            selection = {'snapshot': str(snapshot_id)}
            assert session.post(revalue, json=selection, timeout=5).status_code == 403
            preview_only = session.post(revalue, json=selection, headers=headers, timeout=5)
            assert preview_only.ok and 'saved_id' not in preview_only.json()
            assert session.get(revalue, params={'estimate': 1}, timeout=5).status_code == 404
            snapshots = base + '/api/portfolio/snapshots'
            assert session.get(snapshots, params={'estimate': 1}, timeout=5).status_code == 404
            assert session.get(snapshots, params={'estimate': 1, 'snapshot': snapshot_id}, timeout=5).status_code == 400
            def raw_closes(reader, keys, start, end):
                return {('sz', '000001'): {DAY: (Decimal('10'), None), '2025-11-25': (Decimal('11'), None)}}

            class AfterClose(datetime):
                @classmethod
                def now(cls, tz):
                    return cls(2025, 11, 25, 16, tzinfo=tz)

            monkeypatch.setattr('stock_analysis.domains.portfolio.service.datetime', AfterClose)
            monkeypatch.setattr(revaluation, 'read_closes', raw_closes)
            monkeypatch.setattr(revaluation, 'read_unadjusted_closes', lambda *_: {DAY: '2.5', '2025-11-25': '2.6'})
            published = session.post(revalue, json=selection, headers=headers, timeout=5)
            assert published.ok
            selected = session.get(snapshots, params={'estimate': published.json()['saved_id']}, timeout=5)
            assert selected.ok and selected.json()['snapshot']['snapshot_date'] == '2025-11-25'
            assert selected.json()['totals']['market_value'] == '1620.00'
            delete = revalue + '/delete'
            identity = {'estimate': published.json()['saved_id']}
            assert session.get(delete, params=identity, timeout=5).status_code == 404
            assert session.post(delete, json=identity, timeout=5).status_code == 403
            assert session.post(delete, json={'snapshot': snapshot_id}, headers=headers, timeout=5).status_code == 400
            assert session.post(delete, json=identity, headers=headers, timeout=5).json() == {
                'deleted_id': identity['estimate'],
            }
            assert session.post(delete, json=identity, headers=headers, timeout=5).status_code == 404
            assert session.get(snapshots, params=identity, timeout=5).status_code == 404
            assert session.get(snapshots, params={'snapshot': snapshot_id}, timeout=5).json()['revaluations'] == []
            http.request_gate = SimpleNamespace(enter=lambda: False)
            assert session.get(base + '/api/portfolio/history', timeout=5).status_code == 503
        finally:
            http.shutdown()
            http.server_close()
            worker.join(timeout=5)
