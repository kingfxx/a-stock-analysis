"""Source-separated index points, independent of valuation and stock prices."""
from datetime import date
import json
import math

from stock_analysis.collection.collection import digest, encoded
from stock_analysis.infrastructure.storage import utc_now
from stock_analysis.domains.indices.index_price_codec import PriceCodec,encode_day,decode_day,FIELDS

VERSION='v2'
OHLC_FIELDS=('open','high','low','close')
def version_for(provider, series_key):
    return 'v2' if provider in ('csindex','sws') else 'v1'

DEFINITIONS={
 ('csindex','history.points'):dict(fields={'open':'open','high':'high','low':'low','close':'close','tradingVol':'volume',
     'tradingValue':'amount','change':'change_points','changePct':'change_pct','consNumber':'constituent_count'},frequency='daily',dataset='index.cs.history'),
 ('sws','analysis.points'):dict(fields={'closeindex':'close',
     'bargainamount':'volume','markup':'change_pct','turnoverrate':'turnover_rate','bargainsumrate':'amount_ratio',
     'meanprice':'mean_price','negotiablessharesum1':'float_market_cap','negotiablessharesum2':'mean_float_market_cap'},frequency='daily',dataset='index.sw.analysis'),
 ('legu','pe.points'):dict(fields={'close':'close'},frequency='monthly',dataset='index.legu.pe'),
 ('legu','pb.points'):dict(fields={'close':'close'},frequency='monthly',dataset='index.legu.pb'),
 ('sina','history.points'):dict(fields={k:k for k in OHLC_FIELDS},frequency='daily',dataset='index.price.range_probe'),
}
for definition in DEFINITIONS.values():
    definition.update(unit='index_points',missing='source nulls remain null; absent fields remain null',
        observation_rule='actual source date; monthly moving observations retained',
        evidence='docs/index-data-collection.md#field-mapping')
DEFINITIONS['csindex','history.points'].update(field_metadata={
    'open':dict(raw='open',raw_unit='index points',standard_unit='index points',conversion='identity',null='source null retained'),
    'high':dict(raw='high',raw_unit='index points',standard_unit='index points',conversion='identity',null='source null retained'),
    'low':dict(raw='low',raw_unit='index points',standard_unit='index points',conversion='identity',null='source null retained'),
    'close':dict(raw='close',raw_unit='index points',standard_unit='index points',conversion='identity',null='source null retained'),
    'volume':dict(raw='tradingVol',raw_unit='shares',standard_unit='shares',conversion='identity',null='source null retained'),
    'amount':dict(raw='tradingValue',raw_unit='CNY 100 million',standard_unit='CNY',conversion='×100,000,000',null='source null retained'),
    'change_points':dict(raw='change',raw_unit='index points',standard_unit='index points',conversion='identity',null='source null retained'),
    'change_pct':dict(raw='changePct',raw_unit='percent',standard_unit='percent (2.5=2.5%)',conversion='identity',null='source null retained'),
    'constituent_count':dict(raw='consNumber',raw_unit='count',standard_unit='integer count',conversion='integer only',null='source null retained')})
DEFINITIONS['sws','analysis.points']['field_metadata']={
    'close':dict(raw='closeindex',raw_unit='index points',standard_unit='index points',conversion='identity',null='source null retained'),
    'volume':dict(raw='bargainamount',raw_unit='100 million shares',standard_unit='shares',conversion='×100,000,000',null='source null retained'),
    'change_pct':dict(raw='markup',raw_unit='percent',standard_unit='percent (2.5=2.5%)',conversion='identity',null='source null retained'),
    'turnover_rate':dict(raw='turnoverrate',raw_unit='percent (detailed basis undisclosed)',standard_unit='percent',conversion='identity',null='source null retained'),
    'amount_ratio':dict(raw='bargainsumrate',raw_unit='percent',standard_unit='percent (2.5=2.5%)',conversion='identity',null='source null retained'),
    'mean_price':dict(raw='meanprice',raw_unit='CNY',standard_unit='CNY',conversion='identity',null='source null retained'),
    'float_market_cap':dict(raw='negotiablessharesum1',raw_unit='CNY 100 million',standard_unit='CNY',conversion='×100,000,000',null='source null retained'),
    'mean_float_market_cap':dict(raw='negotiablessharesum2',raw_unit='CNY 100 million',standard_unit='CNY',conversion='×100,000,000',null='source null retained')}
DEFINITIONS['sws','analysis.points'].update(source_units={'bargainamount':'100 million shares','markup':'percent',
    'turnoverrate':'percent; source definition not further disclosed','bargainsumrate':'percent','meanprice':'CNY',
    'negotiablessharesum1':'CNY 100 million','negotiablessharesum2':'CNY 100 million'})
DEFINITIONS['csindex','history.points']['source_units']={'tradingVol':'shares','tradingValue':'CNY 100 million',
    'change':'index points','changePct':'percent','consNumber':'count'}


def legacy_definition(provider,key):
    fields={'csindex':{'open':'open','high':'high','low':'low','close':'close'},
        'sws':{'closeindex':'close'},'legu':{'close':'close'},'sina':{k:k for k in OHLC_FIELDS}}[provider]
    definition=DEFINITIONS[provider,key]
    legacy=dict(fields=fields,frequency=definition['frequency'],dataset=definition['dataset'],unit='index_points',
        missing='source nulls remain null; absent OHLC not derived',observation_rule='actual source date; monthly moving observations retained',
        evidence='docs/index-price-storage.md',volume_amount='not stored; source units not standardized')
    return legacy


def install_definitions(conn):
    for (provider,key),definition in DEFINITIONS.items():
        version=version_for(provider,key)
        body=encoded(definition if version==VERSION else legacy_definition(provider,key))
        old=conn.execute('SELECT definition_json FROM index_price_definitions WHERE provider=? AND series_key=? AND definition_version=?',
                         (provider,key,version)).fetchone()
        if old and old[0]!=body:
            raise ValueError('点位定义变化须增加版本')
        conn.execute('INSERT OR IGNORE INTO index_price_definitions VALUES(?,?,?,?,?)',(provider,key,version,body,utc_now()))


def validate_values(values):
    for field,value in values.items():
        if value is None: continue
        if isinstance(value,bool) or not math.isfinite(float(value)):
            raise ValueError('指数行情值须为有限数值或来源缺值')
        if field in ('open','high','low','close') and value<=0:
            raise ValueError('指数点位须为正数或来源缺值')
        if field in ('volume','amount','turnover_rate','amount_ratio','constituent_count','mean_price','float_market_cap','mean_float_market_cap') and value<0:
            raise ValueError('指数行情字段不允许负值')
        if field=='amount_ratio' and value>100:
            raise ValueError('成交额占比超出0—100%，拒绝裁剪')
    if values.get('constituent_count') is not None and not float(values['constituent_count']).is_integer():
        raise ValueError('成分数量必须为整数')
    for upper,lower in (('high','low'),('high','open'),('high','close'),('open','low'),('close','low')):
        if values[upper] is not None and values[lower] is not None and values[upper]<values[lower]:
            raise ValueError('指数开高低收关系异常')


def parse_prices(records, *, provider, series_key, code_key, date_key, start, end, trading_days,
                 batch_id, source_id, expected_code=None):
    # Use the exact same boundary-placeholder convention as valuation parsing.
    from stock_analysis.domains.indices.index_collection import day, number
    from stock_analysis.domains.indices.index_definitions import canonical
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
        present_fields=[]
        for source,target in fields.items():
            if source not in raw: continue
            present_fields.append(target)
            parsed=number(raw.get(source))
            if provider=='csindex' and target=='amount' and parsed is not None: parsed*=100_000_000
            if provider=='sws' and target in ('volume','float_market_cap','mean_float_market_cap') and parsed is not None:
                parsed*=100_000_000
            values[target]=int(parsed) if target=='constituent_count' and parsed is not None and parsed.is_integer() else parsed
        validate_values(values)
        identity=canonical(code,provider)
        rows.append(dict(index_code=identity,provider=provider,series_key=series_key,frequency=definition['frequency'],
            return_type='total_return' if identity=='cn:000680' else 'unspecified',definition_version=version_for(provider,series_key),
            observation_date=observed,**values,present_fields=present_fields,explicit_nulls=True,batch_id=batch_id,source_id=source_id))
    return rows,anomalies


def price_hash(values, definition, withdrawal_reason=None, lineage=None):
    if lineage is not None:
        values={k:(None if v is None else int(v) if k=='constituent_count' else float(v)) for k,v in values.items()}
    payload=dict(values=values,definition=definition,withdrawal_reason=withdrawal_reason)
    if lineage is not None:
        payload['field_lineage']=lineage
    return digest(encoded(payload).encode())


def publish_prices(conn, rows):
    install_definitions(conn)
    counts=dict(inserted=0,revised=0,unchanged=0)
    identities={}
    sources={}
    codec=PriceCodec(conn)
    seen=set()
    for row in rows:
        provider,key=row['provider'],row['series_key']
        version=row.get('definition_version',version_for(provider,key))
        definition=DEFINITIONS[provider,key] if version==VERSION else legacy_definition(provider,key)
        if version!=version_for(provider,key) or row['frequency']!=definition['frequency']:
            raise ValueError('点位频率或定义版本不匹配')
        observed=row['observation_date']
        if date.fromisoformat(observed).isoformat()!=observed:
            raise ValueError('点位日期须为ISO日期')
        identity=(row['index_code'],provider,key,row['frequency'],row['return_type'],version)
        if (*identity,observed) in seen:
            raise ValueError('点位分段重复序列／日期')
        seen.add((*identity,observed))
        stored_fields=FIELDS if version==VERSION else OHLC_FIELDS
        present_fields=set(row.get('present_fields',(k for k in stored_fields if k in row)))
        values={field:(row.get(field) if field in present_fields else None) for field in FIELDS}
        if any(values[field] is not None and field not in definition['fields'].values() for field in stored_fields):
            raise ValueError('点位含未定义字段')
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
        old=conn.execute('SELECT * FROM index_price_observations WHERE series_id=? AND observation_date=?',(sid,encode_day(observed))).fetchone()
        if old:
            old=codec.decode_rows([dict(old)])[0]
        lineage={} if not old else json.loads(old['field_lineage_json'] or '{}')
        raw_meta=lineage.setdefault('_raw',{'revision':0,'fields':{}})
        field_meta=raw_meta.setdefault('fields',{})
        if old:
            for field in stored_fields:
                if field not in present_fields:
                    values[field]=old[field]
                elif values[field] is None and old[field] is not None and field not in lineage.get('estimates',{}) and not row.get('withdrawal_reason'):
                    raise ValueError('点位有效值变为空须核实撤回原因')
        raw_changed=False
        for field in present_fields:
            if field not in stored_fields:
                continue
            was_estimate=field in lineage.get('estimates',{})
            if old is not None and old[field]==values[field] and not was_estimate:
                continue
            if values[field] is None:
                field_meta.pop(field,None)
                lineage.get('estimates',{}).pop(field,None)
            else:
                field_meta[field]={'source_id':row['source_id'],'batch_id':row['batch_id'],
                                   'obtained_at':source['obtained_at']}
                lineage.get('estimates',{}).pop(field,None)
            raw_changed=True
        validate_values(values)
        if old and not raw_changed and old['withdrawal_reason']==row.get('withdrawal_reason'):
            counts['unchanged']+=1
            continue
        if raw_changed:
            raw_meta['revision']=int(raw_meta.get('revision',0))+1
        if not raw_meta['revision']:
            raw_meta['revision']=1
        hash_values={field:values[field] for field in stored_fields}
        if all(v is None for v in hash_values.values()) and not (row.get('explicit_nulls') or row.get('withdrawal_reason')):
            raise ValueError('点位全空须有完整来源字段或撤回说明')
        content_hash=price_hash(hash_values,definition,row.get('withdrawal_reason'),lineage if version==VERSION else None)
        if old and old['content_hash']==content_hash:
            counts['unchanged']+=1
            continue
        payload=dict(**values,withdrawal_reason=row.get('withdrawal_reason'),field_lineage_json=encoded(lineage),
            content_hash=content_hash,version=old['version']+1 if old else 1,is_latest=1,
            batch_id=row['batch_id'],source_id=row['source_id'],obtained_at=source['obtained_at'],updated_at=utc_now())
        context={**(old or {}),**row,'series_id':sid,'definition_version':version}
        payload=codec.encode_payload(payload,context)
        if old:
            conn.execute('UPDATE index_price_observations SET '+','.join(k+'=?' for k in payload)+' WHERE id=?',
                         (*payload.values(),old['id']))
        else:
            payload=dict(series_id=sid,observation_date=encode_day(observed),**payload)
            conn.execute('INSERT INTO index_price_observations('+','.join(payload)+') VALUES('+','.join('?' for _ in payload)+')',tuple(payload.values()))
        counts['revised' if old else 'inserted']+=1
    return counts


def read_prices(db, *, index_code, provider, series_key, frequency, return_type, start=None, end=None, definition_version=None, fields=None):
    """Explicit series selection; no source stitching or daily synthesis."""
    if fields is not None and (not fields or any(field not in FIELDS for field in fields)):
        raise ValueError('行情读取字段未定义')
    projection='f.*' if fields is None else 'f.observation_date,'+','.join('f.'+field for field in fields)
    with db.connection() as conn:
        if definition_version is None:
            preferred=version_for(provider,series_key)
            exists=conn.execute('SELECT 1 FROM index_price_series WHERE index_code=? AND provider=? AND series_key=? '
                'AND frequency=? AND return_type=? AND definition_version=? LIMIT 1',
                (index_code,provider,series_key,frequency,return_type,preferred)).fetchone()
            if not exists:
                definition_version='v1'
            else:
                definition_version=preferred
        rows=[dict(r) for r in conn.execute('SELECT '+projection+',s.index_code,s.provider,s.series_key,s.frequency,s.return_type,s.definition_version '
            'FROM index_price_series s JOIN index_price_observations f ON f.series_id=s.id '
            'WHERE s.index_code=? AND s.provider=? AND s.series_key=? AND s.frequency=? AND s.return_type=? '
            'AND s.definition_version=? AND f.observation_date BETWEEN ? AND ? ORDER BY f.observation_date',
            (index_code,provider,series_key,frequency,return_type,definition_version,encode_day(start or '0001-01-01'),encode_day(end or '9999-12-31')))]
        if fields is None:
            rows=PriceCodec(conn).decode_rows(rows)
        else:
            for row in rows:row['observation_date']=decode_day(row['observation_date'])
    if frequency=='monthly':
        latest={r['observation_date'][:7]:r for r in rows}
        rows=list(latest.values())
    return rows


def read_overlay(db, index_code, *, field='close'):
    """Pick one real close series for an index without mixing providers or bases."""
    if field not in ('close', 'amount_ratio'):
        raise ValueError('不支持的指数行情指标')
    with db.connection() as conn:
        identity=conn.execute('SELECT s.id,s.index_code,s.provider,s.series_key,s.frequency,s.return_type,s.definition_version,'
            'count(f.close) AS observations,min(CASE WHEN f.close IS NOT NULL THEN f.observation_date END) AS first_date,'
            'max(CASE WHEN f.close IS NOT NULL THEN f.observation_date END) AS last_date '
            'FROM index_price_series s JOIN index_price_observations f ON f.series_id=s.id '
            f'WHERE s.index_code=? AND f.{field} IS NOT NULL '+("AND s.frequency='daily' " if field=='amount_ratio' else '')+'GROUP BY s.id '
            'ORDER BY CASE WHEN s.frequency=\'daily\' THEN 0 ELSE 1 END,'
            'CASE WHEN s.provider=? THEN 0 WHEN s.provider=\'csindex\' THEN 1 WHEN s.provider=\'sws\' THEN 2 WHEN s.provider=\'sina\' THEN 3 WHEN s.provider=\'legu\' THEN 4 ELSE 5 END,'
            'CASE WHEN s.return_type=\'unspecified\' THEN 0 ELSE 1 END,s.series_key,CASE WHEN s.definition_version=\'v2\' THEN 0 ELSE 1 END,s.definition_version LIMIT 1',
            (index_code,'sws' if index_code.startswith('sw:') else 'csindex')).fetchone()
        if identity is None:
            return dict(index_code=index_code,available=False,rows=[])
        rows=[dict(r) for r in conn.execute(f'SELECT observation_date,close,{field} FROM index_price_observations '
            'WHERE series_id=? '+('AND close IS NOT NULL ' if field=='close' else '')+'ORDER BY observation_date',(identity['id'],))]
    for row in rows:row['observation_date']=decode_day(row['observation_date'])
    valid=[row for row in rows if row[field] is not None]
    return dict(index_code=index_code,available=bool(rows),provider=identity['provider'],series_key=identity['series_key'],
        frequency=identity['frequency'],return_type=identity['return_type'],definition_version=identity['definition_version'],
        observations=len(valid),first_date=valid[0]['observation_date'],last_date=valid[-1]['observation_date'],rows=rows)


def price_coverage(rows):
    """Field counts describe returned data, never infer a historical universe."""
    groups={}
    for row in rows:
        key=(row['index_code'],row['provider'],row['series_key'],row['frequency'],row['return_type'],row.get('definition_version',version_for(row['provider'],row['series_key'])))
        group=groups.setdefault(key,dict(index_code=key[0],provider=key[1],series_key=key[2],frequency=key[3],return_type=key[4],definition_version=key[5],dates=set(),fields={k:set() for k in FIELDS}))
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
