"""Operation-owned group workers; real HTTP is serialized per provider channel."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Condition

from stock_analysis.collection.collection import RequestCollector
from stock_analysis.collection.execution_context import execution_context
from stock_analysis.domains.stocks.group_batches import complete_member
from stock_analysis.domains.stocks.group_requests import GroupRequests
from stock_analysis.infrastructure.storage import utc_now


def run_group(service, ref, operation, groups, factory=RequestCollector):
    scope=json.loads(operation['scope_json'])
    broker=GroupRequests(service.db,lambda:service.cancelled(ref),factory)
    from stock_analysis.domains.stocks.collection_operations import DATA_PROVIDERS
    broker.open(sorted({provider for group in groups for provider in DATA_PROVIDERS[group['work_dataset']]}))
    def run(group):
        if service.cancelled(ref):return
        code,dataset=group['code'],group['work_dataset']
        with service.db.connection() as conn:
            pending=[dict(r) for r in conn.execute("SELECT dataset,scope_json,mode FROM collection_batches WHERE operation_id=? AND code=? AND work_dataset=? AND mode=? AND status='pending'",(ref,code,dataset,group['mode']))]
        allowed={(r['dataset'].removeprefix('stock.'),json.loads(r['scope_json']).get('source_interface','')) for r in pending}
        full=any(r['mode']=='full' for r in pending)
        error=None
        with execution_context(operation_id=ref,work_dataset=dataset,stock_code=code,selected_segments=allowed,
                scope=dict(requested_mode='full' if full else 'incremental',batch_pacing=True,
                           **({'schedule':scope['schedule']} if 'schedule' in scope else {})),
                trigger=operation['trigger_type'],cancelled=lambda:service.cancelled(ref),collector=broker,
                request_reason='full' if full else 'incremental',batch_pacing=True,group_batches=broker.batches,batch_complete=complete_member):
            try:
                service.collect(code,dataset,full,broker)
                if dataset=='financial':
                    service.data._add_sqlite_missing_name(service.db,service.data.p4_services()[0].read(code),refresh=True)
            except Exception as exc:
                error=str(exc)
        with service.db.connection(write=True) as conn:
            conn.execute("UPDATE collection_batches SET status=?,finished_at=?,error=? WHERE operation_id=? AND code=? AND work_dataset=? AND mode=? AND status='pending'",
                ('cancelled' if service.cancelled(ref) else 'failed' if error else 'skipped',utc_now(),error,ref,code,dataset,group['mode']))
            # Services may fail before committing a shared member (including cancellation).
            for row in conn.execute("SELECT id,scope_json FROM collection_batches WHERE operation_id=? AND status='running' AND code IS NULL",(ref,)).fetchall():
                members=json.loads(row['scope_json']).get('stock_members',[])
                member=next((m for m in members if m['code']==code and m['work_dataset']==dataset),None)
                if member:
                    status=conn.execute('SELECT status FROM sync_runs WHERE id=?',(member['sync_run_id'],)).fetchone()
                    if status and status[0] in ('failed','superseded'):
                        complete_member(conn,row['id'],member['sync_run_id'],'failed',error=error or '业务同步失败')
    try:
        with ThreadPoolExecutor(max_workers=min(64,max(1,len(groups))),thread_name_prefix='stock-business') as pool:
            financial={g['code'] for g in groups if g['work_dataset']=='financial'}
            dependents={code:[] for code in financial}
            roots=[]
            for group in groups:
                if group['code'] in financial and group['work_dataset'] in ('announcements','valuation','dividends'):
                    dependents[group['code']].append(group)
                else:roots.append(group)
            condition=Condition();remaining=len(groups);errors=[]
            def launch(group):
                future=pool.submit(run,group)
                def finished(done):
                    nonlocal remaining
                    with condition:
                        error=done.exception()
                        if error:errors.append(error)
                        if group['work_dataset']=='financial':
                            for dependent in dependents[group['code']]:launch(dependent)
                        remaining-=1;condition.notify_all()
                future.add_done_callback(finished)
            for group in roots:launch(group)
            with condition:
                while remaining:condition.wait(timeout=1)
            if errors:raise errors[0]
    finally:
        broker.close()
