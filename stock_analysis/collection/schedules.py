"""Shared independent schedule identities, configuration and soft deletion."""
import json
from uuid import uuid4
from stock_analysis.infrastructure.storage import utc_now


def read(conn, task_key):
    result = []
    for row in conn.execute('SELECT * FROM collection_schedules WHERE task_key=? AND deleted=0 ORDER BY schedule_id', (task_key,)):
        value = dict(row)
        value['enabled'] = bool(value['enabled'])
        value['rule'] = json.loads(value.pop('rule_json') or '{"frequency":"daily"}')
        value['params'] = json.loads(value.pop('params_json') or '{"mode":"incremental"}')
        result.append(value)
    return result


def save(conn, task_key, command):
    supplied = 'schedule_key' in command
    selected = command.get('schedule_key')
    if selected is not None and (not isinstance(selected,str) or not selected or len(selected)>200):
        raise ValueError('日程规则身份无效')
    if selected is not None:
        row = conn.execute('SELECT task_key,deleted FROM collection_schedules WHERE schedule_id=?', (selected,)).fetchone()
        if not row or row['task_key'] != task_key or row['deleted']:
            raise ValueError('规则不存在或不属于该任务')
    identity = selected if supplied and selected is not None else task_key + ':' + uuid4().hex if supplied else task_key
    if not supplied and conn.execute('SELECT 1 FROM collection_schedules WHERE schedule_id=? AND deleted=1',(identity,)).fetchone():
        identity=task_key+':'+uuid4().hex
    stamp = utc_now()
    conn.execute('INSERT INTO collection_schedules(schedule_id,task_key,enabled,timezone,local_time,updated_at,rule_json,params_json,rule_updated_at) '
        'VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(schedule_id) DO UPDATE SET enabled=excluded.enabled,local_time=excluded.local_time,'
        'updated_at=excluded.updated_at,rule_json=excluded.rule_json,params_json=excluded.params_json,rule_updated_at=excluded.rule_updated_at',
        (identity,task_key,int(command['enabled']),'Asia/Shanghai',command['local_time'],stamp,
         json.dumps(command['rule'],ensure_ascii=False,separators=(',',':')),
         json.dumps(command['params'],ensure_ascii=False,separators=(',',':')),stamp))
    return identity


def delete(conn, task_key, identity):
    if not isinstance(identity,str) or not identity or len(identity)>200:
        raise ValueError('日程规则身份无效')
    row = conn.execute('SELECT 1 FROM collection_schedules WHERE task_key=? AND schedule_id=? AND deleted=0', (task_key,identity)).fetchone()
    if not row:
        raise ValueError('规则已删除或不存在')
    stamp=utc_now()
    conn.execute('UPDATE collection_schedules SET deleted=1,enabled=0,updated_at=? WHERE schedule_id=?',(stamp,identity))
    conn.execute("UPDATE collection_schedule_slots SET status='failed',finished_at=coalesce(finished_at,?),updated_at=?,error='规则已删除，不再恢复' WHERE schedule_id=? AND status='pending'",(stamp,stamp,identity))
