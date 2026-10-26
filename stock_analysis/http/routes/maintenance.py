"""Existing maintenance HTTP handlers; request contracts are unchanged."""
from __future__ import annotations

import json
import sqlite3
from threading import Thread
from urllib.parse import parse_qs, unquote
from stock_analysis.infrastructure.storage import Database, StorageError
from stock_analysis.collection.collection_management import CollectionManagement
from stock_analysis.maintenance.database_restore import MAX_UPLOAD
from stock_analysis.domains.stocks.stock_library import StockLibrary
from stock_analysis.maintenance.stock_cleanup import StockCleanupService
from stock_analysis import application as app


class MaintenanceRoutes:

    def _cleanup_service(self):
            with app._SERVICE_LOCK:
                if not hasattr(self.server, 'stock_cleanup'):
                    self.server.stock_cleanup = StockCleanupService(Database(app.settings.DATABASE_PATH), activity_check=app._runtime_worker_activity)
            return self.server.stock_cleanup

    def _cleanup_post(self, path):
            try:
                self._maintenance_auth()
                size = int(self.headers.get('Content-Length','0'))
                if self.headers.get_content_type()!='application/json' or not 0<size<=8192:
                    raise ValueError('清理请求格式无效')
                command = json.loads(self.rfile.read(size).decode('utf-8'))
                service = self._cleanup_service()
                if path=='/api/maintenance/compact':
                    data=service.compact(command,self.server.request_gate)
                    with app._SERVICE_LOCK:
                        app._MAINTENANCE_SERVICES.clear()
                elif path.endswith('/preview'):
                    data = service.preview(command)
                else:
                    data = service.execute(command, self.server.request_gate)
                    with app._SERVICE_LOCK:
                        app._MAINTENANCE_SERVICES.clear()
                self._ai_reply(data)
            except (ValueError,TypeError,UnicodeError,OSError,StorageError,sqlite3.DatabaseError) as exc:
                self._ai_reply({'error':str(exc)},400)

    def _maintenance_post(self, path):
            scheduled=False
            try:
                self._maintenance_auth()
                manager=self.server.restore_manager
                size=int(self.headers.get('Content-Length','0'))
                if path=='/api/maintenance/restore/upload':
                    if self.headers.get_content_type()!='application/octet-stream' or not 16<=size<=MAX_UPLOAD:
                        raise ValueError('请选择完整 SQLite 备份文件，最大 4 GiB')
                    self.connection.settimeout(300)
                    data=manager.upload(self.rfile,size,unquote(self.headers.get('X-Backup-Name','backup.sqlite3')))
                else:
                    if self.headers.get_content_type()!='application/json' or not 0<size<=8192:
                        raise ValueError('恢复请求格式无效')
                    command=json.loads(self.rfile.read(size).decode('utf-8'))
                    if not isinstance(command,dict):raise ValueError('恢复请求必须是对象')
                    if path=='/api/maintenance/restore/preview':data=manager.prepare(command)
                    elif path=='/api/maintenance/restore/cancel':
                        if command:raise ValueError('取消请求格式无效')
                        data=manager.cancel()
                    elif path=='/api/maintenance/restore/confirm':
                        data=manager.schedule(command,self.server.request_gate);scheduled=True
                    else:raise ValueError('恢复接口不存在')
                self._ai_reply(data,202 if scheduled else 200)
            except (ValueError,TypeError,UnicodeError,OSError,StorageError,sqlite3.DatabaseError) as exc:
                self._ai_reply({'error':str(exc)},400)
            finally:
                if scheduled:Thread(target=self.server.shutdown,name='database-restore-shutdown',daemon=True).start()

    def _get_maintenance_collections_operation(self, parsed):
        if parsed.path=='/api/maintenance/collections/operation':
            try:
                query=parse_qs(parsed.query)
                self._ai_reply(CollectionManagement(Database(app.settings.DATABASE_PATH),app.industry_service()).detail(query.get('task_key',[''])[0],query.get('operation_ref',[''])[0]))
            except (ValueError,TypeError,OSError,StorageError,sqlite3.DatabaseError) as exc:
                self._ai_reply({'error':str(exc)},400)
            return True
        return False

    def _get_maintenance_collections_evidence(self, parsed):
        if parsed.path=='/api/maintenance/collections/evidence':
            try:
                batch_id=int(parse_qs(parsed.query).get('batch_id',[''])[0])
                with Database(app.settings.DATABASE_PATH).connection() as conn:
                    rows=[dict(row) for row in conn.execute('SELECT id,batch_id,source_system,dataset,request_status,parse_status,http_status,error,obtained_at,file_path,raw_sha256,stored_sha256,reused_source_id FROM collection_sources WHERE batch_id=? ORDER BY id DESC LIMIT 200',(batch_id,))]
                self._ai_reply({'batch_id':batch_id,'sources':rows,'limit':200})
            except (ValueError,TypeError,OSError,StorageError,sqlite3.DatabaseError) as exc:
                self._ai_reply({'error':str(exc)},400)
            return True
        return False

    def _get_maintenance_collections(self, parsed):
        if parsed.path=='/api/maintenance/collections':
            try:
                self._ai_reply(CollectionManagement(Database(app.settings.DATABASE_PATH),app.industry_service()).read())
            except (ValueError,TypeError,OSError,StorageError,sqlite3.DatabaseError) as exc:
                self._ai_reply({'error':str(exc)},400)
            return True
        return False

    def _get_maintenance_backups(self, parsed):
        if parsed.path=='/api/maintenance/backups':
            try:
                self._ai_host()
                self._ai_reply({'backups':self.server.restore_manager.list_backups(),'session_token':app.LOCAL_SESSION_TOKEN,
                    'restore_status':self.server.restore_manager.state})
            except ValueError as exc:self._ai_reply({'error':str(exc)},400)
            return True
        return False

    def _get_maintenance_unfollowed(self, parsed):
        status = 200
        if parsed.path == '/api/maintenance/unfollowed':
            body = json.dumps({'stocks':StockLibrary(app.data.services()[0].db).unfollowed()}, ensure_ascii=False).encode('utf-8')
            content_type = 'application/json; charset=utf-8'
            self._send_body(status, body, content_type)
            return True
        return False

    def _get_maintenance_stock_cleanup(self, parsed):
        status = 200
        if parsed.path == '/api/maintenance/stock-cleanup':
            try:
                self._ai_host()
                data = {**self._cleanup_service().candidates(), 'session_token':app.LOCAL_SESSION_TOKEN}
            except (ValueError,OSError,StorageError,sqlite3.DatabaseError) as exc:
                status,data = 400,{'error':str(exc)}
            body = json.dumps(data,ensure_ascii=False,allow_nan=False).encode('utf-8')
            content_type = 'application/json; charset=utf-8'
            self._send_body(status, body, content_type)
            return True
        return False

    def _get_maintenance_storage(self, parsed):
        status = 200
        if parsed.path == '/api/maintenance/storage':
            query = parse_qs(parsed.query)
            try:
                maintenance = app.maintenance_service()
                data = maintenance.read(refresh=query.get('refresh') == ['1'])
            except (ValueError, OSError, StorageError, sqlite3.DatabaseError) as exc:
                status, data = (503, {'error': str(exc)})
            body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
            content_type = 'application/json; charset=utf-8'
            self._send_body(status, body, content_type)
            return True
        return False

    def _post_maintenance_compact(self, path):
        if path in {'/api/maintenance/compact','/api/maintenance/stock-cleanup/preview','/api/maintenance/stock-cleanup/execute'}:
            self._cleanup_post(path);return True
        return False

    def _post_maintenance_restore_upload(self, path):
        if path in {'/api/maintenance/restore/upload','/api/maintenance/restore/preview','/api/maintenance/restore/confirm','/api/maintenance/restore/cancel'}:
            self._maintenance_post(path);return True
        return False

    def _post_maintenance_collections(self, path, command):
        if not isinstance(command, dict) or set(command) - {'task_key', 'params', 'schedule', 'provider', 'settings'}:
            raise ValueError('采集管理请求无效')
        management = CollectionManagement(Database(app.settings.DATABASE_PATH), app.industry_service())
        key = command.get('task_key')
        if path.endswith('/run'):
            if set(command) - {'task_key', 'params'}:
                raise ValueError('运行参数无效')
            data = management.start(key, command.get('params', {}))
        elif path.endswith('/rate-limit'):
            if set(command) != {'task_key', 'provider', 'settings'}:
                raise ValueError('限速配置参数无效')
            data = management.configure_rate(key, command['provider'], command['settings'])
        elif path.endswith('/cancel'):
            if set(command) != {'task_key'}:
                raise ValueError('停止参数无效')
            data = management.cancel(key)
        else:
            if set(command) != {'task_key', 'schedule'}:
                raise ValueError('配置参数无效')
            data = management.configure(key, command['schedule'])
        return data

    def _post_maintenance_unfollowed(self, path, command):
        if not isinstance(command, dict) or command.get('action') != 'restore':
            raise ValueError('此入口仅支持恢复关注')
        library = StockLibrary(app.data.services()[0].db)
        data = {'library': library.change(command), 'stocks': library.unfollowed()}
        return data
