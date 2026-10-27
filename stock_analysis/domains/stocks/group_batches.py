"""Shared real source batches with independent per-stock business commits."""
import json
from collections import defaultdict

from stock_analysis.collection.collection import begin_batch, encoded
from stock_analysis.collection.execution_context import current_execution, execution_context
from stock_analysis.infrastructure.storage import utc_now

BULK_DATASETS = {'stock.valuation.industry', 'stock.shareholders', 'stock.financing',
                 'stock.dividends', 'stock.business_segments', 'stock.announcements'}
MAX_SYMBOLS = 20


def member_rows(row):
    """Expand shared segments for business progress; never manufacture source rows."""
    scope = json.loads(row.get('scope_json') or '{}')
    members = scope.get('stock_members')
    if not members:
        return [row]
    results = json.loads(row.get('result_json') or '{}').get('stock_results', {})
    return [{**row, **member, 'counts':{}, **results.get(member['code'], {}), 'scope_json': row['scope_json']}
            for member in members]


def complete_member(conn, batch_id, run_id, status, result=None, error=None):
    """Called inside the same transaction as a stock's facts and sync state."""
    row = conn.execute('SELECT scope_json,result_json FROM collection_batches WHERE id=?', (batch_id,)).fetchone()
    if not row:
        return False
    members = json.loads(row['scope_json']).get('stock_members')
    if not members:
        return False
    member = next((m for m in members if m['sync_run_id'] == run_id), None)
    if member is None:
        raise ValueError('共享批次不包含业务同步身份')
    values = json.loads(row['result_json'])
    results = values.setdefault('stock_results', {})
    results[member['code']] = dict(status=status, finished_at=utc_now(), error=error,
                                  counts=result or {}, sync_run_id=run_id)
    counters = defaultdict(int)
    for value in results.values():
        for key in ('returned','inserted','revised','enriched','unchanged','published'):
            number = value['counts'].get(key)
            if type(number) is int:
                counters[key] += number
    values.update(counters)
    derived=[v['counts']['disclosure_counts'] for v in results.values() if isinstance(v['counts'].get('disclosure_counts'),dict)]
    if derived:
        values['disclosure_counts']={key:sum(v.get(key,0) for v in derived) for key in ('inserted','revised','unchanged','returned','published')}
    values['unrecorded_stock_members'] = sum(not v['counts'] for v in results.values())
    states = [results.get(m['code'], {}).get('status', 'running') for m in members]
    state = 'running' if 'running' in states else 'failed' if 'failed' in states else 'cancelled' if 'cancelled' in states else 'complete'
    conn.execute('UPDATE collection_batches SET status=?,finished_at=?,result_json=?,error=? WHERE id=?',
                 (state, None if state=='running' else utc_now(), encoded(values),
                  '部分股票失败，成功部分已保留' if 'failed' in states else None, batch_id))
    return True


class GroupBatches:
    def __init__(self, broker):
        self.broker = broker

    def begin(self, db, operation, dataset, provider, start, end, **kwargs):
        execution = dict(current_execution.get())
        call = (db, operation, dataset, provider, start, end, kwargs, execution)
        if dataset not in BULK_DATASETS:
            return self.create(call)
        return self.broker.submit(provider, 'begin', call)

    @staticmethod
    def create(call):
        db, operation, dataset, provider, start, end, kwargs, context = call
        with execution_context(**{k:v for k,v in context.items() if k!='group_batches'}):
            return begin_batch(db, operation, dataset, provider, start, end, **kwargs)

    def merge(self, calls):
        """Group before returning an ID to callers, so all fact FKs stay exact."""
        ids = [self.create(call) for call in calls]
        if len(ids) < 2:
            return ids
        db = calls[0][0]
        with db.connection(write=True) as conn:
            rows = [dict(conn.execute('SELECT * FROM collection_batches WHERE id=?', (i,)).fetchone()) for i in ids]
            if len({r['code'] for r in rows}) != len(rows):
                return ids
            scopes = [json.loads(r['scope_json']) for r in rows]
            # Only source-free, freshly claimed planned segments may be consolidated.
            if any(conn.execute('SELECT 1 FROM collection_sources WHERE batch_id=?', (i,)).fetchone() for i in ids):
                raise ValueError('不能合并已经请求过的分段')
            members = [dict(code=r['code'], work_dataset=r['work_dataset'], mode=r['mode'],
                sync_run_id=r['sync_run_id'], started_at=r['started_at'], scope_start=r['scope_start'],
                scope_end=r['scope_end']) for r in rows]
            for member,original in zip(members,scopes):
                if 'reports' in original:member['reports']=original['reports']
            scope = {k:v for k,v in scopes[0].items() if k not in ('code','sync_run_id','reports')}
            scope.update(codes=[m['code'] for m in members], stock_members=members)
            scope['source_interface'] = scopes[0].get('source_interface','')
            conn.execute('UPDATE collection_batches SET code=NULL,sync_run_id=NULL,scope_json=?,scope_start=?,scope_end=? WHERE id=?',
                (encoded(scope), min((r['scope_start'] for r in rows if r['scope_start']), default=None),
                 max((r['scope_end'] for r in rows if r['scope_end']), default=None), ids[0]))
            conn.executemany('DELETE FROM collection_batches WHERE id=?', [(i,) for i in ids[1:]])
        return [ids[0]] * len(ids)
