"""Loopback-only web server and small per-stock quarterly cache."""

from __future__ import annotations

import html
import json
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from plotly.offline import get_plotlyjs

from .core import build_period_rows, disclosure_snapshots, view_rows
from .sources import fetch_daily_prices, fetch_financial_reports, fetch_stock_name, normalize_code, normalize_report_dates


ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "cache"
TEMPLATE = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
REPORT_DATE_BASIS = "sina_same_period_shift_v1"


def _save_cache(path: Path, data: dict) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def _add_missing_name(data: dict, path: Path, session: requests.Session) -> dict:
    if "name" not in data:
        try:
            name = fetch_stock_name(data["code"], session)
        except (requests.RequestException, ValueError, KeyError):
            name = None
        data = {**data, "name": name}
        _save_cache(path, data)
    return data


def _disclosure_prices(code: str, reports: list[dict], session: requests.Session,
                       old: dict | None = None) -> tuple[dict, list[str], bool]:
    dates = sorted({report.get("publish_date") for report in reports if report.get("publish_date")})
    prices, warnings, complete = {}, [], True
    for key, adjust in (("raw", ""), ("qfq", "qfq")):
        try:
            daily = fetch_daily_prices(code, session, adjust, dates[0], dates[-1]) if dates else []
            prices[key] = disclosure_snapshots(reports, daily)
        except (requests.RequestException, ValueError, KeyError) as exc:
            complete = False
            prices[key] = (old or {}).get("prices", {}).get(key, []) if (old or {}).get("price_basis") == "disclosure" else []
            warnings.append(f"{key} 披露日价格更新失败：{exc}")
    return prices, warnings, complete


def _migrate_cached(code: str, old: dict, path: Path, session: requests.Session) -> dict:
    old = _add_missing_name(old, path, session)
    if old.get("report_date_basis") == REPORT_DATE_BASIS and old.get("price_basis") == "disclosure":
        return old
    reports = (old["reports"] if old.get("report_date_basis") == REPORT_DATE_BASIS
               else normalize_report_dates(old["reports"]))
    prices, warnings, complete = _disclosure_prices(code, reports, session)
    migrated = {**old, "reports": reports, "prices": prices, "price_basis": "disclosure",
                "report_date_basis": REPORT_DATE_BASIS, "warnings": warnings}
    if complete:
        _save_cache(path, migrated)
    return migrated


def load_stock(code: str, refresh: bool = False) -> dict:
    code = normalize_code(code)
    path = CACHE / f"{code}.json"
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    if old and not refresh:
        return _migrate_cached(code, old, path, session)
    try:
        reports = fetch_financial_reports(code, session)
    except (requests.RequestException, ValueError, KeyError) as exc:
        if old:
            fallback = _migrate_cached(code, old, path, session)
            return {**fallback, "warnings": fallback.get("warnings", []) + [f"财报更新失败，正在显示缓存：{exc}"]}
        raise ValueError(f"无法获取 {code} 的季度财报：{exc}") from exc
    prices, warnings, _ = _disclosure_prices(code, reports, session, old)
    try:
        name = fetch_stock_name(code, session)
    except (requests.RequestException, ValueError, KeyError) as exc:
        name = old.get("name") if old else None
        if not name:
            warnings.append(f"股票名称获取失败：{exc}")
    data = {"code": code, "updated_at": datetime.now(timezone.utc).isoformat(),
            "name": name, "reports": reports, "prices": prices,
            "price_basis": "disclosure", "report_date_basis": REPORT_DATE_BASIS,
            "warnings": warnings}
    _save_cache(path, data)
    return data


def cached_stocks() -> list[dict]:
    stocks = []
    for path in sorted(CACHE.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data = _add_missing_name(data, path, requests.Session())
        except (ValueError, KeyError, json.JSONDecodeError):
            continue
        stocks.append({"code": data["code"], "name": data.get("name")})
    return stocks


def render_page(code: str, refresh: bool) -> str:
    try:
        data = load_stock(code, refresh)
        rows = build_period_rows(data["reports"], data["prices"]["raw"], data["prices"]["qfq"])
        views = {name: view_rows(rows, name) for name in ("quarter", "year", "ttm")}
        payload = {"code": data["code"], "name": data.get("name"), "updated_at": data["updated_at"],
                   "views": views, "warnings": data.get("warnings", [])}
        error = ""
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        payload = {"code": code, "name": None, "views": {}, "warnings": []}
        error = str(exc)
    payload["cached_stocks"] = cached_stocks()
    embedded = json.dumps(payload, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c")
    return (TEMPLATE.replace("__PAYLOAD__", embedded)
            .replace("__CODE__", html.escape(code, quote=True))
            .replace("__ERROR__", html.escape(error)))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/plotly.min.js":
            body = get_plotlyjs().encode("utf-8")
            content_type = "text/javascript; charset=utf-8"
        elif parsed.path == "/":
            query = parse_qs(parsed.query)
            code = query.get("code", ["300750"])[0]
            body = render_page(code, query.get("refresh") == ["1"]).encode("utf-8")
            content_type = "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if parsed.path == "/plotly.min.js":
            self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(body)


def serve(port: int = 8765, open_browser: bool = False):
    address = ("127.0.0.1", port)
    server = ThreadingHTTPServer(address, Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"季度分析页面：{url}", flush=True)
    try:
        if open_browser:
            webbrowser.open(url)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
