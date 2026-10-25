"""Read-only, targeted acceptance for the single-row index price model.

SQL summaries cover price facts; source/hash/formula checks use deterministic
first/middle/last samples per series. This is not a repeated historical full audit.
"""
import argparse
import json
import math
from pathlib import Path

from stock_analysis.collection.collection import atomic_write, encoded, read_source, digest
from stock_analysis.domains.indices.index_collection import IndexCollector, day
from stock_analysis.domains.indices.index_prices import FIELDS, OHLC_FIELDS, DEFINITIONS, legacy_definition, parse_prices, price_hash
from stock_analysis.domains.indices.index_price_replay import DATASETS
from stock_analysis.infrastructure.storage import Database, DEFAULT_DATABASE


def audit(db, *, report_path=None, samples_per_series=3):
    if samples_per_series<1:
        raise ValueError('每序列抽样数须大于0')
    differences=[]
    with db.connection() as conn:
        coverage=[dict(r) for r in conn.execute(
            'SELECT s.*,count(*) AS observations,min(f.observation_date) AS first,max(f.observation_date) AS last,'+
            ','.join(f'count(f.{k}) AS {k}_valid' for k in FIELDS)+','+
            ','.join(f"sum(CASE WHEN f.{k} IS NOT NULL AND (EXISTS(SELECT 1 FROM json_each(json_extract(f.field_lineage_json,'$[4]')) e WHERE json_extract(e.value,'$[0]')={5 if k=='amount' else 9}) OR json_extract(f.field_lineage_json,'$.fallback.estimates.{k}.kind')='estimated') THEN 1 ELSE 0 END) AS {k}_estimated" for k in ('amount','amount_ratio'))+
            ' FROM index_price_series s JOIN index_price_observations_read f ON f.series_id=s.id GROUP BY s.id')]
        for series in coverage:
            for field in ('amount','amount_ratio'):
                series[field+'_raw_valid']=series[field+'_valid']-series[field+'_estimated']
        structural={
            'rows':conn.execute('SELECT count(*) FROM index_price_observations').fetchone()[0],
            'non_current_rows':conn.execute('SELECT count(*) FROM index_price_observations_read WHERE is_latest<>1').fetchone()[0],
            'duplicate_dates':conn.execute('SELECT count(*) FROM (SELECT 1 FROM index_price_observations GROUP BY series_id,observation_date HAVING count(*)>1)').fetchone()[0],
            'synthetic_series':conn.execute("SELECT count(*) FROM index_price_series WHERE provider='derived'").fetchone()[0],
            'missing_references':conn.execute('SELECT count(*) FROM index_price_observations_read f LEFT JOIN collection_sources c ON c.id=f.source_id AND c.batch_id=f.batch_id LEFT JOIN index_price_series s ON s.id=f.series_id LEFT JOIN index_price_definitions d ON d.provider=s.provider AND d.series_key=s.series_key AND d.definition_version=s.definition_version WHERE c.id IS NULL OR s.id IS NULL OR d.provider IS NULL').fetchone()[0],
        }
        for key in ('non_current_rows','duplicate_dates','synthetic_series','missing_references'):
            if structural[key]:differences.append(dict(reason=key,count=structural[key]))
        samples=[]
        for series in coverage:
            n=series['observations']
            count=min(n,samples_per_series)
            offsets=sorted({round(i*(n-1)/max(count-1,1)) for i in range(count)})
            for offset in offsets:
                fact=dict(conn.execute('SELECT * FROM index_price_observations_read WHERE series_id=? ORDER BY observation_date LIMIT 1 OFFSET ?', (series['id'],offset)).fetchone())
                samples.append({**series,**fact})
        from stock_analysis.domains.indices.index_price_codec import PriceCodec
        samples=PriceCodec(conn).decode_rows(samples)
        calendar_source=conn.execute("SELECT id FROM collection_sources WHERE dataset='index.calendar' AND parse_status='complete' ORDER BY id DESC LIMIT 1").fetchone()
    calendar=None
    if calendar_source:
        raw,_=read_source(db,calendar_source[0])
        calendar=IndexCollector.decode_calendar(None,raw.decode('utf-8'))
    cache={}
    js=None
    def source_values(sid):
        nonlocal js
        if sid in cache:return cache[sid]
        raw,source=read_source(db,sid)
        with db.connection() as conn:
            batch=conn.execute('SELECT * FROM collection_batches WHERE id=?',(source['batch_id'],)).fetchone()
        provider,key,code_key,date_key=DATASETS[source['dataset']]
        request=json.loads(source['request_json']);expected=None
        if provider=='sina':
            import re
            from py_mini_racer import MiniRacer
            if js is None:
                js=MiniRacer();js.eval((Path(__file__).parent/'resources/sina_index_history.js').read_text(encoding='utf-8'))
            expected=re.search(r'/company/((?:sh|sz)\d{6})/',request['url'])[1][2:]
            rows=[{**r,'_code':expected} for r in js.call('d',raw.decode('utf-8').split('=',1)[1].split(';',1)[0].replace('"',''))]
        else:
            data=json.loads(raw)['data']
            if provider=='sws':rows=data['results']
            elif provider=='legu':
                expected=request['params']['indexCode'];rows=[{**r,'_code':expected} for r in data]
            else:rows=data;expected=request['params']['indexCode']
        points,_=parse_prices(rows,provider=provider,series_key=key,code_key=code_key,date_key=date_key,
            start=batch['scope_start'] or '0001-01-01',end=batch['scope_end'] or '9999-12-31',
            trading_days=calendar or {day(r[date_key]) for r in rows},batch_id=source['batch_id'],source_id=sid,expected_code=expected)
        cache[sid]=({(r['index_code'],r['observation_date']):r for r in points},source)
        return cache[sid]
    checked_fields=0;checked_estimates=0
    for fact in samples:
        try:
            lineage=json.loads(fact['field_lineage_json'])
            fields=FIELDS if fact['definition_version']=='v2' else OHLC_FIELDS
            definition=DEFINITIONS[fact['provider'],fact['series_key']] if fact['definition_version']=='v2' else legacy_definition(fact['provider'],fact['series_key'])
            if price_hash({k:fact[k] for k in fields},definition,fact['withdrawal_reason'],lineage if fact['definition_version']=='v2' else None)!=fact['content_hash']:
                differences.append(dict(fact_id=fact['id'],reason='content_hash'))
            for field in fields:
                estimate=lineage.get('estimates',{}).get(field)
                if estimate:
                    if estimate['upstream_status']!='current':
                        if fact[field] is not None:differences.append(dict(fact_id=fact['id'],field=field,reason='stale_estimate'))
                        continue
                    parent=estimate['parent_market']
                    with db.connection() as conn:
                        market=conn.execute('SELECT * FROM market_indicator_observations WHERE id=?',(parent['fact_id'],)).fetchone()
                    raw_field=estimate['input_index_field']
                    fingerprint=digest(encoded({'fact_id':fact['id'],'raw_revision':lineage['_raw']['revision'],
                        'field':raw_field,'value':fact[raw_field],'field_source':lineage['_raw']['fields'].get(raw_field)}).encode())
                    valid=market and market['is_latest']==1 and market['observation_date']==fact['observation_date'] and market['provider']=='legu' and market['version']==parent['version'] and market['source_id']==parent['source_id'] and market['batch_id']==parent['batch_id'] and market['obtained_at']==parent['obtained_at'] and fingerprint==estimate['input_index_fingerprint']
                    if not valid:
                        differences.append(dict(fact_id=fact['id'],field=field,reason='estimate_parent'));continue
                    denominator=market['total_amount_yi']*1e8
                    value=fact[raw_field]/denominator*100 if fact['provider']=='csindex' else fact[raw_field]/100*denominator
                    if fact[field] is None or not math.isclose(value,fact[field],rel_tol=1e-12,abs_tol=1e-8) or estimate['raw_target_value'] is not None or not estimate.get('computed_at'):
                        differences.append(dict(fact_id=fact['id'],field=field,reason='estimate_formula_or_marker'))
                    checked_estimates+=1
                elif fact[field] is not None:
                    meta=lineage.get('_raw',{}).get('fields',{}).get(field,{})
                    sid=meta.get('source_id',fact['source_id'])
                    values,source=source_values(sid)
                    expected=values.get((fact['index_code'],fact['observation_date']),{})
                    if expected.get(field)!=fact[field] or source['batch_id']!=meta.get('batch_id',fact['batch_id']) or source['obtained_at']!=meta.get('obtained_at',fact['obtained_at']):
                        differences.append(dict(fact_id=fact['id'],field=field,source_id=sid,reason='raw_field_or_time'))
                    checked_fields+=1
        except Exception as exc:
            differences.append(dict(fact_id=fact['id'],reason='sample_error',error=str(exc)))
    report=dict(status='passed' if not differences else 'failed',mode='targeted',network_requests=0,
        structural=structural,checked_fact_revisions=len(samples),checked_sources=len(cache),checked_raw_fields=checked_fields,
        checked_estimates=checked_estimates,sample_ids=[r['id'] for r in samples],differences=differences,coverage=coverage,
        reused_historical_evidence=['20261018_index_extensions_isolated_audit.json','20261018_index_extensions_isolated_legacy_rows.json','20261018_csindex_units_evidence.json'],
        limitations='First/middle/last per series; no repeated full source audit, no full-database integrity scan or unrelated table hashing.')
    if report_path:atomic_write(report_path,encoded(report).encode('utf-8'))
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,default=DEFAULT_DATABASE)
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args();result=audit(Database(args.database),report_path=args.report)
    print(encoded({k:result[k] for k in ('status','mode','checked_fact_revisions','checked_sources','network_requests')}),flush=True)
    if result['status']!='passed':raise SystemExit(1)


if __name__=='__main__':main()
