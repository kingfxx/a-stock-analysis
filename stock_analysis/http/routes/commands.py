"""Shared validation and dispatch for the existing JSON command endpoints."""
from __future__ import annotations

import json
import sqlite3
from stock_analysis.infrastructure.storage import StorageError


def command_post(self, path):
    if path not in {'/api/stock-collection', '/api/stock-groups', '/api/maintenance/unfollowed', '/api/industry/refresh', '/api/indices/collect', '/api/indices/cancel', '/api/indices/congestion/schedule', '/api/maintenance/collections/run', '/api/maintenance/collections/cancel', '/api/maintenance/collections/schedule', '/api/maintenance/collections/rate-limit'}:
        self.send_error(404)
        return
    status = 200
    try:
        allowed = {f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}'}
        if self.headers.get('Origin') and self.headers['Origin'] not in allowed:
            raise ValueError('不允许跨站修改分组')
        if self.headers.get_content_type() != 'application/json':
            raise ValueError('请使用 JSON 操作分组')
        size = int(self.headers.get('Content-Length', '0'))
        if not 0 < size <= 65536:
            raise ValueError('分组请求大小无效')
        command = json.loads(self.rfile.read(size).decode('utf-8'))
        if path.startswith('/api/maintenance/collections/'):
            data = self._post_maintenance_collections(path, command)
        elif path == '/api/stock-collection':
            data = self._command_stock_collection(command)
        elif path == '/api/indices/congestion/schedule':
            data = self._post_indices_congestion_schedule(path, command)
        elif path in {'/api/indices/collect', '/api/indices/cancel'}:
            data = self._post_indices_collect(path, command)
        elif path == '/api/industry/refresh':
            data = self._post_industry_refresh(path, command)
        elif path == '/api/maintenance/unfollowed':
            data = self._post_maintenance_unfollowed(path, command)
        else:
            data = self._command_stock_groups(path, command)
    except (ValueError, TypeError, OSError, StorageError, sqlite3.DatabaseError) as exc:
        status, data = (400, {'error': str(exc)})
    body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
    self.send_response(status)
    self.send_header('Content-Type', 'application/json; charset=utf-8')
    self.send_header('Content-Length', str(len(body)))
    self.send_header('Cache-Control', 'no-store')
    self.end_headers()
    self.wfile.write(body)
