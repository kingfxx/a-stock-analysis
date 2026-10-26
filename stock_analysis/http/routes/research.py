"""Existing research HTTP handlers; request contracts are unchanged."""
from __future__ import annotations

import html
import json
import secrets
from urllib.parse import parse_qs, unquote
from stock_analysis.infrastructure.storage import StorageError
from stock_analysis.domains.research.reports.company_report_service import CompanyReportService
from stock_analysis.domains.research.reports.research_reports import list_reports, resolve_report
from stock_analysis.domains.stocks.source_parsers import normalize_code
from stock_analysis import application as app
from stock_analysis.http import rendering


class ResearchRoutes:

    def _ai_get(self, parsed):
            try:
                self._ai_host()
                service = app.business_service() if parsed.path.startswith('/api/business-judgment') else app.ai_service()
                parsed = parsed._replace(path=parsed.path.replace('/api/business-judgment', '/api/analysis', 1))
                query = parse_qs(parsed.query)
                if parsed.path == "/api/ai/status":
                    data = {**service.status(), "session_token": app.LOCAL_SESSION_TOKEN}
                elif parsed.path == "/api/ai/models":
                    data = {"models": service.provider.models()}
                elif parsed.path == "/api/analysis":
                    data = service.overview(query.get("code", [""])[0])
                    data['display_version'] = rendering.CHECKLIST_DISPLAY_VERSION
                elif parsed.path == "/api/analysis/history":
                    data = service.repository.history(normalize_code(query.get("code", [""])[0]), query.get("cursor", [None])[0])
                elif parsed.path.startswith("/api/analysis/runs/"):
                    data = service.repository.run(parsed.path.rsplit("/", 1)[-1])
                else:
                    self._ai_reply({"error": "接口不存在"}, 404)
                    return
                self._ai_reply(data)
            except (ValueError, StorageError) as exc:
                self._ai_reply({"error": str(exc)}, 400)
            except Exception:
                self._ai_reply({"error": "AI 接口暂不可用，请检查本地存储和网络"}, 503)

    def _ai_post(self, path):
            try:
                self._ai_host()
                allowed = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
                if self.headers.get("Origin") not in allowed or not secrets.compare_digest(
                        self.headers.get("X-Local-Session", ""), app.LOCAL_SESSION_TOKEN):
                    self._ai_reply({"error": "拒绝跨站或无本地会话的 AI 操作，请刷新页面"}, 403)
                    return
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("请使用 JSON 请求")
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 8192:
                    raise ValueError("AI 请求大小无效")
                command = json.loads(self.rfile.read(size).decode("utf-8"))
                if not isinstance(command, dict):
                    raise ValueError("AI 请求必须为对象")
                business = path.startswith('/api/business-judgment')
                service = app.business_service() if business else app.ai_service()
                path = path.replace('/api/business-judgment', '/api/analysis', 1)
                status = 200
                if path == "/api/analysis":
                    data = service.create(command)
                    status = 202 if data.get("accepted") else 200
                elif path == "/api/analysis/prepare":
                    if business:
                        raise ValueError('经营判断无需准备财报')
                    if set(command) - {"code", "refresh"} or type(command.get("refresh", False)) is not bool:
                        raise ValueError("财报准备请求无效")
                    data = CompanyReportService(service.db).prepare(command.get("code", ""), refresh=command.get("refresh", False))
                elif path == "/api/analysis/history/delete":
                    if set(command) != {'code', 'run_ids'}:
                        raise ValueError("历史删除请求字段无效")
                    data = service.repository.delete_history(normalize_code(command['code']), command['run_ids'])
                elif path == "/api/ai/connect":
                    data = service.provider.connect(self.server.server_port)
                elif path == "/api/ai/disconnect":
                    data = service.disconnect()
                elif path == "/api/ai/preferences":
                    model = command.get("model")
                    if not isinstance(model, str) or len(model) > 128:
                        raise ValueError("模型选择无效")
                    models = service.provider.catalog or service.provider.models()
                    if model not in {m["slug"] for m in models}:
                        raise ValueError("模型不在当前账号列表中")
                    data = service.repository.set_model(model)
                elif path.startswith("/api/analysis/runs/") and path.endswith("/cancel"):
                    data = service.cancel(path.split("/")[-2])
                else:
                    self._ai_reply({"error": "接口不存在"}, 404)
                    return
                self._ai_reply(data, status)
            except (ValueError, UnicodeError, StorageError) as exc:
                self._ai_reply({"error": str(exc)}, 400)
            except Exception:
                self._ai_reply({"error": "AI 操作失败，请检查网络和本地存储"}, 503)

    def _oauth_callback(self, parsed):
            message = "连接已完成。请返回股票页面的 AI 设置，选择模型；登录不会自动分析。"
            try:
                self._ai_host()
                query = parse_qs(parsed.query)
                if any(len(values) != 1 for values in query.values()) or len(parsed.query) > 16384:
                    raise ValueError("授权回调格式无效")
                app.ai_service().provider.callback({key: values[0] for key, values in query.items()})
            except Exception:
                message = "连接未完成或校验失败。请返回股票页面的 AI 设置重新连接。"
            body = ("<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>ChatGPT 连接</title><p>"
                    + html.escape(message) + "</p></html>").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

    def _get_research_reports(self, parsed):
        if parsed.path == '/api/research-reports':
            try:
                self._ai_host()
                self._ai_reply(list_reports(parse_qs(parsed.query).get('code', [''])[0]))
            except (ValueError, OSError, UnicodeError) as exc:
                self._ai_reply({'error': str(exc)}, 400)
            return True
        return False

    def _get_research_report_file(self, parsed):
        if parsed.path.startswith('/research-reports/'):
            try:
                self._ai_host()
                path = resolve_report(unquote(parsed.path[len('/research-reports/'):]))
                body = path.read_bytes()
            except (ValueError, OSError):
                self.send_error(404, 'Report not found')
                return True
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(body)
            return True
        return False

    def _get_ai(self, parsed):
        if parsed.path.startswith("/api/ai/") or parsed.path == "/api/analysis" or parsed.path.startswith("/api/analysis/") or parsed.path == '/api/business-judgment' or parsed.path.startswith('/api/business-judgment/'):
            self._ai_get(parsed)
            return True
        return False

    def _get_auth_callback(self, parsed):
        if parsed.path == "/auth/callback":
            self._oauth_callback(parsed)
            return True
        return False

    def _post_ai(self, path):
        if path.startswith("/api/ai/") or path == "/api/analysis" or path.startswith("/api/analysis/") or path == '/api/business-judgment' or path.startswith('/api/business-judgment/'):
            self._ai_post(path)
            return True
        return False
