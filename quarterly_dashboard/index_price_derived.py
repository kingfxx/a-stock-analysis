"""Explicitly estimated price fields stored on their original index fact row."""
import json
import math
from collections import defaultdict

from .collection import Cancelled, digest, encoded
from .index_prices import DEFINITIONS, FIELDS, VERSION, validate_values, price_hash
from .storage import utc_now

from .index_price_codec import estimate_definition,PriceCodec,encode_day,decode_day,logical_hash


def _unchanged_estimate(fact,body,market):
    """Check compact dependency snapshots directly; do not expand unchanged evidence."""
    if not isinstance(body,list) or not body or body[0]!=2:return False
    target='amount_ratio' if fact['provider']=='csindex' else 'amount'
    entries=body[4] or []
    entry=next((e for e in entries if FIELDS[e[0]]==target),None)
    if entry is None:return fact[target] is not None
    raw_field='amount' if fact['provider']=='csindex' else 'amount_ratio'
    raw=fact[raw_field]
    if (market is None or raw is None or not math.isfinite(raw) or raw<0
        or entry[1]!=body[1] or entry[2]!=fact['source_id'] or entry[3]!=raw
        or entry[4]!=market['id'] or entry[5]!=market['version']):return False
    raw_source=dict(body[3]).get(FIELDS.index(raw_field),fact['source_id'])
    if (entry[7] if entry[7] is not None else entry[2])!=raw_source:return False
    denominator=float(market['total_amount_yi'])*100_000_000
    if not math.isfinite(denominator) or denominator<=0:return False
    value=raw/denominator*100 if fact['provider']=='csindex' else raw/100*denominator
    return fact[target]==value


def recompute(db, operation_id=None, *, start=None, through=None, cancelled=lambda: False):
    """Refresh estimates in place; no synthetic source, series, batch, or fact is created."""
    with db.connection() as conn:
        totals = [dict(r) for r in conn.execute(
            "SELECT * FROM market_indicator_observations WHERE indicator_key='ashares_congestion' "
            "AND scope_key='all_a' AND provider='legu' AND definition_version='v1' AND is_latest=1 "
            "AND channel IN ('html_recent_table','api_history') AND (? IS NULL OR observation_date<=?) "
            "ORDER BY observation_date,CASE channel WHEN 'html_recent_table' THEN 0 ELSE 1 END",
            (through, through))]
        market_any = {}
        market_valid = {}
        for row in totals:
            market_any.setdefault(row['observation_date'], row)
            amount = row['total_amount_yi']
            if amount is not None and math.isfinite(float(amount)) and float(amount) > 0:
                market_valid.setdefault(row['observation_date'], row)
        # Only the previously authorized, locally archived 20-day gap is eligible.
        estimate_market_valid = dict(market_valid)
        for row in conn.execute("SELECT * FROM market_indicator_observations WHERE indicator_key='ashares_congestion' "
                "AND scope_key='all_a' AND provider='bigquant' AND channel='bigquant_calculated' "
                "AND definition_version='amount_rank_top5_v1' AND frequency='daily' AND is_latest=1 "
                "AND observation_date BETWEEN '2026-07-31' AND '2026-08-27'"):
            amount=row['total_amount_yi']
            if amount is not None and math.isfinite(float(amount)) and float(amount)>0:
                estimate_market_valid.setdefault(row['observation_date'],dict(row))
        days=[r[0] for r in conn.execute("SELECT DISTINCT observation_date FROM index_price_observations "
            "WHERE (? IS NULL OR observation_date<=?) AND (? IS NULL OR observation_date>=?) ORDER BY observation_date",
            (encode_day(through),encode_day(through),encode_day(start),encode_day(start)))]

    pending = defaultdict(list)
    missing_denominator = set()
    missing_input = set()
    changed = 0
    checked=0
    for day in days:
        with db.connection() as conn:
            facts=[dict(r) for r in conn.execute(
                "SELECT f.*,s.id AS sequence_id,s.index_code,s.provider,s.series_key,s.frequency,s.return_type,s.definition_version "
                "FROM index_price_observations f JOIN index_price_series s ON s.id=f.series_id "
                "WHERE f.observation_date=? AND s.definition_version=? AND "
                "((s.provider='csindex' AND s.series_key='history.points') OR "
                "(s.provider='sws' AND s.series_key='analysis.points')) ORDER BY f.id",(day,VERSION))]
            dirty=[]
            for fact in facts:
                if checked%256==0 and cancelled():
                    raise Cancelled('同行估算补算收到取消请求')
                checked+=1
                markets=estimate_market_valid
                if not _unchanged_estimate(fact,json.loads(fact['field_lineage_json']),markets.get(decode_day(day))):
                    dirty.append(fact)
            codec=PriceCodec(conn)
            facts=codec.decode_rows(dirty)
        pending=defaultdict(list)
        for fact in facts:
            provider = fact['provider']
            field = 'amount_ratio' if provider == 'csindex' else 'amount'
            raw_field = 'amount' if provider == 'csindex' else 'amount_ratio'
            lineage = json.loads(fact['field_lineage_json'] or '{}')
            estimates = lineage.setdefault('estimates', {})
            if fact[field] is not None and field not in estimates:
                continue
            raw_value = fact[raw_field]
            markets=estimate_market_valid
            market = markets.get(fact['observation_date'])
            market_row = market or market_any.get(fact['observation_date'])
            reason = None
            denominator = None
            if market is None:
                reason = 'upstream_missing:Legu same-day total amount' if market_row is None else 'upstream_invalid:Legu same-day total amount'
                missing_denominator.add(fact['observation_date'])
            elif raw_value is None:
                reason = 'upstream_missing:index raw field ' + raw_field
                missing_input.add(fact['observation_date'])
            elif not math.isfinite(float(raw_value)) or float(raw_value) < 0:
                reason = 'upstream_invalid:index raw field ' + raw_field
                missing_input.add(fact['observation_date'])
            else:
                denominator = float(market['total_amount_yi']) * 100_000_000
                if not math.isfinite(denominator) or denominator <= 0:
                    reason = 'upstream_invalid:Legu same-day total amount'
                    missing_denominator.add(fact['observation_date'])

            estimate = None
            value = None
            if reason is None:
                formula,basis=estimate_definition(provider,market)
                value = float(raw_value) / denominator * 100 if provider == 'csindex' else float(raw_value) / 100 * denominator
                estimate = {
                    'kind': 'estimated', 'raw_target_value': None, 'value': value,
                    'formula': formula, 'upstream_status': 'current',
                    'input_index_field': raw_field, 'input_index_value': raw_value,
                    'input_index_fingerprint': digest(encoded({'fact_id': fact['id'], 'raw_revision': lineage.get('_raw', {}).get('revision', 0),
                        'field': raw_field, 'value': raw_value, 'field_source': lineage.get('_raw', {}).get('fields', {}).get(raw_field)}).encode()),
                    'parent_index': {'fact_id': fact['id'], 'series_id': fact['sequence_id'], 'index_code': fact['index_code'],
                        'provider': provider, 'series_key': fact['series_key'], 'frequency': fact['frequency'],
                        'return_type': fact['return_type'], 'definition_version': fact['definition_version'],
                        'observation_date': fact['observation_date'], 'source_id': fact['source_id'],
                        'batch_id': fact['batch_id'], 'obtained_at': fact['obtained_at'],
                        'raw_revision': lineage.get('_raw', {}).get('revision', 0)},
                    'parent_market': {'fact_id': market['id'], 'version': market['version'], 'provider': market['provider'],
                        'channel': market['channel'], 'definition_version': market['definition_version'],
                        'source_id': market['source_id'], 'batch_id': market['batch_id'],
                        'observation_date': market['observation_date'], 'obtained_at': market['obtained_at'],
                        'total_amount_yi': market['total_amount_yi']},
                    'denominator_cny': denominator, 'market_basis': basis,
                }
            elif field in estimates:
                # Withdraw a stale value while preserving the reason and parents for audit.
                estimate = dict(estimates[field])
                estimate.pop('computed_at', None)
                estimate.update(value=None, upstream_status=reason, raw_target_value=None)
            elif fact[field] is None:
                continue

            if estimate is None:
                continue
            old_estimate=estimates.get(field, {})
            stable_old={k:v for k,v in old_estimate.items() if k!='computed_at'}
            if stable_old==estimate and fact[field]==value:
                continue
            estimate['computed_at']=utc_now()
            estimates[field] = estimate
            values = {name: fact[name] for name in FIELDS}
            values[field] = value
            validate_values(values)
            content_hash = price_hash(values, DEFINITIONS[(provider, fact['series_key'])], fact['withdrawal_reason'], lineage)
            if content_hash != fact['content_hash']:
                pending[fact['observation_date']].append((fact, values, lineage, content_hash))

        rows=pending.get(decode_day(day),[])
        if not rows:
            continue
        if cancelled():
            raise Cancelled('同行估算补算收到取消请求；已提交日期保留')
        with db.connection(write=True) as conn:
            codec=PriceCodec(conn)
            codec.prefetch([item[0] for item in rows])
            for fact, values, lineage, content_hash in rows:
                current = conn.execute('SELECT content_hash FROM index_price_observations WHERE id=?',
                                       (fact['id'],)).fetchone()
                if not current or logical_hash(current['content_hash']) != fact['content_hash']:
                    raise RuntimeError('同行原始行情在补算期间已变化，请重试')
                parent=lineage.get('estimates',{}).get('amount_ratio' if fact['provider']=='csindex' else 'amount',{}).get('parent_market')
                if parent and lineage['estimates']['amount_ratio' if fact['provider']=='csindex' else 'amount']['upstream_status']=='current':
                    current_market=conn.execute('SELECT is_latest,version FROM market_indicator_observations WHERE id=?',(parent['fact_id'],)).fetchone()
                    if not current_market or not current_market['is_latest'] or current_market['version']!=parent['version']:
                        raise RuntimeError('大盘总额在补算期间已变化，请重试')
                payload = {**values, 'field_lineage_json': encoded(lineage), 'content_hash': content_hash,
                           'version': fact['version'] + 1, 'updated_at': utc_now()}
                payload=codec.encode_payload(payload,fact)
                conn.execute('UPDATE index_price_observations SET ' + ','.join(k+'=?' for k in payload) + ' WHERE id=?',
                             (*payload.values(), fact['id']))
                changed += 1
    return {'status': 'partial' if missing_denominator or missing_input else ('complete' if changed else 'current'),
            'facts': changed, 'network_requests': 0, 'batches': 0,
            'missing_denominator_dates': len(missing_denominator), 'missing_input_dates': len(missing_input),
            'pending_dates': sorted(missing_denominator | missing_input)}
