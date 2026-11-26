"""Normalize explicit cash and non-cash fields from Eastmoney Hong Kong F10."""

import re
from datetime import date
from decimal import Decimal

SOURCE = 'eastmoney:RPT_HKF10_MAIN_DIVBASIC'
VERSION = 'hk_f10_dividend_v1'
INTERFACE = 'RPT_HKF10_MAIN_DIVBASIC'
CURRENCIES = {'港元': 'HKD', '港币': 'HKD', '人民币': 'CNY', '美元': 'USD'}
MONEY = r'(港元|港币|人民币|美元)\s*(\d+(?:\.\d+)?)\s*元?'


def _day(value):
    return date.fromisoformat(value[:10].replace('/', '-')).isoformat() if value else None


def _payment_day(value):
    if value and ('：' in value or ':' in value[:10]):
        cash = re.search(r'(?:^|\n)现金[：:]\s*(\d{4}[-/]\d{2}[-/]\d{2})', value)
        return _day(cash[1]) if cash else None
    return _day(value)


def dividend_page(payload, code, page, page_size):
    result = payload.get('result') or {}
    count, pages = int(result['count']), int(result['pages'])
    rows = result.get('data')
    if (payload.get('success') is not True or not isinstance(rows, list) or count < 0
            or pages != (count + page_size - 1) // page_size
            or len(rows) != min(page_size, max(0, count - (page - 1) * page_size))
            or any(row.get('SECUCODE') != code + '.HK' for row in rows)):
        raise ValueError('东财港股分红分页或证券不一致')
    return count, pages, rows


def normalize_events(rows):
    events = {}
    for row in rows:
        ex = _day(row['EX_DIVIDEND_DATE'])
        if not ex:
            continue
        plan = row['PLAN_EXPLAIN']
        notice, payment = _day(row['NOTICE_DATE']), _payment_day(row['DIVIDEND_DATE'])
        declared = re.match(r'每股\s*派\s*' + MONEY, plan)
        equivalent = re.search(r'相当于\s*' + MONEY, plan) if declared else None
        currency = CURRENCIES[declared[1]] if declared else None
        paid_currency, amount = None, None
        if equivalent and '计算值' not in plan:
            paid_currency, amount = CURRENCIES[equivalent[1]], equivalent[2]
        elif currency == 'HKD':
            paid_currency, amount = currency, declared[2]
        key = ':'.join((row['YEAR'], row['REPORT_TYPE'], ex))
        event = {'event_key': key, 'status': 'cash' if declared else 'unclassified',
                 'notice_date': notice, 'ex_dividend_date': ex, 'payment_date': payment,
                 'declared_currency': currency, 'declared_per_share': declared[2] if declared else None,
                 'payment_currency': paid_currency, 'payment_per_share': amount,
                 'cash_per_ten': float(Decimal(amount) * 10) if amount and paid_currency == 'HKD' else None,
                 'raw_json': row}
        if key in events and events[key] != event:
            raise ValueError('东财港股分红同日事件存在歧义')
        events[key] = event
    return list(events.values())
