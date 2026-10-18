"""Versioned compact storage for index prices; logical values stay unchanged.

Format 2: [2, raw_revision, raw_field_mask, [[field_index,source_id],...],
           [[field_index,raw_revision,source_id,input,market_id,market_version,
             computed_at_microseconds,input_source_id_or_null],... ] or null].
Unknown/withdrawn snapshots use {"fallback": full_lineage}; never discard evidence.
"""
from datetime import date,datetime,timedelta,timezone
import json

from .collection import encoded,digest

FIELDS=('open','high','low','close','volume','amount','change_points','change_pct','turnover_rate',
        'amount_ratio','constituent_count','mean_price','float_market_cap','mean_float_market_cap')
FORMULAS={'csindex':'same-day raw amount / Legu all-A total amount * 100',
          'sws':'same-day raw amount_ratio / 100 * Legu all-A total amount'}
BASIS='Legu all-A; excludes block trades; universe may differ'


def estimate_definition(provider, market):
    if market['provider']=='bigquant':
        formula=('same-day raw amount / BigQuant calculated all-A total amount * 100' if provider=='csindex'
                 else 'same-day raw amount_ratio / 100 * BigQuant calculated all-A total amount')
        return (formula,
                'BigQuant cn_stock_real_bar1d all-A amount sum; estimated fallback; universe may differ')
    return FORMULAS[provider], BASIS


EPOCH=datetime(1970,1,1,tzinfo=timezone.utc)


def encode_day(value):
    return (date.fromisoformat(value)-EPOCH.date()).days if isinstance(value,str) else value


def decode_day(value):
    return (EPOCH.date()+timedelta(days=value)).isoformat() if isinstance(value,int) else value


def encode_time(value):
    if not isinstance(value,str):return value
    delta=datetime.fromisoformat(value)-EPOCH
    micros=(delta.days*86400+delta.seconds)*1_000_000+delta.microseconds
    return micros if decode_time(micros)==value else value


def decode_time(value):
    return (EPOCH+timedelta(microseconds=value)).isoformat(timespec='microseconds') if isinstance(value,int) else value


def logical_hash(value):
    return bytes(value).hex() if isinstance(value,(bytes,memoryview)) else value


class PriceCodec:
    def __init__(self,conn):
        self.conn=conn;self.sources={};self.markets={};self.series={}

    def _load(self,table,ids,cache,columns='*'):
        pending=list(set(ids)-cache.keys())
        for i in range(0,len(pending),500):
            batch=pending[i:i+500]
            for r in self.conn.execute('SELECT '+columns+' FROM '+table+' WHERE id IN ('+','.join('?' for _ in batch)+')',batch):
                cache[r['id']]=dict(r)
        if any(sid not in cache for sid in ids):raise ValueError('行情溯源父引用不存在')

    def prefetch(self,rows):
        sources=set();markets=set();series=set()
        for row in rows:
            sources.add(row['source_id'])
            if 'provider' not in row.keys():series.add(row['series_id'])
            body=row['field_lineage_json']
            value=json.loads(body) if isinstance(body,str) else body
            if isinstance(value,list) and value:
                if value[0]!=2:raise ValueError('未知行情溯源格式版本')
                sources.update(sid for _,sid in value[3])
                for e in value[4] or []:
                    sources.add(e[2]);markets.add(e[4])
                    if e[7] is not None:sources.add(e[7])
            elif isinstance(value,dict):
                value=value.get('fallback',value)
                sources.update(v['source_id'] for v in value.get('_raw',{}).get('fields',{}).values())
                for e in value.get('estimates',{}).values():
                    if e.get('parent_market'):markets.add(e['parent_market']['fact_id'])
                    if e.get('parent_index'):sources.add(e['parent_index']['source_id'])
        self._load('collection_sources',sources,self.sources,'id,batch_id,obtained_at')
        self._load('market_indicator_observations',markets,self.markets)
        self._load('index_price_series',series,self.series)

    def source(self,sid):
        if sid not in self.sources:self._load('collection_sources',[sid],self.sources,'id,batch_id,obtained_at')
        source=self.sources[sid]
        return dict(source_id=sid,batch_id=source['batch_id'],obtained_at=source['obtained_at'])

    def context(self,row):
        result=dict(row)
        if 'provider' not in result:
            if row['series_id'] not in self.series:self._load('index_price_series',[row['series_id']],self.series)
            result.update({k:v for k,v in self.series[row['series_id']].items() if k!='id'})
        result['observation_date']=decode_day(row['observation_date'])
        result['content_hash']=logical_hash(row.get('content_hash'))
        result['updated_at']=decode_time(row.get('updated_at'))
        result.update(self.source(row['source_id']),is_latest=1)
        return result

    def expand(self,value,row):
        if isinstance(value,dict):return value.get('fallback',value)
        if not value:return {}
        if value[0]!=2:raise ValueError('未知行情溯源格式版本')
        raw_fields={}
        overrides=dict(value[3])
        for i,field in enumerate(FIELDS):
            if value[2] & (1<<i):raw_fields[field]=self.source(overrides.get(i,row['source_id']))
        result={'_raw':{'revision':value[1],'fields':raw_fields}}
        if value[4] is not None:
            result['estimates']={}
            for i,revision,source_id,raw,market_id,market_version,at,input_source in value[4]:
                provider=row['provider'];field=FIELDS[i];raw_field='amount' if provider=='csindex' else 'amount_ratio'
                source=self.source(source_id);input_meta=self.source(input_source if input_source is not None else source_id)
                market=self.markets[market_id]
                if market['version']!=market_version:raise ValueError('估算父事实版本不一致')
                formula,basis=estimate_definition(provider,market)
                result['estimates'][field]={
                    'kind':'estimated','raw_target_value':None,'value':row[field],'formula':formula,
                    'upstream_status':'current','input_index_field':raw_field,'input_index_value':raw,
                    'input_index_fingerprint':digest(encoded({'fact_id':row['id'],'raw_revision':revision,
                        'field':raw_field,'value':raw,'field_source':input_meta}).encode()),
                    'parent_index':{'fact_id':row['id'],'series_id':row['series_id'],'index_code':row['index_code'],
                        'provider':provider,'series_key':row['series_key'],'frequency':row['frequency'],
                        'return_type':row['return_type'],'definition_version':row['definition_version'],
                        'observation_date':row['observation_date'],**source,'raw_revision':revision},
                    'parent_market':{'fact_id':market['id'],'version':market['version'],'provider':market['provider'],
                        'channel':market['channel'],'definition_version':market['definition_version'],
                        'source_id':market['source_id'],'batch_id':market['batch_id'],'observation_date':market['observation_date'],
                        'obtained_at':market['obtained_at'],'total_amount_yi':market['total_amount_yi']},
                    'denominator_cny':float(market['total_amount_yi'])*100_000_000,'market_basis':basis,'computed_at':decode_time(at)}
        return result

    def compact(self,lineage,row):
        if not lineage:return []
        self.prefetch([{**row,'field_lineage_json':encoded(lineage)}])
        raw=lineage.get('_raw')
        if not raw:return {'fallback':lineage}
        try:
            mask=sum(1<<FIELDS.index(field) for field in raw['fields'])
            overrides=[[FIELDS.index(f),meta['source_id']] for f,meta in raw['fields'].items() if meta['source_id']!=row['source_id']]
            estimates=None
            if 'estimates' in lineage:
                estimates=[]
                for field,e in lineage['estimates'].items():
                    if e['upstream_status']!='current':return {'fallback':lineage}
                    sid=e['parent_index']['source_id'];input_sid=raw['fields'][e['input_index_field']]['source_id']
                    estimates.append([FIELDS.index(field),e['parent_index']['raw_revision'],sid,e['input_index_value'],
                        e['parent_market']['fact_id'],e['parent_market']['version'],encode_time(e['computed_at']),
                        input_sid if input_sid!=sid else None])
            compact=[2,raw['revision'],mask,overrides,estimates]
            return compact if self.expand(compact,row)==lineage else {'fallback':lineage}
        except (KeyError,ValueError,TypeError):
            return {'fallback':lineage}

    def decode_rows(self,rows):
        rows=[dict(r) for r in rows];self.prefetch(rows)
        result=[]
        for row in rows:
            value=self.context(row)
            value['field_lineage_json']=encoded(self.expand(json.loads(row['field_lineage_json']),value))
            result.append(value)
        return result

    def encode_payload(self,payload,context):
        row={**context,**payload}
        source=self.source(row['source_id'])
        if row.get('batch_id',source['batch_id'])!=source['batch_id'] or row.get('obtained_at',source['obtained_at'])!=source['obtained_at']:
            raise ValueError('行情来源批次或真实时间不一致')
        if row.get('is_latest',1)!=1:raise ValueError('紧凑行情只保留当前一行')
        lineage=json.loads(row['field_lineage_json'])
        physical={k:v for k,v in payload.items() if k not in ('batch_id','obtained_at','is_latest')}
        physical['field_lineage_json']=encoded(self.compact(lineage,row))
        if 'observation_date' in physical:physical['observation_date']=encode_day(physical['observation_date'])
        if 'updated_at' in physical:physical['updated_at']=encode_time(physical['updated_at'])
        if 'content_hash' in physical:physical['content_hash']=bytes.fromhex(logical_hash(physical['content_hash']))
        return physical


def migrate(conn):
    codec=PriceCodec(conn);last=0
    names=[r['name'] for r in conn.execute('PRAGMA table_info(index_price_observations_compact)')]
    while True:
        rows=[dict(r) for r in conn.execute('SELECT f.*,s.index_code,s.provider,s.series_key,s.frequency,s.return_type,s.definition_version FROM index_price_observations f JOIN index_price_series s ON s.id=f.series_id WHERE f.id>? ORDER BY f.id LIMIT 1024',(last,))]
        if not rows:break
        codec.prefetch(rows)
        for row in rows:
            if row['is_latest']!=1:raise ValueError('升级前行情不是单行模型')
            values=codec.encode_payload({k:row[k] for k in names},row)
            # Resolve normalized provenance even though these columns are omitted above.
            if row['batch_id']!=codec.source(row['source_id'])['batch_id'] or row['obtained_at']!=codec.source(row['source_id'])['obtained_at']:raise ValueError('升级前来源不一致')
            restored=codec.decode_rows([{**row,**values}])[0]
            if any(restored[k]!=row[k] for k in names if k not in ('content_hash','field_lineage_json')) or restored['content_hash']!=row['content_hash'] or json.loads(restored['field_lineage_json'])!=json.loads(row['field_lineage_json']):
                raise ValueError('紧凑迁移还原不一致')
            conn.execute('INSERT INTO index_price_observations_compact('+','.join(values)+') VALUES('+','.join('?' for _ in values)+')',tuple(values.values()))
            last=row['id']
    conn.execute('DROP TABLE index_price_observations')
    conn.execute('ALTER TABLE index_price_observations_compact RENAME TO index_price_observations')
    conn.execute('CREATE UNIQUE INDEX index_price_one_row_per_date ON index_price_observations(series_id,observation_date)')
    conn.execute('CREATE INDEX index_price_by_date ON index_price_observations(observation_date,series_id)')
    for action in ('insert','update','delete'):
        conn.execute(f"CREATE TRIGGER index_read_index_price_observations_{action} AFTER {action.upper()} ON index_price_observations BEGIN UPDATE index_read_revisions SET revision=revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE scope='indices'; END")
    conn.execute("UPDATE index_read_revisions SET revision=revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE scope='indices'")
    columns=[r['name'] for r in conn.execute('PRAGMA table_info(index_price_observations)')]
    select=[]
    for col in columns:
        if col=='observation_date':select.append("date(f.observation_date*86400,'unixepoch') AS observation_date")
        elif col=='content_hash':select.append('lower(hex(f.content_hash)) AS content_hash')
        elif col=='updated_at':select.append("CASE WHEN typeof(f.updated_at)='integer' THEN strftime('%Y-%m-%dT%H:%M:%S',(f.updated_at-(f.updated_at%1000000+1000000)%1000000)/1000000,'unixepoch')||printf('.%06d+00:00',(f.updated_at%1000000+1000000)%1000000) ELSE f.updated_at END AS updated_at")
        else:select.append('f.'+col)
    conn.execute('CREATE VIEW index_price_observations_read AS SELECT '+','.join(select)+',c.batch_id,c.obtained_at,1 AS is_latest FROM index_price_observations f JOIN collection_sources c ON c.id=f.source_id')


def check_storage(conn):
    invalid=conn.execute("""SELECT count(*) FROM index_price_observations WHERE
        typeof(observation_date)<>'integer' OR typeof(content_hash)<>'blob' OR length(content_hash)<>32 OR
        CASE json_type(field_lineage_json)
          WHEN 'array' THEN NOT (json_array_length(field_lineage_json)=0 OR
            (json_array_length(field_lineage_json)=5 AND json_extract(field_lineage_json,'$[0]')=2
             AND json_type(field_lineage_json,'$[1]')='integer' AND json_type(field_lineage_json,'$[2]')='integer'
             AND json_type(field_lineage_json,'$[3]')='array' AND json_type(field_lineage_json,'$[4]') IN ('array','null')))
          WHEN 'object' THEN NOT (field_lineage_json='{}' OR coalesce(json_type(field_lineage_json,'$.fallback'),'')='object')
          ELSE 1 END""").fetchone()[0]
    if invalid:raise ValueError('紧凑行情存储格式无效：'+str(invalid)+'行')
