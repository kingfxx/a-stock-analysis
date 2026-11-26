"""Pure reconstruction of daily securities P&L from a closing position and ledger."""

from bisect import bisect_left
from collections import defaultdict
from decimal import Decimal


TRADE_TYPES = {'buy', 'sell'}
ACQUISITIONS = {'buy', 'subscription'}
POSITION_TYPES = TRADE_TYPES | {'subscription', 'stock_credit'}
SUPPORTED_TYPES = POSITION_TYPES | {'dividend', 'dividend_tax'}


def _cash(event):
    value = event['net_cash']
    if value is not None:
        return Decimal(value)
    gross = event['gross_amount']
    if gross is None:
        return None
    amount = abs(Decimal(gross))
    kind = event['event_type']
    if kind in POSITION_TYPES:
        signed = -amount if kind in ACQUISITIONS else amount
        return signed - Decimal(event['fees'] or '0')
    return -amount if kind == 'dividend_tax' else amount


def _market_value(held, prices, day):
    value = Decimal(0)
    missing = {}
    for key, quantity in held.items():
        if not quantity:
            continue
        close = prices.get(key, {}).get(day)
        if close is None:
            missing[key] = quantity
        else:
            value += close * quantity
    return value, missing


def _daily_values(before, held, prices, price_days, previous, day, issues):
    opening, missing_open = _market_value(before, prices, previous)
    closing, missing_close = _market_value(held, prices, day)
    missing_keys = missing_open.keys() | missing_close.keys()
    estimates = []

    def resolve(value, missing, target):
        if not missing:
            return value
        if len(missing_keys) == 1:
            key, quantity = next(iter(missing.items()))
            known = price_days.get(key, ())
            offset = bisect_left(known, target)
            if offset:
                quote_day = known[offset - 1]
                estimates.append(dict(code=key[1], date=target, price_date=quote_day))
                return value + quantity * prices[key][quote_day]
        issues.update(f'{key[1]} 缺少 {target} 收盘价' for key in missing)
        return None

    return resolve(opening, missing_open, previous), resolve(closing, missing_close, day), estimates


def reconstruct(days, closing, events, prices, dividends=()):
    """Walk backward from recorded shares, then report coverage and cumulative amounts."""
    calendar = set(days[1:])
    unplaced = sorted({event['event_date'] for event in events if event['event_date'] not in calendar})
    if unplaced:
        return {'records': [], 'blocked_dates': unplaced}
    grouped = defaultdict(list)
    for event in events:
        grouped[event['event_date']].append(event)
    registration_days = {event['registration_date'] for event in dividends}
    price_days = {key: sorted(day for day, close in series.items() if close is not None)
                  for key, series in prices.items()}
    registered = {}
    held = closing.copy()
    results = []
    for previous, day in reversed(list(zip(days, days[1:]))):
        if day in registration_days:
            registered[day] = held.copy()
        before = held.copy()
        trades = grouped[day]
        cash = Decimal(0)
        income = Decimal(0)
        buys = Decimal(0)
        issues = set()
        for event in trades:
            key = event['security']
            kind = event['event_type']
            if kind not in SUPPORTED_TYPES:
                # An unclassified corporate action could invalidate all earlier quantities.
                return {'records': [], 'blocked_dates': [day]}
            if kind in POSITION_TYPES:
                quantity = event['quantity']
                if not quantity:
                    return {'records': [], 'blocked_dates': [day]}
                before[key] = before.get(key, 0) + (abs(quantity) if kind == 'sell' else -abs(quantity))
            value = _cash(event)
            if value is None:
                issues.add(f'{key[1]} 缺少资金金额')
            elif kind in POSITION_TYPES:
                cash += value
                if kind in ACQUISITIONS:
                    buys -= value
            else:
                income += value
        if any(quantity < 0 for quantity in before.values()):
            return {'records': [], 'blocked_dates': [day]}
        opening_value, closing_value, estimates = _daily_values(
            before, held, prices, price_days, previous, day, issues)
        results.append({
            'date': day, 'issues': issues, 'opening': opening_value, 'closing': closing_value,
            'cash': cash, 'buys': buys, 'income': income,
            'trades': sum(event['event_type'] in TRADE_TYPES for event in trades),
            'estimated_closes': estimates,
        })
        held = before
    registered[days[0]] = held
    by_day = {row['date']: row for row in results}
    for event in dividends:
        row = by_day[event['ex_date']]
        key = event['security']
        if event['booked'] is not None:
            row['income'] += event['booked']
        elif event.get('unmatched'):
            row['issues'].add(f'{key[1]} ETF 分红到账匹配待核对')
        elif event['registration_date'] not in registered:
            row['issues'].add(f'{key[1]} 缺少 ETF 分红登记日持仓')
        else:
            amount = (event['cash_per_unit'] *
                      registered[event['registration_date']].get(key, 0)).quantize(Decimal('0.01'))
            row['income'] += amount
            if amount:
                row['estimated_dividend'] = row.get('estimated_dividend', Decimal(0)) + amount
    results.reverse()
    cumulative = Decimal(0)
    for row in results:
        issues = row['issues']
        opening = row.pop('opening')
        closing_value = row.pop('closing')
        cash = row.pop('cash')
        buys = row.pop('buys')
        income = row.pop('income')
        pnl = (closing_value - opening + cash + income).quantize(Decimal('0.01')) if not issues else None
        capital = opening + buys if pnl is not None else None
        rate = pnl / capital if capital is not None and capital > 0 else None
        if pnl is not None:
            cumulative += pnl
        row.update(
            pnl=str(pnl) if pnl is not None else None, issues=sorted(issues),
            market_value=str(closing_value.quantize(Decimal('0.01'))) if closing_value is not None else None,
            return_base=str(capital.quantize(Decimal('0.01'))) if capital is not None else None,
            return_rate=str(rate) if rate is not None else None,
            dividend_net=str(income.quantize(Decimal('0.01'))),
            estimated_dividend=str(row.get('estimated_dividend', '0.00')),
            cumulative=str(cumulative) if pnl is not None else None,
        )
    return {'records': results, 'blocked_dates': []}
