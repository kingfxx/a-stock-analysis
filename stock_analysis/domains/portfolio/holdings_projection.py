"""Advance a confirmed account balance with normalized ledger entries."""

from decimal import Decimal

from stock_analysis.domains.portfolio.calculations import position_cost, position_included
from stock_analysis.domains.portfolio.performance import _cash, position_change, POSITION_TYPES, TRADE_TYPES


TRANSFERS = {'bank_transfer_in', 'bank_transfer_out'}
INCOME = {'dividend', 'dividend_tax'}


def security_key(row):
    return row['exchange'], row['code'], row['asset_type']


def advance_account(snapshot, source_positions, entries):
    positions = {security_key(row): dict(row) for row in source_positions}
    cash = Decimal(snapshot['confirmed_cash']) if snapshot['confirmed_cash'] is not None else None
    rates = {row['exchange_rate'] for row in source_positions
             if row['asset_type'] == 'hong_kong' and position_included(row)}
    ids, issues, activities = [], [], []
    missing_fees = 0
    for entry in entries:
        event = dict(entry, event_type='stock_credit') if entry['source_label'] == '红股入账' else entry
        kind = event['event_type']
        if kind not in POSITION_TYPES | TRANSFERS | INCOME:
            continue
        key = security_key(event)
        if kind not in TRANSFERS and key[2] not in {'stock', 'etf', 'hong_kong'}:
            continue
        if kind in TRANSFERS:
            amount = Decimal(event['net_cash']) if event['net_cash'] is not None else None
        else:
            amount = _cash(event)
        if kind in TRADE_TYPES and event['net_cash'] is None and event['fees'] is None:
            missing_fees += 1
        ids.append(event['id'])
        if amount is None:
            issues.append(f"{event['event_date']} {event['code']} 缺少净收付")
        cash = cash + amount if cash is not None and amount is not None else None
        if kind not in TRANSFERS:
            activities.append(event)
        if kind not in POSITION_TYPES:
            continue
        exchange, code, asset_type = key
        original = positions.get(key)
        if original and not position_included(original):
            raise ValueError(f'{snapshot["broker"]} {code} 的券商持仓需要核对')
        old_qty = original['quantity'] if original else 0
        if not event['quantity']:
            raise ValueError(f"{event['event_date']} {code} 缺少成交数量")
        quantity = old_qty + position_change(event)
        if quantity < 0:
            raise ValueError(f'{snapshot["broker"]} {code} 卖出数量超过可推算持仓')
        cost = position_cost(original) if original and old_qty else Decimal(0)
        if kind == 'sell':
            cost = cost * quantity / old_qty if cost is not None else None
        elif kind != 'stock_credit':
            cost = cost - amount if cost is not None and amount is not None else None
        hk = asset_type == 'hong_kong'
        rate = original.get('exchange_rate') if original else next(iter(rates)) if hk and len(rates) == 1 else None
        positions[key] = {**(original or {}), 'exchange': exchange, 'code': code, 'asset_type': asset_type,
                          'name': event['name'] or (original or {}).get('name') or code,
                          'currency': 'HKD' if hk else 'CNY', 'quantity': quantity, 'book_projected': True,
                          'cost_amount': str(cost) if cost is not None else None, 'exchange_rate': rate,
                          'available_quantity': None, 'holding_pnl': None, 'reference': None}
    if missing_fees:
        issues.append(f'{snapshot["broker"]} {missing_fees} 笔成交缺净收付及费用，按成交额推算')
    return {'snapshot': snapshot, 'positions': positions, 'cash': format(cash, '.2f') if cash is not None else None,
            'ledger_ids': ids, 'activities': activities, 'issues': issues}
