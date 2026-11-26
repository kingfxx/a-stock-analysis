"""Read the local inputs for CMB A-share and ETF performance."""

from collections import Counter
from datetime import date, timedelta
from decimal import Decimal

from stock_analysis.domains.etfs.storage import read_cash_dividends, read_unadjusted_closes
from stock_analysis.domains.portfolio.performance import TRADE_TYPES, reconstruct
from stock_analysis.domains.portfolio.performance_events import normalize_events, reconcile_etf_dividends
from stock_analysis.domains.portfolio.reference import local_market_days
from stock_analysis.domains.portfolio.history_prices import read_closes


def read_performance(db, broker):
    base = {'broker': broker, 'records': []}
    with db.shared_reader() as reader:
        conn = reader.conn
        snapshot = conn.execute("""
            SELECT s.id,s.account_id,s.snapshot_date FROM portfolio_snapshots s
            JOIN portfolio_accounts a ON a.id=s.account_id
            WHERE a.broker=? AND s.session='close'
            ORDER BY s.snapshot_date DESC, s.id DESC LIMIT 1
        """, (broker,)).fetchone()
        if snapshot is None:
            return {**base, 'error': '缺少招商收盘持仓快照'}
        end = snapshot['snapshot_date']
        account = snapshot['account_id']
        start = conn.execute("""
            SELECT min(event_date) FROM portfolio_ledger_entries
            WHERE account_id=? AND event_date<=? AND duplicate_of_id IS NULL
            AND (event_type IN ('bank_transfer_in','bank_transfer_out')
                 OR (asset_type IN ('stock','etf') AND event_type IN ('buy','sell','dividend','dividend_tax')))
        """, (account, end)).fetchone()[0]
        if start is None:
            return {**base, 'as_of': end, 'error': '缺少招商历史成交或银证转账'}
        calendar_start = (date.fromisoformat(start) - timedelta(days=30)).isoformat()
        calendar = local_market_days(reader, calendar_start, end)
        prior = [day for day in calendar if day < start]
        if not prior or calendar[-1] != end:
            return {**base, 'as_of': end, 'error': '期初或快照日缺少本地交易日历'}
        opening = prior[-1]
        dates = calendar[calendar.index(opening):]
        closing = Counter()
        etfs = set()
        for row in conn.execute("""
            SELECT exchange,code,quantity,asset_type FROM portfolio_snapshot_positions
            WHERE snapshot_id=? AND asset_type IN ('stock','etf')
        """, (snapshot['id'],)):
            security = (row['exchange'], row['code'])
            closing[security] += row['quantity']
            if row['asset_type'] == 'etf':
                etfs.add(security)
        events = []
        for row in conn.execute("""
            SELECT id,event_date,event_type,source_label,exchange,code,asset_type,quantity,gross_amount,fees,net_cash
            FROM portfolio_ledger_entries WHERE account_id=? AND duplicate_of_id IS NULL
            AND (asset_type IN ('stock','etf') OR (asset_type='unknown' AND code IN ('760015','787428','732391')))
            AND event_date>? AND event_date<=? ORDER BY event_date,event_time,id
        """, (account, opening, end)):
            event = dict(row)
            event['security'] = (event['exchange'], event['code'])
            events.append(event)
            if row['asset_type'] == 'etf':
                etfs.add(event['security'])
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
        transfers = {}
        for row in conn.execute("""
            SELECT event_date,event_type,net_cash FROM portfolio_ledger_entries WHERE account_id=?
            AND event_type IN ('bank_transfer_in','bank_transfer_out') AND net_cash IS NOT NULL
            AND duplicate_of_id IS NULL AND event_date>? AND event_date<=?
        """, (account, opening, end)):
            amounts = transfers.setdefault(row['event_date'], {'in': Decimal(0), 'out': Decimal(0)})
            direction = 'in' if row['event_type'] == 'bank_transfer_in' else 'out'
            amounts[direction] += abs(Decimal(row['net_cash']))
        transfer_days = [{'date': day, **{key: str(value) for key, value in amounts.items()}}
                         for day, amounts in sorted(transfers.items())]
    result = reconstruct(dates, dict(closing), events, prices, dividends)
    stock_events = [event for event in events if event['security'] not in etfs]
    stock_result = reconstruct(dates, {key: quantity for key, quantity in closing.items() if key not in etfs},
                               stock_events, prices)
    trade_fee_gaps = sum(event['event_type'] in TRADE_TYPES and event['fees'] is None and event['net_cash'] is None
                         for event in events)
    stock_fee_gaps = sum(event['event_type'] in TRADE_TYPES and event['fees'] is None and event['net_cash'] is None
                         for event in stock_events)
    return {**base, **result, 'year': int(end[:4]), 'start': start, 'as_of': end,
            'trading_days': len(dates) - 1, 'fee_gap_count': trade_fee_gaps,
            'transfers': transfer_days, 'stocks': {**stock_result, 'fee_gap_count': stock_fee_gaps}}
