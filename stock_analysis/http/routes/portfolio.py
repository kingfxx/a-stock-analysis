"""Local routes for private brokerage imports and portfolio reads."""

from base64 import b64decode
from binascii import Error as Base64Error
import json
import sqlite3
from urllib.parse import parse_qs

from stock_analysis import application as app
from stock_analysis.domains.portfolio.activity_read import ACTIVITY_FIELDS
from stock_analysis.domains.portfolio.service import ImportConflict
from stock_analysis.http.assets import asset_path
from stock_analysis.http.rendering import render_portfolio
from stock_analysis.infrastructure.storage import StorageError


MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_REQUEST_BYTES = ((MAX_FILE_BYTES + 2) // 3) * 4 + 4096
ASSETS = {'/portfolio.css': 'text/css', '/portfolio.js': 'text/javascript',
          '/portfolio-performance.js': 'text/javascript', '/portfolio-returns.js': 'text/javascript',
          '/portfolio-manual.js': 'text/javascript'}
SNAPSHOT_READS = {'/api/portfolio/snapshots', '/api/portfolio/revaluation'}
MANUAL_OPTIONS = '/api/portfolio/manual-options'
MANUAL_WRITES = {'/api/portfolio/manual/trade': 'history', '/api/portfolio/manual/transfer': 'transfers'}


class PortfolioRoutes:
    def _get_portfolio(self, parsed):
        path = parsed.path
        if path not in {'/portfolio', *SNAPSHOT_READS, '/api/portfolio/history',
                        '/api/portfolio/transfers', '/api/portfolio/performance', MANUAL_OPTIONS, *ASSETS}:
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
            allowed = {'snapshot', 'account', 'day', 'estimate'} if path in SNAPSHOT_READS else (
                set() if path.endswith('/performance') else {'start', 'end', 'offset', *ACTIVITY_FIELDS})
            if path == MANUAL_OPTIONS:
                allowed = {'q', 'account'}
            if path.endswith('/snapshots'):
                allowed.add('current')
            if query.keys() - allowed or any(len(values) != 1 for values in query.values()):
                raise ValueError('持仓查询参数无效')
            args = {key: values[0] for key, values in query.items()}
            service = app.portfolio_service()
            if path == MANUAL_OPTIONS:
                result = service.manual_options(args.get('q', ''), int(args['account']) if 'account' in args else None)
            elif 'current' in args:
                if args['current'] != '1' or args.keys() - {'current', 'account'}:
                    raise ValueError('当前持仓查询参数无效')
                result = service.current_snapshot(int(args['account']) if 'account' in args else None)
                result['session_token'] = app.LOCAL_SESSION_TOKEN
            elif 'estimate' in args:
                if len(args) != 1:
                    raise ValueError('请选择一份估算快照')
                if path.endswith('/snapshots'):
                    result = service.revaluation_snapshot(int(args['estimate']))
                    result['session_token'] = app.LOCAL_SESSION_TOKEN
                else:
                    result = service.saved_revaluation(int(args['estimate']))
            elif path in SNAPSHOT_READS:
                read = service.snapshot if path.endswith('/snapshots') else service.revaluation
                result = read(int(args['snapshot']) if 'snapshot' in args else None,
                              account_id=int(args['account']) if 'account' in args else None,
                              day=args.get('day'))
                if path.endswith('/snapshots'):
                    result['session_token'] = app.LOCAL_SESSION_TOKEN
            elif path.endswith('/performance'):
                result = service.performance()
            else:
                offset = int(args.pop('offset', 0))
                result = service.history(offset=offset, transfers=path.endswith('/transfers'), **args)
            self._ai_reply(result)
        except ValueError as exc:
            self._ai_reply({'error': str(exc)}, 400)
        except LookupError as exc:
            self._ai_reply({'error': str(exc)}, 404)
        except (OSError, StorageError, sqlite3.DatabaseError):
            self._ai_reply({'error': '暂时无法读取持仓数据，请查看后台状态'}, 503)
        return True

    def _post_portfolio(self, path):
        revaluation = path == '/api/portfolio/revaluation'
        delete_estimate = path == '/api/portfolio/revaluation/delete'
        manual = MANUAL_WRITES.get(path)
        if not (revaluation or delete_estimate or manual) and path not in {
            '/api/portfolio/import/preview', '/api/portfolio/import/confirm',
        }:
            return False
        operation = '手工录入' if manual else '重估' if revaluation else '删除本地估算版本' if delete_estimate else '导入'
        try:
            try:
                self._maintenance_auth()
            except ValueError:
                self._ai_reply({'error': '请从本机持仓页面发起' + operation}, 403)
                return True
            length = int(self.headers.get('Content-Length', '0'))
            limit = 4096 if manual else 1024 if revaluation or delete_estimate else MAX_REQUEST_BYTES
            if self.headers.get_content_type() != 'application/json' or not 0 < length <= limit:
                raise ValueError('持仓请求格式无效' if revaluation or delete_estimate or manual else '文件须为 4 MiB 以内的 XLS')
            request = json.loads(self.rfile.read(length).decode('utf-8'))
            if delete_estimate:
                if (not isinstance(request, dict) or set(request) != {'estimate'}
                        or type(request['estimate']) is not int or not 0 < request['estimate'] < 2**63):
                    raise ValueError('请选择一份本地估算版本')
                app.portfolio_service().delete_revaluation(request['estimate'])
                self._ai_reply({'deleted_id': request['estimate']})
                return True
            if manual:
                self._ai_reply(app.portfolio_service().save_manual(manual, request))
                return True
            if revaluation:
                if (not isinstance(request, dict) or set(request) not in (
                        {'snapshot'}, {'day'}, {'current'}, {'current', 'account'})
                        or any(not isinstance(value, str) for value in request.values())
                        or ('current' in request and request['current'] != '1')):
                    raise ValueError('请选择一份仓位快照')
                result = app.portfolio_service().revaluation(
                    int(request['snapshot']) if 'snapshot' in request else None, day=request.get('day'), save=True,
                    current='current' in request, account_id=int(request['account']) if 'account' in request else None)
                self._ai_reply(result)
                return True
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
        except LookupError as exc:
            self._ai_reply({'error': str(exc)}, 404)
        except (ValueError, UnicodeError, Base64Error) as exc:
            message = ('无法读取' + operation + '请求') if isinstance(
                exc, (UnicodeError, Base64Error, json.JSONDecodeError)) else str(exc)
            self._ai_reply({'error': message}, 400)
        except (OSError, StorageError, sqlite3.DatabaseError):
            self._ai_reply({'error': operation + '未完成，请查看后台状态后重试'}, 503)
        return True
