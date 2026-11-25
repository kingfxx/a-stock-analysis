"""Read and validate the inputs for the 2026 CMB A-share reconstruction."""

from collections import Counter
from decimal import Decimal

from stock_analysis.domains.portfolio.performance import TRADE_TYPES, reconstruct
from stock_analysis.domains.portfolio.reference import local_market_days
from stock_analysis.domains.stocks.price_service import PriceService


YEAR = 2026
OPENING = '2025-12-31'


def read_performance(db, broker):
    base = {'year': YEAR, 'broker': broker, 'records': []}
    with db.shared_reader() as reader:
        conn = reader.conn
        snapshot = conn.execute("""
            SELECT s.id,s.account_id,s.snapshot_date FROM portfolio_snapshots s
            JOIN portfolio_accounts a ON a.id=s.account_id
            WHERE a.broker=? AND s.session='close' AND s.snapshot_date BETWEEN '2026-01-01' AND '2026-12-31'
            ORDER BY s.snapshot_date DESC, s.id DESC LIMIT 1
        """, (broker,)).fetchone()
        if snapshot is None:
            return {**base, 'error': '缺少招商 2026 年收盘持仓快照'}
        end = snapshot['snapshot_date']
        dates = local_market_days(reader, OPENING, end)
        if not dates or dates[0] != OPENING or dates[-1] != end:
            return {**base, 'as_of': end, 'error': '期初或快照日缺少本地交易日历'}
        closing = Counter()
        for row in conn.execute("""
            SELECT exchange,code,quantity FROM portfolio_snapshot_positions WHERE snapshot_id=? AND asset_type='stock'
        """, (snapshot['id'],)):
            closing[(row['exchange'], row['code'])] += row['quantity']
        events = []
        for row in conn.execute("""
            SELECT event_date,event_type,exchange,code,quantity,gross_amount,fees,net_cash
            FROM portfolio_ledger_entries WHERE account_id=? AND asset_type='stock' AND duplicate_of_id IS NULL
            AND event_date>? AND event_date<=? ORDER BY event_date,event_time,id
        """, (snapshot['account_id'], OPENING, end)):
            event = dict(row)
            event['security'] = (event['exchange'], event['code'])
            events.append(event)
        if not closing and not events:
            return {**base, 'as_of': end, 'error': '这个区间没有可复算的 A 股持仓或成交'}
        service = PriceService(reader)
        prices = {
            (exchange, code): {row['date']: Decimal(str(row['close']))
                               for row in service.read(code, 'raw', start=OPENING, end=end, lease=False)}
            for exchange, code in closing.keys() | {event['security'] for event in events}
            if exchange in ('sh', 'sz')
        }
        transfers = {}
        for row in conn.execute("""
            SELECT event_date,event_type,net_cash FROM portfolio_ledger_entries WHERE account_id=?
            AND event_type IN ('bank_transfer_in','bank_transfer_out') AND net_cash IS NOT NULL
            AND duplicate_of_id IS NULL AND event_date>? AND event_date<=?
        """, (snapshot['account_id'], OPENING, end)):
            amounts = transfers.setdefault(row['event_date'], {'in': Decimal(0), 'out': Decimal(0)})
            direction = 'in' if row['event_type'] == 'bank_transfer_in' else 'out'
            amounts[direction] += abs(Decimal(row['net_cash']))
        transfer_days = [{'date': day, **{key: str(value) for key, value in amounts.items()}}
                         for day, amounts in sorted(transfers.items())]
    result = reconstruct(dates, dict(closing), events, prices)
    trade_fee_gaps = sum(event['event_type'] in TRADE_TYPES and event['fees'] is None and event['net_cash'] is None
                         for event in events)
    return {**base, **result, 'as_of': end, 'trading_days': len(dates) - 1, 'fee_gap_count': trade_fee_gaps,
            'transfers': transfer_days}
