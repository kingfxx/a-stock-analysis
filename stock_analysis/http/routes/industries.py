"""Existing industries HTTP handlers; request contracts are unchanged."""
from __future__ import annotations

import json
import sqlite3
from urllib.parse import parse_qs
from stock_analysis.infrastructure.storage import StorageError
from stock_analysis import application as app


class IndustriesRoutes:

    def _get_industry(self, parsed):
        status = 200
        if parsed.path in {'/api/industry','/api/industry/status'}:
            query = parse_qs(parsed.query)
            try:
                data = app.industry_service().status() if parsed.path.endswith('/status') else app.industry_service().read(
                    code=query.get('code',[None])[0], industry=query.get('industry',[None])[0],
                    level=int(query.get('level',['3'])[0]), parent=query.get('parent',[None])[0],
                    mode=query.get('mode',['ttm'])[0], period=query.get('period',[None])[0])
            except (ValueError,TypeError) as exc:
                status,data=400,{'error':str(exc)}
            except (OSError,StorageError,sqlite3.DatabaseError) as exc:
                status,data=503,{'error':str(exc)}
            body=json.dumps(data,ensure_ascii=False,allow_nan=False).encode('utf-8')
            content_type='application/json; charset=utf-8'
            self._send_body(status, body, content_type)
            return True
        return False

    def _post_industry_refresh(self, path, command):
        if not isinstance(command, dict) or set(command) - {'action', 'target', 'recheck'} or (not {'action', 'target'} <= set(command)):
            raise ValueError('行业更新须指定任务和报告期／季度')
        data = app.industry_service().start_refresh(command['action'], command['target'], command.get('recheck', False))
        return data
