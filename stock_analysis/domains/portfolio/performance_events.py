"""Normalize the observed CMB history format for securities performance."""

from collections import defaultdict
from datetime import date, timedelta

from stock_analysis.domains.portfolio.performance import _cash


# Shanghai IPO payment codes differ from the listed security codes.
IPO_CODES = {'787428': '688428', '732391': '603391'}
IPO_BOOKKEEPING = {'申购配号', '市值申购中签', '市值申购中签扣款', '市值申购中签扣款回冲'}
SETTLEMENT = {'settlement_adjustment', 'settlement_cancellation'}


def normalize_events(events, prices, start=None):
    by_code = defaultdict(list)
    for event in events:
        by_code[event['security'][1]].append(event)
    replacements = {}
    consumed = set()
    for code, records in by_code.items():
        for payment in (row for row in records if row['source_label'] == '新股申购确认缴款'):
            market, _ = payment['security']
            security = (market or 'sh', IPO_CODES.get(code, code))
            listing = min((day for day in prices.get(security, {}) if start is None or day >= start), default=None)
            quantity = payment['quantity']
            transfers = [row for row in records if row['source_label'] == '托管转出'
                         and row['event_date'] == payment['event_date'] and quantity
                         and row['quantity'] == -quantity]
            credits = [row for row in by_code[security[1]] if row['source_label'] == '新股入账'
                       and listing and payment['event_date'] <= row['event_date'] <= listing
                       and row['quantity'] == quantity]
            if not listing or payment['event_date'] >= listing or len(transfers) != 1 or len(credits) > 1:
                continue
            replacements[payment['id']] = dict(payment, event_type='subscription', event_date=listing,
                                               security=security)
            consumed.update(row['id'] for row in transfers + credits)
    for credit in (row for row in by_code['601015'] if row['source_label'] == '配股入账'):
        payments = [row for row in by_code['760015'] if row['source_label'] == '配股/配债'
                    and row['quantity'] == credit['quantity']
                    and 0 <= (date.fromisoformat(credit['event_date']) -
                              date.fromisoformat(row['event_date'])).days <= 14]
        if len(payments) == 1:
            replacements[credit['id']] = dict(credit, event_type='subscription',
                                              gross_amount=payments[0]['gross_amount'])
            consumed.add(payments[0]['id'])
    return [
        replacements.get(row['id'], dict(row, event_type='stock_credit') if row['source_label'] == '红股入账' else row)
        for row in events if row['id'] not in consumed and row['source_label'] not in IPO_BOOKKEEPING
        and row['source_label'] != '权证上账'
    ]


def reconcile_etf_dividends(events, dividends):
    booked = [row for row in events if row['asset_type'] == 'etf' and row['event_type'] == 'dividend']
    matches = []
    candidate_counts = defaultdict(int)
    matched = set()
    for dividend in dividends:
        end = (date.fromisoformat(dividend['payment_date']) + timedelta(days=3)).isoformat()
        candidates = [row for row in booked if row['security'] == dividend['security']
                      and dividend['ex_date'] <= row['event_date'] <= end]
        matches.append(candidates)
        for row in candidates:
            candidate_counts[row['id']] += 1
    for dividend, candidates in zip(dividends, matches):
        dividend['booked'] = None
        if candidates:
            row = candidates[0]
            if len(candidates) == candidate_counts[row['id']] == 1 and _cash(row) is not None:
                dividend['booked'] = _cash(row)
                matched.add(row['id'])
            else:
                dividend['unmatched'] = True
    for row in events:
        if row['asset_type'] != 'etf' or row['event_type'] not in SETTLEMENT:
            continue
        if row['quantity'] or row['net_cash'] not in (None, '0'):
            continue
        amount = _cash(row)
        if amount == 0 or any(
            credit['security'] == row['security'] and _cash(credit) == amount
            and abs((date.fromisoformat(credit['event_date']) - date.fromisoformat(row['event_date'])).days) <= 14
            for credit in booked
        ):
            matched.add(row['id'])
    return [row for row in events if row['id'] not in matched]
