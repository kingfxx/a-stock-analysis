"""Revalue a broker snapshot using only locally published raw closes."""

from datetime import date, time, timedelta
from decimal import Decimal
from hashlib import sha256
import json

from stock_analysis.domains.etfs.storage import read_unadjusted_closes
from stock_analysis.domains.portfolio.calculations import (
    position_cost, position_included, revalue_positions, snapshot_totals, sum_known,
)
from stock_analysis.domains.portfolio.history_prices import read_closes
from stock_analysis.domains.portfolio.reference import local_market_days
from stock_analysis.infrastructure.storage import utc_now


def revaluation_directory(conn, account_id):
    versions = [dict(row) for row in conn.execute("""
        SELECT id,quote_date FROM portfolio_revaluation_snapshots
        WHERE account_id IS ? ORDER BY quote_date DESC, id DESC
    """, (account_id,))]
    if account_id is not None:
        versions.extend(dict(row, inherited=True) for row in conn.execute("""
            SELECT r.id,r.quote_date FROM portfolio_revaluation_snapshots r
            WHERE r.account_id IS NULL AND EXISTS (
                SELECT 1 FROM json_each(r.snapshot_ids) AS basis
                JOIN portfolio_snapshots s ON s.id=basis.value WHERE s.account_id=?
            )
        """, (account_id,)))
        versions.sort(key=lambda row: (row['quote_date'], row['id']), reverse=True)
    return versions


def load_saved_revaluation(db, estimate_id):
    with db.connection() as conn:
        row = conn.execute("""
            SELECT account_id, snapshot_ids, result_json FROM portfolio_revaluation_snapshots WHERE id=?
        """, (estimate_id,)).fetchone()
    if row is None:
        raise LookupError('找不到本地估算快照')
    result = json.loads(row['result_json'])
    if 'book' in result:
        result['positions'] = book_quotes(result['book'])
    return row['account_id'], json.loads(row['snapshot_ids']), {**result, 'saved_id': estimate_id}


def saved_revaluation(db, estimate_id):
    return load_saved_revaluation(db, estimate_id)[2]


def delete_revaluation(db, estimate_id):
    with db.connection(write=True) as conn:
        cursor = conn.execute('DELETE FROM portfolio_revaluation_snapshots WHERE id=?', (estimate_id,))
        if not cursor.rowcount:
            raise LookupError('找不到本地估算快照')


def book_quotes(book):
    return [{**{key: row[key] for key in ('code', 'name', 'asset_type', 'quantity', 'market_value', 'holding_pnl', 'daily_pnl')},
             'close': row['price']} for row in book['positions'] if row['included']]


def _display_position(source, close, market_value, holding_pnl, daily_pnl, reference):
    costs = [position_cost(part) for part in source.get('accounts', [source])]
    cost = sum(costs) if None not in costs else None
    value, quantity = Decimal(market_value), source['quantity']
    opening = value - Decimal(daily_pnl) if daily_pnl is not None else None
    hk = source['asset_type'] == 'hong_kong'
    currency_rate = Decimal(source['exchange_rate']) if hk else Decimal(1)
    return {**source, 'price': close, 'market_value': market_value, 'holding_pnl': holding_pnl,
            'daily_pnl': daily_pnl, 'reference': reference,
            'holding_pnl_rate': format(Decimal(holding_pnl) * 100 / abs(cost), '.2f')
            if holding_pnl is not None and cost else None,
            'cost_price': source['cost_price'] if source['cost_price'] is not None else
            (format(cost / quantity / currency_rate, '.6f') if cost is not None and quantity else None),
            'daily_pnl_rate': format(Decimal(daily_pnl) * 100 / opening, '.2f') if opening and opening > 0 else None,
            'daily_pnl_basis': ('fixed_fx_estimate' if hk else 'local_close_estimate') if daily_pnl is not None else None}


def display_revaluation(payload, saved, references, previous_closes):
    day = saved['quote_date']
    keys = {(row['asset_type'], row['code']) for row in saved['positions']}
    source_rows = []
    positions = []
    for row in payload['positions']:
        if not position_included(row):
            positions.append(row)
        elif row['quantity'] > 0:
            if (row['asset_type'], row['code']) in keys:
                source_rows.append(row)
            else:
                positions.append({**row, 'exclusion_reason': '该估算版本未覆盖此证券'})
    for source, estimate, reference in zip(source_rows, saved['positions'], references, strict=True):
        close = estimate['close']
        daily = estimate['daily_pnl']
        previous = previous_closes.get((source['exchange'], source['code'], source['asset_type']))
        hk = source['asset_type'] == 'hong_kong'
        if daily is None and previous is not None and not hk:
            daily = format((Decimal(close) - previous) * source['quantity'], '.2f')
        ref = {**(reference or {}), 'close': close, 'close_date': day,
               'valuation': (reference or {}).get('valuation', {})}
        row = _display_position(source, close, estimate['market_value'], estimate['holding_pnl'], daily, ref)
        if source.get('accounts'):
            parts = []
            for part in source['accounts']:
                rate = Decimal(part['exchange_rate']) if hk else Decimal(1)
                value = Decimal(close) * part['quantity'] * rate
                cost = position_cost(part)
                share = part['quantity'] / Decimal(source['quantity']) * rate
                if hk:
                    share /= Decimal(source['exchange_rate'])
                parts.append(_display_position(part, close, format(value, '.2f'),
                                               format(value - cost, '.2f') if cost is not None else None,
                                               format(Decimal(daily) * share, '.2f')
                                               if daily is not None else None, ref))
            row['accounts'] = parts
        positions.append(row)
    source_snapshot = payload['snapshot']
    snapshot = {**source_snapshot, 'snapshot_date': day, 'session': 'close',
                'broker_daily_pnl': None, 'broker_daily_pnl_rate': None, 'broker_assets': None}
    return {**payload, 'snapshot': snapshot, 'positions': positions, 'totals': snapshot_totals(snapshot, positions),
            'estimate_id': saved['saved_id'], 'basis_date': saved['snapshot_date'],
            'funds': payload.get('funds', [{'snapshot': source_snapshot, 'reconciliation': payload.get('reconciliation')}])}


def save_revaluation(db, payload, result):
    if result['quote_date'] is None or result['totals']['market_value'] is None:
        return result
    snapshot = payload['snapshot']
    account_id = snapshot.get('account_id')
    snapshots = [entry['snapshot'] for entry in payload['funds']] if 'funds' in payload else [snapshot]
    basis = json.dumps(sorted(row['id'] for row in snapshots), separators=(',', ':'))
    stored = ({key: value for key, value in result.items() if key != 'positions'} if 'book' in result else result)
    content = json.dumps(stored, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    digest = sha256((basis + content).encode('utf-8')).hexdigest()
    with db.connection(write=True) as conn:
        identity = conn.execute("""
            SELECT id FROM portfolio_revaluation_snapshots WHERE account_id IS ? AND quote_date=? AND result_sha256=?
        """, (account_id, result['quote_date'], digest)).fetchone()
        if identity is not None:
            return {**result, 'saved_id': identity[0]}
        estimate_id = conn.execute("""
            INSERT INTO portfolio_revaluation_snapshots(account_id,quote_date,snapshot_ids,result_sha256,result_json,written_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (account_id, result['quote_date'], basis, digest, content, utc_now())).lastrowid
    return {**result, 'saved_id': estimate_id}


def read_revaluation_prices(reader, positions, start, end):
    stock_closes = read_closes(reader, {(row['exchange'], row['code']) for row in positions
                                        if row['asset_type'] == 'stock'}, start, end)
    prices = {(exchange, code, 'stock'): {day: value for day, (value, _) in series.items()}
              for (exchange, code), series in stock_closes.items()}
    hk = {(row['exchange'], row['code']) for row in positions if row['asset_type'] == 'hong_kong'}
    if hk:
        for (exchange, code), series in read_closes(reader, hk, start, end).items():
            prices[exchange, code, 'hong_kong'] = {day: value for day, (value, _) in series.items()}
    for exchange, code in {(row['exchange'], row['code']) for row in positions
                           if row['asset_type'] == 'etf' and row['exchange'] in ('sh', 'sz')}:
        closes = read_unadjusted_closes(reader.conn, exchange, code, start, end)
        values = {day: Decimal(str(raw)) for day, raw in closes.items() if raw is not None}
        prices[exchange, code, 'etf'] = {day: value for day, value in values.items() if value.is_finite() and value > 0}
    market_days = set(local_market_days(reader, start, end))
    market_days.update(day for series in prices.values() for day in series)
    return prices, market_days


def previous_revaluation_closes(reader, positions, saved):
    if all(row['daily_pnl'] is not None for row in saved['positions']):
        return {}
    end = saved['quote_date']
    start = date.fromordinal(max(1, date.fromisoformat(end).toordinal() - 15)).isoformat()
    prices, days = read_revaluation_prices(reader, positions, start, end)
    previous = max((day for day in days if day < end), default=None)
    return {key: series.get(previous) for key, series in prices.items()}


def read_revaluation(db, payload, now):
    snapshot = payload['snapshot']
    if not snapshot:
        raise LookupError('请选择一份持仓快照')
    baseline = snapshot['snapshot_date']
    today = now.date().isoformat()
    # Daily providers can publish today's provisional bar during trading hours.
    positions = [row for row in payload['positions'] if position_included(row) and row['quantity'] > 0]
    cutoff = time(16, 15) if any(row['asset_type'] == 'hong_kong' for row in positions) else time(15)
    end = (now.date() - timedelta(days=int(now.time() < cutoff))).isoformat()
    start = date.fromordinal(max(1, date.fromisoformat(baseline).toordinal() - 15)).isoformat()
    with db.shared_reader() as reader:
        prices, trading_days = read_revaluation_prices(reader, positions, start, end)
    # Keep at most one fallback session and never combine different quote dates.
    days = sorted(day for day in trading_days if day <= end)
    previous_day = next((day for day in reversed(days) if day < today), None)
    complete_today = all(today in prices.get((row['exchange'], row['code'], row['asset_type']), {}) for row in positions)
    quote_date = today if today in days and complete_today else previous_day
    if quote_date is not None and quote_date < baseline and baseline != today:
        quote_date = None
    previous_day = max((day for day in days if quote_date and day < quote_date), default=None)
    rows = revalue_positions(positions, prices, quote_date, previous_day)
    missing_accounts = payload.get('missing_accounts', [])
    totals = {}
    for key in ('market_value', 'holding_pnl', 'daily_pnl'):
        value = sum_known(rows, key) if rows and not missing_accounts else None
        totals[key] = format(value, '.2f') if value is not None else None
    return dict(snapshot_date=baseline, quote_date=quote_date, today=today, broker=snapshot['broker'],
                positions=rows, totals=totals, missing_accounts=missing_accounts)
