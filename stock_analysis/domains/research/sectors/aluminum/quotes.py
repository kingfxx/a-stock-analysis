"""Explicit SHFE quote fetch and the two-entry process cache."""
from datetime import datetime, time, timedelta, timezone
import json
from threading import Lock
from time import monotonic

from stock_analysis.collection.collection import RequestCollector, begin_batch, finish_batch, parsed_source, read_source
from stock_analysis.infrastructure.storage import utc_now

from .quote_parser import PRODUCTS, settled_quotes


DOMAIN = 'research_aluminum'
DATASET = 'aluminum.shfe_settlement'
PROVIDER = 'shfe'
PARSER = 'aluminum-shfe-daily-v1'
URL = 'https://www.shfe.com.cn/data/tradedata/future/dailydata/kx{}.dat'
BEIJING = timezone(timedelta(hours=8))
FRESH_SECONDS = 15 * 60
EXPIRE_SECONDS = 60 * 60


def candidates(now):
    latest = now.date() - timedelta(days=int(now.time() < time(15, 45)))
    return [day for offset in range(8) if (day := latest - timedelta(days=offset)).weekday() < 5][:5]


def recover_quote_batches(db):
    with db.connection(write=True) as conn:
        conn.execute("UPDATE collection_batches SET status='failed', finished_at=?, "
                     "error='后台重启，已归档来源保留' WHERE domain=? AND status='running'",
                     (utc_now(), DOMAIN))


class AluminumQuotes:
    def __init__(self, db, collector_factory=RequestCollector):
        self.db = db
        self.collector_factory = collector_factory
        self._refresh = Lock()
        self._lock = Lock()
        self._quotes = {}
        self._errors = {}
        self._last_good = None

    def expire_idle(self):
        with self._lock:
            expired = [product for product, (_, stamp) in self._quotes.items()
                       if monotonic() - stamp >= EXPIRE_SECONDS]
            for product in expired:
                del self._quotes[product]
            return len(expired)

    def latest(self):
        self.expire_idle()
        with self._lock:
            quotes = {product: dict(quote, stale=monotonic() - stamp >= FRESH_SECONDS)
                      for product, (quote, stamp) in self._quotes.items()}
            errors = dict(self._errors)
        if len(quotes) < len(PRODUCTS):
            for product, quote in self._archived_quotes().items():
                quotes.setdefault(product, quote)
        return dict(quotes=quotes, errors=errors)

    def _archived_quotes(self):
        with self.db.connection() as conn:
            sources = conn.execute(
                "SELECT s.id,b.id AS batch_id FROM collection_sources s JOIN collection_batches b ON b.id=s.batch_id "
                "WHERE b.domain=? AND s.dataset=? AND s.source_system=? AND s.parse_status='complete' "
                "AND b.status='complete' ORDER BY s.id DESC LIMIT 5", (DOMAIN, DATASET, PROVIDER)).fetchall()
        quotes = {}
        for source in sources:
            try:
                raw, evidence = read_source(self.db, source['id'])
                payload = json.loads(raw)
                day = datetime.strptime(payload['report_date'], '%Y%m%d').date()
                rows, _ = settled_quotes(payload, day)
                url = URL.format(day.strftime('%Y%m%d'))
                for product, quote in rows.items():
                    quotes.setdefault(product, dict(quote, source='上海期货交易所', url=url,
                                                   batch_id=source['batch_id'], source_id=source['id'],
                                                   raw_sha256=evidence['raw_sha256'], obtained_at=evidence['obtained_at'],
                                                   stale=True, archived=True))
            except (ValueError, OSError, KeyError, TypeError):
                continue
            if len(quotes) == len(PRODUCTS):
                break
        return quotes

    def refresh(self, now=None):
        if not self._refresh.acquire(blocking=False):
            return dict(self.latest(), pending=True)
        try:
            if self._last_good is not None and monotonic() - self._last_good < FRESH_SECONDS:
                return dict(self.latest(), reused=True)
            values, errors = self._fetch(now or datetime.now(BEIJING))
            with self._lock:
                for product, quote in values.items():
                    previous = self._quotes.get(product)
                    if previous and quote['trade_date'] < previous[0]['trade_date']:
                        errors[product] = '来源交易日较旧，保留最近有效行情'
                        continue
                    if previous and previous[0]['contract'] != quote['contract']:
                        quote = dict(quote, previous_contract=previous[0]['contract'],
                                     previous_trade_date=previous[0]['trade_date'])
                    elif previous and previous[0].get('previous_contract'):
                        quote = dict(quote, previous_contract=previous[0]['previous_contract'],
                                     previous_trade_date=previous[0]['previous_trade_date'])
                    self._quotes[product] = (quote, monotonic())
                self._errors = errors
                if len(values) == len(PRODUCTS) and not errors:
                    self._last_good = monotonic()
            return self.latest()
        finally:
            self._refresh.release()

    def _fetch(self, now):
        days = candidates(now)
        batch = begin_batch(self.db, None, DATASET, PROVIDER, days[-1].isoformat(), days[0].isoformat(),
                            scope={'code': '601600', 'products': list(PRODUCTS)}, frequency='daily',
                            domain=DOMAIN, parser_version=PARSER, definition_version=PARSER)
        collector = self.collector_factory(self.db)
        values, errors = {}, dict.fromkeys(PRODUCTS, '近期暂无已结算行情')
        try:
            for day in days:
                url = URL.format(day.strftime('%Y%m%d'))
                try:
                    response, source = collector.get(batch, PROVIDER, DATASET, url, healthy_response=(
                        lambda r: bool(settled_quotes(r.json(), day)[0])))
                except ValueError as exc:
                    if str(exc) == 'shfe HTTP 404':
                        continue
                    raise
                try:
                    rows, errors = settled_quotes(response.json(), day)
                except ValueError as exc:
                    parsed_source(self.db, source, error=exc)
                    raise
                parsed_source(self.db, source, fields=rows, error='；'.join(errors.values()) if not rows else None)
                if rows:
                    with self.db.connection() as conn:
                        evidence = conn.execute("SELECT obtained_at,raw_sha256 FROM collection_sources WHERE id=?",
                                                (source,)).fetchone()
                    values = {key: dict(quote, source='上海期货交易所', url=url, batch_id=batch,
                                        source_id=source, **dict(evidence)) for key, quote in rows.items()}
                    break
        except (ValueError, RuntimeError) as exc:
            errors = dict.fromkeys(PRODUCTS, str(exc))
            finish_batch(self.db, batch, 'failed', 'unknown', error=exc)
            return {}, errors
        coverage = 'complete' if len(values) == len(PRODUCTS) else 'partial' if values else 'empty'
        finish_batch(self.db, batch, 'complete', coverage,
                     result=dict(products=list(values), errors=errors))
        return values, errors
