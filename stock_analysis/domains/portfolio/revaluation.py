"""Revalue a broker snapshot using only locally published raw closes."""

from datetime import date, time, timedelta
from decimal import Decimal
from hashlib import sha256
import json

from stock_analysis.domains.etfs.storage import read_unadjusted_closes
from stock_analysis.domains.portfolio.calculations import INCLUDED, revalue_positions, sum_known
from stock_analysis.domains.portfolio.history_prices import read_closes
from stock_analysis.domains.portfolio.reference import local_market_days
from stock_analysis.infrastructure.storage import utc_now


def revaluation_directory(conn, account_id):
    return [dict(row) for row in conn.execute("""
        SELECT id,quote_date FROM portfolio_revaluation_snapshots
        WHERE account_id IS ? ORDER BY quote_date DESC, id DESC
    """, (account_id,))]


def saved_revaluation(db, estimate_id):
    with db.connection() as conn:
        row = conn.execute('SELECT result_json FROM portfolio_revaluation_snapshots WHERE id=?', (estimate_id,)).fetchone()
    if row is None:
        raise LookupError('找不到本地估算快照')
    return {**json.loads(row['result_json']), 'saved_id': estimate_id}


def save_revaluation(db, payload, result):
    if result['quote_date'] != result['today'] or result['totals']['market_value'] is None:
        return result
    snapshot = payload['snapshot']
    account_id = snapshot.get('account_id')
    snapshots = [entry['snapshot'] for entry in payload['funds']] if 'funds' in payload else [snapshot]
    basis = json.dumps(sorted(row['id'] for row in snapshots), separators=(',', ':'))
    content = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    digest = sha256((basis + content).encode('utf-8')).hexdigest()
    with db.connection(write=True) as conn:
        conn.execute("""
            INSERT INTO portfolio_revaluation_snapshots(account_id,quote_date,snapshot_ids,result_sha256,result_json,written_at)
            VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING
        """, (account_id, result['quote_date'], basis, digest, content, utc_now()))
        identity = conn.execute("""
            SELECT id FROM portfolio_revaluation_snapshots WHERE account_id IS ? AND quote_date=? AND result_sha256=?
        """, (account_id, result['quote_date'], digest)).fetchone()[0]
    return {**result, 'saved_id': identity}


def read_revaluation(db, payload, now):
    snapshot = payload['snapshot']
    if not snapshot:
        raise LookupError('请选择一份持仓快照')
    baseline = snapshot['snapshot_date']
    today = now.date()
    # Daily providers can publish today's provisional bar during trading hours.
    end = (today - timedelta(days=int(now.time() < time(15)))).isoformat()
    positions = [row for row in payload['positions'] if row['asset_type'] in INCLUDED and row['quantity'] > 0]
    start = date.fromordinal(max(1, date.fromisoformat(baseline).toordinal() - 15)).isoformat()
    with db.shared_reader() as reader:
        stock_closes = read_closes(reader, {(row['exchange'], row['code']) for row in positions
                                            if row['asset_type'] == 'stock'}, start, end)
        prices = {(exchange, code, 'stock'): {day: value for day, (value, _) in series.items()}
                  for (exchange, code), series in stock_closes.items()}
        for exchange, code in {(row['exchange'], row['code']) for row in positions
                               if row['asset_type'] == 'etf' and row['exchange'] in ('sh', 'sz')}:
            closes = read_unadjusted_closes(reader.conn, exchange, code, start, end)
            values = {day: Decimal(str(raw)) for day, raw in closes.items() if raw is not None}
            prices[exchange, code, 'etf'] = {day: value for day, value in values.items() if value.is_finite() and value > 0}
        trading_days = local_market_days(reader, start, end)
    quote_days = {day for series in prices.values() for day in series}
    quote_date = max((day for day in quote_days.union(trading_days) if day >= baseline), default=None)
    previous_day = trading_days[-2] if len(trading_days) >= 2 and trading_days[-1] == quote_date else None
    rows = revalue_positions(positions, prices, quote_date, previous_day)
    missing_accounts = payload.get('missing_accounts', [])
    totals = {}
    for key in ('market_value', 'holding_pnl', 'daily_pnl'):
        value = sum_known(rows, key) if rows and not missing_accounts else None
        totals[key] = format(value, '.2f') if value is not None else None
    return dict(snapshot_date=baseline, quote_date=quote_date, today=today.isoformat(), broker=snapshot['broker'],
                positions=rows, totals=totals, missing_accounts=missing_accounts)
