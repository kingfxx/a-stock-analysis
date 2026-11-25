"""Local routes for private brokerage imports and portfolio reads."""

from base64 import b64decode
from binascii import Error as Base64Error
import json
import sqlite3
from urllib.parse import parse_qs

from stock_analysis import application as app
from stock_analysis.domains.portfolio.service import ImportConflict
from stock_analysis.http.assets import asset_path
from stock_analysis.http.rendering import render_portfolio
from stock_analysis.infrastructure.storage import StorageError


MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_REQUEST_BYTES = ((MAX_FILE_BYTES + 2) // 3) * 4 + 4096
ASSETS = {'/portfolio.css': 'text/css', '/portfolio.js': 'text/javascript',
          '/portfolio-performance.js': 'text/javascript'}


class PortfolioRoutes:
    def _get_portfolio(self, parsed):
        path = parsed.path
        if path not in {'/portfolio', '/api/portfolio/snapshots', '/api/portfolio/history',
                        '/api/portfolio/transfers', '/api/portfolio/performance', *ASSETS}:
            return False
        try:
            self._ai_host()
            if path in ASSETS:
                self._send_body(200, asset_path(path[1:]).read_bytes(), ASSETS[path] + '; charset=utf-8')
                return True
            if path == '/portfolio':
                self._send_body(200, render_portfolio().encode(), 'text/html; charset=utf-8')
                return True
            query = parse_qs(parsed.query, keep_blank_values=True)
            allowed = {'snapshot', 'account', 'day'} if path.endswith('/snapshots') else (
                set() if path.endswith('/performance') else {'start', 'end', 'offset'})
            if query.keys() - allowed or any(len(values) != 1 for values in query.values()):
                raise ValueError('持仓查询参数无效')
            args = {key: values[0] for key, values in query.items()}
            service = app.portfolio_service()
            if path.endswith('/snapshots'):
                result = service.snapshot(int(args['snapshot']) if 'snapshot' in args else None,
                                          account_id=int(args['account']) if 'account' in args else None,
                                          day=args.get('day'))
                result['session_token'] = app.LOCAL_SESSION_TOKEN
            elif path.endswith('/performance'):
                result = service.performance()
            else:
                result = service.history(args.get('start'), args.get('end'), int(args.get('offset', 0)),
                                         transfers=path.endswith('/transfers'))
            self._ai_reply(result)
        except ValueError as exc:
            self._ai_reply({'error': str(exc)}, 400)
        except LookupError as exc:
            self._ai_reply({'error': str(exc)}, 404)
        except (OSError, StorageError, sqlite3.DatabaseError):
            self._ai_reply({'error': '暂时无法读取持仓数据，请查看后台状态'}, 503)
        return True

    def _post_portfolio(self, path):
        if path not in {'/api/portfolio/import/preview', '/api/portfolio/import/confirm'}:
            return False
        try:
            try:
                self._maintenance_auth()
            except ValueError:
                self._ai_reply({'error': '请从本机持仓页面发起导入'}, 403)
                return True
            length = int(self.headers.get('Content-Length', '0'))
            if self.headers.get_content_type() != 'application/json' or not 0 < length <= MAX_REQUEST_BYTES:
                raise ValueError('文件须为 4 MiB 以内的 XLS')
            request = json.loads(self.rfile.read(length).decode('utf-8'))
            if not isinstance(request, dict):
                raise ValueError('导入请求格式无效')
            file = request.pop('file', None)
            name = request.pop('name', None)
            if not isinstance(file, str) or not isinstance(name, str):
                raise ValueError('请选择导入文件')
            content = b64decode(file, validate=True)
            if not 0 < len(content) <= MAX_FILE_BYTES:
                raise ValueError('文件须为 4 MiB 以内的 XLS')
            preview = request.pop('preview', None)
            confirm = path.endswith('/confirm')
            if confirm and (not isinstance(preview, str) or len(preview) != 64):
                raise ValueError('请先预览再确认')
            if request.keys() - {'day', 'session', 'cash', 'revise', 'overlap', 'draft'}:
                raise ValueError('导入参数无效')
            if 'draft' in request and not isinstance(request['draft'], dict):
                raise ValueError('华泰转录格式无效')
            if any(request[key] is not None and not isinstance(request[key], str)
                   for key in ('day', 'session', 'cash', 'overlap') if key in request):
                raise ValueError('导入日期或现金参数无效')
            if 'revise' in request and not isinstance(request['revise'], bool):
                raise ValueError('快照修订参数无效')
            result = app.portfolio_service().ingest(content, name, confirmation=preview if confirm else None, **request)
            self._ai_reply(result)
        except ImportConflict as exc:
            self._ai_reply({'error': str(exc)}, 409)
        except (ValueError, UnicodeError, Base64Error) as exc:
            message = '无法读取导入请求' if isinstance(exc, (UnicodeError, Base64Error, json.JSONDecodeError)) else str(exc)
            self._ai_reply({'error': message}, 400)
        except (OSError, StorageError, sqlite3.DatabaseError):
            self._ai_reply({'error': '导入未完成，请查看后台状态后重试'}, 503)
        return True
