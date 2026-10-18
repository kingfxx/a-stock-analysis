"""Bounded serialized chart cache; independent lightweight collection status."""
from collections import OrderedDict
from datetime import date
import json
import sqlite3
import threading
import uuid

from .index_definitions import METRICS
from .index_repository import index_valuation_views, read_indices
from .index_prices import read_overlay


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')


class IndexReadService:
    def __init__(self, db, *, max_entries=16, max_bytes=32*1024*1024):
        self.db = db
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self.cache = OrderedDict()
        self.cache_bytes = 0
        self.metadata = None
        self.lock = threading.RLock()
        # Transactional triggers track only index read dependencies, including
        # historical corrections. Ordinary page writes do not evict these caches.
        self.watcher = sqlite3.connect(db.path.resolve().as_uri()+'?mode=ro', uri=True, check_same_thread=False)
        self.version = None
        self.epoch = uuid.uuid4().hex

    def close(self):
        with self.lock:
            self.watcher.close()
            self.cache.clear()
            self.cache_bytes = 0
            self.metadata = None

    def _revision(self):
        version = (self.watcher.execute("SELECT revision FROM index_read_revisions WHERE scope='indices'").fetchone()[0], date.today().isoformat())
        if version != self.version:
            self.version = version
            self.metadata = None
            self.cache.clear()
            self.cache_bytes = 0
        return f'{self.epoch}:{version[0]}:{version[1]}'

    def status(self):
        with self.lock:
            revision = self._revision()
            with self.db.connection() as conn:
                operations = [dict(r) for r in conn.execute('SELECT status,result_json,error FROM index_collection_operations ORDER BY started_at DESC LIMIT 1')]
                cooldowns = [dict(r) for r in conn.execute('SELECT source_system,reason,cooldown_until FROM collection_provider_state WHERE cooldown_until IS NOT NULL')]
            return encode(dict(revision=revision, operations=operations, cooldowns=cooldowns))

    def catalog(self):
        with self.lock:
            revision = self._revision()
            if self.metadata is None:
                result = read_indices(self.db)
                fields = ('index_code','name','index_type','industry_level','parent_index_code',
                          'navigation_parent_index_code','navigation_taxonomy_code')
                catalog = [{k:r[k] for k in fields if k in r} for r in result['catalog']]
                self.metadata = encode(dict(revision=revision, catalog=catalog,
                                            coverage=result['coverage'], definitions=result['definitions']))
            return self.metadata

    def chart(self, *, code, provider, series, definition='v1', metric):
        if not code or not provider or not series or metric not in METRICS:
            raise ValueError('图表查询须明确指数、提供方、口径及有效指标')
        # One lock also coalesces simultaneous misses: the second request reuses
        # encoded output, rather than allocating another full chart computation.
        with self.lock:
            revision = self._revision()
            key = (code, provider, series, definition, metric)
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
            with self.db.connection() as conn:
                identities = conn.execute('SELECT id,frequency FROM index_valuation_series WHERE index_code=? AND provider=? AND series_key=? AND definition_version=?',
                                          (code,provider,series,definition)).fetchall()
                rows = []
                for identity in identities:
                    monthly = " AND NOT EXISTS(SELECT 1 FROM index_valuation_observations n WHERE n.series_id=f.series_id AND n.is_latest=1 AND substr(n.observation_date,1,7)=substr(f.observation_date,1,7) AND n.observation_date>f.observation_date)" if identity['frequency']=='monthly' else ''
                    rows.extend(dict(r,frequency=identity['frequency']) for r in conn.execute(
                        f'SELECT observation_date,{metric} FROM index_valuation_observations f WHERE series_id=? AND is_latest=1'+monthly+' ORDER BY observation_date', (identity['id'],)))
            rows.sort(key=lambda r:r['observation_date'])
            views = index_valuation_views(rows, (metric,))
            target = 'pe' if metric.startswith('pe_') else 'ps' if metric.startswith('ps') else metric
            for view in views[metric].values():
                view.pop('rows', None)  # duplicate of rows_by_frequency
                for frequency, points in view['rows_by_frequency'].items():
                    view['rows_by_frequency'][frequency] = [
                        {k:p[k] for k in ('date',target,target+'_date') if k in p} for p in points]
            body = encode(dict(revision=revision, has_rows=bool(rows), views=views))
            if len(body) <= self.max_bytes:
                self.cache[key] = body
                self.cache_bytes += len(body)
                while len(self.cache)>self.max_entries or self.cache_bytes>self.max_bytes:
                    _, evicted = self.cache.popitem(last=False)
                    self.cache_bytes -= len(evicted)
            return body

    def price_overlay(self, code, *, field='close'):
        if not code:
            raise ValueError('缺少指数代码')
        with self.lock:
            self._revision()
            key=('point_overlay',code,field)
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
            body=encode(dict(revision=self._revision(),**read_overlay(self.db,code,field=field)))
            if len(body)<=self.max_bytes:
                self.cache[key]=body
                self.cache_bytes+=len(body)
                while len(self.cache)>self.max_entries or self.cache_bytes>self.max_bytes:
                    _,evicted=self.cache.popitem(last=False)
                    self.cache_bytes-=len(evicted)
            return body
