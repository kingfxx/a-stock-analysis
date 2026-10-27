"""Persistent stock refresh operations over existing fact services, without payload copies."""
from __future__ import annotations

import json
from threading import Thread
from uuid import uuid4

from stock_analysis.collection.collection import RequestCollector, encoded
from stock_analysis.collection.execution_context import execution_context
from stock_analysis.domains.stocks.collection_plan import segments, selected, versions
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
        work=[]
        sequence = 0
        for code in codes:
            for dataset in DATASETS:
                if dataset not in params['datasets']:
                    continue
                conflict = conn.execute("SELECT 1 FROM collection_operations o,json_each(o.scope_json,'$.codes') c,"
                    "json_each(o.scope_json,'$.datasets') d WHERE o.task_key='stocks' AND o.status='running' "
                    "AND o.operation_id<>? AND c.value=? AND d.value=?", (ref,code,dataset)).fetchone()
                if conflict:
                    raise ValueError(f'{code} 的{DATASETS[dataset]}已有运行任务，请待结束后操作')
                legacy_mode=None
                if params['mode']=='resume':
                    old_work=conn.execute("SELECT json_extract(w.value,'$.mode') FROM collection_operations o,json_each(o.result_json,'$.legacy_unsegmented_work') w "
                        "WHERE o.task_key='stocks' AND json_extract(w.value,'$.code')=? AND json_extract(w.value,'$.dataset')=? ORDER BY o.started_at DESC LIMIT 1",(code,dataset)).fetchone()
                    legacy_mode=old_work[0] if old_work and old_work[0] in ('full','incremental') else None
                for provider, detail, interface in segments(dataset):
                    sequence += 1
                    work_key = ':'.join((code,detail,provider,interface))
                    parser,definition=versions(detail)
                    prior = conn.execute("SELECT id,status,finished_at,mode,parser_version,definition_version,result_json,scope_json FROM collection_batches WHERE (code=? OR EXISTS (SELECT 1 FROM json_each(scope_json,'$.stock_members') m WHERE json_extract(m.value,'$.code')=?)) AND work_dataset=? "
                        "AND dataset=? AND source_system=? AND coalesce(json_extract(scope_json,'$.source_interface'),'')=? "
                        "AND status IN ('complete','failed','cancelled','reused','skipped') ORDER BY id DESC LIMIT 1",
                        (code,code,dataset,'stock.'+detail,provider,interface)).fetchone()
                    if prior and json.loads(prior['scope_json']).get('stock_members'):
                        prior=dict(prior)
                        member=json.loads(prior['result_json']).get('stock_results',{}).get(code,{})
                        prior.update(status=member.get('status','failed'),finished_at=member.get('finished_at'))
                    reused = bool(prior and prior['parser_version']==parser and prior['definition_version']==definition and prior['status'] in ('complete','reused','skipped') and
                        (params['mode']=='resume' or since and prior['finished_at'] and prior['finished_at']>=since))
                    mode = prior['mode'] if params['mode']=='resume' and prior and prior['mode'] else legacy_mode or ('full' if params['mode']=='full' else 'incremental')
                    details = dict(code=code,evidence_mode='metadata_only')
                    if interface:
                        details['source_interface'] = interface

                    work.append((ref,'stock','stock.'+detail,provider,encoded(details),trigger,'reused' if reused else 'pending',stamp,
                         stamp if reused else None,code,dataset,work_key,sequence,mode,prior['id'] if reused else None,parser,definition))

        conn.execute('INSERT INTO collection_operations(operation_id,task_key,mode,trigger_type,scope_json,status,started_at) '
                     "VALUES(?,'stocks',?,?,?,'running',?)", (ref, params['mode'], trigger, encoded(scope), stamp))
        conn.executemany('INSERT INTO collection_batches(operation_id,domain,dataset,source_system,scope_json,trigger_type,'
                        'status,created_at,finished_at,code,work_dataset,work_key,sequence,mode,reused_batch_id,parser_version,definition_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', work)

        return ref

    def start(self, params, *, initial=False):
        if initial:
            params = validate_params(params)
            if len(params.get('codes', [])) != 1:
                raise ValueError('首次初始化须指定一只股票')
            code = params['codes'][0]
        with self.db.connection(write=True) as conn:
            if initial:
                old = conn.execute("SELECT operation_id AS id,status FROM collection_operations WHERE task_key='stocks' AND trigger_type='initial' AND json_extract(scope_json,'$.initial_code')=?", (code,)).fetchone()
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
            row = conn.execute('SELECT cancel_requested FROM collection_operations WHERE operation_id=?', (ref,)).fetchone()
        return not row or bool(row[0])

    def cancel(self):
        with self.db.connection(write=True) as conn:
            conn.execute("UPDATE collection_operations SET cancel_requested=1 WHERE task_key='stocks' AND status='running'")
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
                if not selected('prices_raw' if adjust=='raw' else 'prices_adjusted'):
                    continue
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
        with self.db.connection(write=True) as conn:
            claimed=conn.execute("UPDATE collection_operations SET worker_started_at=? WHERE operation_id=? AND task_key='stocks' AND status='running' AND worker_started_at IS NULL",(utc_now(),ref)).rowcount
        if not claimed:
            return
        try:
            self._run(ref)
        except Exception as exc:
            with self.db.connection(write=True) as conn:
                conn.execute("UPDATE collection_batches SET status='failed',finished_at=?,error=? WHERE operation_id=? AND status IN ('pending','running')", (utc_now(), str(exc), ref))
                conn.execute("UPDATE collection_operations SET status='failed',finished_at=?,error=? WHERE operation_id=?", (utc_now(), str(exc), ref))

    def _run(self, ref):
        with self.db.connection() as conn:
            operation = dict(conn.execute('SELECT * FROM collection_operations WHERE operation_id=?', (ref,)).fetchone())
            groups = conn.execute("SELECT code,work_dataset,mode,min(sequence) AS position FROM collection_batches WHERE operation_id=? "
                "AND status='pending' GROUP BY code,work_dataset,mode ORDER BY position", (ref,)).fetchall()
        scope = json.loads(operation['scope_json'])
        pacing = operation['trigger_type']=='scheduled' or 'group_id' in scope or len(scope['codes'])>1
        if pacing:
            from stock_analysis.domains.stocks.group_execution import run_group
            run_group(self,ref,operation,groups,factory=RequestCollector)
            self._finish(ref)
            return
        collector = RequestCollector(self.db, cancelled=lambda: self.cancelled(ref))
        try:
            for group in groups:
                if self.cancelled(ref):
                    break
                code,dataset = group['code'],group['work_dataset']
                with self.db.connection() as conn:
                    pending = [dict(r) for r in conn.execute("SELECT dataset,scope_json,mode FROM collection_batches WHERE operation_id=? AND code=? AND work_dataset=? AND mode=? AND status='pending'", (ref,code,dataset,group['mode']))]
                allowed = {(r['dataset'].removeprefix('stock.'),json.loads(r['scope_json']).get('source_interface','')) for r in pending}
                full = any(r['mode']=='full' for r in pending)
                error = None
                pacing = operation['trigger_type']=='scheduled' or 'group_id' in scope or len(scope['codes'])>1
                with execution_context(operation_id=ref,work_dataset=dataset,selected_segments=allowed,
                        scope=dict(requested_mode='full' if full else 'incremental',batch_pacing=pacing,
                            **({'schedule':scope['schedule']} if 'schedule' in scope else {})),
                        trigger=operation['trigger_type'],cancelled=lambda:self.cancelled(ref),collector=collector,batch_pacing=pacing):
                    try:
                        self.collect(code,dataset,full,collector)
                        if dataset=='financial':
                            self.data._add_sqlite_missing_name(self.db,self.data.p4_services()[0].read(code),refresh=True)
                    except Exception as exc:
                        error = str(exc)
                with self.db.connection(write=True) as conn:
                    cancelled = conn.execute('SELECT cancel_requested FROM collection_operations WHERE operation_id=?', (ref,)).fetchone()[0]
                    # A service that used local data has no actual start/request; preserve that distinction.
                    conn.execute("UPDATE collection_batches SET status=?,finished_at=?,error=? WHERE operation_id=? AND code=? AND work_dataset=? AND mode=? AND status='pending'",
                        ('cancelled' if cancelled else 'failed' if error else 'skipped',utc_now(),error,ref,code,dataset,group['mode']))
            self._finish(ref)
        finally:
            collector.session.close()

    def _finish(self, ref):
        with self.db.connection(write=True) as conn:
            cancelled = conn.execute('SELECT cancel_requested FROM collection_operations WHERE operation_id=?', (ref,)).fetchone()[0]
            conn.execute("UPDATE collection_batches SET status='cancelled',finished_at=? WHERE operation_id=? AND status='pending'", (utc_now(),ref))
            failed = conn.execute("SELECT count(*) FROM collection_batches WHERE operation_id=? AND status IN ('failed','running')", (ref,)).fetchone()[0]
            conn.execute('UPDATE collection_operations SET status=?,finished_at=?,error=? WHERE operation_id=?',
                ('cancelled' if cancelled else 'failed' if failed else 'complete',utc_now(),f'{failed}个分段失败，已保留成功部分' if failed else None,ref))

    def recover(self):
        with self.db.connection(write=True) as conn:
            conn.execute("UPDATE collection_batches SET status='failed',finished_at=?,error='后台中断，已提交数据保留' WHERE status IN ('pending','running') AND operation_id IN (SELECT operation_id FROM collection_operations WHERE task_key='stocks')", (utc_now(),))
            conn.execute("UPDATE collection_operations SET status='failed',finished_at=?,error='后台中断，需显式继续；定时槽位按公共规则恢复' WHERE task_key='stocks' AND status='running'", (utc_now(),))

    def detail(self, ref):
        with self.db.connection() as conn:
            operation = conn.execute("SELECT *,operation_id AS stock_id FROM collection_operations WHERE operation_id=? AND task_key='stocks'", (ref,)).fetchone()
            if not operation:
                raise ValueError('个股运行不存在')
            result = dict(operation)
            result['id'] = result.pop('stock_id')
            result['scope'] = json.loads(result.pop('scope_json'))
            result['result'] = json.loads(result.pop('result_json'))
            result['items'] = work_summary(conn,ref)
            return result

    def page_status(self, code):
        with self.db.connection() as conn:
            existing = previously_initialized(conn, code)
            row = conn.execute("SELECT o.operation_id AS id,o.status,o.error,o.started_at,o.finished_at FROM collection_operations o WHERE o.task_key='stocks' AND "
                "(EXISTS (SELECT 1 FROM json_each(o.scope_json,'$.codes') c WHERE c.value=?) "
                "OR json_extract(o.scope_json,'$.initial_code')=?) ORDER BY (o.status='running') DESC,o.started_at DESC LIMIT 1", (code,code)).fetchone()
            checks = [dict(r) for r in conn.execute('SELECT dataset,checked_at FROM sync_state WHERE instrument_id=(SELECT id FROM instruments WHERE code=?) ORDER BY dataset', (code,))]
        return dict(needs_initial=not existing and not row,operation=dict(row) if row else None,checks=checks)


def work_summary(conn, ref):
    rows = [dict(r) for r in conn.execute('SELECT id,code,work_dataset,status,mode,started_at,finished_at,error,sync_run_id,scope_json,result_json,dataset,source_system FROM collection_batches WHERE operation_id=? AND work_dataset IS NOT NULL ORDER BY sequence,id', (ref,))]
    from stock_analysis.domains.stocks.group_batches import member_rows
    rows = [member for row in rows for member in member_rows(row)]
    groups = {}
    for row in rows:
        key = row['code'],row['work_dataset']
        item = groups.setdefault(key,dict(id=row['id'],code=row['code'],dataset=row['work_dataset'],mode=row['mode'],statuses=[],sync_runs=[],started_at=None,finished_at=None,error=None))
        item['statuses'].append(row['status'])
        entry=row['counts'] if 'counts' in row else json.loads(row.get('result_json') or '{}')
        if row['status'] in ('reused','skipped'):entry={'published':0}
        item.setdefault('data_counts',[]).append(dict(provider=row['source_system'],dataset=row['dataset'],**{k:entry.get(k) for k in ('inserted','revised','published','returned','unchanged')}))
        if row['sync_run_id'] is not None:
            item['sync_runs'].append('sync_runs:'+str(row['sync_run_id']))
        if row['error']:
            item['error'] = row['error']
        if row['started_at']:
            item['started_at'] = min(item['started_at'] or row['started_at'],row['started_at'])
        if row['finished_at']:
            item['finished_at'] = max(item['finished_at'] or row['finished_at'],row['finished_at'])
    for item in groups.values():
        states=item.pop('statuses')
        item['status']=next((state for state in ('running','pending','failed','cancelled') if state in states),'reused' if all(s=='reused' for s in states) else 'complete')
    # Only legacy aggregates without provable segment identity take this path.
    result=conn.execute('SELECT result_json FROM collection_operations WHERE operation_id=?',(ref,)).fetchone()
    for old in json.loads(result[0]).get('legacy_unsegmented_work',[]) if result else []:
        groups.setdefault((old['code'],old['dataset']),old)
    return list(groups.values())
