"""Pure reconstruction of daily securities P&L from a closing position and ledger."""

from bisect import bisect_left
from collections import defaultdict
from decimal import Decimal


TRADE_TYPES = {'buy', 'sell'}
ACQUISITIONS = {'buy', 'subscription'}
POSITION_TYPES = TRADE_TYPES | {'subscription', 'stock_credit'}
SUPPORTED_TYPES = POSITION_TYPES | {'dividend', 'dividend_tax'}
CENT = Decimal('0.01')


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


def _market_value(held, prices, day, components, sign):
    value = Decimal(0)
    missing = {}
    for key, quantity in held.items():
        if not quantity:
            continue
        close = prices.get(key, {}).get(day)
        if close is None:
            missing[key] = quantity
        else:
            amount = close * quantity
            value += amount
            components[key] += sign * amount
    return value, missing


def _daily_values(before, held, prices, price_days, previous, day, issues, components):
    opening, missing_open = _market_value(before, prices, previous, components, -1)
    closing, missing_close = _market_value(held, prices, day, components, 1)
    missing_keys = missing_open.keys() | missing_close.keys()
    estimates = []

    def resolve(value, missing, target, sign):
        if not missing:
            return value
        if len(missing_keys) == 1:
            key, quantity = next(iter(missing.items()))
            known = price_days.get(key, ())
            offset = bisect_left(known, target)
            if offset:
                quote_day = known[offset - 1]
                estimates.append(dict(code=key[1], date=target, price_date=quote_day))
                amount = quantity * prices[key][quote_day]
                components[key] += sign * amount
                return value + amount
        issues.update(f'{key[1]} 缺少 {target} 收盘价' for key in missing)
        return None

    return resolve(opening, missing_open, previous, -1), resolve(closing, missing_close, day, 1), estimates


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
        components = defaultdict(Decimal)
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
            if value is not None:
                components[key] += value
        if any(quantity < 0 for quantity in before.values()):
            return {'records': [], 'blocked_dates': [day]}
        opening_value, closing_value, estimates = _daily_values(
            before, held, prices, price_days, previous, day, issues, components)
        results.append({
            'date': day, 'issues': issues, 'opening': opening_value, 'closing': closing_value,
            'cash': cash, 'buys': buys, 'income': income,
            'trades': sum(event['event_type'] in TRADE_TYPES for event in trades),
            'fee_gap_count': sum(event['event_type'] in TRADE_TYPES and event['fees'] is None and event['net_cash'] is None
                                 for event in trades),
            'estimated_closes': estimates,
            'security_pnl': components,
        })
        held = before
    registered[days[0]] = held
    by_day = {row['date']: row for row in results}
    for event in dividends:
        row = by_day[event['ex_date']]
        key = event['security']
        if event['booked'] is not None:
            row['income'] += event['booked']
            row['security_pnl'][key] += event['booked']
        elif event.get('unmatched'):
            row['issues'].add(f'{key[1]} ETF 分红到账匹配待核对')
        elif event['registration_date'] not in registered:
            row['issues'].add(f'{key[1]} 缺少 ETF 分红登记日持仓')
        else:
            amount = (event['cash_per_unit'] *
                      registered[event['registration_date']].get(key, 0)).quantize(Decimal('0.01'))
            row['income'] += amount
            row['security_pnl'][key] += amount
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
        pnl = (closing_value - opening + cash + income).quantize(CENT) if not issues else None
        capital = opening + buys if pnl is not None else None
        rate = pnl / capital if capital is not None and capital > 0 else None
        if pnl is not None:
            cumulative += pnl
        components = row.pop('security_pnl')
        if pnl is not None and components:
            cents = {key: int(amount.quantize(CENT) * 100) for key, amount in components.items() if amount}
            residual = int(pnl * 100) - sum(cents.values())
            if residual:
                largest = max(cents, key=lambda key: abs(components[key]))
                cents[largest] += residual
            row['security_pnl'] = {key: amount for key, amount in cents.items() if amount}
        row.update(
            pnl=str(pnl) if pnl is not None else None, issues=sorted(issues),
            opening_market_value=str(opening) if opening is not None else None,
            market_value=str(closing_value.quantize(Decimal('0.01'))) if closing_value is not None else None,
            return_base=str(capital.quantize(Decimal('0.01'))) if capital is not None else None,
            return_rate=str(rate) if rate is not None else None,
            dividend_net=str(income.quantize(Decimal('0.01'))),
            estimated_dividend=str(row.get('estimated_dividend', '0.00')),
            cumulative=str(cumulative) if pnl is not None else None,
        )
    return {'records': results, 'blocked_dates': []}


def merge_performance(reports):
    """Include accounts from their individual start dates, preserving later gaps."""
    base = {'broker': '合并盈亏', 'records': []}
    if not reports or any(report.get('error') for report in reports):
        gaps = '、'.join(report['broker'] for report in reports if report.get('error'))
        return {**base, 'error': f'{gaps}缺少可复算资料' if gaps else '暂无券商账户资料'}
    start = min(report['start'] for report in reports)
    end = max(report['as_of'] for report in reports)

    def merge_scope(scopes):
        by_account = [{row['date']: row for row in scope['records']} for scope in scopes]
        dates = sorted(set().union(*by_account))
        blocked = sorted({day for scope in scopes for day in scope['blocked_dates']})
        records = []
        cumulative = Decimal('0.00')
        for day in dates:
            active = [(report['broker'], rows.get(day)) for report, rows in zip(reports, by_account)
                      if report['start'] <= day]
            daily = [entry for _, entry in active]
            issues = []
            for broker, row in active:
                reasons = row['issues'] if row else ['缺少当日复算结果']
                issues.extend(f'{broker}：{reason}' for reason in reasons)
            row = {'date': day, 'issues': issues}
            for column in ('pnl', 'return_base', 'market_value', 'dividend_net', 'estimated_dividend',
                           'cash_balance', 'total_assets'):
                values = [entry.get(column) if entry else None for entry in daily]
                row[column] = str(sum(map(Decimal, values))) if None not in values else None
            pnl, capital = row['pnl'], row['return_base']
            if pnl is not None:
                cumulative += Decimal(pnl)
                components = defaultdict(int)
                for entry in daily:
                    for key, amount in entry.get('security_pnl', {}).items():
                        components[key] += amount
                row['security_pnl'] = components
            row.update(cumulative=str(cumulative) if pnl is not None else None,
                       return_rate=str(Decimal(pnl) / Decimal(capital)) if pnl is not None and capital and Decimal(capital) > 0 else None,
                       trades=sum(entry['trades'] for entry in daily if entry),
                       fee_gap_count=sum(entry['fee_gap_count'] for entry in daily if entry),
                       estimated_closes=[estimate for entry in daily if entry for estimate in entry['estimated_closes']])
            records.append(row)
        return {'records': records, 'blocked_dates': blocked, 'fee_gap_count': sum(row['fee_gap_count'] for row in records)}

    combined = merge_scope(reports)
    stocks = merge_scope([report['stocks'] for report in reports])
    if not combined['records'] and not stocks['records']:
        return {**base, 'error': '暂无可复算的交易日'}
    transfers = defaultdict(lambda: {'in': Decimal('0.00'), 'out': Decimal('0.00')})
    for report in reports:
        for transfer in report['transfers']:
            if start <= transfer['date'] <= end:
                for direction in ('in', 'out'):
                    transfers[transfer['date']][direction] += Decimal(transfer[direction])
    initial_reports = [report for report in reports if report['start'] == start]
    first = [report.get('opening_assets') for report in initial_reports]
    opening_assets = str(sum(map(Decimal, first))) if None not in first else None
    gaps = {day for report in reports for day in report.get('return_gaps', ())}
    gaps.update(report['start'] for report in reports
                if report['start'] > start and (report.get('opening_assets') is None or Decimal(report['opening_assets']) != 0))
    return {**base, **combined, 'stocks': stocks, 'start': start, 'as_of': end, 'year': int(end[:4]),
            'opening_assets': opening_assets, 'return_gaps': sorted(gaps),
            'opening_date': min(report.get('opening_date', start) for report in initial_reports),
            'opening_assumed_zero': any(report.get('opening_assumed_zero') for report in initial_reports),
            'trading_days': max(len(combined['records']), len(stocks['records'])),
            'transfers': [{'date': day, **{key: str(value) for key, value in amounts.items()}}
                          for day, amounts in sorted(transfers.items())]}


def attach_monthly_security_pnl(scope):
    """Summarize covered trading days in integer cents and discard the daily breakdown."""
    monthly = defaultdict(lambda: defaultdict(int))
    for row in scope['records']:
        components = row.pop('security_pnl', {})
        if components:
            month = monthly[row['date'][:7]]
            for (exchange, code), amount in components.items():
                month[f'{exchange}:{code}'] += amount
    scope['monthly_security_pnl'] = {month: {key: value for key, value in totals.items() if value}
                                     for month, totals in monthly.items()}
