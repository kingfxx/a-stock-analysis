"""Explicit offline gap supplement; never requests sources or changes Legu facts."""
from __future__ import annotations

from stock_analysis.paths import PROJECT_ROOT
from collections import defaultdict
from datetime import date, datetime
import json
import math
from pathlib import Path
import re
from uuid import uuid4

from stock_analysis.domains.market import market_congestion as congestion
from stock_analysis.collection.collection import archive_source, atomic_write, begin_batch, digest, encoded, finish_batch, parsed_source, read_source
from stock_analysis.infrastructure.storage import instance_lock, utc_now

ROOT=PROJECT_ROOT
INPUT=ROOT/'data/collection_sources/bigquant/cn_stock_real_bar1d_20260731_20260827'
REPORT=ROOT/'data/verification/reports/20261017_congestion_gap_backfill.json'


def calculate(records, closes):
    grouped=defaultdict(list)
    seen=set()
    for record in records:
        observed=record['date']
        if isinstance(observed,datetime):observed=observed.date()
        if isinstance(observed,date):observed=observed.isoformat()
        congestion.day(observed)
        instrument=record['instrument']
        if not isinstance(instrument,str) or not re.fullmatch(r'(?:(?:60|68)\d{4}\.SH|(?:00|30)\d{4}\.SZ|\d{6}\.BJ)',instrument):
            raise ValueError('未确认的A股证券身份')
        if (observed,instrument) in seen:raise ValueError('行情重复证券/日期')
        seen.add((observed,instrument))
        amount=congestion.numeric(record.get('amount'))
        if amount is None and congestion.numeric(record.get('volume'))!=0:
            raise ValueError('成交额缺失但成交量不为零，不能确定分母')
        if amount is not None and amount<0:raise ValueError('成交额不能为负')
        grouped[observed].append((instrument,amount))
    if not grouped or set(grouped)!=set(closes):raise ValueError('行情日期和同日点位未完整对应')
    rows=[];statistics={}
    for observed,items in sorted(grouped.items()):
        valid=sorted([(code,amount) for code,amount in items if amount is not None],key=lambda item:(-item[1],item[0]))
        n=len(valid);k=(n+19)//20
        total=math.fsum(amount for _,amount in valid)
        if total<=0:raise ValueError('当日有效成交额总额须为正数')
        top=math.fsum(amount for _,amount in valid[:k])
        close=congestion.numeric(closes[observed])
        if close is None or close<=0:raise ValueError('缺少同日有效上证收盘点位')
        rows.append(dict(observation_date=observed,value=top/total,close=close,
            top5_amount_yi=top/1e8,total_amount_yi=total/1e8,precision_digits=None))
        statistics[observed]=dict(rows=len(items),eligible=n,excluded_null_amount=len(items)-n,top_count=k,
            top_instruments=[code for code,_ in valid[:k]],amount_unit='CNY',amount_conversion=1e8)
    return congestion.validate_rows(rows,congestion.CALCULATED_IDENTITY),statistics


def prepare(db, folder=INPUT):
    """Verify both local formats, source evidence and point lineage before writing."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    folder=Path(folder);manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    if manifest['status']!='verified' or manifest['table']!='cn_stock_real_bar1d':raise ValueError('本地下载未验收')
    for name,metadata in manifest['files'].items():
        if digest((folder/name).read_bytes())!=metadata['sha256']:raise ValueError('本地行情文件哈希不一致')
    table=pq.read_table(folder/'full_market.parquet');counts=pq.read_table(folder/'daily_counts.parquet')
    matched={}
    for sid in manifest['source_ids']:
        raw,source=read_source(db,sid)
        if raw.startswith(b'\xff\xff\xff\xff'):
            replay=pa.ipc.open_stream(raw).read_all()
            for key,expected in (('market',table),('daily_counts',counts)):
                if replay.equals(expected):matched[key]=dict(source_id=sid,batch_id=source['batch_id'],obtained_at=source['obtained_at'],raw_sha256=source['raw_sha256'],stored_sha256=source['stored_sha256'])
    if set(matched)!={'market','daily_counts'}:raise ValueError('本地文件与公共Arrow来源不一致')
    dates=manifest['dates']
    if len(dates)!=20 or min(dates)!='2026-07-31' or max(dates)!='2026-08-27':raise ValueError('本次仅允许授权的20日范围')
    with db.connection() as conn:
        prices=[dict(r) for r in conn.execute('SELECT f.*,s.index_code,s.provider,s.series_key,s.frequency,s.return_type,s.definition_version '
            'FROM index_price_observations_read f JOIN index_price_series s ON s.id=f.series_id WHERE '
            "s.index_code='cn:000001' AND s.provider='csindex' AND s.series_key='history.points' AND s.frequency='daily' "
            "AND s.return_type='unspecified' AND s.definition_version=(SELECT CASE WHEN EXISTS(SELECT 1 FROM index_price_series v2 "
            "WHERE v2.index_code=s.index_code AND v2.provider=s.provider AND v2.series_key=s.series_key "
            "AND v2.frequency=s.frequency AND v2.return_type=s.return_type AND v2.definition_version='v2') "
            "THEN 'v2' ELSE 'v1' END) AND f.is_latest=1 AND f.observation_date BETWEEN ? AND ? ORDER BY f.observation_date",(min(dates),max(dates)))]
    if len(prices)!=len(dates) or {p['observation_date'] for p in prices}!=set(dates):raise ValueError('上证日频点数未完整覆盖20日')
    rows,statistics=calculate(table.select(['date','instrument','amount','volume']).to_pylist(),{p['observation_date']:p['close'] for p in prices})
    actual_counts={r['date'].date().isoformat():{k:int(v) for k,v in r.items() if k!='date'} for r in counts.to_pylist()}
    for observed,stat in statistics.items():
        if actual_counts[observed]!=manifest['validation']['per_date'][observed] or stat['rows']!=actual_counts[observed]['row_count'] or stat['eligible']!=actual_counts[observed]['amount_count']:
            raise ValueError('逐日独立计数校验不一致')
    # Retain the exact point facts and their source/batch/version; no new index observations.
    lineage_fields=('id','series_id','observation_date','close','version','source_id','batch_id','obtained_at','content_hash','index_code','provider','series_key','frequency','return_type','definition_version')
    evidence=dict(kind='offline_derived_gap_supplement',created_at=utc_now(),identity=congestion.CALCULATED_IDENTITY,
        definition=congestion.DEFINITIONS[congestion.CALCULATED_IDENTITY],input_manifest=str(folder/'manifest.json'),
        input_files=manifest['files'],upstream=matched,price_facts=[{k:p[k] for k in lineage_fields} for p in prices],
        statistics=statistics,rows=rows,coverage='unknown',limitation='独立自算；与Legu不保证完全一致；空成交额不补零')
    return evidence


def publish_gaps(conn, rows, batch, source):
    """Validate the entire segment before inserting; caller owns one transaction."""
    for row in rows:
        observed=row['observation_date']
        if conn.execute("SELECT 1 FROM market_indicator_observations WHERE indicator_key='ashares_congestion' AND scope_key='all_a' "
            "AND provider='legu' AND frequency='daily' AND definition_version='v1' AND observation_date=? AND is_latest=1 AND value IS NOT NULL",(observed,)).fetchone():
            raise ValueError('目标日期已有Legu有效值，拒绝补写')
        old=conn.execute('SELECT * FROM market_indicator_observations WHERE '+
            ' AND '.join(k+'=?' for k in congestion.IDENTITY_FIELDS)+' AND observation_date=? AND is_latest=1',
            (*congestion.CALCULATED_IDENTITY,observed)).fetchone()
        if old and any(old[k]!=row[k] for k in (*congestion.VALUE_FIELDS,'precision_digits')):
            raise ValueError('已有自算补值与本次数值不一致，禁止一次性工具改写')
    return congestion.publish(conn,rows,congestion.CALCULATED_CHANNEL,batch,source)


def apply(db, evidence, *, backup_path):
    rows=evidence['rows'];operation='congestion-local-gap-'+uuid4().hex
    with instance_lock(str(db.path)+'.congestion-local-gap'):
        with db.connection() as conn:
            if conn.execute("SELECT 1 FROM collection_operations WHERE status='running'").fetchone():raise ValueError('指数采集运行中，暂不写入')
            existing=[dict(r) for r in conn.execute('SELECT * FROM market_indicator_observations WHERE '+
                ' AND '.join(k+'=?' for k in congestion.IDENTITY_FIELDS)+' AND is_latest=1',congestion.CALCULATED_IDENTITY)]
        by_date={r['observation_date']:r for r in existing}
        if all(row['observation_date'] in by_date and all(by_date[row['observation_date']][key]==row[key] for key in (*congestion.VALUE_FIELDS,'precision_digits')) for row in rows):
            return dict(status='already_present',rows=len(rows),source_ids=sorted({r['source_id'] for r in existing}))
        db.backup(Path(backup_path))
        batch=begin_batch(db,operation,congestion.CALCULATED_DATASET,'bigquant',min(r['observation_date'] for r in rows),max(r['observation_date'] for r in rows),
            scope=dict(operation='LOCAL_DERIVATION',dates=[r['observation_date'] for r in rows],identity=congestion.CALCULATED_IDENTITY,
                definition=evidence['definition'],upstream=evidence['upstream']),frequency='daily',parser_version='offline-gap-v1',definition_version=congestion.CALCULATED_IDENTITY[5])
        source=None
        try:
            source=archive_source(db,batch,'bigquant',congestion.CALCULATED_DATASET,
                dict(method='LOCAL_DERIVATION',operation=operation,network_request=False,upstream=evidence['upstream']),1,raw=encoded(evidence).encode())
            with db.connection(write=True) as conn:
                conn.execute('UPDATE collection_sources SET hash_basis=? WHERE id=?',('derived_json / gzip_file',source))
            parsed_source(db,source,fields=['observation_date',*congestion.VALUE_FIELDS])
            with db.connection(write=True) as conn:
                for fact in evidence['price_facts']:
                    current=conn.execute('SELECT content_hash,is_latest FROM index_price_observations_read WHERE id=?',(fact['id'],)).fetchone()
                    if not current or not current['is_latest'] or current['content_hash']!=fact['content_hash']:raise ValueError('准备后点位已修订，拒绝过期计算证据')
                counts=publish_gaps(conn,rows,batch,source)
                result=dict(status='complete',operation_id=operation,batch_id=batch,source_id=source,backup=str(backup_path),**counts)
                conn.execute("UPDATE collection_batches SET status='complete',coverage_status='unknown',finished_at=?,result_json=? WHERE id=?",(utc_now(),encoded(result),batch))
            return result
        except Exception as exc:
            finish_batch(db,batch,'failed',error=str(exc))
            raise


def main():
    import argparse
    from stock_analysis.infrastructure.storage import Database
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args();db=Database();evidence=prepare(db)
    atomic_write(ROOT/'data/verification/reports/20261017_congestion_gap_prepared.json',encoded(evidence).encode())
    if args.apply:
        stamp=datetime.now().strftime('%Y%m%d_%H%M%S')
        result=apply(db,evidence,backup_path=db.path.parent/'backups'/('before_congestion_gap_'+stamp+'.sqlite3'))
        state=congestion.read(db)
        result.update(valid_dates=state['valid_dates'],missing_dates=state['missing_dates'],supplemented_dates=state['supplemented_dates'])
        atomic_write(REPORT,encoded(result).encode())
        print(encoded(result))
    else:
        print(encoded(dict(rows=len(evidence['rows']),first=evidence['rows'][0],last=evidence['rows'][-1],excluded_null_amount=sum(s['excluded_null_amount'] for s in evidence['statistics'].values()))))

if __name__=='__main__':main()
