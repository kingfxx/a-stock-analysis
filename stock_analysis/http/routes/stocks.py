"""Existing stocks HTTP handlers; request contracts are unchanged."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs
import requests
from stock_analysis.infrastructure.storage import Database, StorageError
from stock_analysis.domains.stocks.price_service import PriceVersionUnavailable
from stock_analysis.domains.financials.business_segment_service import BusinessSegmentService
from stock_analysis.domains.financials.statements import read_statements
from stock_analysis.domains.valuation.dcf import read_dcf, build_auto_wacc
from stock_analysis.domains.valuation.dividend_forecast import read_dividend_forecast
from stock_analysis.domains.valuation.institution_forecast import fetch_institution_forecast
from stock_analysis.domains.stocks.stock_library import StockLibrary
from stock_analysis.domains.valuation.valuation_transport import compact_valuation
from stock_analysis.domains.stocks.source_parsers import normalize_code
from stock_analysis import application as app


class StocksRoutes:

    def _get_business_segments(self, parsed):
        if parsed.path == '/api/business-segments':
            query = parse_qs(parsed.query)
            try:
                self._ai_reply(BusinessSegmentService(app.data.services()[0].db).read(
                    query.get('code', [''])[0], start=query.get('start', [None])[0],
                    end=query.get('end', [None])[0]))
            except (ValueError, OSError, StorageError, sqlite3.DatabaseError) as exc:
                self._ai_reply({'error': str(exc)}, 400)
            return True
        return False

    def _get_stock_announcements(self, parsed):
        if parsed.path == '/api/stock-announcements':
            query = parse_qs(parsed.query)
            try:
                self._ai_reply(app.data.read_announcements(query.get('code', [''])[0]))
            except (ValueError, OSError, StorageError, sqlite3.DatabaseError) as exc:
                self._ai_reply({'error': str(exc)}, 400)
            return True
        return False

    def _get_stock_groups(self, parsed):
        status = 200
        if parsed.path == "/api/stock-groups":
            body = json.dumps(StockLibrary(app.data.services()[0].db).read(), ensure_ascii=False).encode("utf-8")
            content_type = "application/json; charset=utf-8"
            self._send_body(status, body, content_type)
            return True
        return False

    def _get_institution_forecast(self, parsed):
        status = 200
        if parsed.path == "/api/institution-forecast":
            query = parse_qs(parsed.query)
            try:
                data = fetch_institution_forecast(Database(app.settings.DATABASE_PATH), query.get('code', [''])[0])
            except (ValueError, TypeError) as exc:
                status, data = 400, {'error': str(exc)}
            except Exception as exc:
                status, data = 503, {'error': '机构预期读取失败：' + str(exc)}
            body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
            content_type = 'application/json; charset=utf-8'
            self._send_body(status, body, content_type)
            return True
        return False

    def _get_dividend_forecast(self, parsed):
        status = 200
        if parsed.path == "/api/dividend-forecast":
            query = parse_qs(parsed.query)
            try:
                if 'as_of' in query or 'as_of_date' in query:
                    raise ValueError('预测仅支持当前时点，不支持历史回测')
                data = read_dividend_forecast(Database(app.settings.DATABASE_PATH), query.get('code', ['601919'])[0])
            except (ValueError, TypeError) as exc:
                status, data = 400, {'error': str(exc)}
            except (OSError, StorageError, sqlite3.DatabaseError) as exc:
                status, data = 503, {'error': str(exc)}
            body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
            content_type = 'application/json; charset=utf-8'
            self._send_body(status, body, content_type)
            return True
        return False

    def _get_dcf(self, parsed):
        status = 200
        if parsed.path == "/api/dcf":
            query = parse_qs(parsed.query)
            try:
                data = read_dcf(Database(app.settings.DATABASE_PATH), query.get('code', ['601919'])[0], app.settings.CACHE)
            except (ValueError, TypeError) as exc:
                status, data = 400, {'error': str(exc)}
            except (OSError, StorageError, sqlite3.DatabaseError) as exc:
                status, data = 503, {'error': str(exc)}
            body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
            content_type = 'application/json; charset=utf-8'
            self._send_body(status, body, content_type)
            return True
        return False

    def _get_dcf_wacc(self, parsed):
        status = 200
        if parsed.path == "/api/dcf/wacc":
            query = parse_qs(parsed.query)
            try:
                years=int(query.get('years',['5'])[0])
                data=build_auto_wacc(Database(app.settings.DATABASE_PATH),query.get('code',['601919'])[0],years=years)
            except (ValueError,TypeError) as exc:
                status,data=400,{'error':str(exc)}
            except (OSError,StorageError,sqlite3.DatabaseError) as exc:
                status,data=503,{'error':str(exc)}
            body=json.dumps(data,ensure_ascii=False,allow_nan=False).encode('utf-8')
            content_type='application/json; charset=utf-8'
            self._send_body(status, body, content_type)
            return True
        return False

    def _get_statements(self, parsed):
        status = 200
        if parsed.path == "/api/statements":
            query = parse_qs(parsed.query)
            try:
                data = read_statements(Database(app.settings.DATABASE_PATH), query.get('code', ['601919'])[0],
                                       period=query.get('period', [None])[0], mode=query.get('mode', ['ytd'])[0],
                                       comparison=query.get('comparison', ['yoy'])[0])
            except ValueError as exc:
                status, data = 400, {'error': str(exc)}
            except (OSError, StorageError, sqlite3.DatabaseError) as exc:
                status, data = 503, {'error': str(exc)}
            body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
            content_type = 'application/json; charset=utf-8'
            self._send_body(status, body, content_type)
            return True
        return False

    def _get_stock_charts(self, parsed):
        status = 200
        if parsed.path in {"/api/financial", "/api/dividends", "/api/valuation",
                             "/api/shareholders", "/api/financing", "/api/prices"}:
            query = parse_qs(parsed.query)
            try:
                code = query.get("code", ["601919"])[0]
                if Path(app.settings.DATABASE_PATH).exists() and StockLibrary(app.data.services()[0].db).is_unfollowed(normalize_code(code)):
                    raise ValueError('该股票已取消关注，请在后台维护中恢复关注后更新数据')
                version_key = "version" if parsed.path == "/api/prices" else "price_version"
                version = int(query[version_key][0]) if version_key in query else None
                if version is not None and version < 0:
                    raise ValueError("价格版本无效")
                if parsed.path == "/api/prices":
                    data = app.data.price_bundle(code, refresh=query.get("refresh") == ["1"], full=query.get("full") == ["1"], version=version)
                else:
                    data = app.data.load_chart_data(code, parsed.path.rsplit("/", 1)[-1], query.get("refresh") == ["1"],
                                           price_version=version, full=query.get("full") == ["1"])
            except PriceVersionUnavailable as exc:
                status, data = 409, {"error": str(exc), "price_version_expired": True}
            except (requests.RequestException, ValueError, KeyError, TypeError, OSError, StorageError, sqlite3.DatabaseError) as exc:
                status = 503
                data = {"error": str(exc)}
            if status == 200 and parsed.path == '/api/valuation':
                data = compact_valuation(data)
            elif status == 200 and parsed.path == '/api/prices':
                data['valuation'] = compact_valuation(data['valuation'])
            body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
            content_type = "application/json; charset=utf-8"
            self._send_body(status, body, content_type)
            return True
        return False

    def _command_stock_groups(self, path, command):
        data = StockLibrary(app.data.services()[0].db).change(command)
        return data
