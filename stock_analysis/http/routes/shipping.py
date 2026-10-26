"""Existing shipping HTTP handlers; request contracts are unchanged."""
from __future__ import annotations

from stock_analysis.http.assets import asset_path
import json
import sqlite3
import secrets
from urllib.parse import parse_qs
from stock_analysis.infrastructure.storage import Database, StorageError
from stock_analysis import application as app


class ShippingRoutes:

    def _get_shipping_trend(self, parsed):
        if parsed.path == '/api/shipping-trend':
            try:
                self._ai_host()
                from stock_analysis.domains.research.sectors.shipping.shipping_forecast import read_trend
                query=parse_qs(parsed.query)
                if query.get('code',['601919'])[0] != '601919': raise ValueError('航运趋势仅适用601919')
                data=read_trend(Database(app.settings.DATABASE_PATH), index=query.get('index',['CCFI'])[0], frequency=query.get('frequency',['monthly'])[0],
                    route=query.get('route',['综合'])[0], years=query.get('range',['all'])[0], quality=query.get('quality',['official'])[0],
                    profit=query.get('profit',['none'])[0], selected_years=query.get('years',[''])[0].split(',') if query.get('years') else [])
                self._ai_reply(dict(data,session_token=app.LOCAL_SESSION_TOKEN))
            except (ValueError,StorageError,sqlite3.DatabaseError) as exc:self._ai_reply({'error':str(exc)},400)
            return True
        return False

    def _get_shipping_forecast(self, parsed):
        if parsed.path == '/api/shipping-forecast':
            try:
                self._ai_host()
                from stock_analysis.domains.research.sectors.shipping.shipping_forecast import read_forecast, read_version
                query=parse_qs(parsed.query);db=Database(app.settings.DATABASE_PATH)
                if query.get('code',['601919'])[0]!='601919':raise ValueError('航运预测仅适用601919')
                data=read_version(db,query['version'][0]) if 'version' in query else read_forecast(db,query.get('target',['2026Q3'])[0],int(query.get('weekly_lead_weeks',['2'])[0]))
                self._ai_reply(dict(data,session_token=app.LOCAL_SESSION_TOKEN))
            except (ValueError,StorageError) as exc:self._ai_reply({'error':str(exc)},400)
            return True
        return False

    def _get_static_assets(self, parsed):
        status = 200
        if parsed.path in {"/datetime.js", "/research-layout.css", "/ai-assessment.js", "/ai-assessment.css", "/ai-checklist.js", "/ai-checklist.css", '/business-judgment.js',
                             "/financial-statements.js", "/financial-statements.css", "/business-segments.js", "/business-segments.css", "/business-segment-aliases.js", "/business-segment-names.js", "/industry.js", "/industry.css",
                             "/maintenance.js", "/maintenance.css", "/collection-progress.js", "/collection-progress.css", "/investment-notes.js", "/investment-notes.css", "/stock-cleanup.js", "/dcf.js", "/dcf.css",
                             "/dividend-forecast.js", "/dividend-forecast.css", "/shipping-forecast.js", "/shipping-forecast.css"}:
            body = (asset_path(parsed.path.lstrip("/"))).read_bytes()
            content_type = "text/javascript; charset=utf-8" if parsed.path.endswith(".js") else "text/css; charset=utf-8"
            self._send_body(status, body, content_type)
            return True
        return False

    def _post_shipping_forecast_save(self, path):
        if path in {'/api/shipping-forecast/save','/api/shipping-forecast/prepare-report','/api/shipping-forecast/delete'}:
            try:
                self._ai_host()
                size=int(self.headers.get('Content-Length','0'))
                if self.headers.get_content_type()!='application/json' or not 0<size<=32768:
                    raise ValueError('请提交有效JSON')
                # Drain the bounded body before closing an unauthorized request;
                # unread inbound bytes can reset the connection before 403 arrives.
                body=self.rfile.read(size)
                if self.headers.get('Origin') not in {f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}'} or not secrets.compare_digest(self.headers.get('X-Local-Session',''),app.LOCAL_SESSION_TOKEN):
                    self._ai_reply({'error':'拒绝跨站或缺少本地会话的保存'},403);return True
                from stock_analysis.domains.research.sectors.shipping.shipping_forecast import save_version, delete_version
                command=json.loads(body)
                if path.endswith('/delete'):
                    if not isinstance(command,dict) or set(command)!={'id'}:
                        raise ValueError('删除版本请求无效')
                    self._ai_reply(delete_version(Database(app.settings.DATABASE_PATH),command['id']))
                elif path.endswith('/prepare-report'):
                    from stock_analysis.domains.research.sectors.shipping.shipping_report_metrics import prepare_shipping_report
                    if not isinstance(command,dict) or set(command)!={'period'} or not isinstance(command['period'],str):raise ValueError('请指定单一报告期')
                    self._ai_reply(prepare_shipping_report(Database(app.settings.DATABASE_PATH),command['period']))
                else:
                    self._ai_reply(save_version(Database(app.settings.DATABASE_PATH),command))
            except (ValueError,StorageError) as exc:self._ai_reply({'error':str(exc)},400)
            except (OSError,RuntimeError,sqlite3.DatabaseError) as exc:self._ai_reply({'error':str(exc)},503)
            return True
        return False
