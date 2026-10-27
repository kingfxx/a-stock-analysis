"""Group schedule adapters using the public scheduler, rules and slot ledger."""
import json
import re
from uuid import uuid4
from datetime import datetime, timezone

from stock_analysis.collection.collection import encoded
from stock_analysis.domains.stocks.collection_operations import DATASETS, PROVIDERS, DATA_PROVIDERS, validate_params
from stock_analysis.infrastructure.storage import utc_now
from stock_analysis.collection import schedules
from stock_analysis.domains.stocks.collection_operations import work_summary


def read_rules(conn):
    from stock_analysis.collection.collection_management import next_check, ZONE
    rows = schedules.read(conn, "stocks")
    result = []
    for row in rows:
        rule = dict(row)
        rule['enabled'] = bool(rule['enabled'])
        rule['task_key'] = rule['schedule_id']
        group = conn.execute('SELECT name FROM stock_groups WHERE id=?', (rule['params']['group_id'],)).fetchone()
        rule['group_name'] = group[0] if group else '分组已删除'
        if not group:
            rule['enabled'] = False
        rule['next_check'] = next_check(rule, datetime.now(ZONE))
        result.append(rule)
    return result


def configure(db, command):
    from stock_analysis.collection.collection_management import validate_rule
    if isinstance(command, dict) and command.get('action') == 'delete':
        if set(command) != {'action','schedule_key'}:
            raise ValueError('删除规则参数无效')
        return delete_rule(db, command['schedule_key'])
    if not isinstance(command, dict) or set(command) - {'enabled', 'local_time', 'rule', 'params','schedule_key'} or not {'enabled','local_time','rule','params'} <= set(command):
        raise ValueError('分组日程参数无效')
    params = validate_params(command['params'])
    if 'group_id' not in params or params['mode'] != 'incremental':
        raise ValueError('定时只允许自建分组增量检查')
    rule = validate_rule(command['rule'])
    if 'window' in rule:
        raise ValueError('个股定时不支持发布窗口')
    if type(command['enabled']) is not bool or not isinstance(command['local_time'], str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', command['local_time']):
        raise ValueError('请指定布尔开关和北京时间HH:MM')
    base = 'stocks:' + str(params['group_id'])
    selected = command.get('schedule_key')
    if 'schedule_key' in command and selected is not None and (not isinstance(selected,str) or not selected.startswith(base + ':') and selected != base):
        raise ValueError('规则与股票分组不匹配')
    key = selected if selected is not None else base + ':' + uuid4().hex if 'schedule_key' in command else base
    stamp = utc_now()
    with db.connection(write=True) as conn:
        if 'schedule_key' not in command and conn.execute('SELECT 1 FROM collection_schedules WHERE schedule_id=? AND deleted=1',(key,)).fetchone():
            key=base+':'+uuid4().hex
        if not conn.execute('SELECT 1 FROM stock_groups WHERE id=?', (params['group_id'],)).fetchone():
            raise ValueError('分组不存在')
        if selected is not None:
            existing = conn.execute('SELECT params_json FROM collection_schedules WHERE schedule_id=? AND deleted=0',(key,)).fetchone()
            if not existing or json.loads(existing[0]).get('group_id') != params['group_id']:
                raise ValueError('规则已删除或不属于该分组，请重新读取')
        conn.execute('INSERT INTO collection_schedules(schedule_id,task_key,enabled,timezone,local_time,updated_at,rule_json,params_json,rule_updated_at) '
            "VALUES(?,'stocks',?,'Asia/Shanghai',?,?,?,?,?) ON CONFLICT(schedule_id) DO UPDATE SET enabled=excluded.enabled,"
            'local_time=excluded.local_time,updated_at=excluded.updated_at,rule_json=excluded.rule_json,params_json=excluded.params_json,rule_updated_at=excluded.rule_updated_at',
            (key,int(command['enabled']),command['local_time'],stamp,encoded(rule),encoded(params),stamp))

    return dict(saved=True,schedule_key=key)


def delete_rule(db, key):
    if not isinstance(key,str) or not key.startswith('stocks:'):
        raise ValueError('个股规则引用无效')
    with db.connection(write=True) as conn:
        schedules.delete(conn,'stocks',key)

    return dict(deleted=True,schedule_key=key)


def tick(service, now, stopped):
    from stock_analysis.collection.collection_management import due_slot, ZONE
    db = service.db
    with db.connection() as conn:
        rules = read_rules(conn)
    for rule in rules:
        if stopped():
            return
        if not rule['enabled']:
            continue
        key, slot = rule['task_key'], due_slot(rule, now)
        scheduled_at = datetime.fromisoformat(slot + 'T' + rule['local_time']).replace(tzinfo=ZONE)
        if scheduled_at < datetime.fromisoformat(rule['updated_at']):
            continue
        with db.connection(write=True) as conn:
            current = next((r for r in read_rules(conn) if r['task_key'] == key), None)
            if not current or any(current[k] != rule[k] for k in ('enabled','updated_at','rule','params','local_time')) or not current['enabled'] or stopped():
                continue
            old = conn.execute('SELECT * FROM collection_schedule_slots WHERE schedule_id=? AND slot_key=?', (key, slot)).fetchone()
            if old and old['status'] != 'pending':
                continue
            providers = sorted({p for dataset in rule['params']['datasets'] for p in DATA_PROVIDERS[dataset]})
            cooling = conn.execute('SELECT cooldown_until FROM collection_provider_state WHERE source_system IN (' + ','.join('?' for _ in providers) + ')', providers)
            if any(r[0] and datetime.fromisoformat(r[0]) > now for r in cooling):
                continue
            params = rule['params']
            if old and old['operation_ref']:
                prior = conn.execute('SELECT scope_json FROM collection_operations WHERE operation_id=?', (old['operation_ref'],)).fetchone()
                if prior:
                    fixed = json.loads(prior[0])
                    if not fixed['codes']:
                        conn.execute("UPDATE collection_schedule_slots SET status='complete',finished_at=?,updated_at=?,error='原范围为空，无待恢复数据项' WHERE schedule_id=? AND slot_key=?", (utc_now(),utc_now(),key,slot))
                        continue
                    params = dict(codes=fixed['codes'], datasets=fixed['datasets'], mode='incremental')
            try:
                ref = service.reserve(conn, params, trigger='scheduled', since=scheduled_at.astimezone(timezone.utc).isoformat())
            except ValueError:
                continue
            conn.execute("UPDATE collection_operations SET scope_json=json_set(scope_json,'$.schedule',json(?)) WHERE operation_id=?",
                         (encoded(dict(task_key=key,slot_key=slot)),ref))
            stamp = utc_now()
            conn.execute("UPDATE collection_schedule_slots SET status='failed',updated_at=?,error='漏跑已合并到最新槽位' WHERE schedule_id=? AND status='pending' AND slot_key<>?", (stamp, key, slot))
            conn.execute("INSERT INTO collection_schedule_slots VALUES(?,?,'running',?,1,?,NULL,?,NULL) ON CONFLICT(schedule_id,slot_key) DO UPDATE SET status='running',operation_ref=excluded.operation_ref,attempts=attempts+1,started_at=excluded.started_at,finished_at=NULL,updated_at=excluded.updated_at,error=NULL", (key, slot, ref, stamp, stamp))
        service.run(ref)
        with db.connection(write=True) as conn:
            result = conn.execute('SELECT status,error FROM collection_operations WHERE operation_id=?', (ref,)).fetchone()
            attempts = conn.execute('SELECT attempts FROM collection_schedule_slots WHERE schedule_id=? AND slot_key=?', (key, slot)).fetchone()[0]
            status = 'complete' if result['status'] == 'complete' else 'pending' if stopped() and attempts < 2 else 'failed'
            conn.execute('UPDATE collection_schedule_slots SET status=?,finished_at=?,updated_at=?,error=? WHERE schedule_id=? AND slot_key=?', (status, utc_now(), utc_now(), result['error'], key, slot))


def task_summary(service, conn):
    from stock_analysis.collection.collection_rate_limits import describe
    from stock_analysis.collection.collection_progress import attach_data_counts
    rows = [dict(r) for r in conn.execute("SELECT operation_id AS operation_ref,mode,status,trigger_type,started_at,finished_at,error FROM collection_operations WHERE task_key='stocks' ORDER BY started_at DESC LIMIT 20")]
    for row in rows:
        works=work_summary(conn,row['operation_ref'])
        counts={state:sum(w['status']==state for w in works) for state in ('pending','running','complete','failed','cancelled','reused')}
        row['result']=dict(message='股票数据项：'+'，'.join(f'{k} {v}' for k,v in counts.items() if v))
        row['result']['work_progress']=dict(total=len(works),success=counts['complete'],skipped=counts['reused'],failed=counts['failed'],cancelled=counts['cancelled'],running=counts['running'],pending=counts['pending'],providers={},unit='股票数据项')
        row['result']['network_requests']=conn.execute('SELECT count(*) FROM collection_sources s JOIN collection_batches b ON b.id=s.batch_id WHERE b.operation_id=?',(row['operation_ref'],)).fetchone()[0]
        attach_data_counts(conn,[row],PROVIDERS)
        from stock_analysis.collection.collection_progress import merge
        row['result']['provider_counts']=merge([{**entry,'label':'合计'} for entry in row['result']['data_counts']])
    latest = rows[0]['operation_ref'] if rows else None
    batches = [dict(r) for r in conn.execute("SELECT id,dataset,source_system,status,coverage_status,scope_start,scope_end,started_at,finished_at,error FROM collection_batches WHERE operation_id=? ORDER BY id DESC LIMIT 100", (latest,))] if latest else []
    obtained = conn.execute("SELECT max(s.obtained_at) FROM collection_sources s JOIN collection_batches b ON b.id=s.batch_id WHERE b.operation_id=?", (latest,)).fetchone()[0] if latest else None
    groups = [dict(r) for r in conn.execute('SELECT g.id,g.name,count(m.instrument_id) AS member_count FROM stock_groups g LEFT JOIN stock_group_members m ON m.group_id=g.id GROUP BY g.id ORDER BY g.position,g.id')]
    rules = read_rules(conn)
    return dict(task_key='stocks', name='个股采集', group='个股', providers=PROVIDERS, modes=['incremental','full','resume'],
        supports_cancel=True, rate_limits=describe(conn, 'stocks', PROVIDERS), datasets=DATASETS, groups=groups,
        rules=rules, schedule=dict(enabled=any(r['enabled'] for r in rules), local_time='18:00', rule=dict(frequency='daily'),
            next_check=min((r['next_check'] for r in rules if r['next_check']),default=None)),
        running=bool(conn.execute("SELECT 1 FROM collection_operations WHERE task_key='stocks' AND status='running' LIMIT 1").fetchone()),
        history=rows, batches=batches, last_check=obtained,
        last_success=next((r['finished_at'] for r in rows if r['status'] == 'complete'), None),
        cooldowns=[dict(r) for r in conn.execute('SELECT source_system,cooldown_until,reason FROM collection_provider_state WHERE source_system IN (' + ','.join('?' for _ in PROVIDERS) + ')', PROVIDERS)],
        slots=[dict(r) for r in conn.execute("SELECT * FROM collection_schedule_slots WHERE schedule_id IN (SELECT schedule_id FROM collection_schedules WHERE task_key='stocks') ORDER BY started_at DESC LIMIT 10")],
        coverage='unknown', coverage_note='执行与数据覆盖分开；全历史及逐字段覆盖未知')
