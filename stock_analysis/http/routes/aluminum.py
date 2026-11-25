"""Local HTTP endpoints for the 601600 aluminum research page."""
import json
import secrets
import sqlite3
from urllib.parse import parse_qs

from stock_analysis import application as app
from stock_analysis.infrastructure.storage import StorageError


PATH = '/api/aluminum-forecast'


class AluminumRoutes:
    def _get_aluminum_forecast(self, parsed):
        if parsed.path != PATH:
            return False
        try:
            self._ai_host()
            query = parse_qs(parsed.query, keep_blank_values=True)
            if query.pop('code', ['601600']) != ['601600']:
                raise ValueError('仅适用中国铝业 601600')
            service = app.aluminum_forecast()
            if not query:
                result = service.read()
            elif set(query) == {'version'} and len(query['version']) == 1:
                result = service.read_version(int(query['version'][0]))
            elif set(query) == {'before'} and len(query['before']) == 1:
                result = service.directory(int(query['before'][0]))
            else:
                raise ValueError('查询参数无效')
            self._ai_reply(dict(result, session_token=app.LOCAL_SESSION_TOKEN))
        except ValueError as exc:
            self._ai_reply({'error': str(exc)}, 400)
        except LookupError as exc:
            self._ai_reply({'error': str(exc)}, 404)
        except (OSError, StorageError, sqlite3.DatabaseError) as exc:
            self._ai_reply({'error': str(exc)}, 503)
        return True

    def _post_aluminum_forecast(self, path):
        if path not in {PATH + '/save', PATH + '/delete', PATH + '/refresh-quotes'}:
            return False
        try:
            self._ai_host()
            size = int(self.headers.get('Content-Length', '0'))
            if self.headers.get_content_type() != 'application/json' or not 0 < size <= 65536:
                raise ValueError('请提交有效 JSON')
            body = self.rfile.read(size)
            origin = self.headers.get('Origin')
            allowed = {f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}'}
            if origin not in allowed or not secrets.compare_digest(
                    self.headers.get('X-Local-Session', '').encode(), app.LOCAL_SESSION_TOKEN.encode()):
                self._ai_reply({'error': '拒绝跨站或缺少本地会话的操作'}, 403)
                return True
            command = json.loads(body)
            if not isinstance(command, dict):
                raise ValueError('请求须为 JSON 对象')
            service = app.aluminum_forecast()
            if path.endswith('/save'):
                result = service.save(command)
            elif path.endswith('/delete') and set(command) == {'id'}:
                result = service.delete(command['id'])
            elif path.endswith('/refresh-quotes') and not command:
                result = service.refresh_quotes()
            else:
                raise ValueError('请求参数无效')
            self._ai_reply(result)
        except (ValueError, TypeError, OverflowError) as exc:
            self._ai_reply({'error': str(exc)}, 400)
        except LookupError as exc:
            self._ai_reply({'error': str(exc)}, 404)
        except (OSError, RuntimeError, StorageError, sqlite3.DatabaseError) as exc:
            self._ai_reply({'error': str(exc)}, 503)
        return True
