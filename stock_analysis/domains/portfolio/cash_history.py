"""Reconstruct account cash from a confirmed balance and recorded cash movements."""

from bisect import bisect_left
from collections import defaultdict
from decimal import Decimal

from stock_analysis.domains.portfolio.performance import TRADE_TYPES, _cash


TRANSFERS = {'bank_transfer_in', 'bank_transfer_out'}
CNY_SECURITIES = {'stock', 'etf'}
SUBSCRIPTION_PAYMENTS = {'新股申购确认缴款', '配股/配债'}


def reconstruct_cash(days, confirmed_cash, entries, *, anchor_day=None, hk_security=None):
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
        elif event_type in TRADE_TYPES | {'dividend', 'dividend_tax'} and (
            entry['asset_type'] in CNY_SECURITIES or (
                hk_security is not None and entry['asset_type'] == 'hong_kong'
                and (entry['exchange'], entry['code']) == hk_security
            )
        ):
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
    anchor = days.index(anchor_day) if anchor_day is not None else len(days) - 1
    cash = Decimal(confirmed_cash)
    for index in range(anchor, -1, -1):
        balances[days[index]] = format(cash, '.2f')
        if index in gaps:
            break
        cash -= flow[index]
    cash = Decimal(confirmed_cash)
    for index in range(anchor + 1, len(days)):
        if index in gaps:
            break
        cash += flow[index]
        balances[days[index]] = format(cash, '.2f')
    return balances


def attach_cash(records, balances):
    for row in records:
        cash = balances.get(row['date'])
        market = row['market_value']
        row['cash_balance'] = cash
        row['total_assets'] = format(Decimal(cash) + Decimal(market), '.2f') if cash is not None and market is not None else None


def account_return_with_hk(domestic, mixed, accounts):
    days = [row['date'] for row in mixed['records']]
    projections = []
    initial_assets = []
    for account in accounts:
        opening = account['opening_day']
        anchor = account['anchor_day']
        cash_days = sorted({opening, anchor} | {day for day in days if opening <= day <= account['as_of']})
        balances = reconstruct_cash(cash_days, account['confirmed_cash'], account['ledger'],
                                    anchor_day=anchor, hk_security=account['hk_security'])
        projections.append((account, balances))
        if account['start'] == domestic['start']:
            cash = balances[opening]
            market = account['opening_market_value']
            if account['opening_assumed_zero']:
                initial_assets.append(Decimal(0))
            elif cash is None or market is None:
                initial_assets.append(None)
            else:
                assets = Decimal(cash) + Decimal(market)
                initial_assets.append(assets if assets >= 0 else None)
    opening_assets = None
    if mixed['start'] == domestic['start'] and initial_assets and None not in initial_assets:
        opening_assets = format(sum(initial_assets), '.2f')
    merged_cash = {}
    for day in days:
        parts = [balances.get(day) for account, balances in projections if account['start'] <= day]
        merged_cash[day] = format(sum(map(Decimal, parts)), '.2f') if parts and None not in parts else None
    records = [{key: row[key] for key in ('date', 'market_value')} for row in mixed['records']]
    attach_cash(records, merged_cash)
    return {
        'records': records, 'transfers': [flow for flow in domestic['transfers'] if flow['date'] <= mixed['as_of']],
        'opening_assets': opening_assets, 'opening_date': domestic['opening_date'],
        'opening_assumed_zero': domestic['opening_assumed_zero'],
        'return_gaps': domestic['return_gaps'],
    }
