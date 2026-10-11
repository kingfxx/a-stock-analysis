"""Source-separated index points, independent of valuation and stock prices."""
from datetime import date
import math

from .collection import digest, encoded
from .storage import utc_now

VERSION='v1'
FIELDS=('open','high','low','close')
DEFINITIONS={
 ('csindex','history.points'):dict(fields={k:k for k in FIELDS},frequency='daily',dataset='index.cs.history'),
 ('sws','analysis.points'):dict(fields={'closeindex':'close'},frequency='daily',dataset='index.sw.analysis'),
 ('legu','pe.points'):dict(fields={'close':'close'},frequency='monthly',dataset='index.legu.pe'),
 ('legu','pb.points'):dict(fields={'close':'close'},frequency='monthly',dataset='index.legu.pb'),
 ('sina','history.points'):dict(fields={k:k for k in FIELDS},frequency='daily',dataset='index.price.range_probe'),
}
for definition in DEFINITIONS.values():
    definition.update(unit='index_points',missing='source nulls remain null; absent OHLC not derived',
        observation_rule='actual source date; monthly moving observations retained',
        evidence='docs/index-price-storage.md',volume_amount='not stored; source units not standardized')


def install_definitions(conn):
    for (provider,key),definition in DEFINITIONS.items():
        body=encoded(definition)
        old=conn.execute('SELECT definition_json FROM index_price_definitions WHERE provider=? AND series_key=? AND definition_version=?',
                         (provider,key,VERSION)).fetchone()
        if old and old[0]!=body:
            raise ValueError('点位定义变化须增加版本')
        conn.execute('INSERT OR IGNORE INTO index_price_definitions VALUES(?,?,?,?,?)',(provider,key,VERSION,body,utc_now()))


def validate_values(values):
    for value in values.values():
        if value is not None and (isinstance(value,bool) or not math.isfinite(float(value)) or value<=0):
            raise ValueError('指数点位须为有限正数或来源缺值')
    for upper,lower in (('high','low'),('high','open'),('high','close'),('open','low'),('close','low')):
        if values[upper] is not None and values[lower] is not None and values[upper]<values[lower]:
            raise ValueError('指数开高低收关系异常')


def parse_prices(records, *, provider, series_key, code_key, date_key, start, end, trading_days,
                 batch_id, source_id, expected_code=None):
    # Use the exact same boundary-placeholder convention as valuation parsing.
    from .index_collection import day, number
    from .index_definitions import canonical
    definition=DEFINITIONS[provider,series_key]
    fields=definition['fields']
    if not isinstance(records,list):
        raise ValueError('点位响应须为列表')
    rows,anomalies,seen=[],[],set()
    ordered=sorted(records,key=lambda r:day(r[date_key]))
    boundary=set()
    if provider=='csindex' and len(ordered)>1:
        first,next_row=ordered[:2]
        if (day(first[date_key])==start and (date.fromisoformat(day(next_row[date_key]))-date.fromisoformat(start)).days>7
            and 'peg' in first and number(first['peg']) is None
            and {k:v for k,v in first.items() if k!=date_key}=={k:v for k,v in next_row.items() if k!=date_key}):
            boundary.add((str(first[code_key]),start))
    for raw in records:
        code=str(raw[code_key]).strip()
        if expected_code and code!=expected_code:
            raise ValueError('点位来源代码与请求不一致')
        observed=day(raw[date_key])
        key=(code,observed)
        if key in seen:
            raise ValueError('点位分段重复指数／日期')
        seen.add(key)
        reason=('boundary_placeholder_duplicate' if key in boundary else 'out_of_window' if not start<=observed<=end
                else 'non_trading_day' if observed not in trading_days else None)
        if reason:
            anomalies.append(dict(code=code,date=observed,reason=reason))
            continue
        required={source for source,target in fields.items() if target=='close'}
        if not required<=raw.keys():
            anomalies.append(dict(code=code,date=observed,reason='price_fields_absent'))
            continue
        values={field:None for field in FIELDS}
        values.update({target:number(raw.get(source)) for source,target in fields.items()})
        validate_values(values)
        identity=canonical(code,provider)
        rows.append(dict(index_code=identity,provider=provider,series_key=series_key,frequency=definition['frequency'],
            return_type='total_return' if identity=='cn:000680' else 'unspecified',definition_version=VERSION,
            observation_date=observed,**values,explicit_nulls=True,batch_id=batch_id,source_id=source_id))
    return rows,anomalies


def publish_prices(conn, rows):
    install_definitions(conn)
    counts=dict(inserted=0,revised=0,unchanged=0)
    identities={}
    sources={}
    seen=set()
    for row in rows:
        provider,key=row['provider'],row['series_key']
        definition=DEFINITIONS[provider,key]
        if row.get('definition_version',VERSION)!=VERSION or row['frequency']!=definition['frequency']:
            raise ValueError('点位频率或定义版本不匹配')
        observed=row['observation_date']
        if date.fromisoformat(observed).isoformat()!=observed:
            raise ValueError('点位日期须为ISO日期')
        identity=(row['index_code'],provider,key,row['frequency'],row['return_type'],VERSION)
        if (*identity,observed) in seen:
            raise ValueError('点位分段重复序列／日期')
        seen.add((*identity,observed))
        values={field:row.get(field) for field in FIELDS}
        if any(values[field] is not None and field not in definition['fields'].values() for field in FIELDS):
            raise ValueError('点位含未定义字段')
        validate_values(values)
        if all(v is None for v in values.values()) and not (row.get('explicit_nulls') or row.get('withdrawal_reason')):
            raise ValueError('点位全空须有完整来源字段或撤回说明')
        source_key=(row['source_id'],row['batch_id'])
        if source_key not in sources:
            sources[source_key]=conn.execute('SELECT s.obtained_at,s.parse_status,s.source_system,b.status FROM collection_sources s '
                'JOIN collection_batches b ON b.id=s.batch_id WHERE s.id=? AND s.batch_id=?',source_key).fetchone()
        source=sources[source_key]
        if not source or source['parse_status']!='complete' or source['source_system']!=provider or source['status']!='running' or not source['obtained_at']:
            raise ValueError('点位来源解析／批次／提供方／真实时间不匹配')
        if identity not in identities:
            conn.execute('INSERT OR IGNORE INTO index_price_series(index_code,provider,series_key,frequency,return_type,definition_version,created_at) '
                         'VALUES(?,?,?,?,?,?,?)',(*identity,utc_now()))
            identities[identity]=conn.execute('SELECT id FROM index_price_series WHERE index_code=? AND provider=? AND series_key=? '
                'AND frequency=? AND return_type=? AND definition_version=?',identity).fetchone()[0]
        sid=identities[identity]
        old=conn.execute('SELECT * FROM index_price_observations WHERE series_id=? AND observation_date=? AND is_latest=1',(sid,observed)).fetchone()
        content_hash=digest(encoded(dict(values=values,definition=definition,withdrawal_reason=row.get('withdrawal_reason'))).encode())
        if old and any(old[field] is not None and values[field] is None for field in FIELDS) and not row.get('withdrawal_reason'):
            raise ValueError('点位有效值变为空须核实撤回原因')
        if old and old['content_hash']==content_hash:
            counts['unchanged']+=1
            continue
        if old:
            conn.execute('UPDATE index_price_observations SET is_latest=0 WHERE id=?',(old['id'],))
        payload=dict(series_id=sid,observation_date=observed,**values,withdrawal_reason=row.get('withdrawal_reason'),
            content_hash=content_hash,version=old['version']+1 if old else 1,is_latest=1,
            batch_id=row['batch_id'],source_id=row['source_id'],obtained_at=source['obtained_at'],updated_at=utc_now())
        conn.execute('INSERT INTO index_price_observations('+','.join(payload)+') VALUES('+','.join('?' for _ in payload)+')',tuple(payload.values()))
        counts['revised' if old else 'inserted']+=1
    return counts


def read_prices(db, *, index_code, provider, series_key, frequency, return_type, start=None, end=None, definition_version=VERSION):
    """Explicit series selection; no source stitching or daily synthesis."""
    with db.connection() as conn:
        rows=[dict(r) for r in conn.execute('SELECT f.*,s.index_code,s.provider,s.series_key,s.frequency,s.return_type,s.definition_version '
            'FROM index_price_series s JOIN index_price_observations f ON f.series_id=s.id '
            'WHERE s.index_code=? AND s.provider=? AND s.series_key=? AND s.frequency=? AND s.return_type=? '
            'AND s.definition_version=? AND f.is_latest=1 AND f.observation_date BETWEEN ? AND ? ORDER BY f.observation_date',
            (index_code,provider,series_key,frequency,return_type,definition_version,start or '0001-01-01',end or '9999-12-31'))]
    if frequency=='monthly':
        latest={r['observation_date'][:7]:r for r in rows}
        rows=list(latest.values())
    return rows


def read_overlay(db, index_code):
    """Pick one real close series for an index without mixing providers or bases."""
    with db.connection() as conn:
        identity=conn.execute('SELECT s.id,s.index_code,s.provider,s.series_key,s.frequency,s.return_type,s.definition_version,'
            'count(f.close) AS observations,min(CASE WHEN f.close IS NOT NULL THEN f.observation_date END) AS first_date,'
            'max(CASE WHEN f.close IS NOT NULL THEN f.observation_date END) AS last_date '
            'FROM index_price_series s JOIN index_price_observations f ON f.series_id=s.id AND f.is_latest=1 '
            'WHERE s.index_code=? AND f.close IS NOT NULL GROUP BY s.id '
            'ORDER BY CASE WHEN s.frequency=\'daily\' THEN 0 ELSE 1 END,'
            'CASE WHEN s.provider=? THEN 0 WHEN s.provider=\'csindex\' THEN 1 WHEN s.provider=\'sws\' THEN 2 WHEN s.provider=\'sina\' THEN 3 WHEN s.provider=\'legu\' THEN 4 ELSE 5 END,'
            'CASE WHEN s.return_type=\'unspecified\' THEN 0 ELSE 1 END,s.series_key,s.definition_version LIMIT 1',
            (index_code,'sws' if index_code.startswith('sw:') else 'csindex')).fetchone()
        if identity is None:
            return dict(index_code=index_code,available=False,rows=[])
        rows=[dict(r) for r in conn.execute('SELECT observation_date,close FROM index_price_observations '
            'WHERE series_id=? AND is_latest=1 AND close IS NOT NULL ORDER BY observation_date',(identity['id'],))]
    return dict(index_code=index_code,available=bool(rows),provider=identity['provider'],series_key=identity['series_key'],
        frequency=identity['frequency'],return_type=identity['return_type'],definition_version=identity['definition_version'],
        observations=identity['observations'],first_date=identity['first_date'],last_date=identity['last_date'],rows=rows)


def price_coverage(rows):
    """Field counts describe returned data, never infer a historical universe."""
    groups={}
    for row in rows:
        key=(row['index_code'],row['provider'],row['series_key'],row['frequency'],row['return_type'])
        group=groups.setdefault(key,dict(index_code=key[0],provider=key[1],series_key=key[2],frequency=key[3],return_type=key[4],dates=set(),fields={k:set() for k in FIELDS}))
        group['dates'].add(row['observation_date'])
        for field in FIELDS:
            if row.get(field) is not None:
                group['fields'][field].add(row['observation_date'])
    output=[]
    for group in groups.values():
        dates=group.pop('dates')
        fields=group.pop('fields')
        output.append(dict(**group,observations=len(dates),first=min(dates),last=max(dates),coverage_status='unknown',
            fields={k:dict(valid=len(v),missing=len(dates-v),first=min(v) if v else None,last=max(v) if v else None) for k,v in fields.items()}))
    return output
