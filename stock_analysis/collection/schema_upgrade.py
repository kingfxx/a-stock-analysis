"""Transactional migration of collection control, retaining real evidence IDs."""
import json


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def index_task(scope):
    if scope.get('dataset'):
        return scope['dataset']
    return 'sovereign_yields' if scope.get('datasets') == ['bond_zh_us_rate'] and not scope.get('providers') else 'indices'


def compact_history(conn, operation, payload):
    """Move actual batch diagnostics to their original segment, never infer dates."""
    values = dict(payload)
    unmatched = []
    for detail in values.pop('results', []):
        batch = None
        if isinstance(detail, dict) and isinstance(detail.get('batch_id'), int):
            batch = conn.execute('SELECT id,result_json FROM collection_batches WHERE id=? AND operation_id=?',
                                 (detail['batch_id'], operation)).fetchone()
        if batch is None and isinstance(detail, dict) and detail.get('dataset'):
            candidates = conn.execute('SELECT id,result_json FROM collection_batches WHERE operation_id=? '
                'AND dataset=? AND source_system=? AND (? IS NULL OR json_extract(scope_json,\'$.code\')=?)',
                (operation, detail['dataset'], detail.get('provider'), detail.get('code'), detail.get('code'))).fetchall()
            if len(candidates) == 1:
                batch = candidates[0]
        if batch is None:
            unmatched.append(detail)
            continue
        saved = json.loads(batch['result_json'])
        # Retain only fields not already stored identically in the real segment.
        extra = {k: v for k, v in detail.items() if k not in ('batch_id',) and saved.get(k) != v}
        if extra:
            saved.setdefault('legacy_operation_details', []).append(extra)
            conn.execute('UPDATE collection_batches SET result_json=? WHERE id=?', (encoded(saved), batch['id']))
    if unmatched:
        # Historical unknown identity is evidence, not a licence to invent a batch.
        values['legacy_unmapped_results'] = unmatched
    progress = values.get('work_progress')
    if isinstance(progress, dict):
        tasks=progress.pop('tasks', None)
        if tasks:
            # These old control messages can describe local skips without a batch.
            # Preserve their exact history; new executions never generate this field.
            values['legacy_task_states']=tasks
    return values


def migrate_control(conn):
    # Industry IDs are externally referenced; migrate them before allocating other IDs.
    for row in conn.execute('SELECT * FROM sw_update_runs').fetchall():
        conn.execute('INSERT INTO collection_operations(id,action,target,recheck,started_at,finished_at,status,result_json,error,cancel_requested) '
            'VALUES(?,?,?,?,?,?,?,?,?,?)', (row['id'], row['action'], row['target'], row['recheck'], row['started_at'],
            row['finished_at'], row['status'], row['result_json'] or '{}', row['error'], row['cancel_requested']))
    for row in conn.execute('SELECT * FROM index_collection_operations').fetchall():
        scope = json.loads(row['scope_json'])
        result = compact_history(conn, row['operation_id'], json.loads(row['result_json']))
        conn.execute('INSERT INTO collection_operations(operation_id,task_key,mode,trigger_type,scope_json,status,cancel_requested,'
            'started_at,finished_at,result_json,error) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (row['operation_id'], index_task(scope), row['mode'], scope.get('trigger', 'manual'), row['scope_json'], row['status'],
             row['cancel_requested'], row['started_at'], row['finished_at'], encoded(result), row['error']))
    for row in conn.execute('SELECT * FROM stock_collection_operations').fetchall():
        conn.execute('INSERT INTO collection_operations(operation_id,task_key,mode,trigger_type,scope_json,status,cancel_requested,'
            'started_at,finished_at,error) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (row['id'], 'stocks', row['mode'], row['trigger_type'], row['scope_json'], row['status'], row['cancel_requested'],
             row['started_at'], row['finished_at'], row['error']))
    common = [r[1] for r in conn.execute('PRAGMA table_info(collection_batches)')]
    columns = ','.join(common)
    conn.execute(f'INSERT INTO collection_batches_v44({columns}) SELECT {columns} FROM collection_batches')
    conn.execute('DROP TABLE collection_batches')
    conn.execute('ALTER TABLE collection_batches_v44 RENAME TO collection_batches')
    operations = {r[0] for r in conn.execute('SELECT operation_id FROM collection_operations')}
    for row in conn.execute('SELECT id,operation_id,scope_json FROM collection_batches').fetchall():
        scope = json.loads(row['scope_json'])
        old = row['operation_id']
        parent = scope.get('parent_operation', old)
        sync = scope.get('sync_run_id')
        if sync is None and old.startswith('sync_runs:'):
            sync = int(old.split(':', 1)[1])
        if sync is not None and not conn.execute('SELECT 1 FROM sync_runs WHERE id=?', (sync,)).fetchone():
            sync = None
        conn.execute('UPDATE collection_batches SET operation_id=?,legacy_operation_ref=?,sync_run_id=?,code=? WHERE id=?',
            (parent if parent in operations else None, old, sync, scope.get('code'), row['id']))
    # Legacy items are business aggregates, not necessarily independently committed batches.
    # Attach their exact historical fields to real segments; unresolved aggregates remain
    # explicitly labelled historical metadata on their real operation (never fake sources).
    for item in conn.execute('SELECT * FROM stock_collection_items').fetchall():
        rows = conn.execute("SELECT id,scope_json FROM collection_batches WHERE json_extract(scope_json,'$.parent_item')=?",
                            (item['id'],)).fetchall()
        if rows:
            for batch in rows:
                scope = json.loads(batch['scope_json'])
                scope['legacy_work'] = dict(item)
                conn.execute('UPDATE collection_batches SET work_dataset=?,mode=?,scope_json=? WHERE id=?',
                    (item['dataset'], item['mode'], encoded(scope), batch['id']))
        else:
            operation = conn.execute('SELECT result_json FROM collection_operations WHERE operation_id=?', (item['operation_id'],)).fetchone()
            result = json.loads(operation[0])
            result.setdefault('legacy_unsegmented_work', []).append(dict(item))
            conn.execute('UPDATE collection_operations SET result_json=? WHERE operation_id=?', (encoded(result), item['operation_id']))
    trigger=conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='business_segment_version_identity'").fetchone()[0]
    conn.execute('DROP TRIGGER business_segment_version_identity')
    conn.execute(trigger.replace("b.operation_id='sync_runs:'||r.id",'b.sync_run_id=r.id'))
    for name in ('sw_memberships_public_source_insert','sw_memberships_public_source_update'):
        sql=conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",(name,)).fetchone()[0]
        conn.execute('DROP TRIGGER '+name)
        conn.execute(sql.replace('b.operation_id=json_extract','coalesce(b.operation_id,b.legacy_operation_ref)=json_extract'))

    conn.execute('CREATE INDEX collection_batch_operation ON collection_batches(operation_id,id)')
    conn.execute('CREATE INDEX collection_batch_scope ON collection_batches(domain,dataset,source_system,scope_start,scope_end,status)')
    conn.execute("CREATE UNIQUE INDEX collection_batch_idempotency ON collection_batches(domain,dataset,source_system,idempotency_key) WHERE idempotency_key IS NOT NULL AND status='complete'")
    conn.execute('CREATE INDEX collection_batch_legacy_ref ON collection_batches(coalesce(operation_id,legacy_operation_ref),status)')
    conn.execute('CREATE INDEX collection_batch_queue ON collection_batches(operation_id,status,sequence,id)')
    conn.execute('CREATE UNIQUE INDEX collection_batch_work ON collection_batches(operation_id,work_key) WHERE work_key IS NOT NULL')
    conn.execute('CREATE INDEX collection_batch_stock_work ON collection_batches(code,work_dataset,status,finished_at)')
    for schedule in conn.execute('SELECT * FROM collection_schedules').fetchall():
        rule = conn.execute('SELECT * FROM collection_schedule_rules WHERE task_key=?', (schedule['schedule_id'],)).fetchone()
        stock = schedule['schedule_id'].startswith('stocks:')
        conn.execute('UPDATE collection_schedules SET task_key=?,rule_json=?,params_json=?,rule_updated_at=?,deleted=? WHERE schedule_id=?',
            ('stocks' if stock else schedule['schedule_id'], rule['rule_json'] if rule else '{"frequency":"daily"}',
             rule['params_json'] if rule else '{"mode":"incremental"}', rule['updated_at'] if rule else None,
             int(stock and rule is None), schedule['schedule_id']))
    for table in ('stock_collection_items', 'stock_collection_operations', 'index_collection_operations', 'collection_schedule_rules', 'sw_update_runs'):
        conn.execute('DROP TABLE ' + table)
