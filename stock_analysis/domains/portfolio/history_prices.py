"""Read the current raw close and its origin from the existing reader."""

import json
from decimal import Decimal, InvalidOperation

from stock_analysis.domains.stocks.price_service import PriceService, SOURCE


def stored_closes(conn, exchange, code, start, end):
    return conn.execute("""
        SELECT p.trade_date,p.close,p.revision,p.batch_id,p.source_id
        FROM portfolio_historical_closes p
        WHERE p.exchange=? AND p.code=? AND p.trade_date BETWEEN ? AND ?
        AND p.revision=(SELECT max(r.revision) FROM portfolio_historical_closes r
                        WHERE (r.exchange,r.code,r.trade_date)=(p.exchange,p.code,p.trade_date))
        ORDER BY p.trade_date
    """, (exchange, code, start, end))


def read_closes(reader, securities, start, end):
    securities = sorted({(market, code) for market, code in securities if market in ('sh', 'sz')})
    prices = {security: {} for security in securities}
    if not securities:
        return prices
    service = PriceService(reader)
    for market, code in securities:
        for row in service.read(code, 'raw', start=start, end=end, lease=False, exchange=market):
            if row['close'] is None:
                continue
            try:
                close = Decimal(str(row['close']))
            except InvalidOperation:
                continue
            if close.is_finite() and close > 0:
                prices[market, code][row['date']] = (close, ('raw_daily_prices', market, code, row['date'], SOURCE))
    # One range read for all requested securities, using the current read connection.
    rows = reader.conn.execute("""
        WITH requested AS (SELECT json_extract(value,'$[0]') market,
                                  json_extract(value,'$[1]') code FROM json_each(?))
        SELECT p.exchange,p.code,p.trade_date,p.close,p.batch_id,p.source_id,p.revision
        FROM requested q JOIN portfolio_historical_closes p ON p.exchange=q.market AND p.code=q.code
        WHERE p.trade_date BETWEEN ? AND ?
        AND p.revision=(SELECT max(r.revision) FROM portfolio_historical_closes r
                        WHERE (r.exchange,r.code,r.trade_date)=(p.exchange,p.code,p.trade_date))
    """, (json.dumps(securities), start, end))
    for row in rows:
        prices[row['exchange'], row['code']].setdefault(row['trade_date'], (
            Decimal(row['close']), ('portfolio_historical_closes', row['batch_id'], row['source_id'], row['revision'])))
    return prices
