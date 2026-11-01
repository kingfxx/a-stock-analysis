"""Publish an evidence-backed snapshot correction using existing versioned facts."""
from __future__ import annotations

from uuid import uuid4
from stock_analysis.collection.collection import archive_source, begin_batch, encoded, finish_batch, parsed_source
from stock_analysis.domains.market.market_trade_snapshot import DATASET, DEFINITION, publish_metric_patch
from stock_analysis.domains.industries.industry_cap_summary import publish_in_transaction
from stock_analysis.infrastructure.storage import utc_now


def correct_shares(db, *, code, exchange, day, expected_id, expected_hash, shares, evidence_url, reason, quarter):
    """Apply an explicitly verified correction; archive evidence before atomic publication."""
    with db.connection() as conn:
        old = conn.execute('SELECT * FROM market_trade_snapshot WHERE id=? AND security_code=? AND exchange=? '
            'AND trade_date=? AND definition_version=? AND is_latest=1',
            (expected_id, code, exchange, day, DEFINITION)).fetchone()
        if not old or old['content_hash'] != expected_hash or old['close_price'] is None:
            raise ValueError('原快照已变化或缺收盘价，停止核正')
        old = dict(old)
        roster = conn.execute('SELECT target_date FROM sw_cap_quarter_rosters WHERE quarter=?', (quarter,)).fetchone()
        if not roster or roster['target_date'] != day:
            raise ValueError('季度冻结名单交易日不匹配，停止核正')
    cap = shares * old['close_price']
    correction = dict(baseline_id=old['id'], baseline_source_id=old['source_id'],
        baseline_batch_id=old['batch_id'], baseline_content_hash=old['content_hash'],
        original_shares=old['total_shares'], original_market_cap=old['total_market_cap'],
        shares=shares, price=old['close_price'], market_cap=cap, evidence_url=evidence_url,
        reason=reason, unchanged_fields='其他字段沿用原快照，未经重新核实')
    operation = uuid4().hex
    with db.connection(write=True) as conn:
        run_id=conn.execute("INSERT INTO collection_operations(operation_id,task_key,mode,trigger_type,scope_json,status,started_at,action,target,result_json) "
            "VALUES(?, 'market_snapshot_correction', 'review', 'manual', ?, 'running', ?, 'snapshot_correction', ?, '{}')",
            (operation, encoded({'quarter':quarter,'code':code}), utc_now(), quarter)).lastrowid
    batch = None
    try:
        batch = begin_batch(db, operation, DATASET, 'manual_review', day, day,
            scope={'code':code, 'quarter':quarter, 'kind':'snapshot_correction'}, frequency='daily',
            trigger='review', parser_version='snapshot-correction-v1', definition_version=DEFINITION)
        source = archive_source(db, batch, 'manual_review', DATASET,
            {'kind':'snapshot_correction', 'snapshot_correction':correction}, 1,
            raw=encoded(correction).encode('utf-8'))
        parsed_source(db, source, fields=['total_shares','total_market_cap'])
        with db.connection(write=True) as conn:
            current = conn.execute('SELECT id,content_hash FROM market_trade_snapshot WHERE id=? AND is_latest=1', (expected_id,)).fetchone()
            if not current or current['content_hash'] != expected_hash:
                raise ValueError('原快照在核正期间变化，停止发布')
            result = publish_metric_patch(conn, batch, (code,exchange,day),
                {'total_shares':shares,'total_market_cap':cap}, source, utc_now(), definition=DEFINITION)
            if result['revised'] != 1:
                raise ValueError('核正未生成新版本')
            aggregate=publish_in_transaction(conn, quarter, run_id, operation)
            conn.execute("UPDATE collection_operations SET status='complete',finished_at=? WHERE id=?", (utc_now(),run_id))
            conn.execute("UPDATE collection_batches SET status='complete',coverage_status='unknown',finished_at=?,result_json=? WHERE id=?",
                (utc_now(),encoded({'correction':correction,'aggregation':aggregate}),batch))
        return {'batch_id':batch,'source_id':source,'operation_id':operation,'correction':correction,'aggregation':aggregate}
    except Exception as exc:
        if batch is not None:
            finish_batch(db,batch,'failed',error=exc)
        with db.connection(write=True) as conn:
            conn.execute("UPDATE collection_operations SET status='failed',finished_at=?,error=? WHERE id=?", (utc_now(),str(exc),run_id))
        raise
