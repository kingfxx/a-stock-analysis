import io
import json
import os
import socket
import sqlite3
import time
import urllib.request
from threading import Thread

import pytest

from quarterly_dashboard.database_restore import RestoreManager, RequestGate, digest
from quarterly_dashboard.storage import Database, StorageError, instance_lock


def setup_db(tmp_path):
    db=Database(tmp_path/'stock.sqlite3');db.initialize()
    backup=db.backup(tmp_path/'backups'/'empty.sqlite3')
    db.ensure_instrument('600000','current')
    return db,backup,RestoreManager(db)


def schedule(manager,preview):
    gate=RequestGate();gate.enter()
    manager.schedule({'token':preview['token'],'confirm':'恢复'},gate)
    gate.leave();assert gate.blocked


def test_restore_keeps_rollback_backup_and_replaces_only_sqlite(tmp_path):
    db,backup,manager=setup_db(tmp_path)
    pdf=tmp_path/'report.pdf';pdf.write_bytes(b'PDF-preserve')
    before=digest(db.path);preview=manager.prepare({'backup':backup.name})
    assert preview['source_version']==13 and not preview['upgraded']
    assert preview['source_table_count']==preview['table_count'] and preview['added_tables']==[]
    assert digest(db.path)==before
    schedule(manager,preview)
    with instance_lock(db.path):result=manager.execute()
    assert result['phase']=='complete' and db.check()['instruments']==0
    rollback=Database(tmp_path/'backups'/result['rollback_backup'])
    assert rollback.check()['instruments']==1
    assert pdf.read_bytes()==b'PDF-preserve' and not manager.root.exists()


def test_old_schema_upgraded_in_copy_and_original_unchanged(tmp_path,monkeypatch):
    from quarterly_dashboard import storage
    migrations=storage.MIGRATIONS
    monkeypatch.setattr(storage,'MIGRATIONS',migrations[:12])
    old=Database(tmp_path/'old.sqlite3');old.initialize()
    original=digest(old.path)
    monkeypatch.setattr(storage,'MIGRATIONS',migrations)
    db=Database(tmp_path/'stock.sqlite3');db.initialize();manager=RestoreManager(db)
    uploaded=manager.upload(io.BytesIO(old.path.read_bytes()),old.path.stat().st_size,'old.sqlite3')
    preview=manager.prepare({'upload':uploaded['upload']})
    assert preview['upgraded'] and preview['source_version']==12 and preview['target_version']==13
    assert preview['source_table_count']==preview['table_count']-1
    assert preview['added_tables']==[{'name':'sw_cap_provenance','rows':0}]
    assert digest(old.path)==original
    schedule(manager,preview)
    with instance_lock(db.path):assert manager.execute()['phase']=='complete'
    assert db.check()['schema_version']==13


@pytest.mark.parametrize('fault',['future','missing_table','missing_column','wrong_index','foreign_file','corrupt'])
def test_incompatible_backups_rejected_without_changing_current(tmp_path,fault):
    db,backup,manager=setup_db(tmp_path);before=digest(db.path)
    if fault=='corrupt':backup.write_bytes(b'invalid')
    else:
        conn=sqlite3.connect(backup)
        if fault=='future':conn.execute('PRAGMA user_version=999')
        elif fault=='missing_table':conn.execute('DROP TABLE sw_cap_provenance')
        elif fault=='missing_column':conn.execute('ALTER TABLE sw_cap_facts DROP COLUMN provenance_id')
        elif fault=='wrong_index':
            conn.execute('DROP INDEX sw_cap_lookup');conn.execute('CREATE INDEX sw_cap_lookup ON sw_cap_facts(total_cap)')
        else:conn.execute('PRAGMA application_id=123')
        conn.commit();conn.close()
    with pytest.raises((ValueError,StorageError,sqlite3.DatabaseError)):manager.prepare({'backup':backup.name})
    assert digest(db.path)==before and not manager.pending and not manager.prepared


def test_busy_expired_and_parallel_requests_block_confirmation(tmp_path):
    db,backup,manager=setup_db(tmp_path);preview=manager.prepare({'backup':backup.name})
    gate=RequestGate();gate.enter();gate.enter()
    command={'token':preview['token'],'confirm':'恢复'}
    with pytest.raises(ValueError,match='请求'):manager.schedule(command,gate)
    gate.leave()
    with db.connection(write=True) as conn:
        conn.execute("INSERT INTO sw_update_runs(action,target,recheck,started_at,status) VALUES('cap_quarter','2026Q3',0,'2026-10-04','running')")
    with pytest.raises(ValueError,match='任务'):manager.schedule(command,gate)
    with db.connection(write=True) as conn:conn.execute("UPDATE sw_update_runs SET status='failed'")
    manager.prepared[preview['token']]['at']-=901
    with pytest.raises(ValueError,match='过期'):manager.schedule(command,gate)
    assert not gate.blocked and not manager.pending


def test_staged_file_change_and_publish_failure_keep_current(tmp_path,monkeypatch):
    db,backup,manager=setup_db(tmp_path);before=digest(db.path)
    preview=manager.prepare({'backup':backup.name});schedule(manager,preview)
    manager.pending['path'].write_bytes(b'changed')
    with instance_lock(db.path):assert manager.execute()['phase']=='failed'
    assert digest(db.path)==before
    preview=manager.prepare({'backup':backup.name});schedule(manager,preview)
    original_replace=os.replace
    def failed_publish(source,target):
        if target==db.path:raise OSError('replace failure')
        return original_replace(source,target)
    monkeypatch.setattr('quarterly_dashboard.database_restore.os.replace',failed_publish)
    with instance_lock(db.path):assert manager.execute()['phase']=='failed'
    assert digest(db.path)==before


def test_failure_after_publish_rolls_back(tmp_path,monkeypatch):
    db,backup,manager=setup_db(tmp_path);preview=manager.prepare({'backup':backup.name});schedule(manager,preview)
    original=db.check;calls=[]
    def check():
        calls.append(1)
        if len(calls)==1:raise StorageError('injected check failure')
        return original()
    monkeypatch.setattr(db,'check',check)
    with instance_lock(db.path):result=manager.execute()
    assert result['phase']=='failed' and result['rolled_back'] and original()['instruments']==1


def test_http_restore_reloads_listener_and_protects_session(tmp_path,monkeypatch):
    from quarterly_dashboard import server
    db,backup,_=setup_db(tmp_path)
    monkeypatch.setattr(server,'DATABASE_PATH',db.path)
    for name in ('_SERVICES','_AI_SERVICES','_INDUSTRY_SERVICES','_MAINTENANCE_SERVICES','_DATA_LOCKS'):
        monkeypatch.setattr(server,name,{})
    starts=[]
    def initialize(database,skip_legacy=False):
        database.initialize();database.check();starts.append(skip_legacy)
    monkeypatch.setattr(server,'_initialize_runtime',initialize)
    real_server=server.ThreadingHTTPServer;instances=[]
    class TrackingServer(real_server):
        def __init__(self,*args):super().__init__(*args);instances.append(self)
    monkeypatch.setattr(server,'ThreadingHTTPServer',TrackingServer)
    with socket.socket() as selector:
        selector.bind(('127.0.0.1',0));port=selector.getsockname()[1]
    thread=Thread(target=server.serve,args=(port,),daemon=True);thread.start()
    base=f'http://127.0.0.1:{port}'
    def request(path,body=None,token=None):
        headers={'Origin':base,'Content-Type':'application/json'}
        if token:headers['X-Local-Session']=token
        req=urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
        with urllib.request.urlopen(req,timeout=5) as response:return json.load(response)
    try:
        for _ in range(100):
            if starts:break
            time.sleep(.02)
        listing=request('/api/maintenance/backups');token=listing['session_token']
        with pytest.raises(urllib.error.HTTPError) as rejected:request('/api/maintenance/restore/preview',{'backup':backup.name})
        assert rejected.value.code==400
        preview=request('/api/maintenance/restore/preview',{'backup':backup.name},token)
        accepted=request('/api/maintenance/restore/confirm',{'token':preview['token'],'confirm':'恢复'},token)
        assert accepted['phase']=='restoring'
        for _ in range(100):
            try:
                state=request('/api/maintenance/restore/status')
                if state.get('phase')=='complete':break
            except (OSError,urllib.error.URLError):pass
            time.sleep(.05)
        assert state['phase']=='complete' and len(starts)==2 and starts==[False,True]
        assert request('/api/maintenance/storage')['summary']['schema_version']==13
        assert db.check()['instruments']==0
    finally:
        instances[-1].shutdown();thread.join(timeout=10)
    assert not thread.is_alive()


def test_cancel_removes_only_staging_and_invalidates_preview(tmp_path):
    db,backup,manager=setup_db(tmp_path);original=digest(db.path)
    preview=manager.prepare({'backup':backup.name})
    assert manager.root.exists()
    manager.cancel()
    assert not manager.root.exists() and backup.exists() and digest(db.path)==original
    gate=RequestGate();gate.enter()
    with pytest.raises(ValueError,match='过期'):manager.schedule({'token':preview['token'],'confirm':'恢复'},gate)
