"""Public request provenance and conservative, cross-process provider pacing.

Index and market trading-day snapshot collectors use this module. Network waits never hold a SQLite
transaction. The provider file lock covers all hosts, endpoints and retries.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import gzip
import hashlib
import json
import os
from pathlib import Path
import random
import time
from threading import Lock
from uuid import uuid4

import requests

from .network import create_data_session
from .storage import StorageError, instance_lock, utc_now

_PROVIDER_LOCKS={}
_PROVIDER_LOCKS_GUARD=Lock()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        with temporary.open('wb') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Cancelled(RuntimeError):
    pass


class ProviderBlocked(RuntimeError):
    pass


RECOVERY_PENDING = object()


@dataclass(frozen=True)
class PacingPolicy:
    interval: float = 10
    jitter: float = 5
    window: float = 60
    window_requests: int = 5
    rest_every: int = 20
    rest_min: float = 60
    rest_max: float = 120
    cooldown: float = 3600
    max_attempts: int = 3
    provider: str | None = None
    max_concurrency: int = 1

    def __post_init__(self):
        # The sole faster policy is an explicit, fixed cninfo authorization.
        if self.provider == 'cninfo':
            if ((self.interval, self.jitter, self.window, self.window_requests, self.rest_every,
                    self.rest_min, self.rest_max, self.cooldown) != (2, 0, 60, 30, 20, 0, 0, 3600)
                    or self.max_concurrency!=1 or not 1 <= self.max_attempts <= 3):
                raise ValueError('cninfo限速配置必须符合已授权策略')
            return
        if self.provider == 'index_parallel':
            if ((self.interval,self.jitter,self.window,self.window_requests,self.rest_every,
                 self.rest_min,self.rest_max,self.cooldown)!=(2,3,60,30,20,10,10,3600)
                    or self.max_concurrency not in (1,2) or not 1<=self.max_attempts<=3):
                raise ValueError('指数并行限速配置必须符合本轮授权策略')
            return
        if self.provider in ('industry_financial', 'industry_snapshot'):
            concurrency = 2 if self.provider == 'industry_financial' else 1
            if ((self.interval,self.jitter,self.window,self.window_requests,self.rest_every,
                 self.rest_min,self.rest_max,self.cooldown)!=(2,3,60,30,20,15,15,3600)
                    or self.max_concurrency!=concurrency or not 1<=self.max_attempts<=3):
                raise ValueError('行业采集限速配置必须符合本轮授权策略')
            return
        if (self.provider is not None or self.interval < 10 or self.jitter < 0 or self.window < 60
                or not 1 <= self.window_requests <= 5 or not 1 <= self.rest_every <= 20
                or self.rest_min < 60 or self.rest_max < self.rest_min or self.cooldown < 3600
                or self.max_concurrency!=1 or not 1 <= self.max_attempts <= 3):
            raise ValueError('指数采集限速配置不能低于保守默认要求')


def begin_batch(db, operation_id, dataset, provider, start=None, end=None, *, scope=None,
                frequency=None, trigger='manual', idempotency_key=None, domain='market',
                parser_version='index-v2', definition_version='v1', universe_version=None):
    with db.connection(write=True) as conn:
        return conn.execute('INSERT INTO collection_batches(operation_id,domain,dataset,source_system,scope_start,scope_end,'
            'scope_json,frequency,trigger_type,status,started_at,parser_version,definition_version,idempotency_key,universe_version) '
            "VALUES(?,?,?,?,?,?,?,?,?,'running',?,?,?,?,?)",
            (operation_id,domain,dataset,provider,start,end,encoded(scope or {}),frequency,trigger,utc_now(),
             parser_version,definition_version,idempotency_key,universe_version)).lastrowid


def finish_batch(db, batch_id, status, coverage='unknown', *, result=None, error=None):
    with db.connection(write=True) as conn:
        conn.execute('UPDATE collection_batches SET status=?,coverage_status=?,finished_at=?,result_json=?,error=? WHERE id=?',
                     (status,coverage,utc_now(),encoded(result or {}),str(error) if error else None,batch_id))


def archive_source(db, batch_id, provider, dataset, request, attempt, *, raw=None, response=None,
                   request_status='success', obtained_at=None, error=None, reused_source_id=None):
    """Archive the actual decompressed HTTP entity, then register the request attempt."""
    metadata = {}
    if raw is not None:
        stored = gzip.compress(raw, mtime=0)
        relative = Path('collection_sources')/provider/dataset/(uuid4().hex+'.raw.gz')
        path = db.path.parent/relative
        atomic_write(path, stored)
        if digest(path.read_bytes()) != digest(stored):
            raise OSError('来源落盘哈希校验失败')
        metadata = dict(file_path=relative.as_posix(),compression='gzip',raw_sha256=digest(raw),
                        stored_sha256=digest(stored),hash_basis='http_decoded_entity / gzip_file',
                        raw_bytes=len(raw),stored_bytes=len(stored))
    request_key = digest(encoded(request).encode())
    metadata.update(batch_id=batch_id,source_system=provider,dataset=dataset,request_key=request_key,
                    attempt_no=attempt,request_json=encoded(request),request_status=request_status,
                    parse_status='pending' if raw is not None else 'not_applicable',
                    http_status=response.status_code if response is not None else None,
                    content_type=response.headers.get('Content-Type') if response is not None else None,
                    error=error,obtained_at=obtained_at or utc_now(),reused_source_id=reused_source_id)
    columns=tuple(metadata)
    with db.connection(write=True) as conn:
        return conn.execute('INSERT INTO collection_sources('+','.join(columns)+') VALUES ('+
                            ','.join('?' for _ in columns)+')',tuple(metadata.values())).lastrowid


def parsed_source(db, source_id, *, fields=None, error=None):
    with db.connection(write=True) as conn:
        conn.execute('UPDATE collection_sources SET parse_status=?,fields_json=?,error=coalesce(?,error) WHERE id=?',
                     ('failed' if error else 'complete',encoded(sorted(fields)) if fields is not None else None,
                      str(error) if error else None,source_id))


def read_source(db, source_id):
    with db.connection() as conn:
        item=dict(conn.execute('SELECT * FROM collection_sources WHERE id=?',(source_id,)).fetchone())
    path=(db.path.parent/item['file_path']).resolve()
    root=(db.path.parent/'collection_sources').resolve()
    if not path.is_relative_to(root):
        raise ValueError('来源文件不在登记的公共来源目录内')
    stored=path.read_bytes()
    if digest(stored)!=item['stored_sha256']:
        raise ValueError('来源存储哈希不符')
    raw=gzip.decompress(stored)
    if digest(raw)!=item['raw_sha256']:
        raise ValueError('来源原文哈希不符')
    return raw,item


def reusable_source(db, batch_id, source_id):
    """Register a same-day verified archive in a new batch without copying its file."""
    fields=('source_system','dataset','request_key','request_json','http_status','file_path','content_type',
            'compression','raw_sha256','stored_sha256','hash_basis','raw_bytes','stored_bytes','fields_json','obtained_at')
    with db.connection() as conn:
        original=conn.execute('SELECT * FROM collection_sources WHERE id=?',(source_id,)).fetchone()
        target=conn.execute('SELECT source_system,dataset FROM collection_batches WHERE id=?',(batch_id,)).fetchone()
    if not original or original['request_status'] not in ('success','reused') or original['parse_status']!='complete':
        raise ValueError('来源未通过请求及解析校验，不能复用')
    if not target or (target['source_system'],target['dataset'])!=(original['source_system'],original['dataset']):
        raise ValueError('复用来源与目标批次提供方或数据集不一致')
    if not original['obtained_at'] or not original['file_path']:
        raise ValueError('来源缺少原始取得时间或归档文件')
    values={field:original[field] for field in fields}
    values.update(batch_id=batch_id,request_status='reused',parse_status='complete',reused_source_id=source_id)
    with db.connection(write=True) as conn:
        values['attempt_no']=conn.execute('SELECT coalesce(max(attempt_no),0)+1 FROM collection_sources '
            'WHERE batch_id=? AND dataset=? AND request_key=?',
            (batch_id,original['dataset'],original['request_key'])).fetchone()[0]
        return conn.execute('INSERT INTO collection_sources('+','.join(values)+') VALUES('+','.join('?' for _ in values)+')',
                             tuple(values.values())).lastrowid


def retry_after(value, now):
    try:
        return now+max(0,float(value))
    except (ValueError,TypeError):
        try:
            stamp=parsedate_to_datetime(value)
            if stamp.tzinfo is None:
                stamp=stamp.replace(tzinfo=timezone.utc)
            return stamp.timestamp()
        except (ValueError,TypeError,OverflowError):
            return now


class RequestCollector:
    def __init__(self, db, *, session=None, session_factory=None, policy=None, cancelled=lambda: False,
                 clock=time.time, sleep=time.sleep, uniform=random.uniform, progress=lambda message: None):
        self.db=db
        self._owns_session=session is None
        self._has_session_factory=session_factory is not None
        self._session_factory=session_factory or create_data_session
        self.session=session if session is not None else self._session_factory()
        self.policy=policy or PacingPolicy()
        if self.policy.provider is not None:
            raise ValueError('提供方专属策略由RequestCollector按来源选择，不能作为全局policy')
        self.cancelled=cancelled
        self.clock=clock
        self.sleep=sleep
        self.uniform=uniform
        self.progress=progress
        self.halted={}
        self.requests=0
        self.retries=0

    def policy_for(self, provider, *, index_parallel=False, industry_profile=None):
        if industry_profile is not None:
            if index_parallel or provider!='eastmoney' or industry_profile not in ('financial','snapshot'):
                raise ValueError('行业限速策略只能用于明确的东财行业任务')
            return PacingPolicy(interval=2,jitter=3,window=60,window_requests=30,
                rest_every=20,rest_min=15,rest_max=15,cooldown=3600,max_attempts=self.policy.max_attempts,
                provider='industry_financial' if industry_profile=='financial' else 'industry_snapshot',
                max_concurrency=2 if industry_profile=='financial' else 1)
        if industry_profile is None and index_parallel and provider=='cninfo':
            raise ValueError('指数并行策略不能与CNINFO策略混用')
        if provider == 'cninfo':
            return PacingPolicy(interval=2,jitter=0,window=60,window_requests=30,
                rest_every=20,rest_min=0,rest_max=0,cooldown=3600,max_attempts=self.policy.max_attempts,
                provider='cninfo')
        if index_parallel:
            return PacingPolicy(interval=2,jitter=3,window=60,window_requests=30,
                rest_every=20,rest_min=10,rest_max=10,cooldown=3600,max_attempts=self.policy.max_attempts,
                provider='index_parallel',max_concurrency=1 if provider=='legu' else 2)
        return self.policy

    def spawn(self):
        """Create a worker with its own HTTP session and the same shared pacing hooks."""
        if not self._owns_session and not self._has_session_factory:
            raise ValueError('并行采集需要可创建独立Session的session_factory')
        return RequestCollector(self.db,session_factory=self._session_factory,policy=self.policy,
            cancelled=self.cancelled,clock=self.clock,sleep=self.sleep,uniform=self.uniform,progress=self.progress)

    def check_cancelled(self):
        if self.cancelled():
            raise Cancelled('用户取消采集；已发布分段保留')

    def wait(self, seconds):
        deadline=self.clock()+max(0,seconds)
        while True:
            self.check_cancelled()
            remaining=deadline-self.clock()
            if remaining<=0:
                break
            self.sleep(min(1,remaining))
        self.check_cancelled()

    @contextmanager
    def provider_lock(self, provider, *, cancellable=True):
        # Database directory is the scope of the shared cross-process budget.
        path=self.db.path.parent/'collection_sources'/('provider-'+provider)
        with _PROVIDER_LOCKS_GUARD:
            thread_lock=_PROVIDER_LOCKS.setdefault(str(path),Lock())
        while not thread_lock.acquire(timeout=.1):
            if cancellable:self.check_cancelled()
        try:
            seed_failures=0
            while True:
                if cancellable:self.check_cancelled()
                context=instance_lock(path)
                try:
                    context.__enter__()
                    break
                except StorageError:
                    self.wait(.25) if cancellable else self.sleep(.25)
                except PermissionError:
                    # Windows can deny the competing initializer's first flush.
                    # Retry only a bounded number of times; real permissions errors fail.
                    seed_failures+=1
                    if seed_failures>=3:
                        raise
                    self.wait(.25) if cancellable else self.sleep(.25)
            try:
                yield
            finally:
                context.__exit__(None,None,None)
        finally:
            thread_lock.release()

    @contextmanager
    def provider_exclusive_lock(self, provider):
        """Lock both OS request slots for a legacy SDK exchange with concurrency one."""
        while True:
            held=[]
            base=self.db.path.parent/'collection_sources'/('provider-'+provider+'-slot-')
            for slot in range(2):
                context=instance_lock(str(base)+str(slot))
                try:context.__enter__()
                except StorageError:continue
                held.append(context)
                if len(held)==2:break
            if len(held)==2:break
            for context in reversed(held):context.__exit__(None,None,None)
            self.wait(.1)
        try:
            with self.provider_lock(provider):
                yield
        finally:
            for context in reversed(held):context.__exit__(None,None,None)

    def state(self, provider):
        with self.db.connection() as conn:
            row=conn.execute('SELECT * FROM collection_provider_state WHERE source_system=?',(provider,)).fetchone()
        return dict(row) if row else dict(source_system=provider,cooldown_until=None,reason=None,denials=0,budget_json='{}')

    def save_state(self, state):
        with self.db.connection(write=True) as conn:
            conn.execute('INSERT INTO collection_provider_state VALUES(?,?,?,?,?,?) ON CONFLICT(source_system) DO UPDATE SET '
                'cooldown_until=excluded.cooldown_until,reason=excluded.reason,denials=excluded.denials,'
                'budget_json=excluded.budget_json,updated_at=excluded.updated_at',
                (state['source_system'],state['cooldown_until'],state['reason'],state['denials'],state['budget_json'],utc_now()))

    def block(self, provider, state, reason, response=None):
        with self.provider_lock(provider):
            state=self.state(provider)
            self._block_locked(provider,state,reason,response)

    def _block_locked(self,provider,state,reason,response=None):
        now=self.clock()
        policy=self.policy_for(provider)
        deadline=max(now+policy.cooldown*(2**min(state['denials'],4)),
                     retry_after(response.headers.get('Retry-After'),now) if response is not None else now)
        state.update(cooldown_until=datetime.fromtimestamp(deadline,timezone.utc).isoformat(),reason=reason,
                     denials=state['denials']+1)
        self.save_state(state)
        self.halted[provider]=dict(reason=reason,cooldown_until=state['cooldown_until'])
        raise ProviderBlocked(f'{provider} {reason}；停止本轮同源请求，冷却至 {state["cooldown_until"]}')

    def reserve_slot(self, provider, policy, *, stock_fast=False,recovery_token=None,source_ref=None):
        """Reserve shared pacing budget and OS-backed cross-process in-flight slots."""
        token=uuid4().hex
        limit=1 if stock_fast else policy.max_concurrency
        while True:
            self.check_cancelled()
            delay=.05
            obtained=None
            held=[]
            with self.provider_lock(provider):
                if provider in self.halted:
                    raise ProviderBlocked(f'{provider} 本轮已停止')
                state=self.state(provider)
                pending=state.get('reason')
                if pending and pending.startswith('recovery_pending:') and pending!=f'recovery_pending:{recovery_token or token}':
                    updated=datetime.fromisoformat(state.get('updated_at') or utc_now())
                    if (datetime.now(timezone.utc)-updated).total_seconds()<300:
                        self.halted[provider]=dict(reason='恢复验证进行中',cooldown_until=state['cooldown_until'])
                        raise ProviderBlocked(f'{provider} 另一请求正在进行恢复验证')
                    state['reason']='恢复验证超时'
                    pending=state['reason']
                    self.save_state(state)
                if state['cooldown_until'] and datetime.fromisoformat(state['cooldown_until']).timestamp()>self.clock():
                    self.halted[provider]=dict(reason=state['reason'],cooldown_until=state['cooldown_until'])
                    raise ProviderBlocked(f'{provider} 仍在冷却至 {state["cooldown_until"]}')
                if state['cooldown_until']:
                    probe_token=recovery_token or token
                    state['reason']=f'recovery_pending:{probe_token}'
                    self.save_state(state)
                budget=json.loads(state['budget_json'])
                # Older shared budgets only recorded reserved request count. Use it
                # as the completed baseline during lazy migration so the 20-request
                # rest cadence survives deployment without resetting.
                budget.setdefault('completed',budget.get('count',0))
                now=self.clock()
                inflight=[item for item in budget.get('inflight',[]) if item.get('until',0)>now]
                budget['inflight']=inflight
                if not inflight:
                    budget['parallel_pair_used']=False
                required=2 if limit==1 else 1
                lock_base=self.db.path.parent/'collection_sources'/('provider-'+provider+'-slot-')
                for slot in range(2):
                    context=instance_lock(str(lock_base)+str(slot))
                    try:context.__enter__()
                    except StorageError:continue
                    held.append(context)
                    if len(held)>=required:break
                if len(held)<required:
                    for context in reversed(held):context.__exit__(None,None,None)
                    held=[]
                    delay=.1
                else:
                    starts=[stamp for stamp in budget.get('starts',[]) if stamp>now-policy.window]
                    ready=budget.get('ready_at',0)
                    fast_exempt=stock_fast and provider!='cninfo'
                    if not fast_exempt and budget.get('completed_at'):
                        ready=max(ready,budget['completed_at']+policy.interval)
                    if not fast_exempt and len(starts)>=policy.window_requests:
                        ready=max(ready,starts[-policy.window_requests]+policy.window+.001)
                    # One sibling may join an active wave to reach concurrency two. Once
                    # that pair is formed, a completion cannot admit a third early request.
                    overlap=(policy.max_concurrency==2 and len(inflight)==1 and
                             not budget.get('parallel_pair_used',False) and
                             inflight[0].get('started_at',0)>=budget.get('completed_at',0))
                    if not fast_exempt and not overlap and ready>now:
                        for context in reversed(held):context.__exit__(None,None,None)
                        held=[]
                        delay=ready-now
                    elif not fast_exempt and len(starts)>=policy.window_requests and ready>now:
                        for context in reversed(held):context.__exit__(None,None,None)
                        held=[]
                        delay=ready-now
                    else:
                        obtained=datetime.fromtimestamp(now,timezone.utc).isoformat()
                        starts.append(now)
                        count=budget.get('count',0)+1
                        attempt_no=None
                        slot_item=dict(token=token,until=now+180,started_at=now,limit=limit)
                        if source_ref:
                            batch_id,dataset,request_key=source_ref
                            with self.db.connection() as conn:
                                archived=conn.execute('SELECT coalesce(max(attempt_no),0) FROM collection_sources '
                                    'WHERE batch_id=? AND dataset=? AND request_key=?',(batch_id,dataset,request_key)).fetchone()[0]
                            pending=[item.get('attempt_no',0) for item in inflight
                                if item.get('source_ref')==list(source_ref)]
                            attempt_no=max([archived,*pending])+1
                            slot_item.update(source_ref=list(source_ref),attempt_no=attempt_no)
                        budget.update(starts=starts,count=count,ready_at=max(ready,now+policy.interval),
                                      inflight=inflight+[slot_item])
                        if overlap:
                            budget['parallel_pair_used']=True
                        state['budget_json']=encoded(budget)
                        self.save_state(state)
                        self.requests+=1
                        return token,state,obtained,count,held,attempt_no
            for context in reversed(held):context.__exit__(None,None,None)
            self.wait(max(0,delay))

    def release_slot(self, provider, token, policy, count, slots, *, refusal=None, response=None):
        try:
            with self.provider_lock(provider,cancellable=False):
                state=self.state(provider)
                budget=json.loads(state['budget_json'])
                budget['inflight']=[item for item in budget.get('inflight',[]) if item.get('token')!=token and item.get('until',0)>self.clock()]
                if not budget['inflight']:
                    budget['parallel_pair_used']=False
                ready=self.clock()+policy.interval+self.uniform(0,policy.jitter)
                completed=budget.get('completed',0)+1
                if completed%policy.rest_every==0:
                    ready+=self.uniform(policy.rest_min,policy.rest_max)
                budget['ready_at']=max(budget.get('ready_at',0),ready)
                budget.update(completed=completed,completed_at=self.clock())
                state['budget_json']=encoded(budget)
                if refusal:
                    now=self.clock()
                    deadline=max(now+policy.cooldown*(2**min(state['denials'],4)),
                        retry_after(response.headers.get('Retry-After'),now) if response is not None else now)
                    state.update(cooldown_until=datetime.fromtimestamp(deadline,timezone.utc).isoformat(),
                                 reason=refusal,denials=state['denials']+1)
                self.save_state(state)
        finally:
            for context in reversed(slots):context.__exit__(None,None,None)
        if refusal:
            self.halted[provider]=dict(reason=refusal,cooldown_until=state['cooldown_until'])
            raise ProviderBlocked(f'{provider} {refusal}；停止本轮同源请求，冷却至 {state["cooldown_until"]}')

    def confirm_recovery(self, provider, recovery_token):
        """Release a deferred provider probe after its multi-request check passes."""
        with self.provider_lock(provider):
            state=self.state(provider)
            if state.get('reason') != f'recovery_pending:{recovery_token}' or not state.get('cooldown_until'):
                raise ProviderBlocked(f'{provider} 恢复验证状态已变化')
            if datetime.fromisoformat(state['cooldown_until']).timestamp()>self.clock():
                raise ProviderBlocked(f'{provider} 仍在冷却')
            state.update(cooldown_until=None,reason=None,denials=0)
            self.save_state(state)

    def abort_recovery(self, provider, recovery_token, reason):
        """Keep a failed deferred probe blocked and extend its cooldown."""
        with self.provider_lock(provider):
            state=self.state(provider)
            if state.get('reason') != f'recovery_pending:{recovery_token}':
                return
            deadline=self.clock()+self.policy_for(provider).cooldown*(2**min(state.get('denials',0),4))
            state.update(cooldown_until=datetime.fromtimestamp(deadline,timezone.utc).isoformat(),
                         reason=f'恢复验证失败：{reason}',denials=state.get('denials',0)+1)
            self.save_state(state)
            self.halted[provider]=dict(reason=state['reason'],cooldown_until=state['cooldown_until'])

    def report(self):
        return dict(policy=asdict(self.policy),provider_policies={
                        'cninfo':asdict(self.policy_for('cninfo')),
                        'index_parallel':asdict(self.policy_for('index_parallel',index_parallel=True)),
                        'index_legu':asdict(self.policy_for('legu',index_parallel=True)),
                        'industry_financial':asdict(self.policy_for('eastmoney',industry_profile='financial')),
                        'industry_snapshot':asdict(self.policy_for('eastmoney',industry_profile='snapshot'))},
                    network_requests=self.requests,retries=self.retries,halted=self.halted)

    def invoke(self, batch_id, provider, dataset, request, callback, *, serialize,
               hash_basis='sdk_materialized_json / gzip_file', response_of=lambda value: None):
        """Schedule one actual SDK transport exchange; consume streams inside callback.

        Request metadata must be sanitized by the adapter. Exceptions are recorded
        by class only because SDK diagnostics may contain authentication material.
        """
        policy=self.policy_for(provider)
        for attempt in range(1,policy.max_attempts+1):
            if provider in self.halted:
                raise ProviderBlocked(f'{provider} 本轮已停止')
            if attempt>1:
                self.retries+=1
                self.wait(60*(attempt-1)+self.uniform(0,5))
            with self.provider_exclusive_lock(provider):
                state=self.state(provider)
                pending=state.get('reason')
                if pending and pending.startswith('recovery_pending:'):
                    updated=datetime.fromisoformat(state.get('updated_at') or utc_now())
                    if (datetime.now(timezone.utc)-updated).total_seconds()<300:
                        self.halted[provider]=dict(reason='恢复验证进行中',cooldown_until=state['cooldown_until'])
                        raise ProviderBlocked(f'{provider} 另一请求正在进行恢复验证')
                    state.update(reason='恢复验证超时')
                    self.save_state(state)
                if state['cooldown_until'] and datetime.fromisoformat(state['cooldown_until']).timestamp()>self.clock():
                    self.halted[provider]=dict(reason=state['reason'],cooldown_until=state['cooldown_until'])
                    raise ProviderBlocked(f'{provider} 仍在冷却至 {state["cooldown_until"]}')
                budget=json.loads(state['budget_json'])
                budget.setdefault('completed',budget.get('count',0))
                starts=[t for t in budget.get('starts',[]) if t>self.clock()-policy.window]
                ready=budget.get('ready_at',0)
                previous_ready=ready
                if len(starts)>=policy.window_requests:
                    ready=max(ready,starts[-policy.window_requests]+policy.window+.001)
                self.wait(max(0,ready-self.clock()))
                starts=[t for t in starts if t>self.clock()-policy.window]+[self.clock()]
                count=budget.get('count',0)+1
                budget.update(starts=starts,ready_at=self.clock()+120,count=count)
                state['budget_json']=encoded(budget)
                self.save_state(state)
                obtained=datetime.fromtimestamp(self.clock(),timezone.utc).isoformat()
                self.requests+=1
                value=response=raw=None
                error=None
                try:
                    value=callback()
                    response=response_of(value)
                    raw=serialize(value)
                except BaseException as exc:
                    error=exc
                finally:
                    ready=self.clock()+policy.interval+self.uniform(0,policy.jitter)
                    completed=budget.get('completed',0)+1
                    if completed%policy.rest_every==0:
                        ready+=self.uniform(policy.rest_min,policy.rest_max)
                    budget.update(starts=starts,ready_at=max(previous_ready,ready),count=count,
                                  completed=completed,completed_at=self.clock())
                    state['budget_json']=encoded(budget)
                    self.save_state(state)
                key=digest(encoded(request).encode())
                with self.db.connection() as conn:
                    attempt_no=conn.execute('SELECT coalesce(max(attempt_no),0)+1 FROM collection_sources '
                        'WHERE batch_id=? AND dataset=? AND request_key=?',(batch_id,dataset,key)).fetchone()[0]
                status=getattr(response,'status_code',None)
                sid=archive_source(self.db,batch_id,provider,dataset,request,attempt_no,raw=raw,response=response,
                    obtained_at=obtained,request_status='failed' if error or status and status>=400 else 'success',
                    error=type(error).__name__ if error else None)
                with self.db.connection(write=True) as conn:
                    conn.execute('UPDATE collection_sources SET hash_basis=? WHERE id=?',(hash_basis,sid))
                diagnostic=str(error).lower() if error else ''
                if isinstance(error,(Cancelled,KeyboardInterrupt)):
                    parsed_source(self.db,sid,error=type(error).__name__)
                    raise error
                text=getattr(response,'text','')[:100000].lower()
                denied=status in (401,403,429) or any(s in diagnostic+' '+text for s in
                    ('permission denied','unauthorized','forbidden','access denied','captcha','验证码','too many requests','rate limit','访问频率'))
                if denied:
                    parsed_source(self.db,sid,error='SDK transport access refused')
                    self._block_locked(provider,state,'SDK transport access refused',response)
                temporary=status in (500,502,503,504) or error and any(s in type(error).__name__.lower() for s in
                    ('timeout','connecterror','unavailable','connectionerror'))
                if error or status and status>=400:
                    parsed_source(self.db,sid,error=type(error).__name__ if error else f'HTTP {status}')
                    if temporary and attempt<policy.max_attempts and not state['cooldown_until']:
                        continue
                    raise RuntimeError(f'{provider} SDK transport failed: {type(error).__name__ if error else status}') from None
                parsed_source(self.db,sid)
                if state['cooldown_until']:
                    state.update(cooldown_until=None,reason=None,denials=0)
                    self.save_state(state)
                return value,sid
        raise RuntimeError('SDK transport attempts exhausted')

    def get(self, batch_id, provider, dataset, url, params=None, *, headers=None, expect='json', healthy_response=None,
            evidence_mode='archive', recovery_token=None, stock_fast=False, index_parallel=False, industry_profile=None):
        return self.request(batch_id, provider, dataset, 'GET', url, params=params, headers=headers, expect=expect,
                            healthy_response=healthy_response, evidence_mode=evidence_mode,
                            recovery_token=recovery_token, stock_fast=stock_fast,index_parallel=index_parallel,
                            industry_profile=industry_profile)

    def post(self, batch_id, provider, dataset, url, data=None, *, params=None, headers=None, expect='json',
             healthy_response=None, evidence_mode='archive', recovery_token=None, stock_fast=False,index_parallel=False,
             industry_profile=None):
        return self.request(batch_id, provider, dataset, 'POST', url, params=params, data=data, headers=headers,
                            expect=expect, healthy_response=healthy_response, evidence_mode=evidence_mode,
                            recovery_token=recovery_token, stock_fast=stock_fast,index_parallel=index_parallel,
                            industry_profile=industry_profile)

    def request(self, batch_id, provider, dataset, method, url, params=None, *, data=None, headers=None, expect='json',
                healthy_response=None, evidence_mode='archive', recovery_token=None, stock_fast=False,index_parallel=False,
                industry_profile=None):
        method=method.upper()
        if method not in ('GET','POST'):
            raise ValueError('仅支持 GET/POST')
        if evidence_mode not in ('archive', 'metadata_only'):
            raise ValueError('不支持的来源证据策略')
        policy=self.policy_for(provider,index_parallel=index_parallel,industry_profile=industry_profile)
        scrub=lambda values:{k:v for k,v in (values or {}).items() if k.lower() not in ('token','apikey','api_key')}
        request=dict(transport='http',method=method,url=url,params=scrub(params))
        if method=='POST': request['data']=scrub(data)
        key=digest(encoded(request).encode())
        for attempt in range(1,policy.max_attempts+1):
            if provider in self.halted:
                raise ProviderBlocked(f'{provider} 本轮已停止')
            if attempt>1:
                self.retries+=1
                self.wait(60*(attempt-1)+self.uniform(0,5))
            token,state,stamp,count,slots,attempt_no=self.reserve_slot(provider,policy,stock_fast=stock_fast,
                recovery_token=recovery_token,source_ref=(batch_id,dataset,key))
            released=False
            def release(*,refusal=None,response=None):
                nonlocal released
                if released:return
                released=True
                self.release_slot(provider,token,policy,count,slots,refusal=refusal,response=response)
            probe=False
            try:
                with self.provider_lock(provider):
                    state=self.state(provider)
                    probe=state['cooldown_until'] is not None
                    pending=state.get('reason')
                    if pending and pending.startswith('recovery_pending:') and pending!=f'recovery_pending:{recovery_token or token}':
                        self.halted[provider]=dict(reason='恢复验证进行中',cooldown_until=state['cooldown_until'])
                        raise ProviderBlocked(f'{provider} 另一请求正在进行恢复验证')
                    if probe:
                        if datetime.fromisoformat(state['cooldown_until']).timestamp()>self.clock():
                            self.halted[provider]=dict(reason=state['reason'],cooldown_until=state['cooldown_until'])
                            raise ProviderBlocked(f'{provider} 仍在冷却至 {state["cooldown_until"]}')
                        probe_token=recovery_token or token
                        state['reason']=f'recovery_pending:{probe_token}'
                        self.save_state(state)
                response=None;network_error=None
                try:
                    if method=='POST':
                        response=self.session.post(url,params=params,data=data,headers=headers,timeout=(15,60))
                    else:
                        response=self.session.get(url,params=params,headers=headers,timeout=(15,60))
                except requests.RequestException as exc:
                    network_error=type(exc).__name__  # exception URLs may contain credentials
                source_id=archive_source(self.db,batch_id,provider,dataset,request,attempt_no,
                    raw=response.content if response is not None and evidence_mode == 'archive' else None,
                    response=response,obtained_at=stamp,
                    request_status='failed' if network_error or response.status_code>=400 else 'success',error=network_error)
                if evidence_mode == 'metadata_only' and response is not None:
                    with self.db.connection(write=True) as conn:
                        conn.execute("UPDATE collection_sources SET parse_status='pending' WHERE id=?",(source_id,))
                if network_error:
                    if probe:
                        release(refusal='恢复验证网络失败')
                        raise ProviderBlocked(f'{provider} 恢复验证失败，停止本轮')
                    release()
                    if attempt==policy.max_attempts:
                        raise RuntimeError(f'{provider} 网络请求失败：{network_error}')
                    continue
                text=response.text[:100000].lower()
                anti_bot=any(marker in text for marker in ('captcha','验证码','访问过于频繁','访问频率','too many requests','access denied'))
                html_body=text.lstrip().startswith(('<!doctype html','<html'))
                html_content_type='text/html' in response.headers.get('Content-Type','').lower()
                json_valid=False
                if expect=='json':
                    try:
                        response.json()
                        json_valid=True
                    except ValueError:
                        pass
                # Some upstreams label a JSON API response as text/html. Treat it as
                # an HTML block page only when the body is HTML or is not valid JSON.
                html=html_body or (html_content_type and not json_valid)
                if response.status_code in (401,403,429) or anti_bot or (expect=='json' and response.status_code==200 and html):
                    reason=f'HTTP {response.status_code}' if response.status_code in (401,403,429) else '验证码／反爬响应'
                    parsed_source(self.db,source_id,error=reason)
                    release(refusal=reason,response=response)
                if response.status_code in (500,502,503,504):
                    parsed_source(self.db,source_id,error=f'HTTP {response.status_code}')
                    release()
                    if probe or attempt==policy.max_attempts:
                        raise RuntimeError(f'{provider} HTTP {response.status_code}')
                    continue
                if response.status_code>=400:
                    parsed_source(self.db,source_id,error=f'HTTP {response.status_code}')
                    release()
                    raise ValueError(f'{provider} HTTP {response.status_code}')
                if probe:
                    # A blocked provider resumes only after one structurally healthy response.
                    healthy=False
                    try:
                        payload=response.json() if expect=='json' else None
                        healthy=healthy_response(response) if healthy_response else (isinstance(payload,dict) and isinstance(payload.get('data'),(list,dict))
                            and str(payload.get('code','200')) in ('0','200') and payload.get('success') is not False) if expect=='json' else ('_csrf' in text if expect=='html' else len(response.content)>0)
                    except ValueError:
                        pass
                    if healthy is RECOVERY_PENDING and recovery_token:
                        with self.provider_lock(provider):
                            state=self.state(provider)
                            state.update(reason=f'recovery_pending:{recovery_token}')
                            self.save_state(state)
                        release()
                        return response,source_id
                    if not healthy:
                        parsed_source(self.db,source_id,error='恢复验证响应无效')
                        release(refusal='恢复验证响应无效',response=response)
                    with self.provider_lock(provider):
                        state=self.state(provider)
                        state.update(cooldown_until=None,reason=None,denials=0)
                        self.save_state(state)
                release()
                return response,source_id
            except BaseException:
                if not released:
                    try:release()
                    except ProviderBlocked:pass
                raise
        raise RuntimeError('请求尝试耗尽')
