"""Validated SQLite restore staging; publishing requires the server's instance lock."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, Timer
from time import monotonic

from .storage import Database, StorageError, MIGRATIONS

MAX_UPLOAD=4*1024**3


def digest(path):
    result=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):result.update(block)
    return result.hexdigest()


def schema_contract(conn):
    objects={r['name']:(r['type'],' '.join((r['sql'] or '').split()) if r['type'] in ('index','trigger') else '')
             for r in conn.execute("SELECT name,type,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
    columns={}
    for name,(kind,_) in objects.items():
        if kind=='table':
            quoted='"'+name.replace('"','""')+'"'
            columns[name]=([tuple(r[k] for k in ('name','type','notnull','dflt_value','pk'))
                           for r in conn.execute('PRAGMA table_info('+quoted+')')],
                           [tuple(r) for r in conn.execute('PRAGMA foreign_key_list('+quoted+')')],
                           sorted((r['name'],r['unique'],r['origin'],r['partial'])
                                  for r in conn.execute('PRAGMA index_list('+quoted+')')))
    return objects,columns


class RequestGate:
    """Freeze admissions only when the confirming request is the sole active request."""
    def __init__(self):
        self.lock=Lock();self.active=0;self.blocked=False

    def enter(self):
        with self.lock:
            if self.blocked:return False
            self.active+=1;return True

    def leave(self):
        with self.lock:self.active-=1


class RestoreManager:
    def __init__(self, db, activity_check=lambda:False):
        self.db=db;self.lock=Lock();self.pending=None;self.prepared={};self.uploads={}
        self.activity_check=activity_check
        self.root=db.path.parent/'restore_staging'
        self.report=db.path.parent/'verification'/'reports'/'database_restore'
        self.state={'phase':'idle'}
        reports=list(self.report.glob('*.json'))
        if reports:
            self.state=json.loads(max(reports,key=lambda p:p.stat().st_mtime).read_text(encoding='utf-8'))
            if self.state['phase']=='restoring':
                self.state.update(phase='uncertain',message='上次恢复中断，请核对当前库和恢复前备份；未自动重复恢复')

    def _save_state(self):
        self.report.mkdir(parents=True,exist_ok=True)
        path=self.report/(self.state['id']+'.json');temporary=path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.state,ensure_ascii=False,indent=2),encoding='utf-8')
        os.replace(temporary,path)

    def list_backups(self):
        entries=[]
        for path in (self.db.path.parent/'backups').glob('*.sqlite3'):
            if path.is_symlink():continue
            info=path.stat()
            entries.append({'name':path.name,'bytes':info.st_size,
                'file_time':datetime.fromtimestamp(info.st_mtime,timezone.utc).isoformat()})
        return sorted(entries,key=lambda r:r['file_time'],reverse=True)

    def _source(self, command):
        if set(command)=={'backup'} and isinstance(command['backup'],str):
            name=command['backup']
            if name not in {r['name'] for r in self.list_backups()}:raise ValueError('备份文件不存在，请刷新列表')
            path=(self.db.path.parent/'backups'/name).resolve()
            if path.parent!=(self.db.path.parent/'backups').resolve():raise ValueError('备份路径无效')
            return path,name
        if set(command)=={'upload'} and command['upload'] in self.uploads:
            return self.uploads[command['upload']]
        raise ValueError('请选择有效备份文件')

    def upload(self, stream, length, name):
        if not 16<=length<=MAX_UPLOAD:raise ValueError('备份文件须为完整 SQLite 文件，最大 4 GiB')
        with self.lock:
            self._cleanup()
            key=secrets.token_hex(16);folder=self.root/key;folder.mkdir(parents=True)
            path=folder/'input.sqlite3';remaining=length
            try:
                with path.open('wb') as output:
                    while remaining:
                        block=stream.read(min(1024*1024,remaining))
                        if not block:raise ValueError('上传不完整，请重新选择文件')
                        if remaining==length and block[:16]!=b'SQLite format 3\x00':raise ValueError('文件不是 SQLite 数据库')
                        output.write(block);remaining-=len(block)
                self.uploads[key]=(path,name.replace('\\','/').rsplit('/',1)[-1][:256])
                return {'upload':key,'name':self.uploads[key][1]}
            except BaseException:
                shutil.rmtree(folder);raise

    def _cleanup(self):
        # Only this manager's disposable staging tree, never original backups.
        if self.pending:raise ValueError('恢复正在进行，请稍候')
        if self.root.exists():shutil.rmtree(self.root)
        self.prepared.clear();self.uploads.clear()

    def cancel(self):
        with self.lock:self._cleanup()
        return {'cancelled':True}

    def _expire(self,key):
        with self.lock:
            if key in self.prepared and not self.pending:self._cleanup()

    def prepare(self, command):
        try:return self._prepare(command)
        except BaseException:
            with self.lock:
                if not self.pending:self._cleanup()
            raise

    def _prepare(self, command):
        with self.lock:
            source,name=self._source(command)
            if self.pending:raise ValueError('恢复正在进行，请稍候')
            source_db=Database(source,journal_mode='delete')
            conn=source_db._open('ro')
            try:
                if conn.execute('PRAGMA user_version').fetchone()[0]>MIGRATIONS[-1][0]:
                    raise ValueError('备份结构版本高于当前程序，请升级程序后再恢复')
            finally:conn.close()
            checked=source_db.check(current=False)
            original=source_db._open('ro')
            try:
                source_tables={r[0] for r in original.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            finally:original.close()
            if checked['journal_mode']!='delete':raise ValueError('请选择独立完整的 SQLite 备份，不支持带 WAL 的工作数据库')
            key=secrets.token_urlsafe(32);folder=self.root/secrets.token_hex(16);folder.mkdir(parents=True)
            staged=folder/'snapshot.sqlite3'
            try:
                source_db.backup(staged)
                candidate=Database(staged,journal_mode='delete');candidate.initialize();candidate.check()
                with self.db.connection() as current, candidate.connection() as proposed:
                    expected,fields=schema_contract(current);actual,columns=schema_contract(proposed)
                    if any(actual.get(k)!=v for k,v in expected.items()) or any(
                        columns.get(table)!=definition for table,definition in fields.items()):
                        raise ValueError('备份结构缺少当前页面所需的表、字段或索引，未允许恢复')
                    info={'token':key,'name':name,'bytes':source.stat().st_size,
                        'source_version':checked['schema_version'],'target_version':MIGRATIONS[-1][0],
                        'upgraded':checked['schema_version']<MIGRATIONS[-1][0],
                        'table_count':sum(v[0]=='table' for v in actual.values()),
                        'source_table_count':len(source_tables),
                        'added_tables':[{'name':table,'rows':proposed.execute(
                            'SELECT count(*) FROM "'+table.replace('"','""')+'"').fetchone()[0]}
                            for table in sorted(set(columns)-source_tables)],
                        'instruments':proposed.execute('SELECT count(*) FROM instruments').fetchone()[0],
                        'file_time':datetime.fromtimestamp(source.stat().st_mtime,timezone.utc).isoformat()}
                # Upgrade backups are disposable: the selected original is retained.
                if (folder/'backups').exists():shutil.rmtree(folder/'backups')
                for previous in self.prepared.values():shutil.rmtree(previous['path'].parent)
                self.prepared={key:{'path':staged,'hash':digest(staged),'at':monotonic(),'info':info}}
                timer=Timer(900,self._expire,args=(key,));timer.daemon=True;timer.start()
                return info
            except BaseException:
                shutil.rmtree(folder);raise

    def _idle(self):
        if self.activity_check():raise ValueError('采集或分析任务正在启动／运行，请完成后再恢复')
        with self.db.connection() as conn:
            for table,active in [('sync_runs',"'running'"),('sw_update_runs',"'running'"),
                                 ('ai_analysis_runs',"'queued','running','validating'")]:
                if conn.execute(f'SELECT 1 FROM {table} WHERE status IN ({active}) LIMIT 1').fetchone():
                    raise ValueError('采集或分析任务尚未完成，请完成后再恢复')
            if conn.execute('PRAGMA journal_mode').fetchone()[0]!='delete':
                raise ValueError('当前数据库为 WAL 模式，请先离线处理，未执行恢复')

    def schedule(self, command, gate):
        if set(command)!={'token','confirm'} or command['confirm']!='恢复':raise ValueError('需要明确确认恢复数据库')
        with self.lock,gate.lock:
            if self.pending:raise ValueError('恢复已提交，请勿重复操作')
            preview=self.prepared.get(command['token'])
            if not preview or monotonic()-preview['at']>900:raise ValueError('校验结果已过期，请重新校验备份')
            if gate.active!=1:raise ValueError('页面还有请求正在处理，请稍候再确认恢复')
            self._idle()
            self.state={'phase':'restoring','id':secrets.token_hex(16),'name':preview['info']['name']}
            self._save_state()
            self.pending=preview;gate.blocked=True
            return dict(self.state)

    def execute(self):
        """Called after server_close, under the daily database's instance lock."""
        with self.lock:
            preview=self.pending;backup=None;published=False
            if not preview:raise ValueError('没有已确认的恢复任务')
            try:
                self._idle()
                if digest(preview['path'])!=preview['hash']:raise ValueError('校验后备份副本发生变化，未恢复')
                if any(Path(str(self.db.path)+s).exists() for s in ('-wal','-shm','-journal')):
                    raise ValueError('当前库仍有日志旁文件，未替换数据库')
                backup=self.db.backup(self.db.path.parent/'backups'/
                    ('before-restore-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.sqlite3'))
                self.state['rollback_backup']=backup.name;self._save_state()
                os.replace(preview['path'],self.db.path);published=True
                self.db.check()
                self.state.update(phase='complete',rollback_backup=backup.name,
                    message='恢复完成，后台已重新加载数据库')
            except Exception as exc:
                if published and backup:
                    rollback=preview['path'].parent/'rollback.sqlite3'
                    Database(backup).backup(rollback);os.replace(rollback,self.db.path);self.db.check()
                self.state.update(phase='failed',message=str(exc),rolled_back=published)
            finally:
                self.pending=None
                self.state['finished_at']=datetime.now(timezone.utc).isoformat()
                self._save_state()
                self._cleanup()
            return dict(self.state)
