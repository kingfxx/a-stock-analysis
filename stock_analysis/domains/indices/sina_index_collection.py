"""Collect daily ChiNext 50 prices through the index operation."""

from stock_analysis.collection.collection import begin_batch, parsed_source
from stock_analysis.domains.indices.index_price_codec import decode_day, encode_day
from stock_analysis.domains.indices.index_prices import OHLC_FIELDS, parse_prices
from stock_analysis.domains.indices.sina_index_parser import parse_history, parse_quote

CODE = '399673'
SYMBOL = 'sz' + CODE
LABEL = '新浪 创业板50日线'
HISTORY_DATASET = 'index.sina.history'
QUOTE_DATASET = 'index.sina.quote'
HISTORY_URL = f'https://finance.sina.com.cn/realstock/company/{SYMBOL}/hisdata/klc_kl.js'
QUOTE_URL = f'https://hq.sinajs.cn/list={SYMBOL}'


def collect(worker, start, end, mode):
    from stock_analysis.domains.indices.index_collection import SKIPPED_WORK, publish_segment

    target = {day for day in worker.trading_days if day <= end}
    with worker.db.connection() as conn:
        existing = {decode_day(row[0]) for row in conn.execute(
            "SELECT p.observation_date FROM index_price_observations p JOIN index_price_series s ON s.id=p.series_id "
            "WHERE s.index_code=? AND s.provider='sina' AND s.series_key='history.points' "
            "AND s.definition_version='v1' AND s.frequency='daily' AND s.return_type='unspecified' "
            "AND p.observation_date<=? AND p.open IS NOT NULL AND p.high IS NOT NULL "
            "AND p.low IS NOT NULL AND p.close IS NOT NULL", ('cn:' + CODE, encode_day(end)))}
    if worker.auto_incremental and existing:
        start = min(existing)
    days = {day for day in target if start <= day}
    missing = days - existing if mode in ('incremental', 'backfill') else days
    if not missing:
        worker.progress('新浪创业板50日线已覆盖目标范围')
        return SKIPPED_WORK

    first, last = min(missing), max(missing)
    quote = worker.auto_incremental and missing == {end}
    dataset = QUOTE_DATASET if quote else HISTORY_DATASET
    batch = begin_batch(worker.db, worker.operation_id, dataset, 'sina', first, last,
                        scope={'code': CODE, 'symbol': SYMBOL}, frequency='daily', trigger=mode)
    source = None
    try:
        worker.progress(f'{LABEL} {first}—{last}')
        if quote:
            response, source = worker.http.get(batch, 'sina', dataset, QUOTE_URL, expect='text',
                                               headers={'Referer': 'https://finance.sina.com.cn/'})
            records = [parse_quote(response.content, SYMBOL)]
        else:
            response, source = worker.http.get(batch, 'sina', dataset, HISTORY_URL,
                                               params={'d': '2020_2_4'}, expect='text')
            records = parse_history(response.content, SYMBOL)
        points, anomalies = parse_prices(
            records, provider='sina', series_key='history.points', code_key='_code', date_key='date',
            start=first, end=last, trading_days=target, batch_id=batch, source_id=source, expected_code=CODE)
        ready = {point['observation_date'] for point in points if all(point[field] is not None for field in OHLC_FIELDS)}
        uncovered = missing - ready
        if uncovered:
            sample = ', '.join(sorted(uncovered)[:5])
            raise ValueError(f'新浪创业板50日线缺少 {len(uncovered)} 个交易日的开高低收：{sample}')
        parsed_source(worker.db, source, fields=sorted(set().union(*(r.keys() for r in records))))
        identity = dict(code='cn:' + CODE, name='创业板50', kind='broad', provider='sina',
                        provider_code=SYMBOL, source_id=source)
        publish_segment(worker.db, batch, [], [identity], dict(expected_dates=len(missing), price_anomalies=anomalies),
                        coverage='complete', check_cancelled=worker.http.check_cancelled,
                        prices=[point for point in points if point['observation_date'] in missing])
    except Exception as exc:
        worker.failed(batch, source, exc)
        raise
