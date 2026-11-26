"""Attach paced Tencent requests and archived evidence to the shared paginator."""

from decimal import Decimal

from stock_analysis.collection.collection import parsed_source, read_source
from stock_analysis.domains.stocks.source_parsers import parse_price_history
from stock_analysis.domains.stocks.sources import fetch_price_history


PROVIDER = 'tencent'
DATASET = 'portfolio.historical_closes'


def fetch_window(db, collector, batch_id, market, code, start, end):
    sources = {}
    source_id = None
    pages = 0

    def healthy_response(response):
        try:
            return bool(parse_price_history(response.json(), market + code, ''))
        except (AttributeError, KeyError, TypeError, ValueError):
            return False

    def get_response(url, *, params, headers, timeout):
        nonlocal source_id
        source_id = None
        response, source_id = collector.get(batch_id, PROVIDER, DATASET, url, params, headers=headers,
            healthy_response=healthy_response)
        raw, _ = read_source(db, source_id)
        if raw != response.content:
            raise ValueError('归档原文与实际响应不同')
        return response

    def accept_page(rows, limit):
        nonlocal pages, source_id
        if len(rows) > limit:
            raise ValueError('腾讯历史行情超过请求页容量')
        for row in rows:
            day = row['date']
            close = Decimal(str(row['raw'][2]))
            if not close.is_finite() or close <= 0:
                raise ValueError('腾讯未复权收盘价无效')
            if day in sources and sources[day][0] != close:
                raise ValueError(f'腾讯跨页收盘价不一致：{day}')
            sources.setdefault(day, (close, source_id))
        parsed_source(db, source_id, fields=['day', 'close'])
        pages += 1
        source_id = None

    class ArchivedSession:
        get = staticmethod(get_response)

    try:
        fetch_price_history(code, ArchivedSession(), '', start, end, on_page=accept_page)
    except Exception as exc:
        if source_id is not None:
            parsed_source(db, source_id, error=exc)
        raise
    return {day: value for day, value in sources.items() if start <= day <= end}, pages
