"""Reconstruct account cash from a confirmed balance and recorded cash movements."""

from bisect import bisect_left
from collections import defaultdict
from decimal import Decimal

from stock_analysis.domains.portfolio.performance import TRADE_TYPES, _cash


TRANSFERS = {'bank_transfer_in', 'bank_transfer_out'}
CNY_SECURITIES = {'stock', 'etf'}
SUBSCRIPTION_PAYMENTS = {'新股申购确认缴款', '配股/配债'}


def reconstruct_cash(days, confirmed_cash, entries):
    balances = {day: None for day in days}
    if confirmed_cash is None:
        return balances
    flow = defaultdict(Decimal)
    gaps = set()
    for entry in entries:
        event_type = entry['event_type']
        is_transfer = event_type in TRANSFERS
        recorded_net = entry['net_cash']
        if is_transfer:
            amount = Decimal(recorded_net) if recorded_net is not None else None
        elif entry['asset_type'] in CNY_SECURITIES and event_type in TRADE_TYPES | {'dividend', 'dividend_tax'}:
            amount = _cash(entry)
        elif entry['source_label'] in SUBSCRIPTION_PAYMENTS:
            gross = entry['gross_amount']
            amount = Decimal(recorded_net) if recorded_net is not None else -abs(Decimal(gross)) if gross is not None else None
        elif entry['asset_type'] in CNY_SECURITIES and recorded_net is not None:
            amount = Decimal(recorded_net)
        else:
            continue
        # A non-session cash movement first appears in the next available close.
        index = bisect_left(days, entry['event_date'])
        if index < len(days):
            if amount is None:
                gaps.add(index)
            else:
                flow[index] += amount
    cash = Decimal(confirmed_cash)
    for index in range(len(days) - 1, -1, -1):
        balances[days[index]] = format(cash, '.2f')
        if index in gaps:
            break
        cash -= flow[index]
    return balances


def attach_cash(records, balances):
    for row in records:
        cash = balances.get(row['date'])
        market = row['market_value']
        row['cash_balance'] = cash
        row['total_assets'] = format(Decimal(cash) + Decimal(market), '.2f') if cash is not None and market is not None else None
