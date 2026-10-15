import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from quarterly_dashboard.collection import encoded
from quarterly_dashboard.index_collection import OperationProgress,start_operation
from quarterly_dashboard.storage import Database
from quarterly_dashboard import index_collection as ic


@pytest.fixture
def db(tmp_path):
    result=Database(tmp_path/'index-progress.sqlite3')
    result.initialize()
    return result


def read_progress(db,operation):
    with db.connection() as conn:
        value=conn.execute('SELECT result_json FROM index_collection_operations WHERE operation_id=?',(operation,)).fetchone()[0]
    return json.loads(value)['work_progress']


def test_progress_parallel_transitions_do_not_lose_counts_and_skip_failure_cancel_are_distinct(db):
    operation=start_operation(db,providers=['csindex','legu'])
    progress=OperationProgress(db,operation)
    for i in range(24):progress.register('csindex' if i%2 else 'legu',f'task-{i}',f'任务{i}')
    lock=threading.Lock()
    def finish(i):
        provider='csindex' if i%2 else 'legu';task=f'{provider}:task-{i}'
        progress.transition(task,'running')
        state=('success','skipped','failed','cancelled')[i%4]
        progress.transition(task,state)
    with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(finish,range(24)))
    result=read_progress(db,operation)
    assert (result['total'],result['success'],result['skipped'],result['failed'],result['cancelled'])==(24,6,6,6,6)
    assert result['pending']==0 and result['running']==0
    assert sum(item['total'] for item in result['providers'].values())==24


def test_cancel_finalization_marks_unstarted_work_cancelled(db):
    operation=start_operation(db,providers=['csindex'])
    progress=OperationProgress(db,operation)
    progress.register('csindex','started','已启动')
    progress.register('csindex','waiting','尚未启动')
    progress.transition('csindex:started','running')
    progress.transition('csindex:started','success')
    progress.finish('cancelled')
    result=read_progress(db,operation)
    assert result['phase']=='finished' and result['operation_status']=='cancelled'
    assert (result['success'],result['cancelled'],result['pending'])==(1,1,0)


def test_operation_plans_full_denominator_before_first_provider_work(db,monkeypatch):
    observed=[]
    class FakeCollector:
        def __init__(self,db,operation,progress):
            self.db=db;self.operation_id=operation;self.progress=progress
            self.task_progress=None;self.trading_days=set();self.auto_incremental=False
            self.http=type('Http',(),{'requests':0,'retries':0,'halted':{},'report':lambda self:{'network_requests':0,'retries':0,'halted':{}}})()
        def load_calendar(self,**kwargs):self.trading_days={f'2026-10-{day:02}' for day in range(1,15)}
        def plan_operation_work_units(self,*args):
            for code in ic.CS_CODES:self.task_progress.register('csindex','history:'+code,'中证指数历史 '+code)
            self.task_progress.register('csindex','dashboard','中证板块看板')
            self.task_progress.register('csindex','recent-xls','中证近期估值文件')
        def collect_cs(self,*args):
            with self.db.connection() as conn:
                body=json.loads(conn.execute('SELECT result_json FROM index_collection_operations WHERE operation_id=?',(self.operation_id,)).fetchone()[0])
            observed.append(body['work_progress']['total'])

    monkeypatch.setattr(ic,'IndexCollector',FakeCollector)
    operation=start_operation(db,mode='incremental',providers=['csindex'],start='2026-10-01',end='2026-10-14')
    result=ic.run_operation(db,operation)
    assert result['status']=='complete'
    assert observed and observed[0]==len(ic.CS_CODES)+2
    assert read_progress(db,operation)['total']==observed[0]


def test_new_progress_format_does_not_break_empty_or_legacy_operation_json(db):
    operation=start_operation(db,providers=['csindex'])
    progress=OperationProgress(db,operation)
    result=read_progress(db,operation)
    assert result['total']==0 and result['phase']=='planning'
    with db.connection(write=True) as conn:conn.execute('UPDATE index_collection_operations SET result_json=? WHERE operation_id=?',(encoded({'message':'legacy'}),operation))
    with db.connection() as conn:
        legacy=json.loads(conn.execute('SELECT result_json FROM index_collection_operations WHERE operation_id=?',(operation,)).fetchone()[0])
    assert 'work_progress' not in legacy
