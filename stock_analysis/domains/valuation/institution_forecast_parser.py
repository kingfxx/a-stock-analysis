"""Pure parsing of the THS annual profit consensus table; no I/O dependencies."""
from html.parser import HTMLParser
import math
import re


class _Table(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.row = None
        self.cell = None
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            self.depth += 1
            if self.depth != 1:
                raise ValueError('同花顺预测表包含嵌套表格')
        elif tag == 'tr':
            self._end_row()
            self.row = []
        elif tag in ('th', 'td'):
            self._end_cell()
            if self.row is None:
                raise ValueError('同花顺预测表行结构无效')
            attrs = dict(attrs)
            try:
                spans = tuple(int(attrs.get(key, '1')) for key in ('rowspan', 'colspan'))
            except (TypeError, ValueError):
                raise ValueError('同花顺预测表合并单元格无效') from None
            if not all(1 <= value <= 100 for value in spans):
                raise ValueError('同花顺预测表合并单元格无效')
            self.cell = [tag, [], *spans]
        elif tag == 'br' and self.cell is not None:
            self.cell[1].append(' ')

    def handle_endtag(self, tag):
        if tag in ('th', 'td'):
            self._end_cell()
        elif tag in ('tr', 'table'):
            self._end_row()
            if tag == 'table':
                self.depth -= 1

    def handle_data(self, data):
        if self.cell is not None:
            self.cell[1].append(data)

    def _end_cell(self):
        if self.cell is not None:
            tag, parts, rowspan, colspan = self.cell
            self.row.append((tag, ' '.join(''.join(parts).split()), rowspan, colspan))
            self.cell = None

    def _end_row(self):
        self._end_cell()
        if self.row:
            self.rows.append(self.row)
        self.row = None

    def values(self):
        self._end_row()
        # Expand spans exactly into six columns, rejecting collisions/truncation.
        occupied = {}
        result = []
        for index, row in enumerate(self.rows):
            column = 0
            for _, text, rowspan, colspan in row:
                while (index, column) in occupied:
                    column += 1
                for r in range(index, index + rowspan):
                    for c in range(column, column + colspan):
                        if c >= 6 or r >= len(self.rows) or (r, c) in occupied:
                            raise ValueError('同花顺预测表列结构无效')
                        occupied[r, c] = text
                column += colspan
            if any((index, c) not in occupied for c in range(6)):
                raise ValueError('同花顺预测表列结构无效')
            result.append([occupied.pop((index, c)) for c in range(6)])
        return result


def _number(text):
    try:
        value = float(text.replace(',', ''))
    except (ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def parse_forecast(html, code, years):
    title = re.search(r'<title\b[^>]*>(.*?)</title>', html, re.S | re.I)
    if not title or not re.search(rf'(?<!\d){re.escape(code)}(?!\d)', title[1]):
        raise ValueError('同花顺返回股票不匹配')
    if '本年度暂无机构做出业绩预测' in html:
        return [dict(year=y, profit=None, institutions=None) for y in years]
    selected = []
    for candidate in re.findall(r'<table\b[^>]*>.*?</table>', html, re.S | re.I):
        caption = re.search(r'<caption\b[^>]*>(.*?)</caption>', candidate, re.S | re.I)
        text = re.sub(r'<[^>]+>|\s+', '', caption[1]) if caption else ''
        if '单位：亿元' in text and '预测年报净利润' in text:
            selected.append(candidate)
    if len(selected) != 1:
        raise ValueError('同花顺净利润预测表或亿元单位未识别')
    table = _Table()
    table.feed(selected[0])
    table.close()
    values = table.values()
    expected = ['年度', '预测机构数', '最小值', '均值', '最大值', '行业平均数']
    if not values or values[0] != expected or any(cell[0] != 'th' for cell in table.rows[0]):
        raise ValueError('同花顺净利润预测字段发生变化')
    found = {}
    for row in values[1:]:
        label = row[0]
        if not re.fullmatch(r'\d{4}', label):
            raise ValueError('同花顺预测年度无效')
        year = int(label)
        if year in found:
            raise ValueError('同花顺预测年度重复')
        value, count = _number(row[3]), _number(row[1])
        if count is not None and (count < 0 or not count.is_integer()):
            raise ValueError('同花顺预测机构数无效')
        profit = value * 1e8 if value is not None and count is not None and count > 0 else None
        if profit is not None and not math.isfinite(profit):
            profit = None
        found[year] = dict(year=year, profit=profit, institutions=int(count) if count is not None else None)
    return [found.get(y, dict(year=y, profit=None, institutions=None)) for y in years]
