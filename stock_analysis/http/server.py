"""Loopback HTTP protocol adapter and server entrypoint."""
from __future__ import annotations

from stock_analysis.runtime import run_server
import json
import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from stock_analysis.collection.collection_management import CollectionManagement
from stock_analysis import application as app
from stock_analysis.http.routes import dispatch
from stock_analysis.http.routes.shipping import ShippingRoutes
from stock_analysis.http.routes.notes import NotesRoutes
from stock_analysis.http.routes.maintenance import MaintenanceRoutes
from stock_analysis.http.routes.research import ResearchRoutes
from stock_analysis.http.routes.indices import IndicesRoutes
from stock_analysis.http.routes.industries import IndustriesRoutes
from stock_analysis.http.routes.stocks import StocksRoutes
from stock_analysis.http.routes.pages import PagesRoutes


class Handler(ShippingRoutes, NotesRoutes, MaintenanceRoutes, ResearchRoutes, IndicesRoutes, IndustriesRoutes, StocksRoutes, PagesRoutes, BaseHTTPRequestHandler):

    def _maintenance_auth(self):
        self._ai_host()
        allowed = {f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}'}
        if self.headers.get('Origin') not in allowed or not secrets.compare_digest(self.headers.get('X-Local-Session', ''), app.LOCAL_SESSION_TOKEN):
            raise ValueError('拒绝跨站或无本地会话的维护操作，请刷新页面')

    def _dispatch(self, method):
        gate = getattr(self.server, 'request_gate', None)
        if method == 'GET' and urlparse(self.path).path == '/api/maintenance/restore/status':
            try:
                self._ai_host()
                self._ai_reply(self.server.restore_manager.state)
            except ValueError as exc:
                self._ai_reply({'error': str(exc)}, 400)
            return
        if gate and (not gate.enter()):
            self._ai_reply({'error': '后台维护正在执行，请稍候', 'restoring': True}, 503)
            return
        try:
            getattr(self, '_do_' + method)()
        finally:
            if gate:
                gate.leave()

    def do_POST(self):
        self._dispatch('POST')

    def do_GET(self):
        self._dispatch('GET')

    def do_PUT(self):
        if urlparse(self.path).path != '/api/notes/document':
            self.send_error(405)
            return
        self._dispatch('POST')

    def log_message(self, format, *args):
        if urlparse(self.path).path == '/auth/callback':
            return
        super().log_message(format, *args)

    def _ai_reply(self, data, status=200):
        body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        try:
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def _ai_host(self):
        port = self.server.server_port
        if self.headers.get('Host') not in {f'127.0.0.1:{port}', f'localhost:{port}'}:
            raise ValueError('不允许此 Host 访问本地 AI 接口')

    def _send_body(self, status, body, content_type, *, cache='no-store'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', cache)
        self.end_headers()
        self.wfile.write(body)

    def _do_GET(self):
        dispatch.get(self)

    def _do_POST(self):
        dispatch.post(self)

def serve(port: int=8765, open_browser: bool=False):
    return run_server(
        port, open_browser, database_path=app.settings.DATABASE_PATH,
        handler=Handler, server_factory=ThreadingHTTPServer,
        initialize=app._initialize_runtime, activity_check=app._restore_activity,
        management_factory=lambda db: CollectionManagement(db, app.industry_service()),
        clear_state=app._clear_runtime_services, open_url=webbrowser.open,
    )
