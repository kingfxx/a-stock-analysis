"""Bounded serialized chart cache; independent lightweight collection status."""
from collections import OrderedDict
from bisect import bisect_left
from datetime import date
import json
import math
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

from stock_analysis.domains.indices.index_definitions import METRICS
from stock_analysis.domains.indices.index_repository import index_valuation_views, read_indices
from stock_analysis.domains.indices.index_prices import read_overlay


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
        bond_revision = tuple(self.watcher.execute('SELECT max(id),max(updated_at) FROM sovereign_yield_observations').fetchone())
        version = (bond_revision, self.watcher.execute("SELECT revision FROM index_read_revisions WHERE scope='indices'").fetchone()[0], date.today().isoformat())
        if version != self.version:
            self.version = version
            self.metadata = None
            self.cache.clear()
            self.cache_bytes = 0
        return f'{self.epoch}:{version}'

    def status(self):
        with self.lock:
            revision = self._revision()
            with self.db.connection() as conn:
                operations = [dict(r) for r in conn.execute('SELECT status,result_json,error FROM index_collection_operations ORDER BY started_at DESC LIMIT 1')]
                now = datetime.now(timezone.utc)
                cooldowns = [dict(r) for r in conn.execute('SELECT source_system,reason,cooldown_until FROM collection_provider_state WHERE cooldown_until IS NOT NULL')
                             if datetime.fromisoformat(r['cooldown_until']).astimezone(timezone.utc) > now]
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

    def equity_bond_spread(self, code):
        """Same-date earnings yield less China 10Y; never carry yields forward."""
        with self.lock:
            revision = self._revision()
            with self.db.connection() as conn:
                identity = conn.execute(
                    "SELECT s.id FROM index_valuation_series s JOIN index_catalog c USING(index_code) "
                    "WHERE s.index_code=? AND c.index_type='broad' AND s.provider='csindex' "
                    "AND s.series_key='history.rolling' AND s.definition_version='v1' "
                    "AND s.frequency='daily'", (code,)).fetchone()
                if identity is None:
                    raise ValueError('股债利差仅支持有中证历史滚动 PE 的宽基指数')
                facts = conn.execute(
                    'SELECT p.observation_date,p.pe_rolling,b.china_10y FROM index_valuation_observations p '
                    'LEFT JOIN sovereign_yield_observations b ON b.observation_date=p.observation_date '
                    "AND b.is_latest=1 AND b.definition_version='v1' "
                    'WHERE p.series_id=? AND p.is_latest=1 ORDER BY p.observation_date', (identity['id'],)).fetchall()
            rows = []
            for fact in facts:
                pe, bond = fact['pe_rolling'], fact['china_10y']
                bond = float(bond) if bond is not None else None
                value = (100 / pe - bond if pe is not None and math.isfinite(pe) and pe > 0
                         and bond is not None and math.isfinite(bond) else None)
                rows.append(dict(observation_date=fact['observation_date'],pe_rolling=pe,
                                 china_10y=bond,spread=value))
            valid = [row for row in rows if row['spread'] is not None]
            return encode(dict(revision=revision,available=bool(valid),rows=rows,observations=len(valid),
                               first_date=valid[0]['observation_date'] if valid else None,
                               last_date=valid[-1]['observation_date'] if valid else None))

    def industry_crowding(self, level, start, end):
        try:
            level=int(level)
            first=date.fromisoformat(start)
            last=date.fromisoformat(end)
        except (TypeError,ValueError):
            raise ValueError('行业热力图须提供有效的层级和日期范围') from None
        if level not in (1,2) or first>last or (last-first).days>366:
            raise ValueError('行业热力图层级须为1或2，日期范围不得超过367天')
        with self.lock:
            revision=self._revision()
            key=('industry_crowding',level,start,end)
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
            catalog=read_indices(self.db,kind='industry',level=level)['catalog']
            catalog=[row for row in catalog if (row.get('classification_system') or '').lower() in ('sw','sw2021')]
            indices={row['index_code']:row for row in catalog}
            output=[]
            if indices:
                placeholders=','.join('?' for _ in indices)
                with self.db.connection() as conn:
                    identities=conn.execute(
                        'SELECT s.id,s.index_code FROM index_price_series s WHERE s.index_code IN ('+placeholders+") "
                        "AND s.provider='sws' AND s.series_key='analysis.points' AND s.frequency='daily' "
                        "ORDER BY s.index_code,CASE WHEN s.definition_version='v2' THEN 0 ELSE 1 END,s.id",
                        tuple(indices)).fetchall()
                    series_by_code={}
                    for identity in identities:
                        series_by_code.setdefault(identity['index_code'],identity['id'])
                    for code,index in indices.items():
                        series_id=series_by_code.get(code)
                        if series_id is None:
                            output.append(dict(index_code=code,name=index['name'],industry_level=level,
                                parent_index_code=index.get('parent_index_code'),
                                navigation_parent_index_code=index.get('navigation_parent_index_code'),values=[]))
                            continue
                        facts=[dict(row) for row in conn.execute(
                            'SELECT observation_date,amount,amount_ratio,turnover_rate FROM index_price_observations_read '
                            'WHERE series_id=? ORDER BY observation_date',(series_id,))]
                        turnover=sorted(row['turnover_rate'] for row in facts
                            if isinstance(row['turnover_rate'],(int,float)))
                        values=[]
                        for fact in facts:
                            day=fact['observation_date']
                            if not start<=day<=end:
                                continue
                            rate=fact['turnover_rate']
                            percentile=(round(1000*bisect_left(turnover,rate)/len(turnover))/10
                                if turnover and isinstance(rate,(int,float)) else None)
                            values.append(dict(date=day,amount=fact['amount'],congestion=fact['amount_ratio'],turnover_percentile=percentile))
                        output.append(dict(index_code=code,name=index['name'],industry_level=level,
                            parent_index_code=index.get('parent_index_code'),
                            navigation_parent_index_code=index.get('navigation_parent_index_code'),values=values))
            output.sort(key=lambda row:row['name'])
            dates=sorted({value['date'] for row in output for value in row['values']
                if value['congestion'] is not None},reverse=True)
            body=encode(dict(revision=revision,level=level,start=start,end=end,dates=dates,industries=output))
            if len(body)<=self.max_bytes:
                self.cache[key]=body
                self.cache_bytes+=len(body)
                while len(self.cache)>self.max_entries or self.cache_bytes>self.max_bytes:
                    _,evicted=self.cache.popitem(last=False)
                    self.cache_bytes-=len(evicted)
            return body
