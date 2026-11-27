"""Exact snapshot totals derived from explicitly supplied monetary fields."""

from decimal import Decimal
from itertools import groupby


INCLUDED = {'stock', 'etf'}


def position_included(row):
    if row['asset_type'] in INCLUDED:
        return True
    if (row['asset_type'] != 'hong_kong' or row.get('currency') != 'HKD'
            or row.get('exchange') != 'hk' or row.get('exclusion_reason')):
        return False
    if row.get('accounts'):
        return all(position_included(part) for part in row['accounts'])
    if row.get('book_projected'):
        return True
    if any(row.get(key) is None for key in ('price', 'market_value', 'exchange_rate')):
        return False
    price, value, rate = (Decimal(row[key]) for key in ('price', 'market_value', 'exchange_rate'))
    return price > 0 and rate > 0 and abs(price * row['quantity'] * rate - value) <= Decimal('0.01')


def sum_known(rows, key):
    values = [row[key] for row in rows]
    return None if any(value is None for value in values) else sum(map(Decimal, values), Decimal(0))


def position_cost(part):
    if part.get('cost_amount') is not None:
        return Decimal(part['cost_amount'])
    if part.get('market_value') is not None and part.get('holding_pnl') is not None:
        return Decimal(part['market_value']) - Decimal(part['holding_pnl'])
    return None


def revalue_positions(positions, prices, day, previous_day):
    rows = []
    for position in positions:
        code, name, exchange, asset_type, quantity = (
            position[key] for key in ('code', 'name', 'exchange', 'asset_type', 'quantity'))
        series = prices.get((exchange, code, asset_type), {})
        close = series.get(day)
        hk = asset_type == 'hong_kong'
        prior = max((stamp for stamp in series if day and stamp < day), default=None) if hk else previous_day
        previous = series.get(prior)
        rate = Decimal(position['exchange_rate']) if hk else Decimal(1)
        costs = [position_cost(part) for part in position.get('accounts', [position])]
        value = close * quantity * rate if close is not None else None
        pnl = value - sum(costs) if value is not None and None not in costs else None
        daily = (close - previous) * quantity * rate if close is not None and previous is not None else None
        rows.append(dict(code=code, name=name, asset_type=asset_type, quantity=quantity,
                         close=format(close, 'f') if close is not None else None,
                         market_value=format(value, '.2f') if value is not None else None,
                         holding_pnl=format(pnl, '.2f') if pnl is not None else None,
                         daily_pnl=format(daily, '.2f') if daily is not None else None))
    return rows


def reconcile(summary, positions):
    market_total = sum_known(positions, 'market_value')
    target = summary['broker_market_value']
    reported = summary['broker_assets']

    def asset_difference(field):
        amount = summary[field]
        return (format(Decimal(reported) - Decimal(target) - Decimal(amount), 'f')
                if None not in (reported, target, amount) else None)

    return {
        'market_difference': format(market_total - Decimal(target), 'f')
        if market_total is not None and target is not None else None,
        'assets_less_market_and_available': asset_difference('broker_available'),
        'assets_less_market_and_balance': asset_difference('broker_balance'),
    }


def daily_change(assets, pnl, broker_rate=None):
    rate = broker_rate
    basis = 'broker' if broker_rate is not None else None
    if pnl is not None and rate is None and assets is not None:
        opening = Decimal(assets) - pnl
        if opening > 0:
            rate = format(pnl * 100 / opening, '.2f')
            basis = 'estimated_opening_assets'
    return {'daily_pnl': str(pnl) if pnl is not None else None,
            'daily_pnl_rate': rate, 'daily_rate_basis': basis}


def fill_snapshot_daily_pnl(snapshot, positions, previous_day):
    if not previous_day or snapshot['session'] != 'close':
        return
    estimates = []
    for row in positions:
        reference = row.get('reference')
        if (row['asset_type'] == 'hong_kong' or row['daily_pnl'] is not None or not reference or row['price'] is None
                or reference.get('close_date') != snapshot['snapshot_date']
                or reference.get('previous_close_date') != previous_day):
            continue
        close, previous = Decimal(str(reference['close'])), Decimal(str(reference['previous_close']))
        if close != Decimal(row['price']) or previous <= 0:
            continue
        change = close - previous
        amount = format(change * row['quantity'], '.2f')
        estimates.append((row, amount, format(change * 100 / previous, '.2f')))
    if not estimates:
        return
    broker_total = snapshot.get('broker_daily_pnl')
    if broker_total is not None and all(position_included(row) for row in positions):
        existing = [row['daily_pnl'] for row in positions if row['daily_pnl'] is not None]
        if len(existing) + len(estimates) != len(positions):
            return
        values = existing + [amount for _, amount, _ in estimates]
        if abs(sum(map(Decimal, values)) - Decimal(broker_total)) > Decimal('0.01'):
            return
    for row, amount, rate in estimates:
        row.update(daily_pnl=amount, daily_pnl_rate=rate, daily_pnl_basis='local_close_estimate')


def snapshot_totals(snapshot, positions):
    for row in positions:
        row['included'] = position_included(row)
    included = [row for row in positions if row['included']]
    market = sum_known(included, 'market_value')
    pnl = sum_known(included, 'holding_pnl')
    daily = sum_known(included, 'daily_pnl')
    if daily is None and len(included) == len(positions) and snapshot.get('broker_daily_pnl') is not None:
        daily = Decimal(snapshot['broker_daily_pnl'])
    cash = snapshot['confirmed_cash']
    assets = market + Decimal(cash) if market is not None and cash is not None else None
    base = assets if assets is not None else market
    for row in positions:
        value = row['market_value']
        row['weight'] = (format(Decimal(value) * 100 / base, '.2f')
                         if row['included'] and value is not None and base and base > 0 else None)
    return {
        'market_value': str(market) if market is not None else None,
        'holding_pnl': str(pnl) if pnl is not None else None,
        **daily_change(assets, daily, snapshot.get('broker_daily_pnl_rate') if len(included) == len(positions) else None),
        'assets': str(assets) if assets is not None else None,
        'cash': cash,
        'cash_weight': format(Decimal(cash) * 100 / assets, '.2f') if cash is not None and assets and assets > 0 else None,
        'weight_basis': ('assets' if assets is not None else 'securities') if base and base > 0 else None,
        'excluded_count': len(positions) - len(included),
    }


def combine_positions(positions, day):
    """Group same-day positions and use one price for a security held in both accounts."""
    def identity(row):
        return row['exchange'] or '', row['asset_type'], row['code']

    combined = []
    eligible = sorted((row for row in positions if position_included(row)), key=identity)
    for _, group in groupby(eligible, key=identity):
        parts = list(group)
        row = dict(parts[0])
        row['accounts'] = parts
        if len(parts) > 1:
            row['quantity'] = sum(part['quantity'] for part in parts)
            row['available_quantity'] = (sum(part['available_quantity'] for part in parts)
                                         if all(part['available_quantity'] is not None for part in parts) else None)
            row['cost_price'] = None
            row['cost_amount'] = None
            ref = row['reference']
            if row['asset_type'] == 'hong_kong':
                market = sum_known(parts, 'market_value')
                row['market_value'] = str(market) if market is not None else None
                if row['quantity'] and all(part['exchange_rate'] is not None for part in parts):
                    share_rate = sum(Decimal(part['exchange_rate']) * part['quantity'] for part in parts)
                    row['exchange_rate'] = str(share_rate / row['quantity'])
                else:
                    row['exchange_rate'] = None
                if not all(part['price'] == row['price'] for part in parts):
                    row['price'] = None
            elif ref and ref['close_date'] == day:
                close = Decimal(str(ref['close']))
            elif (row['price'] is not None and all(part['price'] == row['price'] and part['market_value'] is not None
                  and abs(Decimal(part['market_value']) - part['quantity'] * Decimal(row['price'])) <= Decimal('0.01')
                  for part in parts)):
                close = Decimal(row['price'])
            else:
                close = None
            if row['asset_type'] != 'hong_kong':
                row['market_value'] = str(close * row['quantity']) if close is not None else None
                row['price'] = str(close) if close is not None else None
            pnl = sum_known(parts, 'holding_pnl')
            row['holding_pnl'] = str(pnl) if pnl is not None else None
            daily = sum_known(parts, 'daily_pnl')
            row['daily_pnl'] = str(daily) if daily is not None else None
            row['holding_pnl_rate'] = row['daily_pnl_rate'] = None
            if daily is not None and any(part.get('daily_pnl_basis') == 'local_close_estimate' for part in parts):
                row['daily_pnl_basis'] = 'local_close_estimate'
                previous = ref.get('previous_close') if ref else None
                if previous and row['quantity']:
                    row['daily_pnl_rate'] = format(daily * 100 / (Decimal(str(previous)) * row['quantity']), '.2f')
        combined.append(row)
    return combined + [row for row in positions if not position_included(row)]
