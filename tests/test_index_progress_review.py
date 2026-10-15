"""Review actual collection integration rather than only counter transitions."""
import json

import pytest

from quarterly_dashboard import index_collection as ic
from quarterly_dashboard.collection import ProviderBlocked, finish_batch
from quarterly_dashboard.index_repository import publish
from test_index_collection import db, source, http, response


def setup_collector(db,monkeypatch,responses=()):
    monkeypatch.setattr(ic,'CS_CODES',{'000300':'沪深300'})
    monkeypatch.setattr(ic,'CS_CANDIDATES',{})
    operation=ic.start_operation(db,'incremental',providers=['csindex'])
    client,session,_=http(db,list(responses))
    collector=ic.IndexCollector(db,operation,request_collector=client)
    collector.task_progress=ic.OperationProgress(db,operation)
    collector.trading_days={'2026-10-09'}
    monkeypatch.setattr(collector,'collect_board',lambda *args:ic.SKIPPED_WORK)
    monkeypatch.setattr(collector,'collect_recent',lambda *args:ic.SKIPPED_WORK)
    return operation,collector,session


def progress(db,operation):
    with db.connection() as conn:
        return json.loads(conn.execute('SELECT result_json FROM index_collection_operations '
            'WHERE operation_id=?',(operation,)).fetchone()[0])['work_progress']


def test_latest_history_and_other_skips_do_not_count_as_collected(db,monkeypatch):
    batch,_,row=source(db)
    with db.connection(write=True) as conn:publish(conn,[row])
    finish_batch(db,batch,'complete')
    operation,collector,session=setup_collector(db,monkeypatch)
    collector.auto_incremental=True
    collector.collect_cs('2026-10-09','2026-10-09','incremental')
    result=progress(db,operation)
    assert (result['total'],result['success'],result['skipped'])==(3,0,3)
    assert not session.calls


def test_refusal_keeps_success_zero_and_unstarted_units_in_denominator(db,monkeypatch):
    operation,collector,_=setup_collector(db,monkeypatch)
    def denied(*args,**kwargs):raise ProviderBlocked('fixture refusal')
    monkeypatch.setattr(collector,'request',denied)
    with pytest.raises(ProviderBlocked):collector.collect_cs('2026-10-09','2026-10-09','review')
    result=progress(db,operation)
    assert (result['total'],result['success'],result['failed'],result['pending'])==(3,0,1,2)


def test_success_count_is_recorded_after_actual_segment_publish(db,monkeypatch):
    payload={'data':[dict(indexCode='000300',indexNameCn='沪深300',tradeDate='20261009',peg=12.3)]}
    operation,collector,session=setup_collector(db,monkeypatch,[response(payload)])
    collector.collect_cs('2026-10-09','2026-10-09','review')
    result=progress(db,operation)
    assert (result['total'],result['success'],result['skipped'])==(3,1,2)
    assert len(session.calls)==1
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM index_valuation_observations').fetchone()[0]==1
