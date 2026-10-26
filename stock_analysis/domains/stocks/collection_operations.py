"""Persistent stock refresh operations over existing fact services, without payload copies."""
from __future__ import annotations

import json
from threading import Thread
from uuid import uuid4

from stock_analysis.collection.collection import RequestCollector, encoded
from stock_analysis.collection.execution_context import execution_context
from stock_analysis.domains.stocks.source_parsers import normalize_code
from stock_analysis.infrastructure.storage import utc_now

DATASETS = dict(financial='财务报表', business_segments='主营构成', announcements='公告元数据',
                prices='价格', valuation='估值', dividends='分红', shareholders='股东人数', financing='融资融券')
PROVIDERS = ['sina', 'eastmoney', 'tencent', 'baidu', 'cninfo']
DATA_PROVIDERS = dict(financial=['sina','tencent'], business_segments=['eastmoney'], announcements=['cninfo'],
    prices=['tencent'], valuation=['baidu','eastmoney'], dividends=['eastmoney'], shareholders=['eastmoney'], financing=['eastmoney'])


def previously_initialized(conn, code):
    instrument = conn.execute('SELECT id FROM instruments WHERE code=?', (code,)).fetchone()
    if not instrument:
        return False
    if conn.execute('SELECT 1 FROM sync_runs WHERE instrument_id=? LIMIT 1', (instrument[0],)).fetchone():
        return True  # Includes explicit no-data and failed legacy attempts.
    return any(conn.execute(f'SELECT 1 FROM {table} WHERE instrument_id=? LIMIT 1', (instrument[0],)).fetchone()
               for table in ('financial_reports','raw_daily_prices','dividend_events','valuation_observations',
                             'shareholder_observations','financing_daily','business_segment_versions'))


def validate_params(params):
    if not isinstance(params, dict) or set(params) - {'mode', 'codes', 'group_id', 'datasets'}:
        raise ValueError('个股采集参数无效')
    mode = params.get('mode', 'incremental')
    if mode not in ('incremental', 'full', 'resume'):
        raise ValueError('个股模式无效')
    datasets = params.get('datasets', list(DATASETS))
    if not isinstance(datasets, list) or not datasets or any(not isinstance(x, str) or x not in DATASETS for x in datasets):
        raise ValueError('请选择有效的数据项')
    result = dict(mode=mode, datasets=list(dict.fromkeys(datasets)))
    if 'group_id' in params:
        if type(params['group_id']) is not int or 'codes' in params:
            raise ValueError('请选择一个分组或指定股票')
        result['group_id'] = params['group_id']
    else:
        codes = params.get('codes')
        if not isinstance(codes, list) or not 1 <= len(codes) <= 2000 or any(not isinstance(c, str) for c in codes):
            raise ValueError('请指定股票代码或分组')
        result['codes'] = list(dict.fromkeys(normalize_code(c) for c in codes))
    return result


class StockCollections:
    def __init__(self, db, data):
        self.db, self.data = db, data

    def reserve(self, conn, params, *, trigger='manual', since=None, initial_code=None):
        params = validate_params(params)
        if 'group_id' in params:
            group = conn.execute('SELECT name FROM stock_groups WHERE id=?', (params['group_id'],)).fetchone()
            if not group:
                raise ValueError('分组已不存在')
            codes = [r[0] for r in conn.execute('SELECT i.code FROM stock_group_members m JOIN instruments i ON i.id=m.instrument_id '
                'WHERE m.group_id=? AND NOT EXISTS (SELECT 1 FROM stock_unfollowed u WHERE u.instrument_id=i.id) ORDER BY m.position,i.code', (params['group_id'],))]
            params['group_name'] = group[0]
        else:
            codes = params['codes']
        for code in codes:
            if conn.execute('SELECT 1 FROM stock_unfollowed u JOIN instruments i ON i.id=u.instrument_id WHERE i.code=?', (code,)).fetchone():
                raise ValueError(f'{code} 已取消关注，请先恢复')
        scope = {**params, 'codes': codes}
        if initial_code:
            scope['initial_code'] = initial_code
        ref, stamp = 'stock:' + uuid4().hex, utc_now()
        work = []
        for code in codes:
            for dataset in DATASETS:
                if dataset not in params['datasets']:
                    continue
                if conn.execute("SELECT 1 FROM stock_collection_items WHERE code=? AND dataset=? AND status IN ('pending','running')", (code, dataset)).fetchone():
                    raise ValueError(f'{code} 的{DATASETS[dataset]}已有运行任务，请待结束后操作')
                previous = conn.execute("SELECT status,finished_at,mode FROM stock_collection_items WHERE code=? AND dataset=? "
                    "AND status IN ('complete','failed','cancelled') ORDER BY id DESC LIMIT 1", (code, dataset)).fetchone()
                reused = bool(since and previous and previous['status'] == 'complete' and previous['finished_at'] >= since)
                if params['mode'] == 'resume':
                    reused = bool(previous and previous['status'] == 'complete')
                mode = previous['mode'] if params['mode'] == 'resume' and previous else 'full' if params['mode'] == 'full' else 'incremental'
                work.append((code, dataset, mode, 'reused' if reused else 'pending'))
        conn.execute('INSERT INTO stock_collection_operations(id,mode,trigger_type,scope_json,status,started_at) '
                     "VALUES(?,?,?,?,'running',?)", (ref, params['mode'], trigger, encoded(scope), stamp))
        conn.executemany('INSERT INTO stock_collection_items(operation_id,code,dataset,mode,status,finished_at) VALUES(?,?,?,?,?,?)',
                         [(ref, code, dataset, mode, status, stamp if status == 'reused' else None) for code, dataset, mode, status in work])
        return ref

    def start(self, params, *, initial=False):
        if initial:
            params = validate_params(params)
            if len(params.get('codes', [])) != 1:
                raise ValueError('首次初始化须指定一只股票')
            code = params['codes'][0]
        with self.db.connection(write=True) as conn:
            if initial:
                old = conn.execute("SELECT id,status FROM stock_collection_operations WHERE trigger_type='initial' AND json_extract(scope_json,'$.initial_code')=?", (code,)).fetchone()
                if old:
                    return dict(operation_ref=old['id'], status=old['status'])
                if previously_initialized(conn, code):
                    return dict(operation_ref=None, status='existing')
                params = dict(codes=[code], mode='full', datasets=list(DATASETS))
            ref = self.reserve(conn, params, trigger='initial' if initial else 'manual', initial_code=code if initial else None)
        Thread(target=self.run, args=(ref,), daemon=True, name='stock-collection').start()
        return dict(operation_ref=ref, status='running')

    def cancelled(self, ref):
        with self.db.connection() as conn:
            row = conn.execute('SELECT cancel_requested FROM stock_collection_operations WHERE id=?', (ref,)).fetchone()
        return not row or bool(row[0])

    def cancel(self):
        with self.db.connection(write=True) as conn:
            conn.execute("UPDATE stock_collection_operations SET cancel_requested=1 WHERE status='running'")
        return dict(cancel_requested=True)

    def collect(self, code, dataset, full, collector):
        fundamentals, dividends, valuation = self.data.p4_services()
        chips, prices = self.data.services()
        if dataset == 'financial':
            fundamentals.update(code, refresh=True, full=full)
        elif dataset == 'business_segments':
            from stock_analysis.domains.financials.business_segment_service import BusinessSegmentService
            BusinessSegmentService(self.db).update(code, refresh=True, full=full)
        elif dataset == 'announcements':
            from stock_analysis.domains.stocks.announcement_service import AnnouncementService
            reports = fundamentals.read(code).get('reports', [])
            if not reports:
                raise ValueError('公告日期核验需要本地财务报告期，请先采集财务报表')
            AnnouncementService(self.db, collector).update(code, reports, refresh=True, full=full, allow_initial=True)
        elif dataset == 'prices':
            for adjust in ('raw', 'qfq'):
                collector.check_cancelled()
                prices.ensure(code, adjust, refresh=True, full=full)
        elif dataset == 'valuation':
            reports = fundamentals.read(code).get('reports', [])
            if not reports:
                raise ValueError('估值需要本地财务报告期，请先采集财务报表')
            valuation.update(code, reports, refresh=True, full=full)
        elif dataset == 'dividends':
            dividends.update(code, refresh=True, full=full)
        else:
            chips.update(code, dataset, refresh=True, full=full)

    def run(self, ref):
        try:
            self._run(ref)
        except Exception as exc:
            with self.db.connection(write=True) as conn:
                conn.execute("UPDATE stock_collection_items SET status='failed',finished_at=?,error=? WHERE operation_id=? AND status IN ('pending','running')", (utc_now(), str(exc), ref))
                conn.execute("UPDATE stock_collection_operations SET status='failed',finished_at=?,error=? WHERE id=?", (utc_now(), str(exc), ref))

    def _run(self, ref):
        with self.db.connection() as conn:
            operation = dict(conn.execute('SELECT * FROM stock_collection_operations WHERE id=?', (ref,)).fetchone())
            items = [dict(r) for r in conn.execute("SELECT * FROM stock_collection_items WHERE operation_id=? AND status='pending' ORDER BY id", (ref,))]
        scope = json.loads(operation['scope_json'])
        collector = RequestCollector(self.db, cancelled=lambda: self.cancelled(ref))
        try:
            for item in items:
                if self.cancelled(ref):
                    break
                with self.db.connection(write=True) as conn:
                    conn.execute("UPDATE stock_collection_items SET status='running',started_at=? WHERE id=?", (utc_now(), item['id']))
                error = None
                with execution_context(scope=dict(parent_operation=ref, parent_item=item['id'],
                        requested_mode=item['mode'],
                        **({'schedule':scope['schedule']} if 'schedule' in scope else {}),
                        batch_pacing=operation['trigger_type'] == 'scheduled' or 'group_id' in scope or len(scope['codes']) > 1),
                        trigger=operation['trigger_type'], cancelled=lambda: self.cancelled(ref),
                        collector=collector, batch_pacing=operation['trigger_type'] == 'scheduled' or 'group_id' in scope or len(scope['codes']) > 1):
                    try:
                        self.collect(item['code'], item['dataset'], item['mode'] == 'full', collector)
                        if item['dataset'] == 'financial':
                            # Name lookup keeps its original conditional/source behavior.
                            self.data._add_sqlite_missing_name(self.db, self.data.p4_services()[0].read(item['code']), refresh=True)
                    except Exception as exc:
                        error = str(exc)
                with self.db.connection(write=True) as conn:
                    failed = conn.execute("SELECT error FROM collection_batches WHERE json_extract(scope_json,'$.parent_item')=? AND status IN ('failed','cancelled') ORDER BY id LIMIT 1", (item['id'],)).fetchone()
                    error = error or (failed['error'] or '数据集同步失败' if failed else None)
                    cancelled = conn.execute('SELECT cancel_requested FROM stock_collection_operations WHERE id=?', (ref,)).fetchone()[0]
                    conn.execute('UPDATE stock_collection_items SET status=?,finished_at=?,error=? WHERE id=?',
                        ('cancelled' if cancelled else 'failed' if error else 'complete', utc_now(), error, item['id']))
            with self.db.connection(write=True) as conn:
                cancelled = conn.execute('SELECT cancel_requested FROM stock_collection_operations WHERE id=?', (ref,)).fetchone()[0]
                conn.execute("UPDATE stock_collection_items SET status='cancelled',finished_at=? WHERE operation_id=? AND status='pending'", (utc_now(), ref))
                failed = conn.execute("SELECT count(*) FROM stock_collection_items WHERE operation_id=? AND status='failed'", (ref,)).fetchone()[0]
                conn.execute('UPDATE stock_collection_operations SET status=?,finished_at=?,error=? WHERE id=?',
                    ('cancelled' if cancelled else 'failed' if failed else 'complete', utc_now(), f'{failed}个数据项失败，已保留成功部分' if failed else None, ref))
        finally:
            collector.session.close()

    def recover(self):
        with self.db.connection(write=True) as conn:
            conn.execute("UPDATE stock_collection_items SET status='failed',finished_at=?,error='后台中断，已提交数据保留' WHERE status IN ('pending','running')", (utc_now(),))
            conn.execute("UPDATE stock_collection_operations SET status='failed',finished_at=?,error='后台中断，需显式继续；定时槽位按公共规则恢复' WHERE status='running'", (utc_now(),))

    def detail(self, ref):
        with self.db.connection() as conn:
            operation = conn.execute('SELECT * FROM stock_collection_operations WHERE id=?', (ref,)).fetchone()
            if not operation:
                raise ValueError('个股运行不存在')
            result = dict(operation)
            result['scope'] = json.loads(result.pop('scope_json'))
            result['items'] = [dict(r) for r in conn.execute('SELECT * FROM stock_collection_items WHERE operation_id=? ORDER BY id', (ref,))]
            for item in result['items']:
                item['sync_runs'] = [r[0] for r in conn.execute("SELECT DISTINCT operation_id FROM collection_batches WHERE json_extract(scope_json,'$.parent_item')=?", (item['id'],))]
            result['result'] = {}
            return result

    def page_status(self, code):
        with self.db.connection() as conn:
            existing = previously_initialized(conn, code)
            row = conn.execute('SELECT o.id,o.status,o.error,o.started_at,o.finished_at FROM stock_collection_operations o '
                'WHERE EXISTS (SELECT 1 FROM stock_collection_items i WHERE i.operation_id=o.id AND i.code=?) '
                "OR json_extract(o.scope_json,'$.initial_code')=? ORDER BY (o.status='running') DESC,o.started_at DESC LIMIT 1", (code, code)).fetchone()
            checks = [dict(r) for r in conn.execute('SELECT dataset,checked_at FROM sync_state WHERE instrument_id=(SELECT id FROM instruments WHERE code=?) ORDER BY dataset', (code,))]
        return dict(needs_initial=not existing and not row, operation=dict(row) if row else None, checks=checks)
