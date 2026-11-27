"""Assemble stock references from existing market-domain readers."""

from copy import deepcopy
from datetime import date
from threading import Lock

from stock_analysis.domains.indices.index_prices import read_prices
from stock_analysis.domains.industries.portfolio_lookup import read_classification
from stock_analysis.domains.stocks.dividend_service import DividendService, SOURCE as DIVIDEND_SOURCE
from stock_analysis.domains.stocks.price_service import PriceService
from stock_analysis.domains.valuation.hk_valuation import read as read_hk_valuation
from stock_analysis.domains.valuation.position_reference import summarize_position_valuation
from stock_analysis.domains.valuation.valuation_service import ValuationService
from stock_analysis.infrastructure.idle_cache import IdleCache


class PortfolioReferenceCache:
    """Keep small stock references tied to the SQLite snapshot that produced them."""

    def __init__(self, *, clock=None):
        self._lock = Lock()
        self._cache = IdleCache(clock=clock)
        self._revision = None

    def revision(self, conn):
        revision = conn.execute('SELECT revision FROM portfolio_reference_revision WHERE id=1').fetchone()[0]
        with self._lock:
            if self._revision is None or revision > self._revision:
                self._cache.clear()
                self._revision = revision
            else:
                self._cache.expire()
            return revision

    def read(self, revision, key, load):
        with self._lock:
            if revision == self._revision:
                if key not in self._cache:
                    self._cache[key] = load()
                self._cache.move_to_end(key)
                if len(self._cache) > 256:
                    self._cache.popitem(last=False)
                return deepcopy(self._cache[key])
        return load()

    def expire_idle(self):
        if not self._lock.acquire(blocking=False):
            return 0
        try:
            return len(self._cache.expire())
        finally:
            self._lock.release()


def local_market_days(db, start, end):
    rows = read_prices(db, index_code='cn:000300', provider='csindex', series_key='history.points',
                       frequency='daily', return_type='unspecified', fields=('close',),
                       start=start, end=end)
    return [row['observation_date'] for row in rows if row['close'] is not None]


def previous_market_day(db, day):
    start = date.fromordinal(max(1, date.fromisoformat(day).toordinal() - 15))
    sessions = local_market_days(db, start.isoformat(), day)
    return sessions[-2] if len(sessions) >= 2 and sessions[-1] == day else None


def read_hk_reference(db, code, day):
    start = date.fromordinal(max(1, date.fromisoformat(day).toordinal() - 15)).isoformat()
    prices = PriceService(db).read(code, 'raw', start=start, end=day, lease=False, exchange='hk')
    latest = prices[-1] if prices else None
    valuation = summarize_position_valuation(read_hk_valuation(db, code), prices, None, day,
                                            include_dividend_yield=False)
    return {'close': latest['close'] if latest else None, 'close_date': latest['date'] if latest else None,
            'valuation': valuation}


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
