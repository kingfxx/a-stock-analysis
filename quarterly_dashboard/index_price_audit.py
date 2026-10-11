"""Read-only point/source comparison, field coverage and maintenance acceptance."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import re

from .collection import atomic_write, encoded, read_source
from .index_collection import IndexCollector
from .index_prices import FIELDS, parse_prices
from .index_price_replay import DATASETS
from .maintenance import MaintenanceService
from .storage import Database, DEFAULT_DATABASE


def audit(db, *, report_path=None):
    from py_mini_racer import MiniRacer
    js=MiniRacer();js.eval((Path(__file__).parent/'resources/sina_index_history.js').read_text(encoding='utf-8'))
    with db.connection() as conn:
        calendar_source=conn.execute("SELECT id FROM collection_sources WHERE dataset='index.calendar' AND parse_status='complete' ORDER BY id DESC LIMIT 1").fetchone()[0]
        sources=[dict(r) for r in conn.execute('SELECT DISTINCT s.*,b.scope_start,b.scope_end,b.status AS batch_status '
            'FROM index_price_observations f JOIN collection_sources s ON s.id=f.source_id '
            'JOIN collection_batches b ON b.id=s.batch_id ORDER BY s.id')]
        coverage=[dict(r) for r in conn.execute('SELECT s.index_code,c.name,c.index_type,c.industry_level,s.provider,s.series_key,s.frequency,s.return_type,'
            'count(*) AS observations,min(f.observation_date) AS first,max(f.observation_date) AS last,'+
            ','.join(f'count(f.{k}) AS {k}_valid' for k in FIELDS)+
            ' FROM index_price_observations f JOIN index_price_series s ON s.id=f.series_id '
            'JOIN index_catalog c ON c.id=(SELECT max(x.id) FROM index_catalog x WHERE x.index_code=s.index_code) '
            'WHERE f.is_latest=1 GROUP BY s.id ORDER BY c.index_type,s.index_code,s.provider,s.series_key')]
        foreign_keys=[tuple(r) for r in conn.execute('PRAGMA foreign_key_check')]
        integrity=conn.execute('PRAGMA integrity_check').fetchone()[0]
        facts_by_source=defaultdict(list)
        for fact in conn.execute('SELECT f.id,f.source_id,f.obtained_at,f.observation_date,f.open,f.high,f.low,f.close,s.index_code '
            'FROM index_price_observations f JOIN index_price_series s ON s.id=f.series_id'):
            facts_by_source[fact['source_id']].append(fact)
    raw,_=read_source(db,calendar_source)
    calendar=IndexCollector.decode_calendar(None,raw.decode('utf-8'))
    differences=[];checked=0;all_expected=0
    for source in sources:
        raw,_=read_source(db,source['id'])
        provider,key,code_key,date_key=DATASETS[source['dataset']]
        request=json.loads(source['request_json'])
        expected=None
        if provider=='sina':
            expected=re.search(r'/company/((?:sh|sz)\d{6})/',request['url'])[1][2:]
            rows=[{**r,'_code':expected} for r in js.call('d',raw.decode('utf-8').split('=',1)[1].split(';',1)[0].replace('"',''))]
        else:
            data=json.loads(raw)['data']
            if provider=='sws':rows=data['results']
            elif provider=='legu':
                expected=request['params']['indexCode'];rows=[{**r,'_code':expected} for r in data]
            else:rows=data;expected=request['params']['indexCode']
        points,_=parse_prices(rows,provider=provider,series_key=key,code_key=code_key,date_key=date_key,
            start=source['scope_start'],end=source['scope_end'],trading_days=calendar,
            batch_id=source['batch_id'],source_id=source['id'],expected_code=expected)
        values={(r['index_code'],r['observation_date']):tuple(r[k] for k in FIELDS) for r in points}
        actual={}
        facts=facts_by_source[source['id']]
        for fact in facts:
            checked+=1
            identity=(fact['index_code'],fact['observation_date'])
            actual[identity]=tuple(fact[k] for k in FIELDS)
            if values.get(identity)!=actual[identity] or fact['obtained_at']!=source['obtained_at']:
                differences.append(dict(source_id=source['id'],fact_id=fact['id'],reason='value_or_actual_time_mismatch'))
        # Identical overlapping sources legitimately create no duplicate facts.
        # Every source row must nevertheless have an equal retained business revision.
        with db.connection() as conn:
            for identity,price in values.items():
                if actual.get(identity)==price:
                    continue
                retained=conn.execute('SELECT 1 FROM index_price_observations f JOIN index_price_series s ON s.id=f.series_id '
                    'WHERE s.index_code=? AND s.provider=? AND s.series_key=? AND s.frequency=? AND f.observation_date=? '
                    'AND f.open IS ? AND f.high IS ? AND f.low IS ? AND f.close IS ? LIMIT 1',
                    (identity[0],provider,key,'monthly' if provider=='legu' else 'daily',identity[1],*price)).fetchone()
                if not retained:differences.append(dict(source_id=source['id'],index_code=identity[0],date=identity[1],reason='source_row_not_retained'))
        all_expected+=len(values)
    categories=defaultdict(lambda:dict(series=0,observations=0))
    for row in coverage:
        categories[row['index_type']]['series']+=1;categories[row['index_type']]['observations']+=row['observations']
    maintenance=[r for r in MaintenanceService(db).read()['tables'] if r['name'].startswith('index_price_')]
    report=dict(status='passed' if not differences and not foreign_keys and integrity=='ok' else 'failed',
        network_requests=0,integrity=integrity,foreign_keys=foreign_keys,checked_fact_revisions=checked,
        checked_source_rows=all_expected,checked_sources=len(sources),differences=differences,
        categories=dict(categories),coverage=coverage,maintenance=maintenance)
    if report_path:atomic_write(report_path,encoded(report).encode('utf-8'))
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,default=DEFAULT_DATABASE)
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args();result=audit(Database(args.database),report_path=args.report)
    print(encoded({k:result[k] for k in ('status','checked_fact_revisions','checked_sources','categories','network_requests')}),flush=True)
    if result['status']!='passed':raise SystemExit(1)


if __name__=='__main__':main()
