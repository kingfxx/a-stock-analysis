"""Existing notes HTTP handlers; request contracts are unchanged."""
from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from stock_analysis.infrastructure.storage import Database, StorageError
from stock_analysis.domains.notes.investment_notes import InvestmentNotes, NoteConflict, MAX_DOCUMENT
from stock_analysis.domains.notes.note_snapshots import catalog as note_catalog
from stock_analysis import application as app


class NotesRoutes:

    def _notes_service(self):
            with app._SERVICE_LOCK:
                if not hasattr(self.server, 'investment_notes'):
                    self.server.investment_notes = InvestmentNotes(Path(app.settings.DATABASE_PATH).parent)
            return self.server.investment_notes

    def _notes_request(self, parsed, *, write=False):
            try:
                self._ai_host()
                service = self._notes_service()
                query = parse_qs(parsed.query)
                path = parsed.path
                if write:
                    self._maintenance_auth()
                    size = int(self.headers.get('Content-Length', '0'))
                    if self.headers.get_content_type() != 'application/json' or not 0 < size <= MAX_DOCUMENT * 4:
                        raise ValueError('笔记请求格式或大小无效')
                    command = json.loads(self.rfile.read(size).decode('utf-8'))
                    if not isinstance(command, dict):
                        raise ValueError('笔记请求须为对象')
                    if 'code' in command and not isinstance(command['code'], str):
                        raise ValueError('股票代码须为字符串')
                    if path == '/api/notes/document':
                        data = service.save(command)
                    elif path == '/api/notes/snapshot':
                        data = note_catalog(Database(app.settings.DATABASE_PATH), command, app.settings.CACHE, app.settings.VALUATION_CACHE, app.settings.CHIP_CACHE)
                    elif path == '/api/notes/settings/check':
                        data = {'directory': str(service.check_directory(command.get('directory', '')))}
                    elif path == '/api/notes/settings/choose':
                        data = service.choose_directory()
                    elif path == '/api/notes/settings/preview':
                        data = service.preview_directory(command)
                    elif path == '/api/notes/settings/apply':
                        data = service.apply_directory(command)
                    elif path == '/api/notes/associate':
                        data = service.associate(command)
                    else:
                        self.send_error(404); return
                elif path == '/api/notes/settings':
                    data = {**service.settings(), 'session_token': app.LOCAL_SESSION_TOKEN}
                elif path == '/api/notes':
                    data = {'items': service.listing(), 'directory_version': service.directory_version()}
                elif path == '/api/notes/document':
                    data = service.read(query.get('code', [''])[0], query.get('name', [''])[0])
                elif path == '/api/notes/resource':
                    body, mime = service.resource(query.get('path', [''])[0])
                    self.send_response(200)
                    self.send_header('Content-Type', mime)
                    self.send_header('Content-Length', str(len(body)))
                    self.send_header('X-Content-Type-Options', 'nosniff')
                    self.send_header('Content-Security-Policy', "default-src 'none'; sandbox")
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers(); self.wfile.write(body); return
                else:
                    self.send_error(404); return
                self._ai_reply(data)
            except NoteConflict as exc:
                self._ai_reply({'error': str(exc), 'conflict': True, 'current': exc.current}, 409)
            except (ValueError, TypeError, KeyError, UnicodeError, OSError, StorageError, sqlite3.DatabaseError, subprocess.TimeoutExpired) as exc:
                self._ai_reply({'error': str(exc)}, 400)

    def _get_notes(self, parsed):
        if parsed.path == '/api/notes' or parsed.path.startswith('/api/notes/'):
            self._notes_request(parsed); return True
        return False

    def _post_notes(self, path):
        if path.startswith('/api/notes/'):
            self._notes_request(urlparse(self.path), write=True); return True
        return False
