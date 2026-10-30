"""Local archive operations, reusing the application's scheduler and schedule store."""
from datetime import datetime, timedelta
import json
import re
from threading import Lock, Thread
from time import monotonic
from uuid import uuid4

from stock_analysis.collection import schedules
from stock_analysis.collection.collection_management import due_slot
from stock_analysis.collection.source_archive import ZONE, build, preview, restore_source
from stock_analysis.collection.collection import atomic_write
from stock_analysis.infrastructure.storage import utc_now

KEY = 'source_archive'


def last_month(now=None):
    now = now or datetime.now(ZONE)
    return (now.astimezone(ZONE).date().replace(day=1) - timedelta(days=1)).strftime('%Y-%m')


class SourceArchiveService:
    def __init__(self, db, gate):
        self.db, self.gate = db, gate
        self.lock = Lock()
        self.worker = None
        self.prepared = None
        self.progress = None

    def _ensure_rule(self, conn):
        rules = schedules.read(conn, KEY)
        if not rules:
            schedules.save(conn, KEY, dict(enabled=False, local_time='02:00',
                rule={'frequency': 'monthly', 'day': 1}, params={}))
            rules = schedules.read(conn, KEY)
        return rules[0]

    def state(self):
        with self.db.connection() as conn:
            rules = schedules.read(conn, KEY)
            rows = [dict(r) for r in conn.execute(
                'SELECT slot_key,status,operation_ref,started_at,finished_at,error FROM collection_schedule_slots '
                'WHERE schedule_id=? ORDER BY started_at DESC LIMIT 20', (KEY,))]
            packages = [dict(r) for r in conn.execute(
                'SELECT archive_path,MAX(archived_at) archived_at,COUNT(*) files FROM collection_source_archive_members '
                'GROUP BY archive_path ORDER BY archived_at DESC LIMIT 100')]
        return dict(schedule=rules[0] if rules else dict(enabled=False,local_time='02:00',
                    rule={'frequency':'monthly','day':1}), runs=rows, packages=packages,
                    default_cutoff=last_month(), progress=self.progress)

    def configure(self, command):
        if (not isinstance(command,dict) or set(command)!={'enabled','day','local_time'}
            or type(command['enabled']) is not bool or type(command['day']) is not int
            or not 1 <= command['day'] <= 31 or not isinstance(command['local_time'],str)
            or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', command['local_time'])):
            raise ValueError('计划需要开关、每月日期 1—31 和北京时间 HH:MM')
        with self.lock, self.db.connection(write=True) as conn:
            self._ensure_rule(conn)
            schedules.save(conn, KEY, dict(enabled=command['enabled'], local_time=command['local_time'],
                rule={'frequency':'monthly','day':command['day']}, params={}))
        return self.state()

    def preview(self, cutoff):
        result = preview(self.db, cutoff)
        with self.lock:
            token = uuid4().hex
            self.prepared = (token, cutoff, monotonic())
        return {**result, 'token': token}

    def _launch(self, cutoff, slot, *, scheduled=False, expected_rule=None):
        preview(self.db, cutoff)  # Validate before reserving a slot.
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise ValueError('响应归档正在运行')
            if self.gate is not None and not self.gate.enter():
                raise ValueError('后台维护正在执行')
            launched = False
            try:
                with self.db.connection(write=True) as conn:
                    self._ensure_rule(conn)
                    if scheduled and schedules.read(conn, KEY) != [expected_rule]:
                        return False
                    if conn.execute("SELECT 1 FROM collection_schedule_slots WHERE schedule_id=? AND status='running'",(KEY,)).fetchone():
                        raise ValueError('响应归档正在运行')
                    stamp = utc_now()
                    ref = uuid4().hex
                    if scheduled:
                        existing = conn.execute('SELECT status FROM collection_schedule_slots WHERE schedule_id=? AND slot_key=?',(KEY,slot)).fetchone()
                        if existing and existing['status'] != 'pending':
                            return False
                        conn.execute("UPDATE collection_schedule_slots SET status='failed',finished_at=?,updated_at=?,error='漏跑已合并到最新槽位' "
                                     "WHERE schedule_id=? AND status='pending' AND slot_key<>?", (stamp,stamp,KEY,slot))
                    conn.execute("INSERT INTO collection_schedule_slots(schedule_id,slot_key,status,operation_ref,attempts,started_at,finished_at,updated_at,error) "
                        "VALUES(?,?,'running',?,1,?,NULL,?,NULL) ON CONFLICT(schedule_id,slot_key) DO UPDATE SET status='running',"
                        "operation_ref=excluded.operation_ref,attempts=attempts+1,started_at=excluded.started_at,finished_at=NULL,updated_at=excluded.updated_at,error=NULL",
                        (KEY, slot, ref, stamp, stamp))
                self.worker = Thread(target=self._run, args=(cutoff, slot, ref), name='source-archive', daemon=True)
                self.progress = dict(phase='准备',done=0,total=0)
                try:
                    self.worker.start()
                except Exception as exc:
                    self.progress = None
                    with self.db.connection(write=True) as conn:
                        conn.execute("UPDATE collection_schedule_slots SET status='failed',finished_at=?,updated_at=?,error=? "
                                     "WHERE schedule_id=? AND slot_key=? AND operation_ref=?",
                                     (utc_now(),utc_now(),str(exc),KEY,slot,ref))
                    raise
                launched = True
                return True
            finally:
                if not launched and self.gate is not None:self.gate.leave()

    def start(self, cutoff, token):
        with self.lock:
            if not self.prepared or self.prepared[:2] != (token,cutoff) or monotonic()-self.prepared[2] > 900:
                raise ValueError('归档预览已过期，请重新预览')
            self.prepared = None
        self._launch(cutoff, 'manual:' + uuid4().hex)
        return {'status':'running'}

    def restore(self, source_id):
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise ValueError('响应归档正在运行')
            return restore_source(self.db, source_id)

    def _run(self, cutoff, slot, ref):
        try:
            result = build(self.db, cutoff, on_progress=lambda value:setattr(self,'progress',value))
            status = 'failed' if result['errors'] else 'complete'
            report = self.db.path.parent / 'verification' / 'reports' / 'source_archive' / (ref + '.json')
            atomic_write(report, json.dumps(result, ensure_ascii=False, indent=2).encode('utf-8'))
            detail = json.dumps({'cutoff':cutoff,'archives':len(result['archives']),
                'removed':result['removed'],'errors':len(result['errors']),
                'report':report.relative_to(self.db.path.parent).as_posix()}, ensure_ascii=False)
        except Exception as exc:
            status, detail = 'failed', str(exc)
        try:
            with self.db.connection(write=True) as conn:
                conn.execute('UPDATE collection_schedule_slots SET status=?,finished_at=?,updated_at=?,error=? '
                    'WHERE schedule_id=? AND slot_key=? AND operation_ref=?',
                    (status,utc_now(),utc_now(),detail,KEY,slot,ref))
        finally:
            self.progress = None
            if self.gate is not None:self.gate.leave()

    def recover(self):
        with self.db.connection(write=True) as conn:
            conn.execute("UPDATE collection_schedule_slots SET status=CASE WHEN slot_key NOT LIKE 'manual:%' AND attempts<2 THEN 'pending' ELSE 'failed' END,finished_at=?,updated_at=?,error=? "
                "WHERE schedule_id=? AND status IN ('running','pending')",
                (utc_now(),utc_now(),'后台中断；归档证据已保留，请重新执行以继续核验',KEY))

    def tick(self, now=None):
        now = now or datetime.now(ZONE)
        with self.db.connection() as conn:
            rules = schedules.read(conn, KEY)
        if not rules or not rules[0]['enabled']:
            return
        rule = rules[0]
        slot = due_slot(rule, now)
        scheduled = datetime.fromisoformat(slot+'T'+rule['local_time']).replace(tzinfo=ZONE)
        if scheduled < datetime.fromisoformat(rule['updated_at']):
            return
        with self.lock:
            if self.worker and self.worker.is_alive():
                return
        with self.db.connection() as conn:
            old = conn.execute('SELECT status FROM collection_schedule_slots WHERE schedule_id=? AND slot_key=?',(KEY,slot)).fetchone()
        if old and old['status'] != 'pending':
            return
        self._launch(last_month(now), slot, scheduled=True, expected_rule=rule)

    def close(self):
        if self.worker:
            self.worker.join(timeout=90)
            if self.worker.is_alive():
                raise RuntimeError('响应归档尚未停止，不能关闭数据库')
