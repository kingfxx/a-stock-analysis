"""Manual backfill of the fixed CMB closed-position windows."""

import json
from decimal import Decimal
from uuid import uuid4

from stock_analysis.collection.collection import Cancelled, ProviderBlocked, RequestCollector, encoded, finish_batch
from stock_analysis.domains.portfolio.history_prices import stored_closes
from stock_analysis.domains.portfolio.history_scope import SAMPLE_COUNT, UNIVERSE, WINDOWS
from stock_analysis.domains.portfolio.history_source import DATASET, PROVIDER, fetch_window
from stock_analysis.domains.portfolio.reference import local_market_days
from stock_analysis.infrastructure.storage import instance_lock, utc_now


TASK_KEY = 'portfolio_historical_closes'
PARSER = 'tencent-ohlc-v2'
DEFINITION = 'cny-unadjusted-daily-close-v1'


def reserve(conn, params):
    if conn.execute("SELECT 1 FROM collection_operations WHERE action IS NULL AND task_key<>'stocks' AND status='running'").fetchone():
        raise ValueError('已有采集任务正在运行，请先等待或停止')
    ref = uuid4().hex
    stamp = utc_now()
    conn.execute("""
        INSERT INTO collection_operations(operation_id,task_key,mode,scope_json,status,started_at)
        VALUES(?,?,?,?,'running',?)
    """, (ref, TASK_KEY, params['mode'], encoded(dict(dataset=DATASET, universe=UNIVERSE,
            scope_count=len(WINDOWS), source=PROVIDER)), stamp))
    conn.executemany("""
        INSERT INTO collection_batches(operation_id,domain,dataset,source_system,scope_start,scope_end,scope_json,
            frequency,universe_version,trigger_type,status,created_at,parser_version,definition_version,code,sequence)
        VALUES(?,'portfolio',?,?,?,?,?,'daily',?,'manual','pending',?,?,?, ?,?)
    """, ((ref, DATASET, PROVIDER, start, end, encoded(dict(exchange=market, code=code,
        candidate_count=count, sample=sequence<SAMPLE_COUNT)), UNIVERSE, stamp, PARSER, DEFINITION, code, sequence)
        for sequence, (market, code, start, end, count) in enumerate(WINDOWS)))
    return ref


def publish(db, batch_id, market, code, start, end, expected, observations, pages, cancelled):
    observed = set(observations)
    missing = sorted(expected - observed)
    if observed - expected:
        raise ValueError('腾讯返回本地交易日历以外的日期')
    if start not in observed or end not in observed:
        raise ValueError('窗口首尾缺行，疑似截断，分段待核')
    stamp = utc_now()
    with db.connection(write=True) as conn:
        if cancelled(conn):
            raise Cancelled('采集已停止')
        previous = {row['trade_date']: row for row in stored_closes(conn, market, code, start, end)}
        new = []
        for day, (close, source_id) in sorted(observations.items()):
            old = previous.get(day)
            if old and close == Decimal(old['close']):
                continue
            new.append((market, code, day, old['revision'] + 1 if old else 1, str(close), stamp, batch_id, source_id))
        conn.executemany('INSERT INTO portfolio_historical_closes VALUES(?,?,?,?,?,?,?,?)', new)
        result = dict(candidate_count=len(expected), row_count=len(observed), returned=len(observed), pages=pages,
                      published=len(new), unchanged=len(observed)-len(new), missing_dates=missing)
        conn.execute("""
            UPDATE collection_batches SET status='complete',coverage_status=?,finished_at=?,result_json=? WHERE id=?
        """, ('partial' if missing else 'complete', stamp, encoded(result), batch_id))


def _claim(db, batch):
    with db.connection(write=True) as conn:
        prior = conn.execute("""
            SELECT id,result_json FROM collection_batches
            WHERE domain='portfolio' AND dataset=? AND source_system=? AND scope_start=? AND scope_end=?
            AND scope_json=? AND universe_version=? AND parser_version=? AND definition_version=?
            AND status='complete' AND coverage_status='complete' ORDER BY id DESC LIMIT 1
        """, (DATASET, PROVIDER, batch['scope_start'], batch['scope_end'], batch['scope_json'],
              UNIVERSE, PARSER, DEFINITION)).fetchone()
        if prior:
            result = json.loads(prior['result_json'])
            result.update(reused_batch_id=prior['id'], published=0)
            conn.execute("""
                UPDATE collection_batches SET status='reused',coverage_status='complete',finished_at=?,
                    reused_batch_id=?,result_json=? WHERE id=? AND status='pending'
            """, (utc_now(), prior['id'], encoded(result), batch['id']))
            return False
        conn.execute("UPDATE collection_batches SET status='running',started_at=? WHERE id=? AND status='pending'",
                     (utc_now(), batch['id']))
        return True


def run(db, ref, *, collector_factory=RequestCollector):
    # Share the lock used by startup recovery for non-stock collection operations.
    with instance_lock(db.path.parent/'collection_sources'/'index-operation'):
        return _run_locked(db, ref, collector_factory)


def _run_locked(db, ref, collector_factory):
    def cancelled(conn=None):
        if conn is None:
            with db.connection() as reader:
                return cancelled(reader)
        return bool(conn.execute('SELECT cancel_requested FROM collection_operations WHERE operation_id=?', (ref,)).fetchone()[0])

    with db.connection(write=True) as conn:
        if not conn.execute("""
            UPDATE collection_operations SET worker_started_at=? WHERE operation_id=? AND task_key=?
            AND status='running' AND worker_started_at IS NULL
        """, (utc_now(), ref, TASK_KEY)).rowcount:
            raise ValueError('补采任务不可领取')
        batches = conn.execute('SELECT * FROM collection_batches WHERE operation_id=? ORDER BY sequence', (ref,)).fetchall()
    collector = collector_factory(db, cancelled=cancelled)
    errors = []
    skipped = 0
    status = 'complete'
    active = None
    try:
        for batch in batches:
            collector.check_cancelled()
            scope = json.loads(batch['scope_json'])
            market, code = scope['exchange'], scope['code']
            start, end = batch['scope_start'], batch['scope_end']
            expected = set(local_market_days(db, start, end))
            if len(expected) != scope['candidate_count'] or not {start, end} <= expected:
                raise ValueError(f'{code} 本地交易日历与已核对窗口不同')
            if not _claim(db, batch):
                skipped += 1
                continue
            active = batch['id']
            try:
                rows, pages = fetch_window(db, collector, active, market, code, start, end)
                if scope['candidate_count'] > 640 and pages < 2:
                    raise ValueError('长窗口缺少跨页证据')
                publish(db, active, market, code, start, end, expected, rows, pages, cancelled)
                active = None
            except (Cancelled, ProviderBlocked):
                raise
            except Exception as exc:
                finish_batch(db, active, 'failed', error=exc)
                active = None
                errors.append(f'{code} {exc}')
                if scope['sample']:
                    raise ValueError('有限样本未通过，停止其余窗口') from exc
        if errors:
            status = 'failed'
    except Exception as exc:
        status = 'cancelled' if isinstance(exc, Cancelled) else 'failed'
        if active is not None:
            finish_batch(db, active, status, error=exc)
        errors.append(str(exc))
    finally:
        collector.session.close()
    stamp = utc_now()
    result = dict(scope_count=len(batches), skipped=skipped, errors=errors[:3], **collector.report())
    with db.connection(write=True) as conn:
        conn.execute("""
            UPDATE collection_batches SET status=?,finished_at=?,error=? WHERE operation_id=? AND status='pending'
        """, ('cancelled' if status=='cancelled' else 'skipped', stamp, '本轮未请求', ref))
        conn.execute("""
            UPDATE collection_operations SET status=?,finished_at=?,result_json=?,error=? WHERE operation_id=?
        """, (status, stamp, encoded(result), errors[-1] if errors else None, ref))
    return result
