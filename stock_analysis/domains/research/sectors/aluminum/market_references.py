"""On-demand aluminum spot and continuous futures references."""
from datetime import datetime
from functools import partial
import json
from threading import Lock
from time import monotonic

from stock_analysis.collection.collection import (PacingPolicy, RequestCollector, begin_batch, finish_batch,
                                                  parsed_source, read_source)

from .market_reference_parser import parse_spot, parse_monthly
from .quotes import BEIJING, DOMAIN, EXPIRE_SECONDS, FRESH_SECONDS


SOURCES = {
    'spot': ('ccmn', 'aluminum.ccmn_spot', '长江有色 · 华东 A00 铝锭',
             'https://www.worldmetalres.com/zh', 'https://www.worldmetalres.com/zh'),
    'monthly': ('sina', 'aluminum.sina_al0_monthly', '新浪财经 · 沪铝连续 AL0',
                'https://stock2.finance.sina.com.cn/futures/api/json.php/InnerFuturesNewService.getDailyKLine',
                'https://finance.sina.com.cn/futures/quotes/AL0.shtml'),
}
MONTHLY_PARAMS = dict(symbol='AL0')
PARSER_VERSION = 'aluminum-reference-v1'


def _decode(kind, raw, today):
    return parse_spot(raw.decode('utf-8'), today) if kind == 'spot' else parse_monthly(json.loads(raw), today)


class MarketReferences:
    def __init__(self, db, collector_factory=None):
        self.db = db
        self._collector_factory = collector_factory or partial(RequestCollector, policy=PacingPolicy(max_attempts=1))
        self._lock = Lock()
        self._refresh = Lock()
        self._values = {}
        self._errors = {}

    def expire_idle(self):
        with self._lock:
            expired = [key for key, (_, stamp) in self._values.items() if monotonic() - stamp >= EXPIRE_SECONDS]
            for key in expired:
                del self._values[key]
            return len(expired)

    def latest(self, today=None):
        today = today or datetime.now(BEIJING).date()
        self.expire_idle()
        values = {}
        with self._lock:
            current = dict(self._values)
            errors = dict(self._errors)
        for key in SOURCES:
            if key in current:
                value, stamp = current[key]
                if key == 'spot' or value['current_month'] == today.isoformat()[:7]:
                    values[key] = dict(value, stale=monotonic() - stamp >= FRESH_SECONDS)
                    continue
            archived = self._archived(key, today)
            if archived:
                values[key] = dict(archived, archived=True)
        return dict(**values, errors=errors)

    def refresh(self, today=None):
        today = today or datetime.now(BEIJING).date()
        if not self._refresh.acquire(blocking=False):
            return self.latest(today)
        try:
            for key in SOURCES:
                with self._lock:
                    cached = self._values.get(key)
                if cached and monotonic() - cached[1] < FRESH_SECONDS:
                    continue
                previous = cached[0] if cached else self._archived(key, today)
                value, error = self._fetch(key, today)
                with self._lock:
                    if value:
                        if previous and value['trade_date'] < previous['trade_date']:
                            self._errors[key] = '来源日期较旧，继续显示最近有效报价'
                        else:
                            self._values[key] = value, monotonic()
                            self._errors.pop(key, None)
                    else:
                        self._errors[key] = error
        finally:
            self._refresh.release()
        return self.latest(today)

    def _archived(self, key, today):
        provider, dataset, _, _, _ = SOURCES[key]
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT s.id FROM collection_sources s JOIN collection_batches b ON b.id=s.batch_id "
                "WHERE b.domain=? AND s.dataset=? AND s.source_system=? AND s.parse_status='complete' "
                "AND b.status='complete' ORDER BY s.id DESC LIMIT 5", (DOMAIN, dataset, provider)).fetchall()
        values = []
        for row in rows:
            try:
                raw, evidence = read_source(self.db, row['id'])
                values.append(self._with_evidence(key, _decode(key, raw, today), evidence))
            except (OSError, TypeError, ValueError):
                continue
        return max(values, key=lambda value: (value['trade_date'], value['source_id']), default=None)

    @staticmethod
    def _with_evidence(key, value, evidence):
        _, _, source, _, url = SOURCES[key]
        return dict(value, source=source, url=url, source_id=evidence['id'],
                    obtained_at=evidence['obtained_at'], raw_sha256=evidence['raw_sha256'])

    def _fetch(self, key, today):
        provider, dataset, _, endpoint, _ = SOURCES[key]
        daily = key == 'spot'
        batch = begin_batch(self.db, None, dataset, provider, end=today.isoformat(),
                            frequency='daily' if daily else 'monthly', domain=DOMAIN,
                            parser_version=PARSER_VERSION, definition_version=PARSER_VERSION)
        source_id = None
        try:
            collector = self._collector_factory(self.db)
            response, source_id = collector.get(
                batch, provider, dataset, endpoint,
                params=None if daily else MONTHLY_PARAMS,
                expect='html' if daily else 'json',
                healthy_response=lambda received: bool(_decode(key, received.content, today)))
            value = _decode(key, response.content, today)
            parsed_source(self.db, source_id, fields=value.keys())
            with self.db.connection() as conn:
                evidence = conn.execute('SELECT id,obtained_at,raw_sha256 FROM collection_sources WHERE id=?',
                                        (source_id,)).fetchone()
            coverage = ('complete' if daily or all(point['close'] is not None for point in value['points'])
                        else 'partial')
            finish_batch(self.db, batch, 'complete', coverage, result=dict(trade_date=value['trade_date']))
            return self._with_evidence(key, value, evidence), None
        except (OSError, ValueError, RuntimeError) as exc:
            if source_id is not None:
                parsed_source(self.db, source_id, error=exc)
            finish_batch(self.db, batch, 'failed', 'unknown', error=exc)
            return None, str(exc)
