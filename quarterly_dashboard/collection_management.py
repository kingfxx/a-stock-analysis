"""Registered adapters over existing collectors; local reads never start source work."""
from __future__ import annotations

from calendar import monthrange
from datetime import datetime, timedelta
import json
import re
import sqlite3
from threading import Event, Thread

from . import index_collection, market_congestion, shipping_collection
from .collection import encoded
from .collection_schedule import latest_daily_date
from .storage import utc_now

ZONE = market_congestion.ZONE
TASKS = {
    'shipping_research_import': dict(name='航运既有研究资料导入', group='航运研究', providers=['user_excel'], modes=['incremental'], manual_only=True),
    'shipping_image_review': dict(name='航运月度图片核验导入', group='航运研究', providers=['mot'], modes=['incremental'], manual_only=True),
    'shipping_weekly': dict(name='航运 CCFI / SCFI 周度', group='航运研究', providers=['sse'], modes=['incremental','review']),
    'shipping_monthly': dict(name='航运官方月度指数与吞吐量', group='航运研究', providers=['mot'], modes=['incremental','backfill','review']),
    'market_financial': dict(name='全市场财务四表', group='全市场', providers=['eastmoney'],
                             action='financial_period', modes=['incremental', 'review']),
    'trade_snapshot': dict(name='季度交易快照', group='全市场', providers=['eastmoney'],
                          action='cap_quarter', modes=['incremental', 'review']),
    'indices': dict(name='指数估值与行情', group='指数', providers=['csindex', 'legu', 'sws'],
                    modes=['incremental', 'full', 'review', 'audit', 'backfill']),
    'market_congestion': dict(name='大盘拥挤度', group='市场指标', providers=['legu'],
                             modes=['incremental', 'review', 'full']),
    'sovereign_yields': dict(name='中美国债收益率', group='宏观', providers=['eastmoney'], modes=['incremental']),
}
# Project only summary fields inside SQLite: old result_json can contain megabytes
# of source manifests, per-company failures and statement field counts.
SUMMARY_FIELDS = ('message', 'network_requests', 'retries', 'scope_count', 'requested_count',
                  'returned_count', 'collection_progress', 'errors', 'channels', 'skipped')
RESULT_SUMMARY = 'json_object(' + ','.join("'" + key + "',json_extract(result_json,'$." + key + "')" for key in SUMMARY_FIELDS) + ", 'work_progress',json_remove(json_extract(result_json,'$.work_progress'),'$.tasks'))"


def task(key):
    if not isinstance(key, str) or key not in TASKS:
        raise ValueError('未登记的采集任务')
    return TASKS[key]


def validate_params(key, params, *, scheduled=False):
    spec = task(key)
    if not isinstance(params, dict):
        raise ValueError('任务参数须为对象')
    allowed = {'mode','file'} if spec.get('manual_only') else {'mode', 'target'} if 'action' in spec else {'mode', 'providers', 'start', 'end'} if key == 'indices' else {'mode','start','end'} if key == 'shipping_monthly' else {'mode'}
    if set(params) - allowed:
        raise ValueError('任务参数不在白名单中')
    params = dict(params)
    mode = params.setdefault('mode', 'incremental')
    if spec.get('manual_only'):
        if scheduled:raise ValueError('本地资料导入不支持定时运行')
        if not isinstance(params.get('file'),str) or not params['file'].strip() or len(params['file'])>1000:
            raise ValueError('请指定本地导入文件')
    if mode not in spec['modes'] or scheduled and mode != 'incremental':
        raise ValueError('模式无效；自动检查仅允许增量')
    if 'action' in spec:
        from .industry_updates import validate_request
        validate_request(spec['action'], params.get('target'), mode == 'review')
    if key == 'shipping_monthly':
        for boundary in ('start','end'):
            if boundary in params and (not isinstance(params[boundary],str) or index_collection.day(params[boundary]) != params[boundary]):
                raise ValueError('范围须为YYYY-MM-DD')
        if params.get('start','') > params.get('end','9999'):
            raise ValueError('日期范围倒置')
        if scheduled and set(params) & {'start','end'}:
            raise ValueError('自动检查不能配置历史范围')
    if key == 'indices':
        providers = params.setdefault('providers', spec['providers'])
        if not isinstance(providers, list) or not providers or any(p not in spec['providers'] for p in providers):
            raise ValueError('指数来源无效')
        for boundary in ('start', 'end'):
            if boundary in params:
                value = params[boundary]
                if not isinstance(value, str) or index_collection.day(value) != value:
                    raise ValueError('范围须为 YYYY-MM-DD')
        if params.get('start', '') > params.get('end', '9999'):
            raise ValueError('日期范围倒置')
        if scheduled and set(params) & {'start', 'end'}:
            raise ValueError('自动检查不能配置历史范围')
    return params


def validate_rule(rule):
    if not isinstance(rule, dict) or set(rule) - {'frequency', 'weekday', 'day', 'window'}:
        raise ValueError('检查规则无效')
    rule = dict(rule)
    frequency = rule.get('frequency', 'daily')
    rule['frequency'] = frequency
    if frequency not in ('daily', 'weekly', 'monthly'):
        raise ValueError('仅支持日、周、月规则')
    if frequency != 'weekly' and 'weekday' in rule or frequency != 'monthly' and ('day' in rule or 'window' in rule):
        raise ValueError('规则字段与频率不匹配')
    if frequency == 'weekly':
        value = rule.setdefault('weekday', 0)
        if type(value) is not int or not 0 <= value <= 6:
            raise ValueError('星期须为0—6（周一—周日）')
    if frequency == 'monthly':
        value = rule.setdefault('day', 1)
        if type(value) is not int or not 1 <= value <= 31:
            raise ValueError('月检查日须为1—31；短月按月末')
        if 'window' in rule:
            window = rule['window']
            if not isinstance(window, list) or len(window) != 2 or any(type(x) is not int for x in window) or not 1 <= window[0] <= window[1] <= 31:
                raise ValueError('发布窗口须为起止日[1—31,1—31]')
    return rule


def due_slot(schedule, now):
    """Latest due slot only: outages coalesce, never replay an unbounded backlog."""
    now = now.astimezone(ZONE)
    day = latest_daily_date(now, schedule['local_time'])
    rule = schedule['rule']
    frequency = rule['frequency']
    if frequency == 'weekly':
        day -= timedelta(days=(day.weekday() - rule['weekday']) % 7)
    elif frequency == 'monthly':
        if 'window' in rule:
            first, last = rule['window']
            if day.day < min(first, monthrange(day.year, day.month)[1]):
                day = day.replace(day=1) - timedelta(days=1)
            day = day.replace(day=min(day.day, last))
        else:
            target = min(rule['day'], monthrange(day.year, day.month)[1])
            if day.day < target:
                day = day.replace(day=1) - timedelta(days=1)
            day = day.replace(day=min(rule['day'], monthrange(day.year, day.month)[1]))
    return day.isoformat()


def next_check(schedule, now):
    if not schedule['enabled']:
        return None
    # Bounded local calendar search, including month-end and publication windows.
    for offset in range(370):
        day = now.astimezone(ZONE).date() + timedelta(days=offset)
        candidate = datetime.fromisoformat(day.isoformat() + 'T' + schedule['local_time']).replace(tzinfo=ZONE)
        if candidate > now and due_slot(schedule, candidate) == day.isoformat():
            return candidate.isoformat()
    return None


class CollectionManagement:
    def __init__(self, db, industry):
        self.db, self.industry = db, industry

    def schedules(self, conn):
        result = {}
        for key in TASKS:
            row = conn.execute('SELECT s.*,r.rule_json,r.params_json FROM collection_schedules s '
                               'LEFT JOIN collection_schedule_rules r USING(task_key) WHERE s.task_key=?', (key,)).fetchone()
            result[key] = dict(enabled=bool(row['enabled']) if row else False,
                               local_time=row['local_time'] if row else '18:00', timezone='Asia/Shanghai',
                               updated_at=row['updated_at'] if row else None,
                               rule=json.loads(row['rule_json']) if row and row['rule_json'] else {'frequency': 'daily'},
                               params=json.loads(row['params_json']) if row and row['params_json'] else {'mode': 'incremental'})
        return result

    def configure(self, key, command):
        task(key)
        if not isinstance(command, dict) or set(command) - {'enabled', 'local_time', 'rule', 'params'}:
            raise ValueError('定时配置参数无效')
        with self.db.connection(write=True) as conn:
            current = self.schedules(conn)[key]
            current.update(command)
            if TASKS[key].get('manual_only') and current['enabled']:
                raise ValueError('资料导入仅手动，不允许开启定时')
            if type(current['enabled']) is not bool or not isinstance(current['local_time'], str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', current['local_time']):
                raise ValueError('请指定布尔开关和北京时间HH:MM')
            rule = validate_rule(current['rule'])
            if 'window' in rule and 'action' not in TASKS[key]:
                raise ValueError('发布窗口仅适用于指定报告期／季度目标')
            # Turning off an old target must remain possible after its date ages out.
            params = validate_params(key, current['params'], scheduled=True) if current['enabled'] or 'params' in command else current['params']
            if key == 'market_congestion' and current['enabled'] and not market_congestion.initialized(conn):
                raise ValueError('请先完成拥挤度历史初始化')
            stamp = utc_now()
            conn.execute('INSERT INTO collection_schedules VALUES(?,?,?,?,?) ON CONFLICT(task_key) DO UPDATE SET '
                         'enabled=excluded.enabled,local_time=excluded.local_time,updated_at=excluded.updated_at',
                         (key, int(current['enabled']), 'Asia/Shanghai', current['local_time'], stamp))
            conn.execute('INSERT INTO collection_schedule_rules VALUES(?,?,?,?) ON CONFLICT(task_key) DO UPDATE SET '
                         'rule_json=excluded.rule_json,params_json=excluded.params_json,updated_at=excluded.updated_at',
                         (key, encoded(rule), encoded(params), stamp))
        return dict(enabled=current['enabled'], rule=rule, params=params, local_time=current['local_time'])

    def reserve(self, conn, key, params):
        spec = task(key)
        if key.startswith('shipping_'):
            return shipping_collection.reserve(conn,key,params)
        if 'action' in spec:
            run = self.industry.reserve_refresh(conn, spec['action'], params['target'], params['mode'] == 'review')
            return 'sw_update_runs:' + str(run)
        if key == 'market_congestion':
            operation = market_congestion.start(self.db, params['mode'], connection=conn)
        else:
            operation = index_collection.start_operation(self.db, params['mode'],
                params.get('providers', [] if key == 'sovereign_yields' else TASKS[key]['providers']),
                params.get('start'), params.get('end'),
                datasets=['bond_zh_us_rate'] if key == 'sovereign_yields' else None, connection=conn)
        return operation

    def execute(self, key, params, ref):
        try:
            if key.startswith('shipping_'):
                shipping_collection.run(self.db,ref)
            elif 'action' in task(key):
                self.industry._refresh(TASKS[key]['action'], params['target'], params['mode'] == 'review', int(ref.split(':')[1]))
            else:
                index_collection.run_operation(self.db, ref)
        except Exception as exc:
            with self.db.connection(write=True) as conn:
                if ref.startswith('sw_update_runs:'):
                    conn.execute("UPDATE sw_update_runs SET status='failed',finished_at=?,error=? WHERE id=? AND status='running'",
                                 (utc_now(), str(exc), int(ref.split(':')[1])))
                else:
                    conn.execute("UPDATE index_collection_operations SET status='failed',finished_at=?,error=? WHERE operation_id=? AND status='running'",
                                 (utc_now(), str(exc), ref))
                conn.execute("UPDATE collection_batches SET status='failed',finished_at=?,error=? WHERE operation_id=? AND status='running'",
                             (utc_now(), str(exc), ref))
            raise

    def start(self, key, params):
        params = validate_params(key, params)
        with self.db.connection(write=True) as conn:
            ref = self.reserve(conn, key, params)
        Thread(target=self.execute, args=(key, params, ref), daemon=True, name='collection-' + key).start()
        return dict(operation_ref=ref, status='running')

    def cancel(self, key):
        task(key)
        if 'action' in TASKS[key]:
            raise ValueError('此任务暂不提供用户停止；已提交分段保留，可在失败后按原范围重试')
        predicate, args = self._predicate(key)
        with self.db.connection(write=True) as conn:
            conn.execute('UPDATE index_collection_operations SET cancel_requested=1 WHERE status=\'running\' AND ' + predicate, args)
        return dict(cancel_requested=True)

    @staticmethod
    def _predicate(key):
        if key.startswith('shipping_'):
            return "json_extract(scope_json,'$.dataset')=?", (key,)
        if key == 'market_congestion':
            return "json_extract(scope_json,'$.dataset')=?", (key,)
        if key == 'sovereign_yields':
            return "json_extract(scope_json,'$.dataset') IS NULL AND json_extract(scope_json,'$.datasets[0]')=? AND coalesce(json_array_length(json_extract(scope_json,'$.providers')),0)=0", ('bond_zh_us_rate',)
        return "json_extract(scope_json,'$.dataset') IS NULL AND (coalesce(json_array_length(json_extract(scope_json,'$.datasets')),0)=0 OR coalesce(json_array_length(json_extract(scope_json,'$.providers')),0)>0)", ()

    def configure_rate(self, key, provider, settings):
        from .collection_rate_limits import save
        with self.db.connection(write=True) as conn:
            return save(conn, key, provider, settings)

    def read(self):
        from .collection_rate_limits import describe
        now = datetime.now(ZONE)
        items = []
        with self.db.connection() as conn:
            schedules = self.schedules(conn)
            for key, spec in TASKS.items():
                if 'action' in spec:
                    rows = [dict(r) for r in conn.execute('SELECT id,action,target,recheck,started_at,finished_at,status,error,' + RESULT_SUMMARY + ' AS result_json FROM sw_update_runs WHERE action=? ORDER BY id DESC LIMIT 20', (spec['action'],))]
                    for row in rows:
                        row['operation_ref'] = 'sw_update_runs:' + str(row['id'])
                else:
                    predicate, args = self._predicate(key)
                    rows = [dict(r) for r in conn.execute('SELECT operation_id,mode,scope_json,status,started_at,finished_at,cancel_requested,error,' + RESULT_SUMMARY + ' AS result_json FROM index_collection_operations WHERE ' + predicate + ' ORDER BY started_at DESC LIMIT 20', args)]
                    for row in rows:
                        row['operation_ref'] = row['operation_id']
                for row in rows:
                    row['result'] = json.loads(row.pop('result_json') or '{}')
                    if 'scope_json' in row:
                        row['scope'] = json.loads(row.pop('scope_json'))
                latest = rows[0] if rows else None
                batches = [dict(r) for r in conn.execute('SELECT id,dataset,source_system,status,coverage_status,scope_start,scope_end,started_at,finished_at,error '
                    'FROM collection_batches WHERE operation_id=? ORDER BY id DESC LIMIT 100', (latest['operation_ref'],))] if latest else []
                source = conn.execute('SELECT s.obtained_at FROM collection_sources s WHERE s.batch_id IN '
                    '(SELECT id FROM collection_batches WHERE operation_id=?) ORDER BY s.obtained_at DESC LIMIT 1', (latest['operation_ref'],)).fetchone() if latest else None
                cooling = [dict(r) for r in conn.execute('SELECT source_system,cooldown_until,reason FROM collection_provider_state WHERE source_system IN (' + ','.join('?' for _ in spec['providers']) + ')', spec['providers'])]
                slots = [dict(r) for r in conn.execute('SELECT * FROM collection_schedule_slots WHERE task_key=? ORDER BY slot_key DESC LIMIT 10', (key,))]
                schedule = schedules[key]
                schedule['next_check'] = next_check(schedule, now)
                items.append(dict(task_key=key, **spec, supports_cancel='action' not in spec,
                    rate_limits=[] if spec.get('manual_only') else describe(conn,key,spec['providers']),
                    schedule=schedule, running=any(r['status'] == 'running' for r in rows),
                    last_check=source[0] if source else None,
                    last_success=next((r['finished_at'] for r in rows if r['status'] == 'complete'), None),
                    history=rows, batches=batches, cooldowns=cooling, slots=slots,
                    coverage=('complete' if all(b['coverage_status']=='complete' for b in batches) else 'empty' if all(b['coverage_status']=='empty' for b in batches) else 'partial' if any(b['coverage_status']=='partial' for b in batches) else 'unknown') if batches else 'unknown',
                    coverage_note='仅最近运行的分段诊断；全历史及逐字段覆盖未知'))
        return dict(tasks=items, generated_at=utc_now())

    def detail(self, key, ref):
        spec = task(key)
        if not isinstance(ref, str) or len(ref) > 100:
            raise ValueError('运行引用无效')
        with self.db.connection() as conn:
            if 'action' in spec:
                if not ref.startswith('sw_update_runs:') or not ref.split(':')[1].isdigit():
                    raise ValueError('行业运行引用无效')
                row = conn.execute('SELECT * FROM sw_update_runs WHERE id=? AND action=?', (int(ref.split(':')[1]), spec['action'])).fetchone()
            else:
                predicate, args = self._predicate(key)
                row = conn.execute('SELECT * FROM index_collection_operations WHERE operation_id=? AND ' + predicate, (ref, *args)).fetchone()
        if row is None:
            raise ValueError('任务运行不存在')
        row = dict(row)
        row['result'] = json.loads(row.pop('result_json'))
        if 'scope_json' in row:
            row['scope'] = json.loads(row.pop('scope_json'))
        return row

    def recover(self):
        # Called only under the server instance lock after domain recovery.
        with self.db.connection(write=True) as conn:
            conn.execute("UPDATE collection_schedule_slots SET status=CASE WHEN attempts<2 THEN 'pending' ELSE 'failed' END,"
                         "updated_at=?,error='后台中断；已提交分段保留，最多自动恢复一次' WHERE status='running'", (utc_now(),))

    @staticmethod
    def target_complete(conn, key, params):
        """Only explicit, validated complete coverage stops a publication window."""
        spec = TASKS[key]
        if 'action' not in spec:
            return False
        run = conn.execute("SELECT id,result_json FROM sw_update_runs WHERE action=? AND target=? AND status='complete' ORDER BY id DESC LIMIT 1",
                           (spec['action'], params['target'])).fetchone()
        if not run or json.loads(run['result_json'] or '{}').get('failures'):
            return False
        rows = conn.execute('SELECT dataset,status,coverage_status FROM collection_batches WHERE operation_id=?',
                            ('sw_update_runs:' + str(run['id']),)).fetchall()
        prefix = 'market.financial.' if key == 'market_financial' else 'market.trade_snapshot'
        rows = [r for r in rows if r['dataset'].startswith(prefix)]
        required = 4 if key == 'market_financial' else 1
        return len(rows) >= required and all(r['status'] == 'complete' and r['coverage_status'] == 'complete' for r in rows)

    def tick(self, *, now=None, stopped=lambda:False):
        now = now or datetime.now(ZONE)
        for key in TASKS:
            if stopped():
                return
            # Congestion daily checks retain initialization, manual reuse,
            # metadata_only and cooldown semantics. There is only one scheduler.
            with self.db.connection() as conn:
                schedule = self.schedules(conn)[key]
            if not schedule['enabled']:
                continue
            if key == 'market_congestion' and schedule['rule'] == {'frequency': 'daily'}:
                market_congestion.scheduled_tick(self.db, now=now)
                continue
            params = validate_params(key, schedule['params'], scheduled=True)
            slot = due_slot(schedule, now)
            scheduled_at = datetime.fromisoformat(slot + 'T' + schedule['local_time']).replace(tzinfo=ZONE)
            if schedule['updated_at'] and scheduled_at < datetime.fromisoformat(schedule['updated_at']):
                continue  # Saving/enabling starts with the next future slot, never an old backlog.
            with self.db.connection(write=True) as conn:
                # Recheck under the same transaction as domain reservation.
                current = self.schedules(conn)[key]
                if stopped() or not current['enabled'] or current != schedule:
                    continue
                if 'window' in schedule['rule'] and self.target_complete(conn, key, params):
                    continue
                old = conn.execute('SELECT * FROM collection_schedule_slots WHERE task_key=? AND slot_key=?', (key, slot)).fetchone()
                if old and old['status'] != 'pending':
                    continue
                # Do not start during any relevant source cooldown. No slot is consumed.
                states = conn.execute('SELECT cooldown_until FROM collection_provider_state WHERE source_system IN (' + ','.join('?' for _ in TASKS[key]['providers']) + ')', TASKS[key]['providers']).fetchall()
                if any(r[0] and datetime.fromisoformat(r[0]) > now for r in states):
                    continue
                try:
                    ref = self.reserve(conn, key, params)
                except ValueError:
                    continue  # Busy or uninitialized; retry when admissible.
                if 'action' not in TASKS[key]:
                    conn.execute("UPDATE index_collection_operations SET scope_json=json_set(scope_json,'$.trigger','scheduled','$.schedule_task',?,'$.schedule_slot',?) WHERE operation_id=?",
                                 (key, slot, ref))
                stamp = utc_now()
                conn.execute("UPDATE collection_schedule_slots SET status='failed',updated_at=?,error='漏跑已合并到最新槽位' "
                             "WHERE task_key=? AND status='pending' AND slot_key<>?", (stamp, key, slot))
                conn.execute("INSERT INTO collection_schedule_slots VALUES(?,?,'running',?,1,?,NULL,?,NULL) ON CONFLICT(task_key,slot_key) DO UPDATE SET "
                             "status='running',operation_ref=excluded.operation_ref,attempts=attempts+1,started_at=excluded.started_at,finished_at=NULL,updated_at=excluded.updated_at,error=NULL",
                             (key, slot, ref, stamp, stamp))
            error = None
            try:
                self.execute(key, params, ref)
            except Exception as exc:
                error = str(exc)
            with self.db.connection(write=True) as conn:
                if 'action' in TASKS[key]:
                    result = conn.execute('SELECT status,error FROM sw_update_runs WHERE id=?', (int(ref.split(':')[1]),)).fetchone()
                else:
                    result = conn.execute('SELECT status,error FROM index_collection_operations WHERE operation_id=?', (ref,)).fetchone()
                status = 'complete' if result and result['status'] == 'complete' and error is None else 'failed'
                if status == 'failed' and stopped():
                    attempts = conn.execute('SELECT attempts FROM collection_schedule_slots WHERE task_key=? AND slot_key=?', (key, slot)).fetchone()[0]
                    if attempts < 2:
                        status = 'pending'
                conn.execute('UPDATE collection_schedule_slots SET status=?,finished_at=?,updated_at=?,error=? WHERE task_key=? AND slot_key=?',
                             (status, utc_now(), utc_now(), error or (result['error'] if result else '领域任务无结果'), key, slot))


class Scheduler:
    def __init__(self, management, *, gate=None):
        self.management, self.gate = management, gate
        self.stop_event = Event()
        self.thread = Thread(target=self._loop, daemon=True, name='collection-scheduler')

    def start(self):
        self.management.recover()
        self.thread.start()

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                admitted = self.gate is None or self.gate.enter()
                if admitted:
                    try:
                        if not self.stop_event.is_set():
                            self.management.tick(stopped=self.stop_event.is_set)
                    finally:
                        if self.gate:
                            self.gate.leave()
            except (ValueError, OSError, sqlite3.DatabaseError) as exc:
                print('统一采集检查：' + str(exc), flush=True)
            self.stop_event.wait(30)

    def close(self):
        self.stop_event.set()
        with self.management.db.connection(write=True) as conn:
            conn.execute("UPDATE index_collection_operations SET cancel_requested=1 WHERE status='running' AND (operation_id IN "
                         "(SELECT operation_ref FROM collection_schedule_slots WHERE status='running') OR json_extract(scope_json,'$.trigger')='scheduled')")
            conn.execute("UPDATE sw_update_runs SET cancel_requested=1 WHERE status='running' AND 'sw_update_runs:'||id IN "
                         "(SELECT operation_ref FROM collection_schedule_slots WHERE status='running')")
        self.thread.join(timeout=90)
        if self.thread.is_alive():
            raise RuntimeError('统一采集尚未停止，不能关闭数据库')
