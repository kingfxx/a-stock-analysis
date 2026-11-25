"""Pure reconstruction of daily A-share P&L from a closing position and ledger."""

from collections import defaultdict
from decimal import Decimal


TRADE_TYPES = {'buy', 'sell'}
SUPPORTED_TYPES = TRADE_TYPES | {'dividend', 'dividend_tax'}


def _cash(event):
    value = event['net_cash']
    if value is not None:
        return Decimal(value)
    gross = event['gross_amount']
    if gross is None:
        return None
    amount = abs(Decimal(gross))
    kind = event['event_type']
    if kind in TRADE_TYPES:
        signed = -amount if kind == 'buy' else amount
        return signed - Decimal(event['fees'] or '0')
    return -amount if kind == 'dividend_tax' else amount


def _market_value(held, prices, day, issues):
    value = Decimal(0)
    complete = True
    for key, quantity in held.items():
        if not quantity:
            continue
        close = prices.get(key, {}).get(day)
        if close is None:
            issues.add(f'{key[1]} 缺少 {day} 收盘价')
            complete = False
        else:
            value += close * quantity
    return value if complete else None


def reconstruct(days, closing, events, prices):
    """Walk backward from recorded shares, then report coverage and cumulative amounts."""
    calendar = set(days[1:])
    unplaced = sorted({event['event_date'] for event in events if event['event_date'] not in calendar})
    if unplaced:
        return {'records': [], 'blocked_dates': unplaced}
    grouped = defaultdict(list)
    for event in events:
        grouped[event['event_date']].append(event)
    held = closing.copy()
    results = []
    for previous, day in reversed(list(zip(days, days[1:]))):
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
            if kind in TRADE_TYPES:
                quantity = event['quantity']
                if not quantity:
                    return {'records': [], 'blocked_dates': [day]}
                before[key] = before.get(key, 0) + (abs(quantity) if kind == 'sell' else -abs(quantity))
            value = _cash(event)
            if value is None:
                issues.add(f'{key[1]} 缺少资金金额')
            elif kind in TRADE_TYPES:
                cash += value
                if kind == 'buy':
                    buys -= value
            else:
                income += value
        if any(quantity < 0 for quantity in before.values()):
            return {'records': [], 'blocked_dates': [day]}
        opening_value = _market_value(before, prices, previous, issues)
        closing_value = _market_value(held, prices, day, issues)
        pnl = (closing_value - opening_value + cash + income).quantize(Decimal('0.01')) if not issues else None
        capital = opening_value + buys if pnl is not None else None
        rate = pnl / capital if capital is not None and capital > 0 else None
        results.append({
            'date': day, 'pnl': str(pnl) if pnl is not None else None, 'issues': sorted(issues),
            'market_value': str(closing_value.quantize(Decimal('0.01'))) if closing_value is not None else None,
            'return_base': str(capital.quantize(Decimal('0.01'))) if capital is not None else None,
            'return_rate': str(rate) if rate is not None else None,
            'trades': sum(event['event_type'] in TRADE_TYPES for event in trades),
            'dividend_net': str(income.quantize(Decimal('0.01'))),
        })
        held = before
    results.reverse()
    cumulative = Decimal(0)
    for row in results:
        if row['pnl'] is None:
            row['cumulative'] = None
        else:
            cumulative += Decimal(row['pnl'])
            row['cumulative'] = str(cumulative)
    return {'records': results, 'blocked_dates': []}
