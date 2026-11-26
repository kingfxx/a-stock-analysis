"""Pure HKEXnews issuer resolution and filing metadata parsing."""

import json
import re
from datetime import date, datetime, timedelta, timezone

SOURCE = 'hkexnews:title-search'
VERSION = 'hkex_title_search_v1'
HOST = 'https://www1.hkexnews.hk'
INTERFACE = 'titleSearchServlet.do'
LOCAL = timezone(timedelta(hours=8))
MONTHS = {name: month for month, name in enumerate(
    ('january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
     'september', 'october', 'november', 'december'), 1)}


def issuer_id(text, code):
    match = re.fullmatch(r'(?:callback|stockIdentity)\((.*)\);?\s*', text, re.S)
    if not match:
        raise ValueError('披露易证券检索格式变化')
    found = {str(item['stockId']) for item in json.loads(match[1])['stockInfo'] if item['code'] == code}
    if len(found) != 1 or not next(iter(found)).isdigit():
        raise ValueError('披露易证券身份未唯一确认')
    return found.pop()


def _period(title):
    match = re.search(r'\b(?:ended|ending)\s+(\d{1,2})\s+([a-z]+)\s+(20\d{2})\b', title.lower())
    if match and match[2] in MONTHS:
        return date(int(match[3]), MONTHS[match[2]], int(match[1])).isoformat()
    return None


def filings(payload, code, start, end):
    records = json.loads(payload['result'])
    count = int(payload['recordCnt'])
    if not isinstance(records, list) or len(records) != int(payload['loadedRecord']) or count < len(records):
        raise ValueError('披露易公告目录计数不一致')
    if payload['hasNextRow'] != (count > len(records)):
        raise ValueError('披露易公告目录截断状态不一致')
    if count > len(records):
        return None
    result, seen = [], set()
    for row in records:
        if code not in re.split(r'[\s,;]+', row['STOCK_CODE'].strip()):
            raise ValueError('披露易返回了不同证券的公告')
        key = row['NEWS_ID']
        instant = datetime.strptime(row['DATE_TIME'], '%d/%m/%Y %H:%M').replace(tzinfo=LOCAL)
        day = instant.date().isoformat()
        link = row['FILE_LINK']
        if not key.isdigit() or key in seen or not start <= day <= end or not link.startswith('/listedco/'):
            raise ValueError('披露易公告身份、日期或链接无效')
        seen.add(key)
        title = ' '.join(row['TITLE'].split())
        headings = row['LONG_TEXT'].lower()
        if 'final results' in headings or 'annual report' in headings:
            category = 'annual'
        elif 'interim results' in headings or 'half-year report' in headings:
            category = 'interim'
        elif 'quarterly' in headings:
            category = 'quarterly'
        elif 'dividend or distribution' in headings:
            category = 'dividend'
        else:
            continue
        result.append({'id': key, 'title': title, 'date': day, 'time': int(instant.timestamp() * 1000),
                       'url': link, 'category': category, 'period': _period(title),
                       'revised': bool(re.search(r'\b(?:revised|updated|correction|clarification)\b', title, re.I))})
    return result
