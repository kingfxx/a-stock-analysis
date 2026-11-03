"""On-demand THS profit consensus. Forecast values and HTML are never persisted."""
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo


from stock_analysis.collection.collection import RequestCollector, begin_batch, finish_batch, parsed_source
from stock_analysis.domains.valuation.institution_forecast_parser import parse_forecast
from stock_analysis.domains.stocks.source_parsers import normalize_code


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
