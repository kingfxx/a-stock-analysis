"""Assemble stock references from existing market-domain readers."""

from datetime import date

from stock_analysis.domains.indices.index_prices import read_prices
from stock_analysis.domains.industries.portfolio_lookup import read_classification
from stock_analysis.domains.stocks.dividend_service import DividendService, SOURCE as DIVIDEND_SOURCE
from stock_analysis.domains.stocks.price_service import PriceService
from stock_analysis.domains.valuation.position_reference import summarize_position_valuation
from stock_analysis.domains.valuation.valuation_service import ValuationService


def local_market_days(db, start, end):
    rows = read_prices(db, index_code='cn:000300', provider='csindex', series_key='history.points',
                       frequency='daily', return_type='unspecified', fields=('close',),
                       start=start, end=end)
    return [row['observation_date'] for row in rows if row['close'] is not None]


def previous_market_day(db, day):
    start = date.fromordinal(max(1, date.fromisoformat(day).toordinal() - 15))
    sessions = local_market_days(db, start.isoformat(), day)
    return sessions[-2] if len(sessions) >= 2 and sessions[-1] == day else None


def read_stock_reference(db, code, as_of, *, cache, valuation_cache):
    prices = PriceService(db).read(code, 'raw', end=as_of, lease=False)
    dividends = DividendService(db, cache).read(code)
    saved = ValuationService(db, valuation_cache).read(code, [])
    with db.connection() as conn:
        covered = conn.execute("""
            SELECT 1 FROM sync_runs r JOIN instruments i ON i.id=r.instrument_id
            WHERE i.code=? AND r.dataset='dividends' AND r.source=?
            AND r.requested_start IS NULL AND r.status IN ('success','no_data') LIMIT 1
        """, (code, DIVIDEND_SOURCE)).fetchone()
    return {
        'close': prices[-1]['close'] if prices else None,
        'close_date': prices[-1]['date'] if prices else None,
        'previous_close': prices[-2]['close'] if len(prices) >= 2 else None,
        'previous_close_date': prices[-2]['date'] if len(prices) >= 2 else None,
        'valuation': summarize_position_valuation(saved, prices, dividends if covered else None, as_of),
        'industry': read_classification(db, code, as_of),
    }
