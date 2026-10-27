"""Local schedule history and explicit enable/pause commands."""
import json

from stock_analysis.collection.collection_progress import attach_data_counts
from stock_analysis.infrastructure.storage import utc_now


def attach_history(conn, tasks, result_projection):
    for task in tasks:
        for rule in task.get('rules', []):
            rows = [dict(row) for row in conn.execute(
                'SELECT s.slot_key,s.status AS slot_status,s.attempts,s.error AS slot_error,'
                'o.operation_id AS operation_ref,o.status,o.started_at,o.finished_at,o.error,'
                'o.scope_json,' + result_projection.replace('result_json', 'o.result_json') + ' AS result_json '
                'FROM collection_schedule_slots s JOIN collection_operations o ON o.operation_id=s.operation_ref '
                "WHERE s.schedule_id=? AND o.task_key=? AND o.trigger_type='scheduled' "
                'ORDER BY s.slot_key DESC LIMIT 5', (rule['schedule_id'], task['task_key']))]
            for row in rows:
                row['result'] = json.loads(row.pop('result_json') or '{}')
                row['scope'] = json.loads(row.pop('scope_json'))
            if task['task_key'] == 'stocks':
                from stock_analysis.domains.stocks.collection_schedules import decorate_runs
                decorate_runs(conn, rows)
            else:
                attach_data_counts(conn, rows, task['providers'])
            rule['history'] = rows


def toggle(conn, key, command):
    from stock_analysis.collection.schedules import read
    from stock_analysis.collection.collection_management import validate_params
    from stock_analysis.domains.market import market_congestion

    if set(command) != {'action', 'schedule_key', 'enabled'} or type(command['enabled']) is not bool:
        raise ValueError('启停计划参数无效')
    current = next((rule for rule in read(conn, key) if rule['schedule_id'] == command['schedule_key']), None)
    if current is None:
        raise ValueError('计划已删除或不存在，请刷新')
    if command['enabled']:
        validate_params(key, current['params'], scheduled=True)
        if key == 'stocks' and not conn.execute('SELECT 1 FROM stock_groups WHERE id=?', (current['params']['group_id'],)).fetchone():
            raise ValueError('分组已删除，不能激活计划')
        if key == 'market_congestion' and not market_congestion.initialized(conn):
            raise ValueError('请先完成拥挤度历史初始化')
    conn.execute('UPDATE collection_schedules SET enabled=?,updated_at=? WHERE schedule_id=?',
                 (int(command['enabled']), utc_now(), current['schedule_id']))
    return dict(saved=True, schedule_key=current['schedule_id'], enabled=command['enabled'])
