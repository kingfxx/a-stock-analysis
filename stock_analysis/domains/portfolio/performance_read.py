"""Read local brokerage coverage and inputs for A-share and ETF performance."""

from collections import Counter
from datetime import date, timedelta
from decimal import Decimal

from stock_analysis.domains.etfs.storage import read_cash_dividends, read_unadjusted_closes
from stock_analysis.domains.portfolio.cash_history import TRANSFERS, account_return_with_hk, attach_cash, reconstruct_cash
from stock_analysis.domains.portfolio.performance import (
    advance_positions, attach_monthly_security_pnl, combine_market_performance, merge_performance, reconstruct,
    sum_domestic_performance,
)
from stock_analysis.domains.portfolio.performance_events import normalize_events, reconcile_etf_dividends
from stock_analysis.domains.portfolio.reference import local_market_days
from stock_analysis.domains.portfolio.history_prices import read_closes
from stock_analysis.domains.portfolio.hk_performance import SECURITY as HK_SECURITY, read_hk_performance


def _read_account_coverage(conn):
    return [dict(row) for row in conn.execute("""
        SELECT a.broker,
            (SELECT max(s.snapshot_date) FROM portfolio_snapshots s
             WHERE s.account_id=a.id AND s.session='close') AS snapshot_date,
            count(CASE WHEN l.event_type IN ('buy','sell') THEN 1 END) AS trade_count,
            min(CASE WHEN l.event_type IN ('buy','sell') THEN l.event_date END) AS first_trade,
            max(CASE WHEN l.event_type IN ('buy','sell') THEN l.event_date END) AS last_trade,
            count(CASE WHEN l.event_type IN ('bank_transfer_in','bank_transfer_out')
                       AND l.net_cash IS NOT NULL THEN 1 END) AS transfer_count
        FROM portfolio_accounts a
        LEFT JOIN portfolio_ledger_entries l ON l.account_id=a.id AND l.duplicate_of_id IS NULL
        GROUP BY a.id, a.broker ORDER BY a.id
    """)]


def _saved_close_days(conn, account, anchor_day):
    return [row[0] for row in conn.execute("""
        SELECT DISTINCT r.quote_date FROM portfolio_revaluation_snapshots r
        JOIN json_each(r.snapshot_ids) AS basis
        JOIN portfolio_snapshots s ON s.id=basis.value
        WHERE s.account_id=? AND r.quote_date>? ORDER BY r.quote_date
    """, (account, anchor_day))]


def _read_performance(reader, broker, security_names, cash_inputs, *, use_local_closes=True):
    base = {'broker': broker, 'records': []}
    broker_label = broker.removesuffix('证券')
    conn = reader.conn
    snapshot = conn.execute("""
        SELECT s.id,s.account_id,s.snapshot_date,s.confirmed_cash FROM portfolio_snapshots s
        JOIN portfolio_accounts a ON a.id=s.account_id
        WHERE a.broker=? AND s.session='close'
        ORDER BY s.snapshot_date DESC, s.id DESC LIMIT 1
    """, (broker,)).fetchone()
    if snapshot is None:
        return {**base, 'error': f'缺少{broker_label}收盘持仓快照'}
    anchor_day = snapshot['snapshot_date']
    account = snapshot['account_id']
    local_days = _saved_close_days(conn, account, anchor_day) if use_local_closes else []
    end = local_days[-1] if local_days else anchor_day
    if broker == '招商证券':
        hk = read_hk_performance(reader, snapshot, local_days, security_names)
        if hk is not None:
            base['hong_kong'] = hk
    start = conn.execute("""
        SELECT min(event_date) FROM portfolio_ledger_entries
        WHERE account_id=? AND event_date<=? AND duplicate_of_id IS NULL
        AND (event_type IN ('bank_transfer_in','bank_transfer_out')
             OR (asset_type IN ('stock','etf') AND event_type IN ('buy','sell','dividend','dividend_tax','stock_credit')))
    """, (account, anchor_day)).fetchone()[0]
    if start is None:
        return {**base, 'as_of': end, 'error': f'缺少{broker_label}历史成交或银证转账'}
    calendar_start = (date.fromisoformat(start) - timedelta(days=30)).isoformat()
    calendar = local_market_days(reader, calendar_start, end)
    prior = [day for day in calendar if day < start]
    if not prior or anchor_day not in calendar:
        return {**base, 'as_of': anchor_day, 'error': '期初或快照日缺少本地交易日历'}
    opening = prior[-1]
    dates = calendar[calendar.index(opening):]
    closing = Counter()
    etfs = set()
    snapshot_names = {}
    for row in conn.execute("""
        SELECT exchange,code,name,quantity,asset_type FROM portfolio_snapshot_positions
        WHERE snapshot_id=? AND asset_type IN ('stock','etf')
    """, (snapshot['id'],)):
        security = (row['exchange'], row['code'])
        closing[security] += row['quantity']
        if row['name']:
            snapshot_names[f'{row["exchange"]}:{row["code"]}'] = row['name']
        if row['asset_type'] == 'etf':
            etfs.add(security)
    ledger = [dict(row) for row in conn.execute("""
        SELECT id,event_date,event_type,source_label,exchange,code,name,asset_type,quantity,gross_amount,fees,net_cash
        FROM portfolio_ledger_entries WHERE account_id=? AND duplicate_of_id IS NULL
        AND event_date>? AND event_date<=? ORDER BY event_date,event_time,id
    """, (account, opening, end))]
    events = []
    for row in ledger:
        if (row['asset_type'] not in ('stock', 'etf')
                and (row['asset_type'] != 'unknown' or row['code'] not in ('760015', '787428', '732391'))):
            continue
        security = (row['exchange'], row['code'])
        events.append(dict(row, security=security))
        if row['asset_type'] == 'etf':
            etfs.add(security)
    if not closing and not events:
        return {**base, 'as_of': end, 'error': '这个区间没有可复算的 A 股或 ETF 持仓及成交'}
    stocks = (closing.keys() | {event['security'] for event in events}) - etfs
    closes = read_closes(reader, stocks, opening, end, include_prior=True)
    prices = {
        security: {day: close for day, (close, origin) in values.items()}
        for security, values in closes.items()
    }
    dividends = []
    for exchange, code in sorted(etfs):
        security = (exchange, code)
        prices[security] = {day: Decimal(str(close)) for day, close in
                            read_unadjusted_closes(conn, exchange, code, opening, end, include_prior=True).items()}
        dividends.extend(dict(row, security=security, cash_per_unit=Decimal(row['cash_per_unit']))
                         for row in read_cash_dividends(conn, exchange, code, opening, end))
    events = reconcile_etf_dividends(normalize_events(events, prices, opening), dividends)
    local_close_from = None
    if local_days:
        priced_days = {day for series in prices.values() for day in series if anchor_day < day <= end}
        dates = sorted(set(dates).union(local_days, priced_days))
        local_close_from = dates[dates.index(anchor_day) + 1]
        closing = advance_positions(closing, events, anchor_day)
        if closing is None:
            return _read_performance(reader, broker, security_names, cash_inputs, use_local_closes=False)
    for event in events:
        exchange, code = event['security']
        if event['name']:
            security_names[f'{exchange}:{code}'] = event['name']
    security_names.update(snapshot_names)
    transfers = {}
    missing_transfers = set()
    for row in ledger:
        if row['event_type'] not in TRANSFERS:
            continue
        if row['net_cash'] is None:
            missing_transfers.add(row['event_date'])
            continue
        amounts = transfers.setdefault(row['event_date'], {'in': Decimal(0), 'out': Decimal(0)})
        direction = 'in' if row['event_type'] == 'bank_transfer_in' else 'out'
        amounts[direction] += abs(Decimal(row['net_cash']))
    transfer_days = [{'date': day, **{key: str(value) for key, value in amounts.items()}}
                     for day, amounts in sorted(transfers.items())]
    stock_events = [event for event in events if event['security'] not in etfs]
    stock_result = reconstruct(dates, {key: quantity for key, quantity in closing.items() if key not in etfs},
                               stock_events, prices)
    etf_events = [event for event in events if event['security'] in etfs]
    etf_result = reconstruct(dates, {key: quantity for key, quantity in closing.items() if key in etfs},
                             etf_events, prices, dividends)
    result = sum_domestic_performance(stock_result, etf_result)
    if local_days and any(day > anchor_day for scope in (result, stock_result) for day in scope['blocked_dates']):
        return _read_performance(reader, broker, security_names, cash_inputs, use_local_closes=False)
    balances = reconstruct_cash(dates, snapshot['confirmed_cash'], ledger, anchor_day=anchor_day)
    for scope in (result, stock_result, etf_result):
        attach_cash(scope['records'], balances)
        scope['fee_gap_count'] = sum(row['fee_gap_count'] for row in scope['records'])
    first_market = result['records'][0]['opening_market_value'] if result['records'] else None
    first_cash = balances[opening]
    first_flow = transfers.get(start)
    assumed_zero = (first_market is not None and Decimal(first_market) == 0
                    and first_flow is not None and first_flow['in'] > first_flow['out'])
    opening_assets = None
    if assumed_zero:
        opening_assets = '0.00'
    elif first_market is not None and first_cash is not None:
        opening_amount = Decimal(first_market) + Decimal(first_cash)
        if opening_amount >= 0:
            opening_assets = format(opening_amount, '.2f')
    cash_inputs[broker] = {
        'opening_day': opening, 'anchor_day': anchor_day, 'confirmed_cash': snapshot['confirmed_cash'],
        'start': start, 'as_of': end, 'ledger': ledger, 'opening_market_value': first_market,
        'opening_assumed_zero': assumed_zero,
        'hk_security': HK_SECURITY if 'hong_kong' in base else None,
    }
    return {**base, **result, 'year': int(end[:4]), 'start': start, 'as_of': end,
            'local_close_from': local_close_from,
            'opening_assets': opening_assets, 'opening_assumed_zero': assumed_zero,
            'opening_date': start if assumed_zero else opening,
            'return_gaps': sorted(missing_transfers),
            'trading_days': len(dates) - 1, 'transfers': transfer_days, 'stocks': stock_result, 'etfs': etf_result}


def read_performance_reports(db, default_broker):
    security_names = {}
    cash_inputs = {}
    with db.shared_reader() as reader:
        accounts = _read_account_coverage(reader.conn)
        reports = {account['broker']: _read_performance(reader, account['broker'], security_names, cash_inputs)
                   for account in accounts}
        primary = reports.get(default_broker)
        if primary is None:
            primary = _read_performance(reader, default_broker, security_names, cash_inputs)
    covered = [reports[row['broker']] for row in accounts if row['snapshot_date']]
    combined = merge_performance(covered)
    combined['missing_accounts'] = [row['broker'] for row in accounts if not row['snapshot_date']]
    if 'hong_kong' in primary:
        hk = combined['hong_kong'] = primary['hong_kong']
        for report in (combined, primary):
            for domestic_key, market_key, label in (
                (None, 'all_markets', 'A 股及 ETF'),
                ('stocks', 'stocks_hk', 'A 股'),
                ('etfs', 'etfs_hk', 'ETF'),
            ):
                if domestic_key and domestic_key not in report:
                    continue
                domestic = {**report, **report[domestic_key]} if domestic_key else report
                mixed = combine_market_performance(domestic, hk, domestic_label=label)
                if mixed is not None:
                    attach_monthly_security_pnl(mixed)
                    report[market_key] = mixed
            if 'all_markets' in report:
                cash_accounts = covered if report is combined else [primary]
                report['account_with_hk'] = account_return_with_hk(
                    report, report['all_markets'], [cash_inputs[row['broker']] for row in cash_accounts])
        attach_monthly_security_pnl(hk)
    for report in (combined, primary, *(report for report in reports.values() if report is not primary)):
        attach_monthly_security_pnl(report)
        for scope in ('stocks', 'etfs'):
            if scope in report:
                attach_monthly_security_pnl(report[scope])
    return {**primary, 'accounts': accounts, 'combined': combined, 'security_names': security_names,
            'other_accounts': [report for broker, report in reports.items() if broker != default_broker]}
