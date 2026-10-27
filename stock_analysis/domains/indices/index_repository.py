"""Atomic segmented publication and explicit, provenance-preserving index queries."""
from __future__ import annotations

from datetime import date
import math

from stock_analysis.collection.collection import digest, encoded
from stock_analysis.domains.indices.index_definitions import DEFINITIONS, METRICS, VERSION
from stock_analysis.infrastructure.storage import utc_now
from stock_analysis.domains.valuation.valuation import valuation_summary_observations


def index_valuation_views(rows, metrics=METRICS):
    """Use the stock page's historical sampling and thresholds without mixing series."""
    views = {}
    for metric in metrics:
        target = 'pe' if metric.startswith('pe_') else 'ps' if metric.startswith('ps') else metric
        observations = []
        for row in rows:
            value = row.get(metric)
            valid = value is not None and (value >= 0 if target == 'dividend_yield' else value > 0)
            point = {'date': row['observation_date'], target: value if valid else None}
            if target == 'pe':
                point['pe_raw'] = value
            observations.append(point)
        views[metric] = {str(years): valuation_summary_observations(observations, target, years, {})
                         for years in (3, 5, 10)}
        if rows and all(row['frequency'] == 'monthly' for row in rows):
            for view in views[metric].values():
                # Monthly observations cannot be represented as daily/weekly history.
                view['rows_by_frequency']['day'] = []
                view['rows_by_frequency']['week'] = []
    return views


def install_definitions(conn):
    for (provider,series),definition in DEFINITIONS.items():
        body=encoded(definition)
        old=conn.execute('SELECT definition_json FROM index_metric_definitions WHERE provider=? AND series_key=? AND definition_version=?',
                         (provider,series,VERSION)).fetchone()
        if old and old[0]!=body:
            raise ValueError('指标定义改变须增加 definition_version，不能原地覆盖')
        conn.execute('INSERT OR IGNORE INTO index_metric_definitions VALUES(?,?,?,?,?)',(provider,series,VERSION,body,utc_now()))


def catalog(conn, code, name, kind, *, provider, provider_code, source_id=None, level=None,
            parent=None, valid_from=None, valid_to=None, system=None):
    """Do not infer historical effective dates from the first observation."""
    stamp=utc_now()
    conn.execute('INSERT OR IGNORE INTO index_identities VALUES(?,?)',(code,stamp))
    if parent:
        # Across historical versions the stable identity graph must also remain acyclic.
        cycle=conn.execute('WITH RECURSIVE ancestors(code) AS (SELECT ? UNION '
            'SELECT c.parent_index_code FROM index_catalog c JOIN ancestors a ON c.index_code=a.code WHERE c.parent_index_code IS NOT NULL) '
            'SELECT 1 FROM ancestors WHERE code=?',(parent,code)).fetchone()
        if cycle:
            raise ValueError('指数父级存在环')
    status='confirmed' if valid_from else 'unknown'
    if status=='unknown':
        old=conn.execute("SELECT * FROM index_catalog WHERE index_code=? AND validity_status='unknown'",(code,)).fetchone()
        # Unknown records describe current navigation only. Historical reads mark them unresolved.
        if old is None:
            conn.execute('INSERT INTO index_catalog(index_code,name,index_type,classification_system,industry_level,parent_index_code,'
                'valid_from,valid_to,validity_status,mapping_status,source_id,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (code,name,kind,system,level,parent,None,None,status,'pending' if kind=='industry' and level and level>1 and not parent else 'confirmed',source_id,stamp))
    else:
        conn.execute('INSERT INTO index_catalog(index_code,name,index_type,classification_system,industry_level,parent_index_code,'
            'valid_from,valid_to,validity_status,mapping_status,source_id,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (code,name,kind,system,level,parent,valid_from,valid_to,status,'confirmed',source_id,stamp))
    mapping=conn.execute('SELECT index_code FROM index_provider_codes WHERE provider=? AND provider_code=? '
                        'AND valid_from IS ? AND valid_to IS ?',(provider,provider_code,valid_from,valid_to)).fetchone()
    if mapping:
        if mapping[0]!=code:
            raise ValueError('提供方代码映射冲突')
    else:
        conn.execute('INSERT INTO index_provider_codes(index_code,provider,provider_code,valid_from,valid_to,source_id,updated_at) VALUES(?,?,?,?,?,?,?)',
                     (code,provider,provider_code,valid_from,valid_to,source_id,stamp))


def publish(conn, rows):
    install_definitions(conn)
    counts=dict(inserted=0,revised=0,unchanged=0)
    seen=set()
    series_ids={}
    for row in rows:
        provider,series=row['provider'],row['series_key']
        definition=DEFINITIONS[provider,series]
        version=row.get('definition_version',VERSION)
        if version!=VERSION:
            raise ValueError('未登记的定义版本')
        day=row['observation_date']
        if date.fromisoformat(day).isoformat()!=day:
            raise ValueError('观察日期须为 ISO 日期')
        fields=set(definition['fields'].values())
        values={key:row.get(key) for key in METRICS}
        for key,value in values.items():
            if value is not None and (key not in fields or isinstance(value,bool) or not math.isfinite(float(value))):
                raise ValueError('事实包含无定义或无效的指标')
        if all(values[key] is None for key in fields) and not row.get('withdrawal_reason'):
            # A complete, explicitly-null source row is valid missing data, not an inferred withdrawal.
            if not row.get('explicit_nulls'):
                raise ValueError('全空值必须来自完整来源字段或显式撤回')
        key=(row['index_code'],provider,series,day,row['frequency'],version)
        if key in seen:
            raise ValueError('同一分段重复观察')
        seen.add(key)
        source=conn.execute('SELECT s.obtained_at,s.parse_status,s.source_system,b.status FROM collection_sources s '
            'JOIN collection_batches b ON b.id=s.batch_id WHERE s.id=? AND s.batch_id=?',(row['source_id'],row['batch_id'])).fetchone()
        if not source or source['parse_status']!='complete' or source['source_system']!=provider or source['status']!='running':
            raise ValueError('事实来源未通过解析或批次／提供方不匹配')
        content_hash=digest(encoded(dict(values=values,definition=definition,withdrawal_reason=row.get('withdrawal_reason'))).encode())
        identity=(row['index_code'],provider,series,row['frequency'],version)
        if identity not in series_ids:
            conn.execute('INSERT OR IGNORE INTO index_valuation_series(index_code,provider,series_key,frequency,definition_version,created_at) '
                         'VALUES(?,?,?,?,?,?)',(*identity,utc_now()))
            series_ids[identity]=conn.execute('SELECT id FROM index_valuation_series WHERE index_code=? AND provider=? '
                'AND series_key=? AND frequency=? AND definition_version=?',identity).fetchone()[0]
        series_id=series_ids[identity]
        old=conn.execute('SELECT id,version,content_hash FROM index_valuation_observations '
            'WHERE series_id=? AND observation_date=? AND is_latest=1',(series_id,day)).fetchone()
        if old and not row.get('withdrawal_reason'):
            previous=conn.execute('SELECT '+','.join(METRICS)+' FROM index_valuation_observations WHERE id=?',(old['id'],)).fetchone()
            if any(previous[metric] is not None and values[metric] is None for metric in fields):
                raise ValueError('已发布有效值变为空值须核实撤回原因；不静默覆盖')
        if old and old['content_hash']==content_hash:
            counts['unchanged']+=1
            continue
        if old:
            conn.execute('UPDATE index_valuation_observations SET is_latest=0 WHERE id=?',(old['id'],))
        payload={'series_id':series_id,'observation_date':day,**values,
                 'content_hash':content_hash,'version':old['version']+1 if old else 1,'is_latest':1,
                 'batch_id':row['batch_id'],'source_id':row['source_id'],'obtained_at':source['obtained_at'],
                 'updated_at':utc_now(),'withdrawal_reason':row.get('withdrawal_reason')}
        conn.execute('INSERT INTO index_valuation_observations('+','.join(payload)+') VALUES ('+','.join('?' for _ in payload)+')',tuple(payload.values()))
        counts['revised' if old else 'inserted']+=1
    return counts


def map_current_sw_navigation(conn, catalog_rows):
    """Map current SW2021 taxonomy names, never rewrite historical index identities."""
    taxonomy = [dict(row) for row in conn.execute(
        "SELECT code,name,level,parent_code FROM sw_industries WHERE standard='SW2021' AND level IN (1,2)")]
    by_code = {row['code']: row for row in taxonomy}
    tax_names, index_names = {}, {}
    eligible = [row for row in catalog_rows if row['index_type']=='industry'
                and row['index_code'].startswith('sw:')
                and (row['classification_system'] or '').lower() in ('sw','sw2021')]
    for row in taxonomy:
        tax_names.setdefault((row['name'], row['level']), []).append(row)
    for row in eligible:
        index_names.setdefault((row['name'], row['industry_level']), []).append(row)
    for row in eligible:
        matches = tax_names.get((row['name'], row['industry_level']), [])
        if len(matches)!=1 or len(index_names[(row['name'], row['industry_level'])])!=1:
            continue
        tax = matches[0]
        row['navigation_taxonomy_code'] = tax['code']
        if row['industry_level']!=2 or row['parent_index_code']:
            continue
        parent = by_code.get(tax['parent_code'])
        if not parent or parent['level']!=1 or len(tax_names.get((parent['name'], 1), []))!=1:
            continue
        parents = index_names.get((parent['name'], 1), [])
        if len(parents)!=1 or parents[0]['classification_system']!=row['classification_system']:
            continue
        row['navigation_parent_index_code'] = parents[0]['index_code']
        row['navigation_mapping_source'] = 'sw_industries:SW2021:name+level'


def read_indices(db, *, code=None, kind=None, level=None, provider=None, series=None,
                 definition_version=VERSION, start=None, end=None, weekly=False):
    # Keep captured moving monthly snapshots, show the latest actual date per month.
    current="WITH current AS (SELECT f.*,s.index_code,s.provider,s.series_key,s.frequency,s.definition_version "\
        "FROM index_valuation_observations f JOIN index_valuation_series s ON s.id=f.series_id WHERE f.is_latest=1 AND (s.frequency='daily' OR NOT EXISTS ("\
        "SELECT 1 FROM index_valuation_observations n WHERE n.is_latest=1 AND n.series_id=f.series_id "\
        "AND substr(n.observation_date,1,7)=substr(f.observation_date,1,7) AND n.observation_date>f.observation_date))) "
    with db.connection() as conn:
        # Navigation uses the current known version; unknown validity is visibly labelled.
        catalog_rows=[dict(r) for r in conn.execute('SELECT c.* FROM index_catalog c WHERE c.id=(SELECT x.id FROM index_catalog x '
            'WHERE x.index_code=c.index_code AND (x.valid_to IS NULL OR x.valid_to>=date(\'now\')) AND (x.valid_from IS NULL OR x.valid_from<=date(\'now\')) '
            "ORDER BY x.validity_status='confirmed' DESC,x.valid_from DESC,x.id DESC LIMIT 1) ORDER BY c.index_type,c.industry_level,c.name")]
        map_current_sw_navigation(conn, catalog_rows)
        if kind:
            catalog_rows=[r for r in catalog_rows if r['index_type']==kind]
        if level is not None:
            catalog_rows=[r for r in catalog_rows if r['industry_level']==int(level)]
        summaries=[dict(r) for r in conn.execute(current+'SELECT index_code,provider,series_key,frequency,definition_version,min(observation_date) AS first_date,'
            'max(observation_date) AS last_date,count(*) AS observations,'+
            ','.join(f'count({metric}) AS {metric}_count,min(CASE WHEN {metric} IS NOT NULL THEN observation_date END) AS {metric}_first_date,'
                     f'max(CASE WHEN {metric} IS NOT NULL THEN observation_date END) AS {metric}_last_date' for metric in METRICS)+
            ' FROM current GROUP BY index_code,provider,series_key,frequency,definition_version')]
        batches=[dict(r) for r in conn.execute('SELECT * FROM collection_batches WHERE domain=\'market\' AND dataset LIKE \'index%\' ORDER BY id DESC LIMIT 40')]
        cooldowns=[dict(r) for r in conn.execute('SELECT * FROM collection_provider_state WHERE cooldown_until IS NOT NULL')]
        operations=[dict(r) for r in conn.execute('SELECT * FROM collection_operations WHERE action IS NULL AND task_key<>\'stocks\' ORDER BY started_at DESC LIMIT 5')]
        rows=[]
        views={}
        if code:
            if not provider or not series:
                raise ValueError('查询历史须明确选择提供方和序列')
            identities=conn.execute('SELECT * FROM index_valuation_series WHERE index_code=? AND provider=? '
                'AND series_key=? AND definition_version=?',(code,provider,series,definition_version)).fetchall()
            for identity in identities:
                monthly=" AND NOT EXISTS(SELECT 1 FROM index_valuation_observations n WHERE n.series_id=f.series_id AND n.is_latest=1 AND substr(n.observation_date,1,7)=substr(f.observation_date,1,7) AND n.observation_date>f.observation_date)" if identity['frequency']=='monthly' else ''
                for record in conn.execute('SELECT f.* FROM index_valuation_observations f WHERE f.series_id=? AND f.is_latest=1 '
                    'AND f.observation_date>=? AND f.observation_date<=?'+monthly+' ORDER BY f.observation_date',
                    (identity['id'],start or '0001-01-01',end or '9999-12-31')):
                    item=dict(record)
                    item.pop('series_id')
                    item.update({field:identity[field] for field in ('index_code','provider','series_key','frequency','definition_version')})
                    rows.append(item)
            rows.sort(key=lambda row:row['observation_date'])
            views=index_valuation_views(rows)
            if weekly:
                if any(r['frequency']!='daily' for r in rows):
                    raise ValueError('月频来源不能派生周频')
                groups={}
                for row in rows:
                    groups[date.fromisoformat(row['observation_date']).isocalendar()[:2]]=row
                rows=list(groups.values())
            histories=[dict(r) for r in conn.execute("SELECT * FROM index_catalog WHERE index_code=? AND validity_status='confirmed'",(code,))]
            for row in rows:
                row['catalog_as_of']=next((c for c in histories if c['valid_from']<=row['observation_date']<=(c['valid_to'] or '9999-12-31')),None)
                row['catalog_status']='confirmed' if row['catalog_as_of'] else 'historical_classification_pending'
        definitions=[dict(r) for r in conn.execute('SELECT * FROM index_metric_definitions')]
    return dict(catalog=catalog_rows,coverage=summaries,rows=rows,batches=batches,cooldowns=cooldowns,
                operations=operations,definitions=definitions,views=views)
