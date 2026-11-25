"""Pure validation for the display-only aluminum market observations."""
from datetime import date, datetime
from html.parser import HTMLParser
from math import isfinite
import re
import unicodedata


class _MarketRows(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self._row = {}
        elif tag == 'td' and self._row is not None:
            self._cell = (dict(attrs).get('class', '').split(), [])

    def handle_data(self, data):
        if self._cell is not None:
            self._cell[1].append(data)

    def handle_endtag(self, tag):
        if tag == 'td' and self._cell is not None:
            classes, chunks = self._cell
            for name in classes:
                self._row[name] = ''.join(chunks).strip()
            self._cell = None
        elif tag == 'tr' and self._row is not None:
            self.rows.append(self._row)
            self._row = None
            self._cell = None


def _price(text):
    value = float(text.replace(',', ''))
    if not isfinite(value) or value <= 0:
        raise ValueError('报价须为正数')
    return value


def parse_spot(html, as_of):
    parser = _MarketRows()
    parser.feed(html)
    rows = [row for row in parser.rows
            if unicodedata.normalize('NFKC', row.get('product', '')) == 'A00铝锭(华东)']
    if len(rows) != 1 or '长江综合 ● 基础金属' not in html:
        raise ValueError('未定位到唯一的长江综合华东 A00 铝锭报价')
    row = rows[0]
    try:
        low, high = (_price(value) for value in re.split(r'\s*[-–—]\s*', row['range']))
        midpoint = _price(row['mid'])
        trade_day = datetime.strptime(row['date'], '%d/%m/%Y').date()
        if row.get('unitc') != '元/吨' or trade_day > as_of or not low <= midpoint <= high:
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('华东 A00 铝锭报价区间、单位或日期无效') from exc
    return dict(trade_date=trade_day.isoformat(), price=midpoint, low=low, high=high, unit='元/吨')


def parse_monthly(payload, as_of):
    if not isinstance(payload, list) or not payload:
        raise ValueError('沪铝连续日线响应结构无效')
    month_number = as_of.year * 12 + as_of.month - 1
    months = [f'{year:04d}-{month + 1:02d}' for year, month in
              (divmod(index, 12) for index in range(month_number - 11, month_number + 1))]
    earliest = date.fromisoformat(months[0] + '-01')
    points = {}
    seen = set()
    for row in payload:
        try:
            trade_day = date.fromisoformat(row['d'])
            if trade_day < earliest:
                continue
            period = trade_day.isoformat()[:7]
            open_price, close_price, maximum, minimum = (_price(row[key]) for key in ('o', 'c', 'h', 'l'))
            if (trade_day > as_of or trade_day in seen or minimum > min(open_price, close_price)
                    or maximum < max(open_price, close_price)):
                raise ValueError
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError('沪铝连续交易日或价格无效') from exc
        seen.add(trade_day)
        if period not in points or points[period]['trade_date'] < trade_day.isoformat():
            points[period] = dict(month=period, trade_date=trade_day.isoformat(), close=close_price)
    if not points:
        raise ValueError('最近 12 个月暂无有效沪铝主连月线')
    return dict(points=[points.get(month, dict(month=month, trade_date=None, close=None)) for month in months],
                trade_date=max(point['trade_date'] for point in points.values()), unit='元/吨',
                current_month=months[-1])
