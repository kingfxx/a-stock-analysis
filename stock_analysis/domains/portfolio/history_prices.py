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


def read_closes(reader, securities, start, end, *, include_prior=False):
    securities = sorted({(market, code) for market, code in securities if market in ('sh', 'sz', 'hk')})
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
    if include_prior:
        for (market, code), series in prices.items():
            if start in series:
                continue
            raw = reader.conn.execute("""
                SELECT p.trade_date,p.close FROM instruments i JOIN raw_daily_prices p ON p.instrument_id=i.id
                WHERE i.exchange=? AND i.code=? AND p.source=? AND p.trade_date<?
                ORDER BY p.trade_date DESC LIMIT 1
            """, (market, code, SOURCE, start)).fetchone()
            saved = reader.conn.execute("""
                SELECT trade_date,close,batch_id,source_id,revision FROM portfolio_historical_closes
                WHERE exchange=? AND code=? AND trade_date<? ORDER BY trade_date DESC,revision DESC LIMIT 1
            """, (market, code, start)).fetchone()
            if raw and (not saved or raw['trade_date'] >= saved['trade_date']):
                stamp = raw['trade_date']
                series[stamp] = (Decimal(str(raw['close'])), ('raw_daily_prices', market, code, stamp, SOURCE))
            elif saved:
                series[saved['trade_date']] = (Decimal(saved['close']), (
                    'portfolio_historical_closes', saved['batch_id'], saved['source_id'], saved['revision']))
    return prices
