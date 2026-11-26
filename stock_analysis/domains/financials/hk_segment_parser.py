"""Parse the publicly displayed fiscal-year segment and geography revenue tables."""

import re
from datetime import datetime
from decimal import Decimal
from html.parser import HTMLParser

SOURCE = 'stockanalysis:hk-segment-revenue'
VERSION = 'hk_segment_revenue_v1'
INTERFACE = 'financials/metrics/'
TABLES = {'main-table-revenue-by segment': '1', 'main-table-revenue-by geography': '3'}
SCALE = {'K': 1000, 'M': 1000000, 'B': 1000000000, 'T': 1000000000000}


class RevenueTable(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables, self.text = {}, []
        self.dimension = None
        self.row = self.cell = None
        self.hidden = False

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden = True
        if tag == 'table':
            self.dimension = TABLES.get(dict(attrs).get('id'))
            if self.dimension:
                if self.dimension in self.tables:
                    raise ValueError('港股主营表重复')
                self.tables[self.dimension] = []
        elif self.dimension and tag == 'tr':
            self.row = []
        elif self.row is not None and tag in ('td', 'th'):
            self.cell = []

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = False
        if tag in ('td', 'th') and self.cell is not None:
            self.row.append(' '.join(' '.join(self.cell).split()))
            self.cell = None
        elif tag == 'tr' and self.row is not None:
            self.tables[self.dimension].append(self.row)
            self.row = None
        elif tag == 'table':
            self.dimension = None

    def handle_data(self, data):
        if not self.hidden:
            if self.cell is not None:
                self.cell.append(data)
            self.text.append(data)


def _amount(text, scale):
    if text in ('', '-', '—', 'N/A'):
        return None
    match = re.fullmatch(r'(-?\d+(?:\.\d+)?)([KMBT])?', text.replace(',', '').replace('−', '-'))
    if not match:
        raise ValueError('港股主营收入字段无效')
    return Decimal(match[1]) * (SCALE[match[2]] if match[2] else scale)


def parse_segments(text, code):
    document = RevenueTable()
    document.feed(text)
    flat = ' '.join(' '.join(document.text).split())
    if not re.search(r'\(HKG:' + code[1:] + r'\)', flat):
        raise ValueError('港股主营页面未确认证券身份')
    units = set(re.findall(r'Financials in (thousands|millions|billions) ([A-Z]{3})\.', flat))
    if len(units) != 1 or not document.tables:
        raise ValueError('港股主营页面缺少可确认的币种、单位或收入表')
    unit, currency = units.pop()
    scale = {'thousands': 1000, 'millions': 1000000, 'billions': 1000000000}[unit]
    periods = {}
    for dimension, rows in document.tables.items():
        if len(rows) < 3 or rows[0][0] != 'Fiscal Year' or rows[1][0] != 'Period Ending':
            raise ValueError('港股主营收入表头变化')
        years, dates = rows[0][1:], rows[1][1:]
        if len(years) != len(dates) or any(len(row) != len(years) + 1 for row in rows[2:]):
            raise ValueError('港股主营收入列数不匹配')
        for column, (year, label) in enumerate(zip(years, dates), 1):
            if not re.fullmatch(r'FY \d{4}', year):
                continue
            match = re.search(r'([A-Z][a-z]{2} \d{1,2}, \d{4})$', label)
            if not match:
                raise ValueError('港股主营报告期缺少实际日期')
            period = datetime.strptime(match[1], '%b %d, %Y').date().isoformat()
            values = {row[0]: _amount(row[column], scale) for row in rows[2:]}
            if len(values) != len(rows) - 2 or '' in values:
                raise ValueError('港股主营项目名称无效或重复')
            total = values.pop('Total', None)
            complete = total is not None and total > 0 and all(value is not None for value in values.values())
            for rank, (name, value) in enumerate(values.items(), 1):
                if value is not None:
                    periods.setdefault(period, []).append({
                        'report_date': period, 'report_name': year, 'dimension_type': dimension,
                        'item_key': name, 'item_name': name, 'item_code': None, 'display_rank': rank,
                        'revenue': str(value), 'revenue_share': str(value / total) if complete else None})
    if not periods:
        raise ValueError('港股主营收入表无有效年度数据')
    return currency, periods
