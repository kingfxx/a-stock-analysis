"""Read and mark portfolio balances advanced from confirmed broker closes."""

from collections import defaultdict
from datetime import timedelta, time
from decimal import Decimal

from stock_analysis.domains.portfolio.calculations import (
    combine_positions, daily_change, position_cost, position_included, snapshot_totals, sum_known,
)
from stock_analysis.domains.portfolio.holdings_projection import advance_account, security_key
from stock_analysis.domains.portfolio.performance import POSITION_TYPES, _cash, position_change
from stock_analysis.domains.portfolio.revaluation import read_revaluation_prices


def read_accounts(reader, payload, through):
    if not payload['snapshot']:
        return []
    if payload['scope'] == 'combined':
        positions = defaultdict(list)
        for row in payload['positions']:
            for part in row.get('accounts', [row]):
                positions[part['broker']].append(part)
        sources = [(entry['snapshot'], positions[entry['snapshot']['broker']]) for entry in payload['funds']]
    else:
        sources = [(payload['snapshot'], payload['positions'])]
    accounts = []
    for snapshot, held in sources:
        rows = reader.conn.execute("""
            SELECT id,event_date,event_type,source_label,exchange,code,asset_type,name,quantity,net_cash,gross_amount,fees
            FROM portfolio_ledger_entries WHERE account_id=? AND duplicate_of_id IS NULL
            AND event_date>? AND event_date<=? ORDER BY event_date,coalesce(event_time,'23:59:59'),id
        """, (snapshot['account_id'], snapshot['snapshot_date'], through))
        accounts.append(advance_account(snapshot, held, rows))
    return accounts


def read_quotes(reader, positions, now):
    cutoff = time(16, 15) if any(row['asset_type'] == 'hong_kong' for row in positions) else time(15)
    end = now.date() - timedelta(days=int(now.time() < cutoff))
    prices, dates = read_revaluation_prices(reader, positions, (end - timedelta(days=15)).isoformat(), end.isoformat())
    return prices, sorted(day for day in dates if day <= end.isoformat())


def quote_for_close(accounts, prices, days, today):
    if days and days[-1] == today and all(
        today in prices.get(key, {}) for account in accounts for key, row in account['positions'].items()
        if row['quantity'] and position_included(row)
    ):
        return today
    return next((day for day in reversed(days) if day < today), None)


def _marked(account, prices, days, quote, through):
    positions = []
    daily_by_security = defaultdict(list)
    for event in account['activities']:
        if event['event_date'] == quote:
            daily_by_security[security_key(event)].append(event)
    daily_values = []
    for key, row in account['positions'].items():
        if not position_included(row):
            positions.append(row)
            continue
        quantity = row['quantity']
        series = prices.get(key, {})
        close = series.get(quote)
        fx = Decimal(row['exchange_rate']) if key[2] == 'hong_kong' and row['exchange_rate'] else (
            None if key[2] == 'hong_kong' else Decimal(1))
        value = Decimal(0) if not quantity else close * quantity * fx if close is not None and fx else None
        cost = position_cost(row)
        events = daily_by_security[key]
        daily = opening = None
        if quote and through == quote and value is not None and account['snapshot']['snapshot_date'] < quote:
            calendar = series if key[2] == 'hong_kong' else days
            prior = max((day for day in calendar if day < quote), default=None)
            previous = series.get(prior)
            opening_qty = quantity - sum(position_change(event) for event in events if event['event_type'] in POSITION_TYPES)
            opening = Decimal(0) if not opening_qty else previous * opening_qty * fx if previous is not None and fx else None
            amounts = [_cash(event) for event in events]
            if opening is not None and None not in amounts:
                daily = value - opening + sum(amounts)
        daily_values.append({'daily_pnl': format(daily, '.2f') if daily is not None else None})
        if quantity:
            pnl = value - cost if value is not None and cost is not None else None
            positions.append({**row, 'broker': account['snapshot']['broker'], 'reference': None, 'book_projected': True,
                              'price': str(close) if close is not None else None,
                              'cost_price': format(cost / quantity / fx, '.6f') if cost is not None and fx else None,
                              'market_value': format(value, '.2f') if value is not None else None,
                              'holding_pnl': format(pnl, '.2f') if pnl is not None else None,
                              'holding_pnl_rate': format(pnl * 100 / abs(cost), '.2f') if cost and pnl is not None else None,
                              'daily_pnl': format(daily, '.2f') if daily is not None else None,
                              'daily_pnl_rate': format(daily * 100 / opening, '.2f') if opening and daily is not None else None,
                              'daily_pnl_basis': 'ledger_projection' if daily is not None else None})
    if quote and through == quote and account['snapshot']['snapshot_date'] < quote:
        for key in daily_by_security.keys() - account['positions'].keys():
            amounts = [_cash(event) for event in daily_by_security[key]]
            daily_values.append({'daily_pnl': format(sum(amounts), '.2f') if None not in amounts else None})
        return positions, sum_known(daily_values, 'daily_pnl')
    return positions, None


def build_book(payload, accounts, prices, days, quote, through):
    marked = [_marked(account, prices, days, quote, through) for account in accounts]
    positions = [row for rows, _ in marked for row in rows]
    if payload['scope'] == 'combined':
        positions = combine_positions(positions, quote)
    cash = sum_known(accounts, 'cash') if not payload.get('missing_accounts') else None
    snapshot = {'confirmed_cash': str(cash) if cash is not None else None}
    totals = snapshot_totals(snapshot, positions)
    daily = sum_known([{'pnl': str(value) if value is not None else None} for _, value in marked], 'pnl')
    if payload.get('missing_accounts'):
        daily = None
    totals.update(daily_change(totals['assets'], daily))
    issues = [issue for account in accounts for issue in account['issues']]
    missing_quotes = [row['code'] for row in positions if row['included'] and row['price'] is None]
    missing_fx = [row['code'] for row in positions if row['included'] and row['asset_type'] == 'hong_kong'
                  and row['exchange_rate'] is None]
    if missing_quotes:
        issues.append(f'缺收盘价：{"、".join(missing_quotes)}')
    if missing_fx:
        issues.append(f'缺港股快照汇率：{"、".join(missing_fx)}')
    return dict(positions=positions, totals=totals, position_date=through, ledger_ids=sorted({
        entry_id for account in accounts for entry_id in account['ledger_ids']
    }), issues=issues)


def book_view(payload, book, quote, reference, *, estimate_id=None):
    snapshot = payload['snapshot']
    positions = [{**row, 'reference': reference(row, quote) if quote and row['included'] else None}
                 for row in book['positions']]
    return {**payload, 'snapshot': {**snapshot, 'snapshot_date': quote or book['position_date'],
                                   'broker_assets': None, 'session': 'close'},
            'positions': positions, 'totals': book['totals'], 'estimate_id': estimate_id,
            'live': estimate_id is None, 'quote_date': quote, 'position_date': book['position_date'],
            'basis_date': snapshot['snapshot_date'], 'projection_issues': book['issues'],
            'funds': payload.get('funds', [{'snapshot': snapshot, 'reconciliation': payload.get('reconciliation')}])}
