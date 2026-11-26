"""Reconstruct the confirmed CMB 01919 history at the broker snapshot rate."""

from datetime import date, timedelta
from decimal import Decimal

from stock_analysis.domains.portfolio.calculations import position_included
from stock_analysis.domains.portfolio.history_prices import read_closes
from stock_analysis.domains.portfolio.performance import TRADE_TYPES, advance_positions, reconstruct


SECURITY = ('hk', '01919')


def read_hk_performance(reader, snapshot, saved_days, security_names):
    conn = reader.conn
    anchor = snapshot['snapshot_date']
    positions = [dict(row) for row in conn.execute("""
        SELECT * FROM portfolio_snapshot_positions
        WHERE snapshot_id=? AND exchange=? AND code=? AND asset_type='hong_kong'
    """, (snapshot['id'], *SECURITY))]
    if len(positions) != 1 or not position_included(positions[0]):
        return None
    position = positions[0]
    security_names['hk:01919'] = position['name']
    rate = Decimal(position['exchange_rate'])
    end = saved_days[-1] if saved_days else anchor
    ledger = [dict(row, security=SECURITY) for row in conn.execute("""
        SELECT event_date,event_type,quantity,gross_amount,fees,net_cash
        FROM portfolio_ledger_entries
        WHERE account_id=? AND exchange=? AND code=? AND asset_type='hong_kong'
          AND duplicate_of_id IS NULL AND event_date<=?
        ORDER BY event_date,event_time,id
    """, (snapshot['account_id'], *SECURITY, end))]
    start = next((row['event_date'] for row in ledger if row['event_type'] in TRADE_TYPES
                  and row['event_date'] <= anchor), None)
    if not start:
        return None
    window = (date.fromisoformat(start) - timedelta(days=15)).isoformat()
    raw = read_closes(reader, {SECURITY}, window, end).get(SECURITY, {})
    prices = {day: close * rate for day, (close, _) in raw.items()}
    # The original CNY market value is the accounting mark at the snapshot.
    if position['quantity']:
        prices[anchor] = Decimal(position['market_value']) / position['quantity']
    opening = max((stamp for stamp in prices if stamp < start), default=None)
    if opening is None or start not in prices:
        return None
    # A CNY dividend can arrive on a Hong Kong non-trading day.
    as_of = max(stamp for stamp in prices if stamp <= end)
    events = [row for row in ledger if start <= row['event_date'] <= as_of]
    dates = sorted({day for day in prices if day >= opening} | {event['event_date'] for event in events})
    closing = advance_positions({SECURITY: position['quantity']}, events, anchor)
    if closing is None:
        if saved_days:
            return read_hk_performance(reader, snapshot, [], security_names)
        return None
    result = reconstruct(dates, closing, events, {SECURITY: prices}, round_marks=True)
    if saved_days and any(day > anchor for day in result['blocked_dates']):
        return read_hk_performance(reader, snapshot, [], security_names)
    return {**result, 'start': start, 'as_of': as_of, 'year': int(as_of[:4]),
            'transfers': [], 'fixed_fx': str(rate), 'fx_basis_date': anchor,
            'local_close_from': next((day for day in dates if day > anchor), None),
            'fee_gap_count': sum(row['fee_gap_count'] for row in result['records'])}
