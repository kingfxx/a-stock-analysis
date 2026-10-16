"""Stock HTTP routing through the shared provider scheduler."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from .collection import RECOVERY_PENDING, RequestCollector, begin_batch, finish_batch, parsed_source


_ACTIVE_ROUTE = ContextVar("stock_collection_route", default=None)


def begin_stock_batch(db, run_id, provider, dataset, code, *, start=None, end=None,
                      trigger="automatic", scope=None, parser_version="stock-v1", definition_version="v1"):
    details = {"code": code, "sync_run_id": run_id, "evidence_mode": "metadata_only", **(scope or {})}
    return begin_batch(db, f"sync_runs:{run_id}", f"stock.{dataset}", provider, start, end,
                       scope=details, trigger=trigger, domain="stock", parser_version=parser_version,
                       definition_version=definition_version)


class RoutedSession:
    def __init__(self, route):
        self.route = route
        self.source_ids = []
        self.recovery_token = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type and self.recovery_token:
            self.abort_stock_recovery(exc_type.__name__)
        for source_id in self.source_ids:
            parsed_source(self.route["db"], source_id, error=type(exc).__name__ if exc else None)

    def close(self):
        self.__exit__(None, None, None)

    def get(self, url, params=None, *, headers=None, timeout=None, **kwargs):
        host = (urlparse(url).hostname or "").lower()
        if "eastmoney.com" in host:
            provider = "eastmoney"
        elif "sina.cn" in host or "sina.com.cn" in host:
            provider = "sina"
        elif "gtimg.cn" in host or "qq.com" in host:
            provider = "tencent"
        elif "baidu.com" in host:
            provider = "baidu"
        else:
            raise ValueError(f"未登记的个股数据提供方：{host}")
        if provider != self.route["provider"]:
            raise ValueError(f"个股请求提供方与批次不符：{provider}")
        if provider == "tencent" and host == "qt.gtimg.cn":
            expect = "text"
            healthy = lambda response: self._healthy_quote(response, url)
        else:
            expect = "json"
            query = (params or {}).get("param", "").split(",")
            qfq_probe = (provider == "tencent" and len(query) >= 6 and query[0]
                         and query[-1] == "qfq" and self._cooldown_expired(provider))
            candidate_token = self.recovery_token or (uuid4().hex if qfq_probe else None)
            validator = self._healthy_json(provider, url, params or {},
                                           defer_qfq_fallback=bool(qfq_probe),
                                           pending_recovery=bool(self.recovery_token))

            def healthy(response):
                result = validator(response)
                if result is RECOVERY_PENDING and candidate_token:
                    self.recovery_token = candidate_token
                return result
        response, source_id = self.route["collector"].get(
            self.route["batch_id"], provider, self.route["dataset"], url, params,
            headers=headers, expect=expect, healthy_response=healthy, evidence_mode="metadata_only",
            recovery_token=self.recovery_token or (candidate_token if expect == "json" else None), stock_fast=True)
        self.source_ids.append(source_id)
        return response

    def _cooldown_expired(self, provider):
        state = self.route["collector"].state(provider)
        return bool(state.get("cooldown_until") and
                    datetime.fromisoformat(state["cooldown_until"]).timestamp() <= self.route["collector"].clock())

    def confirm_stock_recovery(self):
        if self.recovery_token:
            self.route["collector"].confirm_recovery(self.route["provider"], self.recovery_token)
            self.recovery_token = None

    def abort_stock_recovery(self, reason):
        if self.recovery_token:
            self.route["collector"].abort_recovery(self.route["provider"], self.recovery_token, reason)
            self.recovery_token = None

    @staticmethod
    def _healthy_quote(response, url):
        try:
            from .sources import parse_stock_name
            symbol = parse_qs(urlparse(url).query).get("q", [None])[0]
            if symbol is None:
                symbol = urlparse(url).path.rsplit("/", 1)[-1].removeprefix("q=")
            if not symbol or len(symbol) != 8 or symbol[:2] not in ("sh", "sz") or not symbol[2:].isdigit():
                return False
            return bool(parse_stock_name(response.content.decode("gbk", errors="replace"), symbol))
        except (AttributeError, UnicodeError, ValueError):
            return False

    @staticmethod
    def _healthy_json(provider, url, params, *, defer_qfq_fallback=False, pending_recovery=False):
        def healthy(response):
            try:
                payload = response.json()
                if not isinstance(payload, dict):
                    return False
                if provider == "sina":
                    result = payload["result"]
                    status, data = result["status"], result["data"]
                    count, records = int(data["report_count"]), data["report_list"]
                    page, size = int(params.get("page", 1)), int(params.get("num", 8))
                    expected = min(size, max(0, count - (page - 1) * size))
                    return (status.get("code", 0) in (0, "0") and count >= 0
                            and isinstance(records, dict) and len(records) == expected
                            and all(isinstance(row, dict) and isinstance(row.get("data"), list)
                                    for row in records.values()))
                if provider == "baidu":
                    chart = payload["Result"][0]["DisplayData"]["resultData"]["tplData"]["result"]["chartInfo"][0]
                    indicator, window = params["query"], params["chart_select"]
                    body = chart["body"]
                    if (not body or chart.get("type", window) not in (window, "全部")
                            or chart.get("header", [indicator])[0] != indicator):
                        return False
                    from datetime import date
                    seen = set()
                    for point in body:
                        if not isinstance(point, (list, tuple)) or len(point) != 2:
                            return False
                        day = date.fromisoformat(point[0]).isoformat()
                        if day in seen:
                            return False
                        seen.add(day)
                    return True
                if provider == "tencent":
                    query = params.get("param", "").split(",")
                    symbol = query[0] if query else None
                    adjustment = query[-1] if query and query[-1] in ("qfq", "hfq") else ""
                    from .sources import parse_price_history
                    node = (payload.get("data") or {}).get(symbol, {})
                    if adjustment == "qfq" and "qfqday" not in node and isinstance(node.get("day"), list):
                        fallback = {**payload, "data": {symbol: {"qfqday": node["day"]}}}
                        rows = parse_price_history(fallback, symbol, "qfq")
                        return RECOVERY_PENDING if defer_qfq_fallback and rows else bool(rows)
                    parse_adjustment = "" if pending_recovery and adjustment in ("", "hfq") else adjustment
                    rows = parse_price_history(payload, symbol, parse_adjustment)
                    if pending_recovery:
                        return RECOVERY_PENDING if rows else False
                    return bool(rows)
                if provider == "eastmoney":
                    result = payload.get("result")
                    if payload.get("code") == 9201 and result is None:
                        return True
                    if payload.get("success") is not True:
                        return False
                    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                        return False
                    rows = result["data"]
                    report = params.get("reportName", "")
                    if report == "RPT_PCF10_INDUSTRY_CVALUE":
                        return any(isinstance(row, dict) and row.get("CORRE_SECURITY_CODE") == "行业平均"
                                   for row in rows)
                    count, pages = int(result["count"]), int(result["pages"])
                    page = int(params.get("pageNumber", 1))
                    size = int(params.get("pageSize", 500 if report in
                                           ("RPT_F10_EH_HOLDERNUM", "RPT_HOLDERNUM_DET", "RPTA_WEB_RZRQ_GGMX")
                                           else 200))
                    expected = min(size, max(0, count - (page - 1) * size))
                    expected_pages = max(1, (count + size - 1) // size)
                    pages_valid = (pages == expected_pages if count else
                                   pages in ((0, 1) if report in ("RPT_SHAREBONUS_DET", "RPT_F10_FN_MAINOP") else (1,)))
                    if (count < 0 or pages < 0 or (pages and page > pages) or not pages_valid
                            or len(rows) != expected):
                        return False
                    code_field = "SCODE" if report == "RPTA_WEB_RZRQ_GGMX" else "SECURITY_CODE"
                    import re
                    match = re.search(r'(?:SECURITY_CODE|SCODE)="(\d{6})"', str(params.get("filter", "")))
                    if match and not all(isinstance(row, dict) and row.get(code_field) == match.group(1)
                                         for row in rows):
                        return False
                    if report in ("RPT_F10_EH_HOLDERNUM", "RPT_HOLDERNUM_DET", "RPTA_WEB_RZRQ_GGMX"):
                        from .chips import chip_rows
                        section = "financing" if report == "RPTA_WEB_RZRQ_GGMX" else "shareholders"
                        chip_rows(rows, section)
                    elif report == "RPT_SHAREBONUS_DET":
                        for row in rows:
                            if not isinstance(row, dict) or (match and row.get("SECURITY_CODE") != match.group(1)):
                                return False
                            for field in ("NOTICE_DATE", "PLAN_NOTICE_DATE", "EX_DIVIDEND_DATE"):
                                if f"({field}>=" in params.get("filter", ""):
                                    from datetime import date
                                    date.fromisoformat(str(row.get(field) or "")[:10])
                    elif report == 'RPT_F10_FN_MAINOP':
                        from .business_segment_service import parse_rows
                        import re
                        query_filter = str(params.get('filter', ''))
                        stock = re.search(r'SECUCODE="(\d{6}\.(?:SH|SZ))"', query_filter)
                        start = re.search(r"REPORT_DATE>='(\d{4}-\d{2}-\d{2})'", query_filter)
                        end = re.search(r"REPORT_DATE<='(\d{4}-\d{2}-\d{2})'", query_filter)
                        if not (stock and start and end):
                            return False
                        parse_rows(rows, stock.group(1), start.group(1), end.group(1))
                    return True
            except (KeyError, IndexError, TypeError, ValueError, AttributeError, OverflowError):
                return False
            return False
        return healthy


def stock_request_scope(db, batch_id, provider, dataset, *, collector=None):
    route = {"db": db, "batch_id": batch_id, "provider": provider, "dataset": f"stock.{dataset}",
             "collector": collector or RequestCollector(db)}
    return _ACTIVE_ROUTE.set(route)


def reset_stock_request_scope(token):
    _ACTIVE_ROUTE.reset(token)


@contextmanager
def routed_stock_requests(db, batch_id, provider, dataset, *, collector=None):
    token = stock_request_scope(db, batch_id, provider, dataset, collector=collector)
    try:
        yield
    finally:
        reset_stock_request_scope(token)


def current_stock_session():
    route = _ACTIVE_ROUTE.get()
    return RoutedSession(route) if route else None


def complete_stock_batch(db, batch_id, *, result=None):
    finish_batch(db, batch_id, "complete", "unknown", result=result)


def fail_stock_batch(db, batch_id, error):
    finish_batch(db, batch_id, "failed", "unknown", error=type(error).__name__)


def recent_failed_stock_batch(db, dataset, code, seconds=600):
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    scope_code = f'%"code":"{code}"%'
    with db.connection() as conn:
        row = conn.execute("SELECT id,error,finished_at FROM collection_batches WHERE domain='stock' "
                           "AND dataset=? AND status='failed' AND finished_at>=? AND scope_json LIKE ? "
                           "ORDER BY id DESC LIMIT 1", (f"stock.{dataset}", cutoff, scope_code)).fetchone()
    return dict(row) if row else None
