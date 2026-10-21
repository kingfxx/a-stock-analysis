"""On-demand THS profit consensus. Forecast values and HTML are never persisted."""
from datetime import datetime
from io import StringIO
import re
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd

from .collection import RequestCollector, begin_batch, finish_batch, parsed_source
from .dividend_forecast import finite
from .sources import normalize_code


def parse_forecast(html, code, years):
    title = re.search(r'<title\b[^>]*>(.*?)</title>', html, re.S | re.I)
    if not title or not re.search(rf'(?<!\d){re.escape(code)}(?!\d)', title[1]):
        raise ValueError('同花顺返回股票不匹配')
    if '本年度暂无机构做出业绩预测' in html:
        return [dict(year=y, profit=None, institutions=None) for y in years]
    # Bind the table to its heading and unit, rather than a fragile table index.
    tables = re.findall(r'<table\b[^>]*>.*?</table>', html, re.S | re.I)
    selected = []
    for candidate in tables:
        caption = re.search(r'<caption\b[^>]*>(.*?)</caption>', candidate, re.S | re.I)
        text = re.sub(r'<[^>]+>|\s+', '', caption[1]) if caption else ''
        if '单位：亿元' in text and '预测年报净利润' in text:
            selected.append(candidate)
    if len(selected) != 1:
        raise ValueError('同花顺净利润预测表或亿元单位未识别')
    table = pd.read_html(StringIO(selected[0]))[0]
    expected = ['年度', '预测机构数', '最小值', '均值', '最大值', '行业平均数']
    if list(table.columns) != expected:
        raise ValueError('同花顺净利润预测字段发生变化')
    found = {}
    for row in table.to_dict('records'):
        label = str(row['年度']).strip()
        if not re.fullmatch(r'\d{4}', label):
            raise ValueError('同花顺预测年度无效')
        year = int(label)
        if year in found:
            raise ValueError('同花顺预测年度重复')
        value, count = finite(row['均值']), finite(row['预测机构数'])
        if count is not None and (count < 0 or not count.is_integer()):
            raise ValueError('同花顺预测机构数无效')
        profit = finite(value * 1e8) if value is not None and count is not None and count > 0 else None
        found[year] = dict(year=year, profit=profit, institutions=int(count) if count is not None else None)
    return [found.get(y, dict(year=y, profit=None, institutions=None)) for y in years]


def fetch_institution_forecast(db, code, *, collector=None, now=None):
    code = normalize_code(code)
    current = now or datetime.now(ZoneInfo('Asia/Shanghai'))
    years = list(range(current.year, current.year + 3))
    url = f'https://basic.10jqka.com.cn/{code}/worth.html'
    batch = begin_batch(db, 'institution-forecast:' + uuid4().hex, 'stock.institution_forecast', 'ths',
                        domain='stock', scope={'code': code, 'evidence_mode': 'metadata_only', 'years': years},
                        parser_version='ths-profit-v1')
    owned = collector is None
    collector = collector or RequestCollector(db)
    source_id = None
    try:
        response, source_id = collector.get(batch, 'ths', 'stock.institution_forecast', url,
            headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://stockpage.10jqka.com.cn/'},
            expect='text', evidence_mode='metadata_only',
            healthy_response=lambda r: bool(parse_forecast(r.content.decode('gbk'), code, years)))
        rows = parse_forecast(response.content.decode('gbk'), code, years)
        parsed_source(db, source_id)
        # Request status only: no forecast values, response snippets or hashes.
        finish_batch(db, batch, 'complete')
        return dict(code=code, years=years, rows=rows, source='同花顺机构净利润预测均值',
                    source_url=url, obtained_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
                    unit='CNY', warnings=['预测缺失年份留空，不沿用其他年度'] if any(r['profit'] is None for r in rows) else [])
    except Exception:
        if source_id is not None:
            parsed_source(db, source_id, error='净利润预测表校验失败')
        finish_batch(db, batch, 'failed', error='机构预测读取失败')
        raise
    finally:
        if owned:
            collector.session.close()
